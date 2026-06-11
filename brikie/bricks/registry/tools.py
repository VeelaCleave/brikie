"""OpenAI-compatible tool schemas for the Kadeia registry.

Provides ``get_kadeia_tools()`` returning three tool schemas that allow
an LLM to search the Kadeia brick registry, list available bricks, and
install them.
"""

from __future__ import annotations

from typing import Any


def get_kadeia_tools() -> list[dict[str, Any]]:
    """Return OpenAI-compatible tool definitions for the Kadeia installer.

    Tools returned:
        - **kadeia_search** — Search the Kadeia registry for bricks.
        - **kadeia_install** — Download and register a brick from the registry.
        - **kadeia_list** — List available bricks, optionally filtered by type.

    Returns:
        A list of tool definition dicts conforming to the OpenAI tool-call
        schema (``type: "function"`` with a ``function`` sub-dict).
    """
    return [
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
