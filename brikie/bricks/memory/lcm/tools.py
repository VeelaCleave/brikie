"""LCM Tool Schemas — OpenAI-compatible function definitions.

Provides the tool schemas that the LCM Brick registers with the Provider.
The agent can invoke these tools to retrieve data from the immutable store.
"""

from typing import Any, Dict, List

# OpenAI-compatible tool schemas.
LCM_EXPAND_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "lcm_expand",
        "description": (
            "Losslessly retrieve original messages from the immutable store. "
            "Use this when you need the exact content of messages that have been "
            "compacted into a summary. Returns the original role, content, and index "
            "of each message in the range."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "The session to retrieve messages from.",
                },
                "start_index": {
                    "type": "integer",
                    "description": "Starting message index (inclusive, 0-based).",
                },
                "end_index": {
                    "type": "integer",
                    "description": "Ending message index (inclusive, 0-based).",
                },
            },
            "required": ["session_id", "start_index", "end_index"],
        },
    },
}

LCM_GREP_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "lcm_grep",
        "description": (
            "Search messages in the immutable store by content pattern. "
            "Use this when you need to find specific information across the "
            "session history (e.g., error traces, architectural decisions). "
            "Supports filtering by role and limiting results."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "The session to search within.",
                },
                "pattern": {
                    "type": "string",
                    "description": "The content pattern to search for (SQL LIKE pattern).",
                },
                "roles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter by message roles (e.g., ['user', 'assistant']).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default: 20).",
                },
            },
            "required": ["session_id", "pattern"],
        },
    },
}

def get_lcm_tools() -> List[Dict[str, Any]]:
    """Return all LCM tool schemas.

    Returns:
        A list of OpenAI-compatible tool schema dicts.
    """
    return [LCM_EXPAND_TOOL, LCM_GREP_TOOL]
