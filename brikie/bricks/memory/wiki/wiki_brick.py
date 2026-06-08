"""Wiki Brick — LLM Wiki Memory + Tool Brick.

Implements both MemoryBrick (auto-extraction) and ToolBrick (agent tools)
interfaces. The brick automatically extracts structured knowledge from
messages via the event bus, and exposes 4 tools for the agent to manage
the wiki knowledge base: ingest, query, lint, and index.
"""

import logging
import re
from typing import Any, Dict, List

from brikie.config.types import BrickState
from brikie.bricks.memory.memory_brick import MemoryBrick
from brikie.bricks.memory.wiki.wiki_store import WikiStore
from brikie.bricks.memory.wiki.wiki_tools import get_wiki_tools
from brikie.kernel.registry import ToolBrick

logger = logging.getLogger(__name__)


class WikiBrick(MemoryBrick, ToolBrick):
    """LLM Wiki Brick with auto-extraction and wiki management tools.

    Implements:
    - MemoryBrick: Auto-extracts structured knowledge from messages
    - ToolBrick: Exposes 4 wiki management tools for the agent
    """

    def __init__(self, db_path: str = "wiki.db") -> None:
        super().__init__()
        self._name = "wiki"
        self._store = WikiStore(db_path)
        self._tools = get_wiki_tools()
        self._initialized = False

    @property
    def tools(self) -> List[Dict[str, Any]]:
        """Return the 4 Wiki tool schemas."""
        return self._tools

    async def init(self) -> None:
        """Initialize the Wiki store."""
        await self._store.initialize()
        self._initialized = True
        self._state = BrickState.ACTIVE
        logger.info("WikiBrick: initialized at %s", self._store._db_path)

    async def shutdown(self) -> None:
        """Shutdown the Wiki store."""
        await self._store.shutdown()
        self._initialized = False
        self._state = BrickState.WARM_UP
        logger.info("WikiBrick: shutdown complete")

    async def intercept_message(
        self, session_id: str, role: str, content: str
    ) -> None:
        """Intercept messages and auto-extract structured knowledge.

        For POST_LLM events, detects structured knowledge patterns in
        the content (markdown headings, lists, code blocks). If content
        is substantial (>200 chars), creates a draft wiki page with
        source="auto-extract".
        """
        if not self._initialized:
            return

        if len(content) < 200:
            return

        has_headings = bool(re.search(r"^#{1,3}\s+.+", content, re.MULTILINE))
        has_lists = bool(re.search(r"^[-*]\s+.+", content, re.MULTILINE))
        has_code_blocks = bool(re.search(r"^```", content, re.MULTILINE))

        structured_count = sum([bool(has_headings), bool(has_lists), bool(has_code_blocks)])

        if structured_count < 1:
            return

        title = self._extract_title(content)
        if not title:
            title = "Auto-Extracted: " + content[:50].strip().replace("\n", " ")

        await self._store.upsert_page(
            title=title,
            body=content,
            status="draft",
            tags=["auto-extract"],
            source="auto-extract",
        )

        logger.info("WikiBrick: auto-extracted page '%s'", title)

    def _extract_title(self, content: str) -> str:
        """Try to extract a title from the first markdown heading."""
        match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        if match:
            return match.group(1).strip()
        match = re.search(r"^##\s+(.+)$", content, re.MULTILINE)
        if match:
            return match.group(1).strip()
        return ""

    async def build_context(self, session_id: str) -> Dict[str, Any]:
        """Build context from the wiki knowledge base.

        Returns a summary including total page count and the 5 most
        recently updated pages.
        """
        count = await self._store.page_count()
        pages = await self._store.list_pages()
        recent = [
            {
                "id": p["id"],
                "title": p["title"],
                "status": p["status"],
                "source": p["source"],
                "updated_at": p["updated_at"],
            }
            for p in pages[:5]
        ]

        return {
            "wiki": {
                "page_count": count,
                "recent_pages": recent,
            }
        }

    async def execute(self, name: str, args: Dict[str, Any]) -> Any:
        """Execute a Wiki tool.

        Handles the 4 wiki tools:
        - wiki:ingest: Create or update wiki pages
        - wiki:query: BM25 search across wiki pages
        - wiki:lint: Structural linting
        - wiki:index: List all pages
        """
        if name == "wiki:ingest":
            return await self._handle_ingest(args)
        elif name == "wiki:query":
            return await self._handle_query(args)
        elif name == "wiki:lint":
            return await self._handle_lint(args)
        elif name == "wiki:index":
            return await self._handle_index(args)
        else:
            return {"error": f"Unknown tool: {name}"}

    # ------------------------------------------------------------------
    # Tool Handlers
    # ------------------------------------------------------------------

    async def _handle_ingest(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle wiki:ingest tool — create or update wiki pages."""
        title = args.get("title", "")
        content = args.get("content", "")
        tags = args.get("tags", [])
        source = args.get("source", "manual")
        operation = args.get("operation", "create")

        page_id = self._store._slugify(title)
        existing = await self._store.get_page(page_id)

        page_id = await self._store.upsert_page(
            title=title,
            body=content,
            status="draft",
            tags=tags,
            source=source,
        )

        if existing is not None:
            status = "updated"
        else:
            status = "created"

        return {"status": status, "page_id": page_id, "operation": operation}

    async def _handle_query(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle wiki:query tool — BM25 search across wiki pages."""
        query = args.get("query", "")
        limit = args.get("limit", 10)
        status = args.get("status")
        tags = args.get("tags", [])

        from brikie.bricks.memory.wiki.wiki_search import WikiSearcher

        searcher = WikiSearcher(self._store)
        await searcher.rebuild_index()
        results = await searcher.search(
            query=query,
            limit=limit,
            status=status,
            tags=tags,
        )

        return {"results": results, "query": query}

    async def _handle_lint(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle wiki:lint tool — structural linting."""
        check = args.get("check", "all")

        from brikie.bricks.memory.wiki.wiki_linter import WikiLinter

        linter = WikiLinter(self._store)
        violations = await linter.lint(check=check)

        return {"violations": violations}

    async def _handle_index(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle wiki:index tool — list all pages with formatted index."""
        pages = await self._store.list_pages()
        total = len(pages)

        index_text = "# Wiki Index\n\n"
        index_text += f"**Total pages:** {total}\n\n"

        current_status = None
        for page in pages:
            status = page.get("status", "draft")
            if status != current_status:
                current_status = status
                index_text += f"## {status.title()}\n\n"

            title = page.get("title", "Untitled")
            tags = page.get("tags", [])
            tags_str = f" [{', '.join(tags)}]" if tags else ""
            index_text += f"- [{title}](#{page['id']}){tags_str}\n"

        return {"index": index_text, "pages": pages, "count": total}
