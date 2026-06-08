"""MemPalace Tool Schemas — OpenAI-compatible function definitions.

Provides the tool schemas that the MemPalace Brick registers with the Provider.
The agent can invoke these tools to query the spatial hierarchy and temporal graph.

All 5 tools are READ-ONLY. The write path (entities, triples, drawers) is
handled entirely by the middleware's auto-extraction paradigm.
"""

from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# Tool 1: mempalace_query — Semantic Search
# ---------------------------------------------------------------------------
MEMPALACE_QUERY_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "mempalace_query",
        "description": (
            "Semantic search within the MemPalace spatial hierarchy. "
            "Search for memories by query string or vector embedding "
            "within a specific Wing/Room/Hall scope. Uses ChromaDB similarity "
            "scores to rank results. Use this when you need to find relevant "
            "memories across the spatial organization (e.g., past decisions, "
            "architectural choices, session milestones)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Search query string. The system will vector-embed "
                        "this and compare against ChromaDB."
                    ),
                },
                "wing": {
                    "type": "string",
                    "description": (
                        "Top-level organizational unit to scope the search "
                        "(e.g., 'project-alpha', 'person-milla', 'domain-security'). "
                        "Omit to search across all wings."
                    ),
                },
                "room": {
                    "type": "string",
                    "description": (
                        "Room within the wing to scope the search "
                        "(e.g., 'auth-migration', 'api-design')."
                    ),
                },
                "hall": {
                    "type": "string",
                    "description": (
                        "Hall category within the wing. "
                        "Common values: 'hall_facts', 'hall_events', "
                        "'hall_discoveries'. Omit to search all halls."
                    ),
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of results to return. Default: 10.",
                    "default": 10,
                },
                "min_similarity": {
                    "type": "number",
                    "description": (
                        "Minimum similarity score (0.0 - 1.0) for results. "
                        "Default: 0.7. Lower values return more results "
                        "with less precision."
                    ),
                    "default": 0.7,
                },
                "temporal_window": {
                    "type": "string",
                    "description": (
                        "Time range filter for results. "
                        "Formats: 'start..end' (ISO 8601), 'last N[h|d|w|m]', "
                        "or a single point-in-time for 'as_of' queries. "
                        "Examples: '2026-05-01..2026-06-08', 'last 7d', 'last 2w'."
                    ),
                },
            },
            "required": ["query"],
        },
    },
}

# ---------------------------------------------------------------------------
# Tool 2: mempalace_traverse — Spatial Navigation
# ---------------------------------------------------------------------------
MEMPALACE_TRAVERSE_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "mempalace_traverse",
        "description": (
            "Navigate the MemPalace spatial hierarchy. Discover wings, "
            "rooms, halls, and tunnels. Use this to explore the structure "
            "of stored memories before searching or querying specific data. "
            "Supports listing top-level units, drilling into rooms, finding "
            "cross-wing tunnels, and viewing paths through the hierarchy."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "list_wings",
                        "list_rooms",
                        "list_halls",
                        "enter_room",
                        "enter_hall",
                        "tunnel",
                        "path",
                    ],
                    "description": (
                        "Traversal action: "
                        "'list_wings' shows top-level units; "
                        "'list_rooms' shows rooms in a wing; "
                        "'list_halls' shows categories in a wing; "
                        "'enter_room' navigates to a specific room and lists its drawers; "
                        "'enter_hall' navigates to a specific hall and lists its drawers; "
                        "'tunnel' finds cross-wing bridges for a shared room name; "
                        "'path' shows the path from root to a target location."
                    ),
                },
                "wing": {
                    "type": "string",
                    "description": (
                        "Target wing for actions: list_rooms, list_halls, "
                        "enter_room, enter_hall. Required for room/hall actions."
                    ),
                },
                "room": {
                    "type": "string",
                    "description": (
                        "Target room for enter_room action. "
                        "Also used for tunnel action (find wings sharing this room name)."
                    ),
                },
                "hall": {
                    "type": "string",
                    "description": "Target hall for enter_hall action.",
                },
                "depth": {
                    "type": "integer",
                    "description": (
                        "How deep to traverse (1 = direct children only, "
                        "2 = nested). Default: 1. Applies to list_wings "
                        "and list_rooms actions."
                    ),
                    "default": 1,
                },
                "sort_by": {
                    "type": "string",
                    "enum": ["recency", "size", "relevance", "alphabetical"],
                    "description": (
                        "Sort order for results. "
                        "'recency' sorts by last updated; "
                        "'size' by drawer count; "
                        "'relevance' by embedding density; "
                        "'alphabetical' by name."
                    ),
                    "default": "recency",
                },
                "target": {
                    "type": "string",
                    "description": (
                        "Target location for path action. Can be a "
                        "wing, room, or entity reference."
                    ),
                },
            },
            "required": ["action"],
        },
    },
}

