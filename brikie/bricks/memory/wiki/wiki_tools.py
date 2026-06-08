"""LLM Wiki Tool Schemas — OpenAI-compatible function definitions.

Provides the tool schemas that the Wiki Brick registers with the Provider.
The agent can invoke these tools to manage the wiki knowledge base: ingest pages,
search by BM25 relevance, lint structural integrity, and read the index.

All 4 tools operate on the wiki directory structure and markdown page files.
"""

from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# Tool 1: wiki:ingest — Create or update wiki pages
# ---------------------------------------------------------------------------
WIKI_INGEST_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "wiki:ingest",
        "description": (
            "Create or update a wiki page with markdown content. "
            "Pages are compiled into a searchable knowledge base. "
            "The 'merge' operation surgically updates only changed sections "
            "while preserving existing content."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": (
                        "Page title. Used as the filename stem (e.g., 'Auth-Migration'). "
                        "Must be unique within the wiki directory."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": (
                        "Markdown body of the page. Can include YAML frontmatter, "
                        "headings, lists, code blocks, and wiki-links. "
                        "Keep under 800 lines for optimal search performance."
                    ),
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "List of tags to categorize the page. "
                        "Useful for filtering during queries. "
                        "Common examples: ['auth', 'migration', '2026-q2']."
                    ),
                },
                "source": {
                    "type": "string",
                    "enum": ["auto-extract", "manual", "tool-capture"],
                    "description": (
                        "Origin of the page content. "
                        "'auto-extract' for middleware-extracted memories, "
                        "'manual' for direct agent edits, "
                        "'tool-capture' for structured tool output."
                    ),
                    "default": "manual",
                },
                "operation": {
                    "type": "string",
                    "enum": ["create", "update", "merge"],
                    "description": (
                        "How to apply the content. "
                        "'create' adds a new page (fails if title exists); "
                        "'update' replaces the entire page; "
                        "'merge' surgically updates only changed sections "
                        "while preserving existing content."
                    ),
                    "default": "create",
                },
            },
            "required": ["title", "content"],
        },
    },
}

# ---------------------------------------------------------------------------
# Tool 2: wiki:query — BM25 search across wiki pages
# ---------------------------------------------------------------------------
WIKI_QUERY_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "wiki:query",
        "description": (
            "Search the wiki knowledge base using BM25 relevance scoring. "
            "Returns pages ranked by relevance to the query. "
            "Use this to find existing knowledge before creating new pages."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Search query string. Supports full-text BM25 scoring "
                        "across page titles, frontmatter, and body content."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results to return. Default: 10.",
                    "default": 10,
                },
                "status": {
                    "type": "string",
                    "description": (
                        "Filter by page status. "
                        "Common values: 'draft', 'reviewed', 'canonical', 'stale'. "
                        "Omit to include all statuses."
                    ),
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Filter by tags. A page must have at least one matching tag "
                        "to be included in results."
                    ),
                },
            },
            "required": ["query"],
        },
    },
}

# ---------------------------------------------------------------------------
# Tool 3: wiki:lint — Structural linting
# ---------------------------------------------------------------------------
WIKI_LINT_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "wiki:lint",
        "description": (
            "Check wiki structural integrity. Finds orphaned pages (no inbound links), "
            "broken wiki-links, pages exceeding line caps, missing YAML frontmatter, "
            "and stale pages. Use this to maintain wiki health."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "check": {
                    "type": "string",
                    "enum": ["all", "orphans", "broken-links", "caps", "frontmatter", "stale"],
                    "description": (
                        "Which structural check to run. "
                        "'all' runs the full suite; "
                        "'orphans' finds pages with no inbound wiki-links; "
                        "'broken-links' detects [[Link]] references pointing to missing pages; "
                        "'caps' flags pages exceeding the 400-line soft cap or 800-line hard cap; "
                        "'frontmatter' checks for missing or malformed YAML frontmatter; "
                        "'stale' identifies pages not updated within the recency window."
                    ),
                    "default": "all",
                },
            },
        },
    },
}

# ---------------------------------------------------------------------------
# Tool 4: wiki:index — Read or regenerate index
# ---------------------------------------------------------------------------
WIKI_INDEX_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "wiki:index",
        "description": (
            "Read the auto-generated wiki index page showing the complete directory "
            "structure, page counts, and recent updates. Use this to get an overview "
            "of all wiki content."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


def get_wiki_tools() -> List[Dict[str, Any]]:
    """Return all LLM Wiki tool schemas.

    Returns:
        A list of OpenAI-compatible tool schema dicts.
    """
    return [
        WIKI_INGEST_TOOL,
        WIKI_QUERY_TOOL,
        WIKI_LINT_TOOL,
        WIKI_INDEX_TOOL,
    ]
