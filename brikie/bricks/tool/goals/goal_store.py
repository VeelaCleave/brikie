"""Goal Store — SQLite-backed persistence for long-running goals.

The durable spine of the goal system: goals, their linked subtasks, and
an append-only progress log, all persisted so an agent can pause, crash,
or end a session and resume the same goal later without losing state.

Mirrors the LCM store's design — a ``VersionedConnectionPool`` subclass
with its own ``goals.db`` and ``schema.sql`` — so the data layer is
consistent across bricks.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Optional

from brikie.bricks.memory.sqlite_pool import VersionedConnectionPool

_ACTIVE_GOAL_KEY = "__active__"


class GoalConnectionPool(VersionedConnectionPool):
    """SQLite connection pool for the goal store."""

    SCHEMA_VERSION = 1
    MIGRATIONS: dict = {}
    DB_FILENAME = "goals.db"

    def _get_schema_path(self) -> Path:
        return Path(__file__).resolve().parent / "schema.sql"


class GoalStore:
    """Async data store for goals, subtasks, and progress events."""

    def __init__(self, db_path: str = "goals.db") -> None:
        self._pool = GoalConnectionPool(db_path)

    async def initialize(self) -> None:
        await self._pool.initialize()

    async def shutdown(self) -> None:
        await self._pool.shutdown()

    # ------------------------------------------------------------------
    # Goals
    # ------------------------------------------------------------------

    async def create_goal(self, title: str, detail: str = "") -> str:
        """Create a goal (active) and return its id."""
        goal_id = str(uuid.uuid4())
        await self._pool._execute(
            "INSERT INTO goals (id, title, detail) VALUES (?, ?, ?)",
            (goal_id, title, detail),
        )
        await self._log(goal_id, "created", title)
        return goal_id

    async def set_status(self, goal_id: str, status: str) -> bool:
        """Set a goal's status; returns False if the goal is unknown."""
        if not await self._goal_exists(goal_id):
            return False
        await self._pool._execute(
            "UPDATE goals SET status = ?, "
            "updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc') "
            "WHERE id = ?",
            (status, goal_id),
        )
        await self._log(goal_id, "status", status)
        return True

    async def get_goal(self, goal_id: str) -> Optional[dict]:
        row = await self._pool._execute(
            "SELECT id, title, detail, status, created_at, updated_at "
            "FROM goals WHERE id = ?",
            (goal_id,),
            fetch="one",
        )
        return self._goal_row(row) if row else None

    async def list_goals(self, status: Optional[str] = None) -> list[dict]:
        if status:
            rows = await self._pool._execute(
                "SELECT id, title, detail, status, created_at, updated_at "
                "FROM goals WHERE status = ? ORDER BY created_at DESC",
                (status,),
                fetch="all",
            )
        else:
            rows = await self._pool._execute(
                "SELECT id, title, detail, status, created_at, updated_at "
                "FROM goals ORDER BY created_at DESC",
                (),
                fetch="all",
            )
        return [self._goal_row(r) for r in (rows or [])]

    async def latest_active_goal(self) -> Optional[dict]:
        """The most recently updated active goal — what a resume picks up."""
        row = await self._pool._execute(
            "SELECT id, title, detail, status, created_at, updated_at "
            "FROM goals WHERE status = 'active' ORDER BY updated_at DESC LIMIT 1",
            (),
            fetch="one",
        )
        return self._goal_row(row) if row else None

    # ------------------------------------------------------------------
    # Subtasks
    # ------------------------------------------------------------------

    async def add_subtask(self, goal_id: str, title: str) -> Optional[str]:
        """Append a subtask to a goal; returns its id, or None if no goal."""
        if not await self._goal_exists(goal_id):
            return None
        row = await self._pool._execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM subtasks WHERE goal_id = ?",
            (goal_id,),
            fetch="one",
        )
        position = row[0] if row else 0
        subtask_id = str(uuid.uuid4())
        await self._pool._execute(
            "INSERT INTO subtasks (id, goal_id, position, title) "
            "VALUES (?, ?, ?, ?)",
            (subtask_id, goal_id, position, title),
        )
        await self._touch_goal(goal_id)
        await self._log(goal_id, "subtask_added", title)
        return subtask_id

    async def set_subtask_status(
        self, subtask_id: str, status: str, note: str = ""
    ) -> Optional[str]:
        """Update a subtask's status; returns its goal_id, or None if unknown."""
        row = await self._pool._execute(
            "SELECT goal_id, title FROM subtasks WHERE id = ?",
            (subtask_id,),
            fetch="one",
        )
        if not row:
            return None
        goal_id, title = row[0], row[1]
        await self._pool._execute(
            "UPDATE subtasks SET status = ?, note = ?, "
            "updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc') "
            "WHERE id = ?",
            (status, note, subtask_id),
        )
        await self._touch_goal(goal_id)
        await self._log(goal_id, f"subtask_{status}", title)
        return goal_id

    async def list_subtasks(self, goal_id: str) -> list[dict]:
        rows = await self._pool._execute(
            "SELECT id, position, title, status, note "
            "FROM subtasks WHERE goal_id = ? ORDER BY position ASC",
            (goal_id,),
            fetch="all",
        )
        return [
            {"id": r[0], "position": r[1], "title": r[2],
             "status": r[3], "note": r[4]}
            for r in (rows or [])
        ]

    async def log_event(self, goal_id: str, kind: str, detail: str) -> None:
        """Append an external progress event to a goal's log and touch it.

        Used by collaborators (e.g. the Swarm brick) to record delegated
        work against the active goal without owning a subtask.
        """
        await self._log(goal_id, kind, detail)
        await self._touch_goal(goal_id)

    async def recent_events(self, goal_id: str, limit: int = 20) -> list[dict]:
        rows = await self._pool._execute(
            "SELECT kind, detail, created_at FROM goal_events "
            "WHERE goal_id = ? ORDER BY created_at DESC LIMIT ?",
            (goal_id, limit),
            fetch="all",
        )
        return [
            {"kind": r[0], "detail": r[1], "at": r[2]}
            for r in (rows or [])
        ]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _goal_row(row: Any) -> dict:
        return {
            "id": row[0], "title": row[1], "detail": row[2],
            "status": row[3], "created_at": row[4], "updated_at": row[5],
        }

    async def _goal_exists(self, goal_id: str) -> bool:
        row = await self._pool._execute(
            "SELECT 1 FROM goals WHERE id = ?", (goal_id,), fetch="one"
        )
        return row is not None

    async def _touch_goal(self, goal_id: str) -> None:
        await self._pool._execute(
            "UPDATE goals SET updated_at = "
            "strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc') WHERE id = ?",
            (goal_id,),
        )

    async def _log(self, goal_id: str, kind: str, detail: str) -> None:
        await self._pool._execute(
            "INSERT INTO goal_events (id, goal_id, kind, detail) "
            "VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), goal_id, kind, detail[:500]),
        )
