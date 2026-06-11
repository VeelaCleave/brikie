"""LCM Store — SQLite-backed immutable message store.

Implements the core data layer for the Lossless Context Management Brick.
Provides:
- Immutable, append-only message store
- Session management
- DAG node creation and hierarchical compaction
- Context window building (summaries + fresh tail)
- Deterministic retrieval (expand, grep)
- Token budget tracking

All database operations are wrapped in strict try/finally blocks to prevent
connection leaks in long-running AFK loops.

DESIGN DECISIONS:

1. Append-Only Store: Messages are never deleted. The `is_compacted` flag
   marks messages folded into DAG summaries for context window building.

2. Connection Pool: Uses aiosqlite for async I/O with a simple pool of
   connections. Each connection is opened/closed per transaction.

3. WAL Mode: SQLite is opened in WAL (Write-Ahead Log) mode for concurrent
   reads/writes, critical for background compaction workers.

4. Token Counting: Uses a simple heuristic (len(content) / 4) as a default.
   The provider's tokenizer can override this for precision.
"""

import logging
import uuid
from pathlib import Path

from brikie.bricks.memory.sqlite_pool import VersionedConnectionPool

logger = logging.getLogger(__name__)


class LcmConnectionPool(VersionedConnectionPool):
    """Manages SQLite connections for the LCM store."""

    SCHEMA_VERSION = 1
    MIGRATIONS = {}
    DB_FILENAME = "lcm.db"

    def __init__(self, db_path: str) -> None:
        super().__init__(db_path)

    def _get_schema_path(self) -> Path:
        module_dir = Path(__file__).resolve().parent
        return module_dir / "schema.sql"


