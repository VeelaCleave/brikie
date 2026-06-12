"""OpenAI-compatible tool schemas for the brikie.co registry.

Provides ``get_registry_tools()`` returning six tool schemas that let an
LLM search the brick registry, list available bricks, install them, author
brand-new bricks from source, publish them, and uninstall bricks again.
"""

from __future__ import annotations

from typing import Any

_BRICK_TYPES = [
    "soul", "tool", "provider", "interface", "memory",
    "logging", "security", "improvement",
]


def get_registry_tools() -> list[dict[str, Any]]:
    """Return OpenAI-compatible tool definitions for the registry installer.

    Tools returned:
        - **registry_search** — Search the brikie.co registry for bricks.
        - **registry_install** — Download, verify, and seat a brick.
        - **registry_list** — List available bricks, optionally by type.
        - **registry_create_brick** — Author a new brick from source and seat it.
        - **registry_publish** — Push an authored brick to the registry.
        - **registry_uninstall** — Unseat a brick installed this session.

    Returns:
        A list of tool definition dicts conforming to the OpenAI tool-call
        schema (``type: "function"`` with a ``function`` sub-dict).
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "registry_search",
                "description": "Search the brikie.co brick registry for available bricks matching a query. Optionally filter by brick type.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Free-text search query (e.g. 'orchestrator', 'web design', 'crypto').",
                        },
                        "type_filter": {
                            "type": "string",
                            "description": "Optional brick type to filter by.",
                            "enum": _BRICK_TYPES,
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "registry_install",
                "description": "Download a brick from the brikie.co registry, verify its checksum, and seat it into the running agent. The brick's tools become available immediately.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Canonical brick name to install (e.g. 'foreman').",
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
                "name": "registry_list",
                "description": "List all bricks available in the brikie.co registry, optionally filtered by brick type.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "type_filter": {
                            "type": "string",
                            "description": "Optional brick type to filter by.",
                            "enum": _BRICK_TYPES,
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "registry_create_brick",
                "description": "Author a brand-new brick from Python source and seat it into the running agent. The source must be a complete module defining one class with a BRICK_NUMBER class attribute, a name property, and async init()/shutdown() methods. A manifest sidecar is written so the brick can be published later.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Name for the new brick (lowercase, underscores).",
                        },
                        "type": {
                            "type": "string",
                            "description": "Brick category.",
                            "enum": _BRICK_TYPES,
                        },
                        "description": {
                            "type": "string",
                            "description": "One-line summary of what the brick does.",
                        },
                        "source_code": {
                            "type": "string",
                            "description": "Complete Python source for the brick module.",
                        },
                        "version": {
                            "type": "string",
                            "description": "Semantic version for the new brick (default '0.1.0').",
                        },
                    },
                    "required": ["name", "type", "source_code"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "registry_publish",
                "description": "Publish a brick you authored with registry_create_brick to the brikie.co registry so other agents can install it. The registry computes the canonical checksum and download URL.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Name of the locally authored brick to publish.",
                        },
                        "version": {
                            "type": "string",
                            "description": "Specific local version to publish. Defaults to the highest version present locally.",
                        },
                    },
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "registry_uninstall",
                "description": "Unseat a brick that was installed or authored this session. Optionally delete its source files from the install directory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Name of the brick to uninstall.",
                        },
                        "delete_files": {
                            "type": "boolean",
                            "description": "Also delete the brick's source/manifest files (default false).",
                        },
                    },
                    "required": ["name"],
                },
            },
        },
    ]
