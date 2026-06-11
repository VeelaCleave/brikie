"""Kadeia Registry client — remote brick manifest fetcher and installer.

Provides the KadeiaRegistry class that communicates with the Kadeia brick
registry over HTTP (via httpx) to search, list, fetch manifests, and
simulate brick downloads.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from brikie.bricks.registry.base import BrickManifest

logger = logging.getLogger(__name__)


class KadeiaRegistryError(Exception):
    """Raised when a registry request fails (HTTP error, timeout, etc.)."""


class KadeiaRegistry:
    """HTTP client for the Kadeia brick registry.

    Fetches brick manifests, indexes, and search results from a remote
    registry server. Downloads are simulated by writing placeholder
    receipt files.

    Args:
        registry_url: Base URL of the Kadeia registry.
    """

    def __init__(self, registry_url: str = "https://kadeia.co/bricks") -> None:
        self._registry_url = registry_url.rstrip("/")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_manifest(
        self, name: str, version: str | None = None
    ) -> BrickManifest:
        """Fetch a single brick manifest from the registry.

        Args:
            name: Brick name (e.g. "sisyphus_orchestrator").
            version: Optional semantic version.  If None the latest is used.

        Returns:
            A fully populated BrickManifest.

        Raises:
            KadeiaRegistryError: On HTTP failure or invalid response.
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

    async def download_brick(
        self, manifest: BrickManifest, target_dir: str
    ) -> str:
        """Simulate downloading and extracting a brick.

        Writes a JSON receipt into *target_dir* describing the brick that
        would have been downloaded.

        Args:
            manifest: The brick manifest to "download".
            target_dir: Local directory to place the receipt into.

        Returns:
            The absolute path to the installation receipt.
        """
        target = Path(target_dir).expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)

        receipt = {
            "name": manifest.name,
            "version": manifest.version,
            "type": manifest.type,
            "description": manifest.description,
            "downloaded_from": manifest.download_url,
            "checksum": manifest.checksum,
            "dependencies": manifest.dependencies,
        }

        receipt_path = target / f"{manifest.name}-{manifest.version}.receipt.json"
        receipt_path.write_text(json.dumps(receipt, indent=2))
        logger.info(
            "Simulated download of %s v%s -> %s",
            manifest.name,
            manifest.version,
            receipt_path,
        )
        return str(receipt_path)

    async def register_brick(
        self, manifest: BrickManifest, brick_registry: Any  # noqa: ANN401
    ) -> bool:
        """Register a brick manifest with a local brick registry.

        This is a **placeholder** implementation that simply logs the
        registration attempt and returns ``True``.

        Args:
            manifest: The brick to register.
            brick_registry: The target local brick registry object.

        Returns:
            Always True.
        """
        logger.info(
            "Placeholder: registering %s v%s (type=%s) with %s",
            manifest.name,
            manifest.version,
            manifest.type,
            type(brick_registry).__name__,
        )
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_json(self, url: str) -> Any:
        """Perform an HTTP GET and parse the response as JSON.

        Args:
            url: Fully qualified URL to fetch.

        Returns:
            Parsed JSON payload.

        Raises:
            KadeiaRegistryError: On HTTP failure.
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.get(url)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                msg = f"Registry HTTP {exc.response.status_code} for {url}"
                logger.error(msg)
                raise KadeiaRegistryError(msg) from exc
            except httpx.RequestError as exc:
                msg = f"Registry request failed for {url}: {exc}"
                logger.error(msg)
                raise KadeiaRegistryError(msg) from exc
