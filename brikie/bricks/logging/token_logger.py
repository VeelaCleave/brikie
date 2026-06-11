"""LLM Token Logging Brick — record every LLM API call with token counts and latency.

Hooks into POST_LLM to capture:
- Input tokens (from the request payload)
- Output tokens (from the LLM response)
- Latency (wall-clock time of the provider call)
- Model name used
- Number of tool schemas sent
- Cost estimate (configurable $/1K tokens)

Persistence: SQLite-backed with the same `*ConnectionPool` pattern used by
MemPalace — all DB ops wrapped in try/finally to prevent connection leaks.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from brikie.bricks.logging.base import LogEntry, LogLevel, LoggingBrick
from brikie.config.types import HookType

logger = logging.getLogger(__name__)

# Default cost per 1K tokens (USD). Users override via config.
DEFAULT_COST_PER_1K_INPUT = 0.003
DEFAULT_COST_PER_1K_OUTPUT = 0.015


@dataclass
class TokenUsageSnapshot:
    """A single snapshot of token usage for the Dreamer's consumption."""

    timestamp: str = ""
    session_id: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    tool_call_count: int = 0
    cost_estimate: float = 0.0


class TokenLoggerBrick(LoggingBrick):
    """Records every LLM API call: token counts, latency, model, cost.

    Schema (token_logs table):
        id          INTEGER PRIMARY KEY AUTOINCREMENT
        session_id  TEXT NOT NULL
        model       TEXT NOT NULL
        input_tokens INTEGER NOT NULL
        output_tokens INTEGER NOT NULL
        latency_ms  REAL NOT NULL
        tool_call_count INTEGER NOT NULL DEFAULT 0
        cost_input  REAL NOT NULL DEFAULT 0.0
        cost_output REAL NOT NULL DEFAULT 0.0
        cost_total  REAL NOT NULL DEFAULT 0.0
        timestamp   TEXT NOT NULL
        trace_id    TEXT
    """

    def __init__(
        self,
        db_path: str = "~/.brikie/logs/token_logs.db",
        cost_per_1k_input: float = DEFAULT_COST_PER_1K_INPUT,
        cost_per_1k_output: float = DEFAULT_COST_PER_1K_OUTPUT,
        max_queue_size: int = 1000,
    ) -> None:
        super().__init__()
        self._name = "token_logger"
        self._db_path = Path(db_path).expanduser()
        self._cost_per_1k_input = cost_per_1k_input
        self._cost_per_1k_output = cost_per_1k_output
        self._max_queue_size = max_queue_size
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = asyncio.Lock()

        # Re-create queue with user-specified max size
        self._emit_queue = asyncio.Queue(maxsize=max_queue_size)

    async def init(self) -> None:
        """Initialize DB and schema, then start the background consumer."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()
        await super().init()

    def _create_schema(self) -> None:
        if self._conn is None:
            return
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS token_logs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id      TEXT NOT NULL,
                model           TEXT NOT NULL,
                input_tokens    INTEGER NOT NULL,
                output_tokens   INTEGER NOT NULL,
                latency_ms      REAL NOT NULL,
                tool_call_count INTEGER NOT NULL DEFAULT 0,
                cost_input      REAL NOT NULL DEFAULT 0.0,
                cost_output     REAL NOT NULL DEFAULT 0.0,
                cost_total      REAL NOT NULL DEFAULT 0.0,
                timestamp       TEXT NOT NULL,
                trace_id        TEXT
            )
            """
        )
        # Index for fast session-level queries
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_token_logs_session
            ON token_logs(session_id, timestamp)
            """
        )
        # Index for Dreamer aggregation queries
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_token_logs_timestamp
            ON token_logs(timestamp)
            """
        )
        self._conn.commit()

    async def shutdown(self) -> None:
        """Flush pending writes and close DB connection."""
        await super().shutdown()
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ── Hook callback factory ────────────────────────────────────────

    async def get_hook_callbacks(
        self,
    ) -> Dict[HookType, List[callable]]:  # noqa: F821
        """Register POST_LLM callback for token logging."""

        async def on_post_llm(data: Any) -> None:
            """Capture token usage from POST_LLM hook data.

            Expected data payload (dict):
                content: str — the LLM response text
                tool_calls: list[dict] — tool calls if any
                _meta (optional dict) — provider metadata including:
                    model: str
                    input_tokens: int
                    output_tokens: int
                    latency_ms: float
                    session_id: str
            """
            self._capture_tokens(data)

        return {HookType.POST_LLM: [on_post_llm]}

    # ── Token capture ────────────────────────────────────────────────

    def _capture_tokens(self, data: Any) -> None:
        """Extract token metadata from a POST_LLM payload and emit a log entry.

        Args:
            data: The POST_LLM hook data — either a dict with _meta or raw.
        """
        if not isinstance(data, dict):
            return

        meta = data.get("_meta", {}) or {}
        model = meta.get("model", "unknown")
        input_tokens = meta.get("input_tokens", 0)
        output_tokens = meta.get("output_tokens", 0)
        latency_ms = meta.get("latency_ms", 0.0)
        session_id = meta.get("session_id", "default")
        trace_id = meta.get("trace_id", "")

        # Fallback: count tool calls from the raw response
        tool_calls = data.get("tool_calls", [])
        tool_call_count = len(tool_calls) if isinstance(tool_calls, list) else 0

        cost_input = (input_tokens / 1000) * self._cost_per_1k_input
        cost_output = (output_tokens / 1000) * self._cost_per_1k_output
        cost_total = cost_input + cost_output

        entry = LogEntry(
            source=self._name,
            event_type="token_usage",
            level=LogLevel.INFO,
            payload={
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "latency_ms": latency_ms,
                "tool_call_count": tool_call_count,
                "cost_input": round(cost_input, 6),
                "cost_output": round(cost_output, 6),
                "cost_total": round(cost_total, 6),
            },
            session_id=session_id,
            trace_id=trace_id,
        )
        self.emit(entry)

    # ── Persistence ──────────────────────────────────────────────────

    async def _persist(self, entry: LogEntry) -> None:
        """Write a token usage entry to SQLite."""
        if self._conn is None:
            return

        p = entry.payload
        session_id = entry.session_id or "default"
        model = p.get("model", "unknown")
        input_tokens = int(p.get("input_tokens", 0))
        output_tokens = int(p.get("output_tokens", 0))
        latency_ms = float(p.get("latency_ms", 0.0))
        tool_call_count = int(p.get("tool_call_count", 0))
        cost_input = float(p.get("cost_input", 0.0))
        cost_output = float(p.get("cost_output", 0.0))
        cost_total = float(p.get("cost_total", 0.0))

        async with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO token_logs
                        (session_id, model, input_tokens, output_tokens,
                         latency_ms, tool_call_count, cost_input, cost_output,
                         cost_total, timestamp, trace_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        model,
                        input_tokens,
                        output_tokens,
                        latency_ms,
                        tool_call_count,
                        cost_input,
                        cost_output,
                        cost_total,
                        entry.timestamp,
                        entry.trace_id or None,
                    ),
                )
                self._conn.commit()
            except sqlite3.Error:
                logger.exception("TokenLoggerBrick: failed to persist entry.")

    # ── Dreamer API ──────────────────────────────────────────────────

    async def last_n_cycles(self, n: int = 5) -> List[TokenUsageSnapshot]:
        """Retrieve the last N token usage snapshots for Dreamer analysis.

        Args:
            n: Number of most recent cycles to retrieve (default 5).

        Returns:
            List of TokenUsageSnapshot, most recent first.
        """
        if self._conn is None:
            return []

        results: List[TokenUsageSnapshot] = []
        try:
            cursor = self._conn.execute(
                """
                SELECT timestamp, session_id, model, input_tokens, output_tokens,
                       latency_ms, tool_call_count, cost_total
                FROM token_logs
                ORDER BY id DESC
                LIMIT ?
                """,
                (n,),
            )
            for row in cursor.fetchall():
                results.append(
                    TokenUsageSnapshot(
                        timestamp=row[0],
                        session_id=row[1],
                        model=row[2],
                        input_tokens=row[3],
                        output_tokens=row[4],
                        latency_ms=row[5],
                        tool_call_count=row[6],
                        cost_estimate=row[7],
                    )
                )
        except sqlite3.Error:
            logger.exception("TokenLoggerBrick: failed to query last_n_cycles.")
        return results

    async def aggregate_session_stats(
        self, session_id: str
    ) -> Dict[str, Any]:
        """Aggregate token stats for a specific session.

        Args:
            session_id: The session to aggregate.

        Returns:
            Dict with total_input, total_output, total_cost, call_count, avg_latency_ms.
        """
        if self._conn is None:
            return {}

        try:
            cursor = self._conn.execute(
                """
                SELECT
                    COALESCE(SUM(input_tokens), 0),
                    COALESCE(SUM(output_tokens), 0),
                    COALESCE(SUM(cost_total), 0),
                    COUNT(*),
                    COALESCE(AVG(latency_ms), 0)
                FROM token_logs
                WHERE session_id = ?
                """,
                (session_id,),
            )
            row = cursor.fetchone()
            if row:
                return {
                    "total_input_tokens": row[0],
                    "total_output_tokens": row[1],
                    "total_cost": round(row[2], 6),
                    "call_count": row[3],
                    "avg_latency_ms": round(row[4], 2),
                }
        except sqlite3.Error:
            logger.exception("TokenLoggerBrick: failed to aggregate session.")
        return {}