# ---------------------------------------------------------------------------
# Tool 3: mempalace_entities — Temporal Graph Entities
# ---------------------------------------------------------------------------
MEMPALACE_ENTITIES_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "mempalace_entities",
        "description": (
            "Query entities in the MemPalace temporal knowledge graph. "
            "Find entities by type, wing, or property match. Entities represent "
            "people, projects, tools, concepts, decisions, and milestones. "
            "Use this to discover what entities exist in the knowledge graph "
            "and their properties."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "description": (
                        "Entity type to filter by. "
                        "Common values: 'person', 'project', 'tool', "
                        "'concept', 'decision', 'milestone', 'dependency'. "
                        "Omit to match all types."
                    ),
                },
                "wing": {
                    "type": "string",
                    "description": (
                        "Scope to a specific wing. "
                        "Useful for finding all entities in a project's "
                        "knowledge graph."
                    ),
                },
                "room": {
                    "type": "string",
                    "description": (
                        "Scope to a specific room within a wing. "
                        "Narrows the entity search to a conceptual area."
                    ),
                },
                "properties_filter": {
                    "type": "object",
                    "description": (
                        "JSON blob to match against entity properties. "
                        "Each key is a property name, each value is a filter. "
                        "Example: {'status': 'active', 'priority': 'high'}."
                    ),
                },
                "sort_by": {
                    "type": "string",
                    "enum": ["recency", "alphabetical", "connectedness"],
                    "description": (
                        "Sort order for results. "
                        "'recency' by last updated; "
                        "'alphabetical' by display_name; "
                        "'connectedness' by number of triples."
                    ),
                    "default": "recency",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Maximum number of results to return. Default: 20.",
                    "default": 20,
                },
            },
        },
    },
}

# ---------------------------------------------------------------------------
# Tool 4: mempalace_triples — Temporal Graph Relationships
# ---------------------------------------------------------------------------
MEMPALACE_TRIPLES_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "mempalace_triples",
        "description": (
            "Query relationships in the MemPalace temporal knowledge graph. "
            "Finds RDF-style subject-predicate-object relationships with "
            "temporal constraints. Use this to discover how entities are "
            "connected (e.g., dependencies, decisions, milestones). "
            "Supports wildcard queries and temporal filtering."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "description": (
                        "Entity ID or display_name of the subject. "
                        "Use '*' for all subjects. "
                        "Examples: 'auth-migration', 'ent_123', '*'."
                    ),
                },
                "predicate": {
                    "type": "string",
                    "description": (
                        "Relationship type. "
                        "Common values: 'depends_on', 'extends', 'blocks', "
                        "'is_part_of', 'decided_by', 'migrates_to'. "
                        "Use '*' for all predicates."
                    ),
                },
                "object": {
                    "type": "string",
                    "description": (
                        "Target entity ID or display_name. "
                        "Use '*' for all objects. "
                        "Examples: 'session-store', 'ent_456', '*'."
                    ),
                },
                "temporal_window": {
                    "type": "string",
                    "description": (
                        "Time range filter for valid_from/valid_to. "
                        "Formats: 'start..end' (ISO 8601), 'last N[h|d|w|m]'. "
                        "Examples: '2026-05-01..2026-06-08', 'last 7d'."
                    ),
                },
                "current_only": {
                    "type": "boolean",
                    "description": (
                        "Filter to currently valid relationships only "
                        "(valid_to is null or in the future)."
                    ),
                    "default": False,
                },
                "reverse": {
                    "type": "boolean",
                    "description": (
                        "Query backward: match where 'object' is the "
                        "subject and vice versa."
                    ),
                    "default": False,
                },
                "include_properties": {
                    "type": "boolean",
                    "description": (
                        "Include entity properties in the result. "
                        "Useful for context without extra queries."
                    ),
                    "default": False,
                },
            },
        },
    },
}

# ---------------------------------------------------------------------------
# Tool 5: mempalace_inject — L1 Essential Story
# ---------------------------------------------------------------------------
MEMPALACE_INJECT_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "mempalace_inject",
        "description": (
            "Inject the L1 Essential Story into the agent's context window. "
            "Takes the highest-scoring snippets from specified wings/rooms/halls, "
            "truncates them to fit the token budget, and groups them by room. "
            "This provides massive contextual awareness using fewer tokens. "
            "Use this at the start of AFK loops or when you need a broad "
            "overview of stored memories. The system pre-computes context "
            "using metadata filters for efficiency."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "wing": {
                    "type": "string",
                    "description": (
                        "Wing to inject from. Omit to include all wings. "
                        "Example: 'project-alpha', 'person-milla'."
                    ),
                },
                "rooms": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "List of specific rooms to include. "
                        "Omit to include all rooms in the wing."
                    ),
                },
                "halls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "List of specific halls to include. "
                        "Common values: 'hall_facts', 'hall_events', "
                        "'hall_discoveries'."
                    ),
                },
                "max_tokens": {
                    "type": "integer",
                    "description": (
                        "Token budget for the injection. "
                        "Default: 1000. The system truncates the "
                        "least-relevant snippets to fit."
                    ),
                    "default": 1000,
                },
                "compress_level": {
                    "type": "string",
                    "enum": ["raw", "compressed", "summary"],
                    "description": (
                        "Compression level using AAAK dialect. "
                        "'raw' returns full drawer content; "
                        "'compressed' applies AAAK shorthand; "
                        "'summary' returns one-line summaries per room. "
                        "Default: 'compressed'."
                    ),
                    "default": "compressed",
                },
            },
        },
    },
}


def get_mempalace_tools() -> List[Dict[str, Any]]:
    """Return all MemPalace tool schemas.

    Returns:
        A list of OpenAI-compatible tool schema dicts.
    """
    return [
        MEMPALACE_QUERY_TOOL,
        MEMPALACE_TRAVERSE_TOOL,
        MEMPALACE_ENTITIES_TOOL,
        MEMPALACE_TRIPLES_TOOL,
        MEMPALACE_INJECT_TOOL,
    ]
