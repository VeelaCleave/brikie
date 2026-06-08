"""Wiki Linter — structural integrity checker for the LLM Wiki Brick.

Scans the wiki for five violation types:
- frontmatter: missing YAML frontmatter fields on disk
- orphan: pages with zero inbound or zero outbound wiki-links
- broken_link: wiki-links pointing to non-existent pages
- cap: line count exceeding soft (400) or hard (800) caps
- stale: pages not updated within 30 days (excluding archived)
"""

import re
import yaml
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List

from brikie.bricks.memory.wiki.wiki_store import WikiStore


@dataclass
class LintViolation:
    """A single structural violation found during linting."""
    page_id: str
    check: str
    detail: str


class WikiLinter:
    """Runs structural checks against a WikiStore instance."""

    REQUIRED_FIELDMATTER_FIELDS = ["title", "created", "updated", "status", "tags"]

    _LINK_RE = re.compile(r"\[\[(.+?)\]\]")

    def __init__(self, store: WikiStore) -> None:
        self._store = store

    async def lint(self, check: str = "all") -> List[LintViolation]:
        """Run one or all linter sub-checks and return violations."""
        pages = await self._collect_pages()

        if check == "all":
            return (
                await self._check_frontmatter(pages)
                + await self._check_orphans(pages)
                + await self._check_broken_links(pages)
                + await self._check_caps(pages)
                + await self._check_stale(pages)
            )
        elif check == "frontmatter":
            return await self._check_frontmatter(pages)
        elif check == "orphan":
            return await self._check_orphans(pages)
        elif check == "broken_link":
            return await self._check_broken_links(pages)
        elif check == "cap":
            return await self._check_caps(pages)
        elif check == "stale":
            return await self._check_stale(pages)
        else:
            raise ValueError(f"Unknown check: {check}")

    async def _collect_pages(self) -> list[dict]:
        """Collect all pages from the store into a list."""
        result = []
        async for page in self._store.iter_pages():
            result.append(page)
        return result

    # ── Frontmatter check ──────────────────────────────────────────

    async def _check_frontmatter(self, pages: list[dict]) -> List[LintViolation]:
        """Verify required YAML frontmatter fields on each page's markdown file."""
        violations: List[LintViolation] = []
        for page in pages:
            missing = self._parse_missing_frontmatter(page)
            for field in missing:
                violations.append(LintViolation(
                    page_id=page["id"],
                    check="frontmatter",
                    detail=f"Missing frontmatter field: {field}",
                ))
        return violations

    def _parse_missing_frontmatter(self, page: dict) -> list[str]:
        """Read the markdown file and return the list of missing frontmatter fields."""
        filepath = Path(page["path"])
        try:
            raw = filepath.read_text(encoding="utf-8")
        except FileNotFoundError:
            return list(self.REQUIRED_FIELDMATTER_FIELDS)

        parts = raw.split("---", 2)
        if len(parts) < 3:
            return list(self.REQUIRED_FIELDMATTER_FIELDS)

        yaml_text = parts[1]
        try:
            fm = yaml.safe_load(yaml_text)
        except yaml.YAMLError:
            return list(self.REQUIRED_FIELDMATTER_FIELDS)

        if not isinstance(fm, dict):
            return list(self.REQUIRED_FIELDMATTER_FIELDS)

        return [f for f in self.REQUIRED_FIELDMATTER_FIELDS if f not in fm]

    # ── Orphan check ────────────────────────────────────────────────

    async def _check_orphans(self, pages: list[dict]) -> List[LintViolation]:
        """Detect pages with zero inbound links or zero outbound links."""
        violations: List[LintViolation] = []
        all_page_ids = {p["id"] for p in pages}

        # Build inbound/outbound maps
        inbound_count: dict[str, int] = {pid: 0 for pid in all_page_ids}
        outbound_has_any: dict[str, bool] = {pid: False for pid in all_page_ids}

        for page in pages:
            body = page.get("body", "")
            links = self._LINK_RE.findall(body)
            if links:
                outbound_has_any[page["id"]] = True
                for target in links:
                    slug = self._slugify(target.strip())
                    if slug in inbound_count:
                        inbound_count[slug] += 1

        for page in pages:
            pid = page["id"]
            if inbound_count[pid] == 0:
                violations.append(LintViolation(
                    page_id=pid,
                    check="orphan",
                    detail="Zero inbound wiki-links (no other page links to this page)",
                ))
            if not outbound_has_any[pid]:
                violations.append(LintViolation(
                    page_id=pid,
                    check="orphan",
                    detail="Zero outbound wiki-links (this page links to no other page)",
                ))

        return violations

    # ── Broken link check ──────────────────────────────────────────

    async def _check_broken_links(self, pages: list[dict]) -> List[LintViolation]:
        """Find wiki-links that point to non-existent pages."""
        violations: List[LintViolation] = []
        all_page_ids = {p["id"] for p in pages}

        for page in pages:
            body = page.get("body", "")
            links = self._LINK_RE.findall(body)
            for target in links:
                slug = self._slugify(target.strip())
                if slug not in all_page_ids:
                    violations.append(LintViolation(
                        page_id=page["id"],
                        check="broken_link",
                        detail=f"Link [[{target}]] → {slug} not found",
                    ))

        return violations

    # ── Cap check ───────────────────────────────────────────────────

    async def _check_caps(self, pages: list[dict]) -> List[LintViolation]:
        """Check line count against soft (400) and hard (800) caps."""
        violations: List[LintViolation] = []
        for page in pages:
            lc = page.get("line_count", 0)
            if lc > 800:
                violations.append(LintViolation(
                    page_id=page["id"],
                    check="cap",
                    detail=f"Hard cap exceeded: {lc} lines (max 800)",
                ))
            elif lc > 400:
                violations.append(LintViolation(
                    page_id=page["id"],
                    check="cap",
                    detail=f"Soft cap exceeded: {lc} lines (max 400)",
                ))

        return violations

    # ── Stale check ────────────────────────────────────────────────

    async def _check_stale(self, pages: list[dict]) -> List[LintViolation]:
        """Find pages not updated in 30 days (excluding archived)."""
        violations: List[LintViolation] = []
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=30)

        for page in pages:
            if page["status"] == "archived":
                continue

            updated_str = page.get("updated_at", "")
            try:
                updated_dt = datetime.strptime(updated_str, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                updated_dt = datetime.strptime(updated_str.split(".")[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)

            if updated_dt < cutoff:
                violations.append(LintViolation(
                    page_id=page["id"],
                    check="stale",
                    detail=f"Not updated since {updated_dt.strftime('%Y-%m-%d %H:%M:%S')}",
                ))

        return violations

    # ── Utility ─────────────────────────────────────────────────────

    @staticmethod
    def _slugify(target: str) -> str:
        """Generate a slug from a page title, matching WikiStore._slugify."""
        return re.sub(r"[^a-z0-9-]", "", target.lower().replace(" ", "-"))
