"""brikie.co registry client — remote brick fetcher and dynamic loader.

Provides the RegistryClient class that communicates with the brikie.co
brick registry over HTTP (via httpx) to search, list, fetch manifests,
download brick source, and seat downloaded bricks into a live
BrickRegistry at runtime.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any

import httpx

from brikie.bricks.registry.base import BrickManifest

logger = logging.getLogger(__name__)

DEFAULT_REGISTRY_URL = "https://brikie.co/bricks"


class RegistryError(Exception):
    """Raised when a registry request fails (HTTP error, checksum, load)."""


class RegistryClient:
    """HTTP client for the brikie.co brick registry.

    Fetches brick manifests, indexes, and search results from a remote
    registry server, downloads brick source files (with checksum
    verification), and dynamically loads them into a BrickRegistry.

    Args:
        registry_url: Base URL of the brick registry.
    """

    def __init__(self, registry_url: str = DEFAULT_REGISTRY_URL) -> None:
        self._registry_url = registry_url.rstrip("/")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_manifest(
        self, name: str, version: str | None = None
    ) -> BrickManifest:
        """Fetch a single brick manifest from the registry.

        Args:
            name: Brick name (e.g. "foreman").
            version: Optional semantic version.  If None the latest is used.

        Returns:
            A fully populated BrickManifest.

        Raises:
            RegistryError: On HTTP failure or invalid response.
        """
        url = f"{self._registry_url}/{name}/manifest.json"
        if version:
            url = f"{self._registry_url}/{name}/{version}/manifest.json"

        data = await self._get_json(url)
        return BrickManifest.from_dict(data)

    async def search(self, query: str) -> list[BrickManifest]:
        """Search the registry for bricks matching *query*.

        Args:
            query: Free-text search string.

        Returns:
            A list of matching manifests (may be empty).
        """
        url = f"{self._registry_url}/search?q={query}"
        data = await self._get_json(url)
        return [BrickManifest.from_dict(item) for item in data]

    async def list_available(self) -> list[BrickManifest]:
        """List all bricks published in the registry.

        Returns:
            A list of all available manifests.
        """
        url = f"{self._registry_url}/index.json"
        data = await self._get_json(url)
        return [BrickManifest.from_dict(item) for item in data]

    async def publish(
        self, manifest: BrickManifest, source_code: str
    ) -> BrickManifest:
        """Publish a brick (manifest + source) to the registry.

        The server is the authority on ``checksum`` and ``download_url`` —
        whatever this manifest carries for those fields, the returned
        manifest holds the canonical registry values.

        Args:
            manifest: Manifest describing the brick to publish.
            source_code: Complete Python source of the brick module.

        Returns:
            The canonical manifest as stored by the registry.

        Raises:
            RegistryError: On HTTP failure, validation rejection, or a
                version that is already published.
        """
        url = f"{self._registry_url}/publish"
        payload = {"manifest": manifest.to_dict(), "source_code": source_code}
        data = await self._post_json(url, payload)
        return BrickManifest.from_dict(data)

    async def download_brick(
        self, manifest: BrickManifest, target_dir: str
    ) -> str:
        """Download a brick's source file into *target_dir*.

        Fetches ``manifest.download_url``, verifies the SHA-256 checksum
        when the manifest provides one, and writes the source alongside a
        JSON receipt describing the installation.

        Args:
            manifest: The brick manifest to download.
            target_dir: Local directory to install into.

        Returns:
            The absolute path to the downloaded brick source file.

        Raises:
            RegistryError: On HTTP failure or checksum mismatch.
        """
        target = Path(target_dir).expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)

        content = await self._get_bytes(manifest.download_url)
        self._verify_checksum(manifest, content)

        source_path = target / f"{manifest.name}-{manifest.version}.py"
        source_path.write_bytes(content)

        receipt = {
            "name": manifest.name,
            "version": manifest.version,
            "type": manifest.type,
            "description": manifest.description,
            "downloaded_from": manifest.download_url,
            "checksum": manifest.checksum,
            "dependencies": manifest.dependencies,
            "source_file": str(source_path),
        }
        receipt_path = target / f"{manifest.name}-{manifest.version}.receipt.json"
        receipt_path.write_text(json.dumps(receipt, indent=2))

        logger.info(
            "Downloaded %s v%s -> %s", manifest.name, manifest.version, source_path
        )
        return str(source_path)

    def load_brick_from_file(
        self, source_path: str, brick_registry: Any  # noqa: ANN401
    ) -> Any:
        """Dynamically import a brick source file and seat it in the registry.

        Imports the module from *source_path*, finds the class carrying a
        ``BRICK_NUMBER`` attribute, instantiates it (injecting the registry
        when the constructor accepts a ``registry`` parameter, mirroring
        BuildLoader), and registers it.

        Args:
            source_path: Path to a Python file defining one brick class.
            brick_registry: A ``BrickRegistry`` instance to register into.

        Returns:
            The seated brick instance.

        Raises:
            RegistryError: If the file cannot be imported or holds no brick.
        """
        path = Path(source_path).expanduser().resolve()
        module_name = f"brikie_dynamic_{path.stem.replace('-', '_').replace('.', '_')}"

        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise RegistryError(f"Cannot create import spec for '{path}'")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            sys.modules.pop(module_name, None)
            raise RegistryError(f"Brick source '{path}' failed to import: {exc}") from exc

        brick_cls = self._find_brick_class(module)
        if brick_cls is None:
            raise RegistryError(
                f"No brick class found in '{path}' "
                "(must have a BRICK_NUMBER class attribute)"
            )

        brick = self._instantiate(brick_cls, brick_registry)
        brick_registry.register(brick)
        logger.info(
            "Seated brick %s (%s) from %s",
            brick.name, getattr(brick_cls, "BRICK_NUMBER", "?"), path,
        )
        return brick

    async def register_brick(
        self, manifest: BrickManifest, brick_registry: Any  # noqa: ANN401
    ) -> bool:
        """Register a brick whose module is already importable locally.

        Used for bricks that ship with brikie itself: the manifest's
        download_url is interpreted as a module path relative to the
        registry root.

        Args:
            manifest: The brick to register.
            brick_registry: A ``BrickRegistry`` instance.

        Returns:
            True if the brick was imported and registered.

        Raises:
            RegistryError: If the brick module or class cannot be loaded.
        """
        module_path = manifest.download_url
        for prefix in (f"{self._registry_url}/", "https://brikie.co/bricks/"):
            module_path = module_path.replace(prefix, "")
        module_path = module_path.replace("/", ".").rstrip(".")

        try:
            mod = importlib.import_module(module_path)
        except ImportError as exc:
            raise RegistryError(
                f"Cannot import brick module '{module_path}': {exc}"
            ) from exc

        brick_cls = self._find_brick_class(mod)
        if brick_cls is None:
            raise RegistryError(
                f"No brick class found in module '{module_path}' "
                "(must have BRICK_NUMBER class attribute)"
            )

        brick = self._instantiate(brick_cls, brick_registry)
        brick_registry.register(brick)

        logger.info(
            "Registered brick %s (%s) v%s",
            manifest.name, getattr(brick_cls, "BRICK_NUMBER", "?"), manifest.version,
        )
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _find_brick_class(module: Any) -> type | None:  # noqa: ANN401
        """Locate the first class in *module* defined there with BRICK_NUMBER."""
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                isinstance(attr, type)
                and getattr(attr, "__module__", None) == module.__name__
                and hasattr(attr, "BRICK_NUMBER")
            ):
                return attr
        return None

    @staticmethod
    def _instantiate(brick_cls: type, brick_registry: Any) -> Any:  # noqa: ANN401
        """Instantiate a brick class, injecting the registry when accepted."""
        import inspect

        try:
            sig = inspect.signature(brick_cls.__init__)
            if "registry" in sig.parameters:
                return brick_cls(registry=brick_registry)
        except (TypeError, ValueError):
            pass
        return brick_cls()

    @staticmethod
    def _verify_checksum(manifest: BrickManifest, content: bytes) -> None:
        """Raise RegistryError when the manifest checksum doesn't match."""
        if not manifest.checksum:
            return
        expected = manifest.checksum.lower().removeprefix("sha256:")
        actual = hashlib.sha256(content).hexdigest()
        if actual != expected:
            raise RegistryError(
                f"Checksum mismatch for {manifest.name} v{manifest.version}: "
                f"expected {expected}, got {actual}"
            )

    async def _get_json(self, url: str) -> Any:
        """Perform an HTTP GET and parse the response as JSON.

        Args:
            url: Fully qualified URL to fetch.

        Returns:
            Parsed JSON payload.

        Raises:
            RegistryError: On HTTP failure.
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.get(url)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                msg = f"Registry HTTP {exc.response.status_code} for {url}"
                logger.error(msg)
                raise RegistryError(msg) from exc
            except httpx.RequestError as exc:
                msg = f"Registry request failed for {url}: {exc}"
                logger.error(msg)
                raise RegistryError(msg) from exc

    async def _post_json(self, url: str, payload: Any) -> Any:
        """Perform an HTTP POST with a JSON body and parse the JSON response.

        Args:
            url: Fully qualified URL to post to.
            payload: JSON-serialisable request body.

        Returns:
            Parsed JSON payload.

        Raises:
            RegistryError: On HTTP failure (the server's ``error`` field is
                surfaced when present).
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                detail = ""
                try:
                    detail = exc.response.json().get("error", "")
                except (ValueError, AttributeError):
                    pass
                msg = f"Registry HTTP {exc.response.status_code} for {url}"
                if detail:
                    msg = f"{msg}: {detail}"
                logger.error(msg)
                raise RegistryError(msg) from exc
            except httpx.RequestError as exc:
                msg = f"Registry request failed for {url}: {exc}"
                logger.error(msg)
                raise RegistryError(msg) from exc

    async def _get_bytes(self, url: str) -> bytes:
        """Perform an HTTP GET and return the raw response body.

        Args:
            url: Fully qualified URL to fetch.

        Returns:
            Response body bytes.

        Raises:
            RegistryError: On HTTP failure.
        """
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            try:
                response = await client.get(url)
                response.raise_for_status()
                return response.content
            except httpx.HTTPStatusError as exc:
                msg = f"Registry HTTP {exc.response.status_code} for {url}"
                logger.error(msg)
                raise RegistryError(msg) from exc
            except httpx.RequestError as exc:
                msg = f"Registry request failed for {url}: {exc}"
                logger.error(msg)
                raise RegistryError(msg) from exc
