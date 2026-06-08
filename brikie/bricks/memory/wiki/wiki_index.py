"""Wiki Index Manager — index regeneration, page sharding, and directory sharding.

Manages the auto-generated index.md for the LLM Wiki Brick.
- Regenerates the index with page counts by status
- Scans pages for soft/hard line-count cap violations
- Splits oversized pages at heading boundaries
- Suggests directory sharding when page count exceeds threshold
"""

import logging
import re
from datetime import datetime, timezone
from typing import List

from brikie.bricks.memory.wiki.wiki_store import WikiStore
from brikie.bricks.memory.wiki.wiki_search import WikiSearcher
from brikie.bricks.memory.wiki.wiki_linter import WikiLinter

logger = logging.getLogger(__name__)

SOFT_CAP = 400
HARD_CAP = 800
DIR_SHARD_THRESHOLD = 150


class WikiIndex:
    """Manages the wiki index page, sharding, and directory layout."""

    def __init__(self, store: WikiStore, searcher: WikiSearcher, linter: WikiLinter) -> None:
        self._store = store
        self._searcher = searcher
        self._linter = linter

    # ── Index regeneration ──────────────────────────────────────────

    async def regenerate_index(self) -> str:
        """Iterate all pages and write a fresh index.md. Return the index text."""
        pages: List[dict] = []
        async for page in self._store.iter_pages():
            pages.append(page)

        draft = sum(1 for p in pages if p["status"] == "draft")
        review = sum(1 for p in pages if p["status"] == "review")
        published = sum(1 for p in pages if p["status"] == "published")
        archived = sum(1 for p in pages if p["status"] == "archived")
        total = len(pages)

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        lines: List[str] = [
            "# Wiki Index",
            f"*Auto-generated {now}*",
            "",
            f"**Total Pages**: {total}",
            f"**Draft**: {draft} | **Review**: {review} | **Published**: {published} | **Archived**: {archived}",
            "",
            "## Pages by Status",
        ]

        for status in ("published", "draft", "review", "archived"):
            status_pages = [p for p in pages if p["status"] == status]
            if not status_pages:
                continue

            lines.append(f"### {status.capitalize()}")
            for p in status_pages:
                title = p["title"]
                updated = p.get("updated_at", "—")
                tag_str = ", ".join(p.get("tags", [])) if p.get("tags") else "— "
                lines.append(f"- [[{title}]] — *updated {updated}* — tags: {tag_str}")

        index_text = "\n".join(lines) + "\n"

        # Write index.md to wiki directory
        index_path = self._store._wiki_dir / "index.md"
        index_path.write_text(index_text, encoding="utf-8")
        logger.info("WikiIndex: index.md regenerated at %s (%d pages)", index_path, total)

        return index_text

    async def read_index(self) -> str:
        """Read and return index.md content. Regenerate if missing."""
        index_path = self._store._wiki_dir / "index.md"
        if index_path.exists():
            return index_path.read_text(encoding="utf-8")

        logger.info("WikiIndex: index.md not found, regenerating")
        return await self.regenerate_index()

    # ── Page sharding ───────────────────────────────────────────────

    async def check_shards(self) -> List[dict]:
        """Return pages exceeding soft or hard line-count caps."""
        violations: List[dict] = []

        pages: List[dict] = []
        async for page in self._store.iter_pages():
            pages.append(page)

        for page in pages:
            lc = page.get("line_count", len(page.get("body", "").splitlines()))
            if lc > HARD_CAP:
                violations.append({
                    "page_id": page["id"],
                    "title": page["title"],
                    "line_count": lc,
                    "severity": "hard",
                })
            elif lc > SOFT_CAP:
                violations.append({
                    "page_id": page["id"],
                    "title": page["title"],
                    "line_count": lc,
                    "severity": "soft",
                })

        return violations

    async def shard_page(self, page_id: str) -> List[str]:
        """Split a page at ## heading boundaries and upsert child pages.

        Returns list of new child page_ids.
        """
        page = await self._store.get_page(page_id)
        if page is None:
            return []

        body = page.get("body", "")
        title = page["title"]
        tags = page.get("tags", [])
        source = page.get("source", "manual")
        status = page["status"]

        line_count = len(body.splitlines())
        if line_count <= SOFT_CAP:
            logger.info("WikiIndex: %s has %d lines, under soft cap — no sharding needed", page_id, line_count)
            return []

        # Split at ## heading boundaries
        sections = re.split(r'(?=^##\s)', body, flags=re.MULTILINE)
        sections = [s for s in sections if s.strip()]

        if len(sections) <= 1:
            logger.info("WikiIndex: %s has only 1 section, minimal sharding", page_id)
            sections = [body]

        # Group sections into children of ~200-400 lines each
        children: List[List[str]] = []
        current_child: List[str] = []
        current_lines = 0

        for section in sections:
            sec_lines = len(section.splitlines())
            if current_lines + sec_lines > 400 and current_child:
                children.append(current_child)
                current_child = [section]
                current_lines = sec_lines
            else:
                current_child.append(section)
                current_lines += sec_lines

        if current_child:
            children.append(current_child)

        # If splitting produces only one child, no sharding needed
        if len(children) == 1:
            logger.info("WikiIndex: %s splits into 1 child, no sharding needed", page_id)
            return []

        # Determine heading text for the first section (keeps as parent summary)
        child_ids: List[str] = []

        for idx, child_sections in enumerate(children):
            combined_body = "".join(child_sections)

            # Extract first ## heading as the child title suffix
            heading_match = re.search(r'^##\s+(.+?)(?:\n|$)', combined_body, re.MULTILINE)
            if heading_match:
                child_title = f"{title} - {heading_match.group(1).strip()}"
            else:
                child_title = f"{title} - Part {idx + 1}"

            child_id = await self._store.upsert_page(
                title=child_title,
                body=combined_body,
                status=status,
                tags=tags,
                source=source,
            )
            child_ids.append(child_id)

        # Update parent: keep first section only + links to children
        first_section = children[0]
        parent_body = "".join(first_section)
        links_text = "\n\nSee: " + ", ".join(f"[[{cid}]]" for cid in child_ids)
        parent_body = parent_body + links_text

        await self._store.upsert_page(
            title=title,
            body=parent_body,
            status=status,
            tags=tags,
            source=source,
        )

        logger.info("WikiIndex: %s sharded into %d children: %s", page_id, len(child_ids), child_ids)
        return child_ids

    # ── Directory sharding ──────────────────────────────────────────

    async def directory_shard(self) -> dict:
        """Check if the wiki directory needs sharding by count.

        Returns suggestion with shard_needed flag and rationale.
        """
        count = await self._store.page_count()

        if count > DIR_SHARD_THRESHOLD:
            return {
                "shard_needed": True,
                "page_count": count,
                "suggestion": (
                    f"Wiki has {count} pages (threshold: {DIR_SHARD_THRESHOLD}). "
                    "Consider sharding by first-letter (e.g., pages/a/, pages/b/) "
                    "or by tag prefix (e.g., pages/concepts/, pages/tools/) "
                    "to improve filesystem lookup performance."
                ),
            }

        return {
            "shard_needed": False,
            "page_count": count,
            "suggestion": f"Wiki has {count} pages — under threshold of {DIR_SHARD_THRESHOLD}.",
        }
