"""KadeiaInstallerBrick — Tool Brick for remote brick registry operations.

Provides three tools (kadeia_search, kadeia_install, kadeia_list) that
allow an agent to discover and install bricks from the Kadeia registry
at runtime.  All installed bricks are recorded in an in-memory dict.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from brikie.bricks.registry.base import BrickManifest
from brikie.bricks.registry.kadeia_registry import KadeiaRegistry
from brikie.bricks.tool.base import ToolBrick

logger = logging.getLogger(__name__)


class KadeiaInstallerBrick(ToolBrick):
    """Tool Brick that wraps the Kadeia registry client as agent tools.

    Tools:
        - kadeia_search: Search bricks by query, optionally filtered by type.
        - kadeia_install: Download and register a brick by name + optional version.
        - kadeia_list: List available bricks, optionally filtered by type.
    """

    tools: List[Dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": "kadeia_search",
                "description": "Search the Kadeia brick registry for available bricks matching a query. Optionally filter by brick type (soul, tool, provider, interface, memory).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Free-text search query (e.g. 'orchestrator', 'web design', 'crypto').",
                        },
                        "type_filter": {
                            "type": "string",
                            "description": "Optional brick type to filter by: 'soul', 'tool', 'provider', 'interface', or 'memory'.",
                            "enum": ["soul", "tool", "provider", "interface", "memory"],
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "kadeia_install",
                "description": "Download and register a brick from the Kadeia registry by name and optional version.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Canonical brick name to install (e.g. 'sisyphus_orchestrator').",
                        },
                        "version": {
                            "type": "string",
                            "description": "Optional semantic version string (e.g. '1.0.0'). Defaults to latest if omitted.",
                        },
                    },
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "kadeia_list",
                "description": "List all bricks available in the Kadeia registry, optionally filtered by brick type.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "type_filter": {
                            "type": "string",
                            "description": "Optional brick type to filter by: 'soul', 'tool', 'provider', 'interface', or 'memory'.",
                            "enum": ["soul", "tool", "provider", "interface", "memory"],
                        },
                    },
                    "required": [],
                },
            },
        },
    ]

    def __init__(self, registry_url: str = "https://kadeia.co/bricks") -> None:
        """Initialize the installer with a KadeiaRegistry client.

        Args:
            registry_url: Base URL of the Kadeia brick registry.
        """
        super().__init__()
        self._name = "kadeia_installer"
        self._registry = KadeiaRegistry(registry_url)
        self._installed: Dict[str, BrickManifest] = {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def installed(self) -> Dict[str, BrickManifest]:
        """Read-only view of currently installed bricks, keyed by name."""
        return dict(self._installed)

    async def execute(self, name: str, args: Dict[str, Any]) -> Any:
        """Execute one of the kadeia tools by name.

        Args:
            name: Tool name ('kadeia_search', 'kadeia_install', or 'kadeia_list').
            args: Tool arguments.

        Returns:
            The tool's result (list of dicts, installation summary, etc.).

        Raises:
            KeyError: If the tool name is not recognized.
        """
        if name == "kadeia_search":
            return await self._search(args)
        elif name == "kadeia_install":
            return await self._install(args)
        elif name == "kadeia_list":
            return await self._list(args)
        else:
            raise KeyError(f"Unknown tool: {name}")

    # ------------------------------------------------------------------
    # Tool handlers
    # ------------------------------------------------------------------

    async def _search(self, args: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Search the registry and return results as serialisable dicts."""
        query = args.get("query", "")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("kadeia_search: 'query' must be a non-empty string")

        results = await self._registry.search(query)

        # Apply optional type filter client-side
        type_filter = args.get("type_filter")
        if type_filter:
            results = [m for m in results if m.type == type_filter]

        return [m.to_dict() for m in results]

    async def _install(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch a manifest, simulate download, and register locally."""
        name = args.get("name", "")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("kadeia_install: 'name' must be a non-empty string")

        version = args.get("version")
        if version is not None and not isinstance(version, str):
            raise ValueError("kadeia_install: 'version' must be a string or None")

        logger.info("Installing brick '%s' (version=%s)", name, version)

        manifest = await self._registry.fetch_manifest(name, version)
        install_path = await self._registry.download_brick(
            manifest, target_dir="/tmp/brikie/installed"
        )

        self._installed[manifest.name] = manifest
        logger.info("Brick '%s' v%s installed to %s", manifest.name, manifest.version, install_path)

        return {
            "name": manifest.name,
            "version": manifest.version,
            "type": manifest.type,
            "description": manifest.description,
            "installed_path": install_path,
            "dependencies": list(manifest.dependencies),
        }

    async def _list(self, args: Dict[str, Any]) -> List[Dict[str, Any]]:
        """List available bricks, optionally filtered by type."""
        manifests = await self._registry.list_available()

        type_filter = args.get("type_filter")
        if type_filter:
            manifests = [m for m in manifests if m.type == type_filter]

        return [m.to_dict() for m in manifests]