class LcmStore:
    """Core LCM data store with immutable message storage and DAG compaction."""

    DEFAULT_MAX_CONTEXT_TOKENS = 4096
    DEFAULT_TAIL_LENGTH = 5
    DEFAULT_TRIGGER_THRESHOLD = 3200

    def __init__(self, db_path: str = "lcm.db") -> None:
        self._pool = LcmConnectionPool(db_path)
        self._db_path = db_path

    async def initialize(self) -> None:
        await self._pool.initialize()

    async def shutdown(self) -> None:
        await self._pool.shutdown()

    @staticmethod
    def estimate_tokens(content: str) -> int:
        return max(1, len(content) // 4)

    async def create_session(
        self,
        name: str = "default",
        soul_id: str = "default",
        max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
        tail_length: int = DEFAULT_TAIL_LENGTH,
    ) -> str:
        session_id = str(uuid.uuid4())
        await self._pool._insert(
            "INSERT INTO sessions (id, name, soul_id, max_context_tokens, tail_length) VALUES (?, ?, ?, ?, ?)",
            (session_id, name, soul_id, max_context_tokens, tail_length),
        )
        await self._pool._insert(
            "INSERT INTO token_budgets (id, session_id, max_budget, trigger_threshold) VALUES (?, ?, ?, ?)",
            (session_id, session_id, max_context_tokens, max_context_tokens * 0.8),
        )
        return session_id

    async def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_call_id = None,
    ) -> int:
        result = await self._pool._execute(
            'SELECT COALESCE(MAX("index"), -1) + 1 FROM messages WHERE session_id = ?',
            (session_id,),
            fetch="value",
        )
        next_index = result if result is not None else 0
        msg_id = str(uuid.uuid4())
        token_count = self.estimate_tokens(content)
        await self._pool._insert(
            'INSERT INTO messages (id, session_id, "index", role, content, tool_call_id, token_count) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (msg_id, session_id, next_index, role, content, tool_call_id, token_count),
        )
        await self._pool._execute(
            'UPDATE sessions SET total_messages = total_messages + 1, total_tokens = total_tokens + ?, updated_at = strftime("%Y-%m-%d %H:%M:%f", "now", "utc") WHERE id = ?',
            (token_count, session_id),
        )
        return next_index

    async def should_compact(self, session_id: str) -> bool:
        result = await self._pool._execute(
            """
            SELECT (
                SELECT COALESCE(SUM(token_count), 0) FROM messages
                WHERE session_id = ? AND is_compacted = 0
            ) - (
                SELECT COALESCE(SUM(token_count), 0) FROM messages
                WHERE session_id = ? AND is_compacted = 0
                ORDER BY "index" DESC LIMIT 5
            ) AS compactable_tokens,
            (SELECT trigger_threshold FROM token_budgets WHERE session_id = ?) AS threshold
            """,
            (session_id, session_id, session_id),
            fetch="one",
        )
        if result is None:
            return False
        return result[0] > result[1]

    async def get_compactable_range(self, session_id: str) -> dict:
        result = await self._pool._execute(
            'SELECT MIN("index"), MAX("index"), COUNT(*), SUM(token_count) FROM messages WHERE session_id = ? AND is_compacted = 0',
            (session_id,),
            fetch="one",
        )
        if result is None:
            return {"start_index": 0, "end_index": 0, "count": 0, "total_tokens": 0}
        tail_count = 5
        total_count = result[2]
        compactable_end = result[1] - tail_count if total_count > tail_count else result[1]
        return {
            "start_index": result[0],
            "end_index": compactable_end,
            "count": total_count - tail_count,
            "total_tokens": result[3],
        }

    async def create_dag_node(
        self,
        session_id: str,
        start_index: int,
        end_index: int,
        content: str,
        depth: int = 0,
        parent_id = None,
    ) -> str:
        node_id = str(uuid.uuid4())
        token_count = self.estimate_tokens(content)
        await self._pool._insert(
            "INSERT INTO dag_nodes (id, session_id, depth, start_index, end_index, content, token_count, parent_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (node_id, session_id, depth, start_index, end_index, content, token_count, parent_id),
        )
        await self._pool._execute(
            'UPDATE messages SET is_compacted = 1, compacted_at = strftime(\'%Y-%m-%d %H:%M:%f\', \'now\', \'utc\') WHERE session_id = ? AND "index" >= ? AND "index" <= ?',
            (session_id, start_index, end_index),
        )
        await self._pool._execute(
            "UPDATE sessions SET last_compaction_index = MAX(last_compaction_index, ?) WHERE id = ?",
            (end_index, session_id),
        )
        # Update budget with summary/tail tokens.
        return node_id

    async def get_active_context(self, session_id: str) -> dict:
        summaries = await self._pool._execute(
            "SELECT id, depth, start_index, end_index, content, token_count FROM dag_nodes WHERE session_id = ? ORDER BY depth ASC",
            (session_id,),
            fetch="all",
        )
        summary_list = []
        if summaries:
            for row in summaries:
                summary_list.append({
                    "id": row[0], "depth": row[1], "start_index": row[2],
                    "end_index": row[3], "content": row[4], "token_count": row[5],
                })
        tail = await self._pool._execute(
            'SELECT id, "index", role, content, token_count FROM messages WHERE session_id = ? AND is_compacted = 0 ORDER BY "index" DESC LIMIT 5',
            (session_id,),
            fetch="all",
        )
        tail_list = []
        if tail:
            for row in tail:
                tail_list.append({
                    "id": row[0], "index": row[1], "role": row[2],
                    "content": row[3], "token_count": row[4],
                })
        return {"summaries": summary_list, "tail": tail_list, "total_tokens": 0, "budget": {}}

    async def expand(self, session_id: str, start_index: int, end_index: int) -> list:
        messages = await self._pool._execute(
            'SELECT id, "index", role, content, tool_call_id, token_count, is_compacted FROM messages WHERE session_id = ? AND "index" >= ? AND "index" <= ? ORDER BY "index" ASC',
            (session_id, start_index, end_index),
            fetch="all",
        )
        result = []
        if messages:
            for row in messages:
                result.append({
                    "id": row[0], "index": row[1], "role": row[2],
                    "content": row[3], "tool_call_id": row[4],
                    "token_count": row[5], "is_compacted": row[6],
                })
        return result

    async def grep(self, session_id: str, pattern: str, roles = None, limit: int = 20) -> list:
        query = 'SELECT id, "index", role, content, tool_call_id, token_count FROM messages WHERE session_id = ? AND content LIKE ?'
        params: list = [session_id, f"%{pattern}%"]
        if roles:
            placeholders = ", ".join(["?"] * len(roles))
            query += f" AND role IN ({placeholders})"
            params.extend(roles)
        query += ' ORDER BY "index" DESC LIMIT ?'
        params.append(limit)
        messages = await self._pool._execute(query, tuple(params), fetch="all")
        result = []
        if messages:
            for row in messages:
                result.append({
                    "id": row[0], "index": row[1], "role": row[2],
                    "content": row[3], "tool_call_id": row[4], "token_count": row[5],
                })
        return result

    async def get_token_budget(self, session_id: str) -> dict:
        result = await self._pool._execute(
            "SELECT active_context_tokens, summary_tokens, tail_tokens, max_budget, trigger_threshold FROM token_budgets WHERE session_id = ?",
            (session_id,),
            fetch="one",
        )
        if result is None:
            return {
                "active_context_tokens": 0, "summary_tokens": 0, "tail_tokens": 0,
                "max_budget": self.DEFAULT_MAX_CONTEXT_TOKENS,
                "trigger_threshold": self.DEFAULT_TRIGGER_THRESHOLD,
            }
        return {
            "active_context_tokens": result[0], "summary_tokens": result[1],
            "tail_tokens": result[2], "max_budget": result[3],
            "trigger_threshold": result[4],
        }

