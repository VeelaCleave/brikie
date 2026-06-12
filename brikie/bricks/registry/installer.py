"""RegistryInstallerBrick — Tool Brick for brikie.co registry operations.

Provides five tools that let an agent discover, install, author, and
remove bricks at runtime:

- registry_search       — search the brikie.co registry
- registry_list         — list available bricks
- registry_install      — download, verify, and seat a brick
- registry_create_brick — author a new brick from source and seat it
- registry_uninstall    — unseat a brick installed this session

This is the heart of the Phase D vision: the agent can grow (and prune)
its own brick stack while running.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from brikie.bricks.registry.base import BrickManifest
from brikie.bricks.registry.registry_client import (
    DEFAULT_REGISTRY_URL,
    RegistryClient,
    RegistryError,
)
from brikie.bricks.registry.tools import get_registry_tools
from brikie.bricks.tool.base import ToolBrick
from brikie.kernel.registry import BrickRegistry

logger = logging.getLogger(__name__)

DEFAULT_INSTALL_DIR = "~/.brikie/bricks"

_VALID_BRICK_TYPES = {
    "soul", "tool", "provider", "interface", "memory",
    "logging", "security", "improvement",
}


class RegistryInstallerBrick(ToolBrick):
    BRICK_NUMBER = "BRK-450"
    """Tool Brick that wraps the brikie.co registry client as agent tools.

    Args:
        registry_url: Base URL of the brick registry.
        registry: The local BrickRegistry to seat installed bricks into.
                  (Named ``registry`` so BuildLoader auto-injects it.)
        install_dir: Directory where downloaded/authored brick source lives.
    """

    tools: List[Dict[str, Any]] = get_registry_tools()

    def __init__(
        self,
        registry_url: str = DEFAULT_REGISTRY_URL,
        registry: Optional[BrickRegistry] = None,
        install_dir: str = DEFAULT_INSTALL_DIR,
    ) -> None:
        super().__init__()
        self._name = "registry_installer"
        self._client = RegistryClient(registry_url)
        self._registry = registry
        self._install_dir = Path(install_dir).expanduser()
        self._installed: Dict[str, BrickManifest] = {}
        self._seated: Dict[str, Any] = {}  # name -> live brick instance

    @property
    def name(self) -> str:
        return self._name

    @property
    def installed(self) -> Dict[str, BrickManifest]:
        """Read-only view of bricks installed this session, keyed by name."""
        return dict(self._installed)

    async def execute(self, name: str, args: Dict[str, Any]) -> Any:
        """Execute one of the registry tools by name.

        Args:
            name: Tool name (registry_search, registry_list,
                  registry_install, registry_create_brick,
                  registry_uninstall).
            args: Tool arguments.

        Returns:
            The tool's result (list of dicts, installation summary, etc.).

        Raises:
            KeyError: If the tool name is not recognized.
        """
        if name == "registry_search":
            return await self._search(args)
        elif name == "registry_install":
            return await self._install(args)
        elif name == "registry_list":
            return await self._list(args)
        elif name == "registry_create_brick":
            return await self._create_brick(args)
        elif name == "registry_uninstall":
            return await self._uninstall(args)
        else:
            raise KeyError(f"Unknown tool: {name}")

    # ------------------------------------------------------------------
    # Tool handlers
    # ------------------------------------------------------------------

    async def _search(self, args: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Search the registry and return results as serialisable dicts."""
        query = args.get("query", "")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("registry_search: 'query' must be a non-empty string")

        results = await self._client.search(query)

        # Apply optional type filter client-side
        type_filter = args.get("type_filter")
        if type_filter:
            results = [m for m in results if m.type == type_filter]

        return [m.to_dict() for m in results]

    async def _install(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch a manifest, download + verify the source, and seat the brick."""
        name = args.get("name", "")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("registry_install: 'name' must be a non-empty string")

        version = args.get("version")
        if version is not None and not isinstance(version, str):
            raise ValueError("registry_install: 'version' must be a string or None")

        logger.info("Installing brick '%s' (version=%s)", name, version)

        manifest = await self._client.fetch_manifest(name, version)
        source_path = await self._client.download_brick(
            manifest, target_dir=str(self._install_dir)
        )

        seated = False
        if self._registry is not None:
            brick = self._client.load_brick_from_file(source_path, self._registry)
            await self._init_brick(brick)
            self._seated[manifest.name] = brick
            seated = True

        self._installed[manifest.name] = manifest
        logger.info(
            "Brick '%s' v%s installed%s",
            manifest.name, manifest.version,
            " and seated" if seated else " (no registry attached — not seated)",
        )

        return {
            "name": manifest.name,
            "version": manifest.version,
            "type": manifest.type,
            "description": manifest.description,
            "installed_path": source_path,
            "seated": seated,
            "dependencies": list(manifest.dependencies),
        }

    async def _list(self, args: Dict[str, Any]) -> List[Dict[str, Any]]:
        """List available bricks, optionally filtered by type."""
        manifests = await self._client.list_available()

        type_filter = args.get("type_filter")
        if type_filter:
            manifests = [m for m in manifests if m.type == type_filter]

        return [m.to_dict() for m in manifests]

    async def _create_brick(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Author a new brick from agent-written source and seat it.

        The source must be a complete Python module defining exactly one
        class with a ``BRICK_NUMBER`` class attribute. The source is
        syntax-checked, written into the install directory with a manifest
        sidecar (ready for future publishing), then dynamically loaded and
        registered.
        """
        name = args.get("name", "")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("registry_create_brick: 'name' must be a non-empty string")
        name = name.strip().lower().replace(" ", "_").replace("-", "_")

        brick_type = args.get("type", "tool")
        if brick_type not in _VALID_BRICK_TYPES:
            raise ValueError(
                f"registry_create_brick: 'type' must be one of "
                f"{sorted(_VALID_BRICK_TYPES)}, got {brick_type!r}"
            )

        source_code = args.get("source_code", "")
        if not isinstance(source_code, str) or not source_code.strip():
            raise ValueError(
                "registry_create_brick: 'source_code' must be a complete "
                "Python module defining a class with a BRICK_NUMBER attribute"
            )

        description = str(args.get("description", ""))
        version = str(args.get("version", "0.1.0"))

        # Syntax check before anything touches disk
        try:
            compile(source_code, f"<{name}>", "exec")
        except SyntaxError as exc:
            raise ValueError(
                f"registry_create_brick: source has a syntax error at "
                f"line {exc.lineno}: {exc.msg}"
            ) from exc

        self._install_dir.mkdir(parents=True, exist_ok=True)
        source_path = self._install_dir / f"{name}-{version}.py"
        source_path.write_text(source_code)

        manifest = BrickManifest(
            name=name,
            version=version,
            type=brick_type,
            description=description,
            download_url=f"file://{source_path}",
            author="agent",
        )
        manifest_path = self._install_dir / f"{name}-{version}.manifest.json"
        manifest_path.write_text(json.dumps(manifest.to_dict(), indent=2))

        seated = False
        if self._registry is not None:
            try:
                brick = self._client.load_brick_from_file(
                    str(source_path), self._registry
                )
            except RegistryError:
                # Bad source must not leave artifacts behind
                source_path.unlink(missing_ok=True)
                manifest_path.unlink(missing_ok=True)
                raise
            await self._init_brick(brick)
            self._seated[name] = brick
            seated = True

        self._installed[name] = manifest
        logger.info("Agent-authored brick '%s' v%s created%s",
                    name, version, " and seated" if seated else "")

        return {
            "name": name,
            "version": version,
            "type": brick_type,
            "source_path": str(source_path),
            "manifest_path": str(manifest_path),
            "seated": seated,
        }

    async def _uninstall(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Unseat a brick installed this session, optionally deleting files."""
        name = args.get("name", "")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("registry_uninstall: 'name' must be a non-empty string")

        manifest = self._installed.get(name)
        if manifest is None:
            return {
                "name": name,
                "uninstalled": False,
                "error": f"'{name}' was not installed this session",
            }

        brick = self._seated.pop(name, None)
        if brick is not None and self._registry is not None:
            try:
                await brick.shutdown()
            except Exception as exc:
                logger.warning("Brick '%s' shutdown failed: %s", name, exc)
            self._registry.unregister(brick.name)

        deleted_files: List[str] = []
        if args.get("delete_files"):
            for path in self._install_dir.glob(f"{name}-{manifest.version}.*"):
                path.unlink(missing_ok=True)
                deleted_files.append(str(path))

        del self._installed[name]
        logger.info("Brick '%s' uninstalled", name)

        return {
            "name": name,
            "uninstalled": True,
            "was_seated": brick is not None,
            "deleted_files": deleted_files,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _init_brick(brick: Any) -> None:  # noqa: ANN401
        """Warm up a brick seated after the kernel's warm-up phase ran."""
        try:
            await brick.init()
        except Exception as exc:
            logger.warning("Brick '%s' init failed: %s", brick.name, exc)
