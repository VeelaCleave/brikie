"""LLM Wiki Store — SQLite-backed wiki with filesystem markdown pages.

Implements the core data layer for the LLM Wiki Brick.
- Page CRUD with YAML frontmatter
- Wiki-link extraction and relationship tracking
- Tag management

All database operations are wrapped in strict try/finally blocks to prevent
connection leaks in long-running AFK loops.
"""

import aiosqlite
import logging
import re
import yaml
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class WikiConnectionPool:
    """Manages SQLite connections for the Wiki store."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._initialized = False

    def _get_schema_path(self) -> Path:
        module_dir = Path(__file__).resolve().parent
        return module_dir / "schema.sql"

    async def initialize(self) -> None:
        """Create the database and apply the schema."""
        schema_path = self._get_schema_path()
        conn = None
        try:
            conn = await aiosqlite.connect(self._db_path)
            try:
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA foreign_keys=ON")
                await conn.execute("PRAGMA busy_timeout=500")

                if schema_path.exists():
                    schema_sql = schema_path.read_text(encoding="utf-8")
                    await conn.executescript(schema_sql)
                await conn.commit()
                self._initialized = True
                logger.info("WikiStore: schema initialized at %s", self._db_path)
            except Exception:
                await conn.rollback()
                raise
        except Exception as exc:
            logger.error("WikiStore: initialization failed: %s", exc)
            raise
        finally:
            if conn is not None:
                await conn.close()

    async def shutdown(self) -> None:
        """Close any open connections."""
        self._initialized = False
        logger.info("WikiStore: shutdown complete")

    async def _execute(self, query: str, params: tuple, fetch: str = "one"):
        """Execute a single query and return the result."""
        conn = None
        try:
            conn = await aiosqlite.connect(self._db_path)
            try:
                cursor = await conn.execute(query, params)
                if fetch == "value":
                    row = await cursor.fetchone()
                    return row[0] if row else None
                elif fetch == "one":
                    return await cursor.fetchone()
                else:
                    return await cursor.fetchall()
            except Exception:
                await conn.rollback()
                raise
            finally:
                await conn.commit()
        finally:
            if conn is not None:
                await conn.close()

    async def _execute_many(self, query: str, params_list):
        """Execute a batch of queries."""
        conn = None
        try:
            conn = await aiosqlite.connect(self._db_path)
            try:
                await conn.executemany(query, params_list)
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        finally:
            if conn is not None:
                await conn.close()

    async def _insert(self, query: str, params: tuple) -> None:
        conn = None
        try:
            conn = await aiosqlite.connect(self._db_path)
            try:
                await conn.execute(query, params)
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        finally:
            if conn is not None:
                await conn.close()


class WikiStore:
    """Core Wiki data store for pages, links, and tags."""

    def __init__(
        self,
        db_path: str = "wiki.db",
        wiki_dir: Path | None = None,
    ) -> None:
        self._pool = WikiConnectionPool(db_path)
        self._db_path = db_path
        self._wiki_dir = wiki_dir or (Path(__file__).resolve().parent / "wiki_directory")
        self._pages_dir = self._wiki_dir / "pages"

    async def initialize(self) -> None:
        """Create the database schema and filesystem directories."""
        await self._pool.initialize()
        self._pages_dir.mkdir(parents=True, exist_ok=True)
        logger.info("WikiStore: filesystem initialized at %s", self._pages_dir)

    async def shutdown(self) -> None:
        """Close the connection pool."""
        await self._pool.shutdown()

    @staticmethod
    def _slugify(title: str) -> str:
        """Generate a slug from a page title."""
        return re.sub(r"[^a-z0-9-]", "", title.lower().replace(" ", "-"))

    def _write_markdown_file(
        self,
        page_id: str,
        title: str,
        body: str,
        status: str,
        tags: list[str],
        source: str,
        created_at: str,
        updated_at: str,
    ) -> None:
        """Write a markdown file with YAML frontmatter to the filesystem."""
        filepath = self._pages_dir / f"{page_id}.md"
        frontmatter = {
            "title": title,
            "status": status,
            "tags": tags,
            "source": source,
            "created_at": created_at,
            "updated_at": updated_at,
        }
        yaml_header = yaml.dump(frontmatter, default_flow_style=False, sort_keys=False)
        content = f"---\n{yaml_header}---\n{body}"
        filepath.write_text(content, encoding="utf-8")

    def _read_markdown_file(self, page_id: str) -> str:
        """Read the raw body content from a markdown file (stripping frontmatter)."""
        filepath = self._pages_dir / f"{page_id}.md"
        content = filepath.read_text(encoding="utf-8")
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                body = parts[2]
                if body.startswith("\n"):
                    body = body[1:]
                return body
        return content

    def _extract_wiki_links(self, body: str) -> list[str]:
        """Extract wiki-links from body text using [[page-title]] pattern."""
        return [m.strip() for m in re.findall(r"\[\[(.+?)\]\]", body)]

    def _now_utc(self) -> str:
        """Return current UTC timestamp."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")

    async def upsert_page(
        self,
        title: str,
        body: str,
        status: str = "draft",
        tags: list[str] | None = None,
        source: str = "manual",
    ) -> str:
        """Create or update a wiki page. Returns page_id (slug)."""
        if tags is None:
            tags = []

        page_id = self._slugify(title)
        now = self._now_utc()
        line_count = len(body.splitlines())

        existing = await self._pool._execute(
            "SELECT id, created_at FROM pages WHERE id = ?",
            (page_id,),
            fetch="one",
        )

        if existing:
            created_at = existing[1]
            await self._pool._execute(
                """UPDATE pages
                   SET title = ?, path = ?, status = ?, line_count = ?, source = ?, updated_at = ?
                   WHERE id = ?""",
                (title, str(self._pages_dir / f"{page_id}.md"), status, line_count, source, now, page_id),
            )
        else:
            created_at = now
            await self._pool._insert(
                """INSERT INTO pages (id, path, title, status, line_count, source, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (page_id, str(self._pages_dir / f"{page_id}.md"), title, status, line_count, source, created_at, now),
            )

        # Upsert tags: delete old, insert new
        await self._pool._execute(
            "DELETE FROM tags WHERE page_id = ?",
            (page_id,),
            fetch="none",
        )
        if tags:
            tag_params = [(page_id, tag) for tag in tags]
            await self._pool._execute_many(
                "INSERT OR REPLACE INTO tags (page_id, tag) VALUES (?, ?)",
                tag_params,
            )

        # Delete old links, insert new ones extracted from body
        await self._pool._execute(
            "DELETE FROM links WHERE source_page_id = ?",
            (page_id,),
            fetch="none",
        )
        wiki_links = self._extract_wiki_links(body)
        if wiki_links:
            link_params = [(page_id, self._slugify(link), "wiki") for link in wiki_links]
            await self._pool._execute_many(
                "INSERT OR REPLACE INTO links (source_page_id, target_page_id, link_type) VALUES (?, ?, ?)",
                link_params,
            )

        # Write markdown file to filesystem
        self._write_markdown_file(page_id, title, body, status, tags, source, created_at, now)

        return page_id

    async def get_page(self, page_id: str) -> dict | None:
        """Retrieve a single page by ID, combining DB metadata with filesystem body."""
        row = await self._pool._execute(
            "SELECT id, path, title, status, line_count, source, created_at, updated_at "
            "FROM pages WHERE id = ?",
            (page_id,),
            fetch="one",
        )
        if row is None:
            return None

        try:
            body = self._read_markdown_file(page_id)
        except FileNotFoundError:
            body = ""

        tags_rows = await self._pool._execute(
            "SELECT tag FROM tags WHERE page_id = ?",
            (page_id,),
            fetch="all",
        )
        tags = [r[0] for r in tags_rows] if tags_rows else []

        return {
            "id": row[0],
            "path": row[1],
            "title": row[2],
            "status": row[3],
            "line_count": row[4],
            "source": row[5],
            "created_at": row[6],
            "updated_at": row[7],
            "body": body,
            "tags": tags,
        }

    async def delete_page(self, page_id: str) -> bool:
        """Delete a page from DB and filesystem. Returns True if found and deleted."""
        exists = await self._pool._execute(
            "SELECT id FROM pages WHERE id = ?",
            (page_id,),
            fetch="value",
        )
        if not exists:
            return False

        await self._pool._execute(
            "DELETE FROM links WHERE source_page_id = ? OR target_page_id = ?",
            (page_id, page_id),
            fetch="none",
        )
        await self._pool._execute(
            "DELETE FROM tags WHERE page_id = ?",
            (page_id,),
            fetch="none",
        )
        await self._pool._execute(
            "DELETE FROM pages WHERE id = ?",
            (page_id,),
            fetch="none",
        )

        filepath = self._pages_dir / f"{page_id}.md"
        if filepath.exists():
            filepath.unlink()

        return True

    async def list_pages(
        self,
        status: str | None = None,
        tags: list[str] | None = None,
    ) -> list[dict]:
        """Query pages with optional status and tag filters."""
        query = "SELECT id, path, title, status, line_count, source, created_at, updated_at FROM pages WHERE 1=1"
        params: list = []

        if status:
            query += " AND status = ?"
            params.append(status)

        if tags:
            for tag in tags:
                query += " AND id IN (SELECT page_id FROM tags WHERE tag = ?)"
                params.append(tag)

        query += " ORDER BY updated_at DESC"
        rows = await self._pool._execute(query, tuple(params), fetch="all")

        result = []
        if rows:
            for row in rows:
                result.append({
                    "id": row[0],
                    "path": row[1],
                    "title": row[2],
                    "status": row[3],
                    "line_count": row[4],
                    "source": row[5],
                    "created_at": row[6],
                    "updated_at": row[7],
                })
        return result

    async def page_count(self) -> int:
        """Return total number of pages in the wiki."""
        count = await self._pool._execute(
            "SELECT COUNT(*) FROM pages", (), fetch="value"
        )
        return count or 0

    async def iter_pages(self):
        """Yield all pages as dicts with body content read from filesystem."""
        rows = await self._pool._execute(
            "SELECT id, path, title, status, line_count, source, created_at, updated_at FROM pages ORDER BY updated_at DESC",
            (),
            fetch="all",
        )
        if rows:
            for row in rows:
                page_id = row[0]
                try:
                    body = self._read_markdown_file(page_id)
                except FileNotFoundError:
                    body = ""

                tags_rows = await self._pool._execute(
                    "SELECT tag FROM tags WHERE page_id = ?",
                    (page_id,),
                    fetch="all",
                )
                tags = [r[0] for r in tags_rows] if tags_rows else []

                yield {
                    "id": row[0],
                    "path": row[1],
                    "title": row[2],
                    "status": row[3],
                    "line_count": row[4],
                    "source": row[5],
                    "created_at": row[6],
                    "updated_at": row[7],
                    "body": body,
                    "tags": tags,
                }
