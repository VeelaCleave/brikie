"""Shared SQLite connection pool with schema version tracking and migration.

All three memory stores (LCM, MemPalace, Wiki) use this as a base class
to ensure consistent connection management, WAL mode, and safe migrations.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List

import aiosqlite

logger = logging.getLogger(__name__)

MigrationFn = Callable[[aiosqlite.Connection], Any]


class MigrationError(Exception):
    """Raised when a schema migration fails."""


class SchemaIsolationError(Exception):
    """Raised when a pool's schema is applied to the wrong database file."""


class VersionedConnectionPool:
    """SQLite connection pool with schema version tracking.

    Each subclass provides a SCHEMA_VERSION (int) and a MIGRATIONS dict
    mapping source version -> migration function. On initialize(), the pool
    checks the current DB version and runs any pending migrations.

    Usage:
        class MyPool(VersionedConnectionPool):
            SCHEMA_VERSION = 2
            MIGRATIONS = {
                1: _migrate_v1_to_v2,
            }
    """

    # Registry of all known pool DB filenames, populated by subclasses.
    # Used by the schema isolation guard to prevent cross-contamination.
    _KNOWN_DB_FILENAMES: dict[str, str] = {}  # filename -> pool class name

    SCHEMA_VERSION: int = 1
    MIGRATIONS: Dict[int, MigrationFn] = {}
    DB_FILENAME: str = "store.db"

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Auto-register each subclass's DB_FILENAME in the class-level registry."""
        super().__init_subclass__(**kwargs)
        fn = cls.DB_FILENAME
        if fn != "store.db" and fn not in cls._KNOWN_DB_FILENAMES:
            cls._KNOWN_DB_FILENAMES[fn] = f"{cls.__module__}.{cls.__qualname__}"

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._initialized = False

    def _get_schema_path(self) -> Path:
        module_dir = Path(__file__).resolve().parent
        # Subclasses may override to point to their own schema.sql
        return module_dir / "schema.sql"

    async def initialize(self) -> None:
        """Create the database, apply schema, and run pending migrations."""
        # Schema isolation guard: refuse to write this pool's schema into a
        # database file that belongs to a different pool.
        # This catches bugs like LcmStore("mempalace.db") which would write
        # dag_nodes into the MemPalace database.
        db_filename = Path(self._db_path).name
        for known_fn, known_pool in self._KNOWN_DB_FILENAMES.items():
            if db_filename == known_fn and known_fn != self.DB_FILENAME:
                raise SchemaIsolationError(
                    f"Refusing to write {type(self).__name__} schema into "
                    f"'{db_filename}' (owned by {known_pool}). "
                    f"This would corrupt the database."
                )

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

                current_version = await self._get_schema_version(conn)
                logger.info(
                    "DB %s: schema version %d (target %d)",
                    self._db_path, current_version, self.SCHEMA_VERSION,
                )
                await self._run_migrations(conn, current_version)

                await conn.commit()
                self._initialized = True
            except Exception:
                await conn.rollback()
                raise
        except Exception as exc:
            logger.error("Pool init failed for %s: %s", self._db_path, exc)
            raise
        finally:
            if conn is not None:
                await conn.close()

    async def _get_schema_version(self, conn: aiosqlite.Connection) -> int:
        """Read the current schema version from the DB."""
        try:
            # Order by version, not applied_at: the highest applied version
            # IS the current schema. applied_at ties at millisecond
            # resolution (detect+migrate land in the same ms), and a tie there
            # would non-deterministically read an OLD version and re-run a
            # migration — e.g. ALTER ADD COLUMN → "duplicate column".
            row = await conn.execute_fetchall(
                "SELECT version FROM _schema_version ORDER BY version DESC LIMIT 1"
            )
            if row:
                return row[0][0]
        except Exception:
            pass
        # Create the tracking table if missing
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS _schema_version ("
            "  version INTEGER NOT NULL,"
            "  applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc'))"
            ")"
        )
        # No version recorded — assume base schema
        await conn.execute(
            "INSERT INTO _schema_version (version) VALUES (1)"
        )
        return 1

    async def _run_migrations(self, conn: aiosqlite.Connection, from_version: int) -> None:
        """Run all pending migrations in order."""
        if from_version >= self.SCHEMA_VERSION:
            return

        for version in sorted(self.MIGRATIONS.keys()):
            if version < from_version:
                continue
            if version >= self.SCHEMA_VERSION:
                break

            target_version = version + 1
            fn = self.MIGRATIONS.get(version)
            if fn is None:
                raise MigrationError(
                    f"No migration from version {version} to {target_version}"
                )

            logger.info("Running migration %d -> %d on %s", version, target_version, self._db_path)
            try:
                await fn(conn)
                await conn.execute(
                    "INSERT INTO _schema_version (version) VALUES (?)",
                    (target_version,),
                )
            except Exception as exc:
                raise MigrationError(
                    f"Migration {version} -> {target_version} failed: {exc}"
                ) from exc

    async def shutdown(self) -> None:
        self._initialized = False
        logger.info("Pool shutdown: %s", self._db_path)

    async def _execute(self, query: str, params: tuple = (), fetch: str = "one") -> Any:
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

    async def _execute_many(self, query: str, params_list: List[tuple]) -> None:
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
