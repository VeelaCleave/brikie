"""Swarm Store — SQLite-backed audit log for swarm dispatches.

A swarm run is ephemeral by nature: sub-agents spin up, do scoped work, and
their contexts are discarded. What survives — for observability and audit
(a core brikie principle) — is *what was delegated and what came back*. This
store records each run and each sub-agent's reported outcome, mirroring the
GoalStore's design so the data layer stays consistent across bricks.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, List

from brikie.bricks.memory.sqlite_pool import VersionedConnectionPool


async def _m_add_orphaned(conn) -> None:
    """v1→v2: flag runs left mid-flight by a crash (vs. cleanly finished)."""
    await conn.execute(
        "ALTER TABLE swarm_runs ADD COLUMN orphaned INTEGER NOT NULL DEFAULT 0")


class SwarmConnectionPool(VersionedConnectionPool):
    """SQLite connection pool for the swarm audit store."""

    SCHEMA_VERSION = 2
    MIGRATIONS: dict = {1: _m_add_orphaned}
    DB_FILENAME = "swarm.db"

    def _get_schema_path(self) -> Path:
        return Path(__file__).resolve().parent / "schema.sql"


class SwarmStore:
    """Async audit store for swarm runs and their sub-agent outcomes."""

    def __init__(self, db_path: str = "swarm.db") -> None:
        self._pool = SwarmConnectionPool(db_path)

    async def initialize(self) -> None:
        await self._pool.initialize()

    async def shutdown(self) -> None:
        await self._pool.shutdown()

    async def start_run(self, goal: str, task_count: int) -> str:
        """Record the start of a dispatch; returns the run id."""
        run_id = str(uuid.uuid4())
        await self._pool._execute(
            "INSERT INTO swarm_runs (id, goal, task_count) VALUES (?, ?, ?)",
            (run_id, goal[:500], task_count),
        )
        return run_id

    async def record_task(
        self, run_id: str, position: int, result: Any,
    ) -> None:
        """Persist one sub-agent's outcome (a SubAgentResult), idempotently.

        Keyed on (run_id, position) via a deterministic id and INSERT OR
        REPLACE, so it can be called incrementally as each wave completes
        (durability) and again at the end with the enriched final state
        without duplicating rows.
        """
        await self._pool._execute(
            "INSERT OR REPLACE INTO swarm_tasks "
            "(id, run_id, position, role, task, ok, report, steps, "
            " tool_calls, tools_used) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"{run_id}-{position}", run_id, position,
                result.role, result.task[:1000], 1 if result.ok else 0,
                result.report[:4000], result.steps, result.tool_calls,
                ", ".join(result.tools_used)[:500],
            ),
        )

    async def reconcile_orphans(self) -> int:
        """Mark runs still 'running' as orphaned and return the count.

        Called at startup: a swarm can't survive a process restart, so any
        run left 'running' was abandoned by a crashed/killed process. We flag
        it (status→done, orphaned=1) so it shows up honestly in swarm_status
        instead of lingering as 'running' forever.
        """
        rows = await self._pool._execute(
            "SELECT id FROM swarm_runs WHERE status = 'running'",
            (), fetch="all",
        )
        count = len(rows or [])
        if count:
            await self._pool._execute(
                "UPDATE swarm_runs SET status = 'done', orphaned = 1, "
                "summary = CASE WHEN summary = '' THEN "
                "'orphaned — interrupted before completion' ELSE summary END, "
                "finished_at = strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc') "
                "WHERE status = 'running'",
            )
        return count

    async def finish_run(self, run_id: str, ok_count: int, summary: str) -> None:
        """Mark a run done with its aggregate outcome."""
        await self._pool._execute(
            "UPDATE swarm_runs SET status = 'done', ok_count = ?, "
            "summary = ?, finished_at = strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc') "
            "WHERE id = ?",
            (ok_count, summary[:1000], run_id),
        )

    async def recent_runs(self, limit: int = 10) -> List[dict]:
        rows = await self._pool._execute(
            "SELECT id, status, goal, task_count, ok_count, summary, created_at, "
            "orphaned FROM swarm_runs ORDER BY created_at DESC LIMIT ?",
            (limit,),
            fetch="all",
        )
        return [
            {
                "run_id": r[0], "status": r[1], "goal": r[2],
                "task_count": r[3], "ok_count": r[4], "summary": r[5],
                "at": r[6], "orphaned": bool(r[7]),
            }
            for r in (rows or [])
        ]

    async def run_tasks(self, run_id: str) -> List[dict]:
        rows = await self._pool._execute(
            "SELECT role, task, ok, report, steps, tool_calls, tools_used "
            "FROM swarm_tasks WHERE run_id = ? ORDER BY position ASC",
            (run_id,),
            fetch="all",
        )
        return [
            {
                "role": r[0], "task": r[1], "ok": bool(r[2]), "report": r[3],
                "steps": r[4], "tool_calls": r[5], "tools_used": r[6],
            }
            for r in (rows or [])
        ]
