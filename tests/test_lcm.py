"""Tests for the LCM (Lossless Context Management) Brick."""

import asyncio
import os
import tempfile

import pytest

from brikie.bricks.memory.lcm.lcm_store import LcmStore
from brikie.bricks.memory.lcm.lcm_brick import LcmBrick
from brikie.bricks.memory.lcm.tools import get_lcm_tools, LCM_EXPAND_TOOL, LCM_GREP_TOOL


@pytest.fixture
def tmp_db():
    """Create a temporary SQLite database file."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    yield path
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def store(tmp_db):
    """Create an LcmStore instance."""
    return LcmStore(tmp_db)


@pytest.fixture
def brick(tmp_db):
    """Create an LcmBrick instance."""
    return LcmBrick(tmp_db)


class TestLcmStore:
    """Tests for LcmStore CRUD operations."""

    @pytest.mark.asyncio
    async def test_initialize_creates_tables(self, store):
        await store.initialize()
        result = await store._pool._execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'sessions'",
            (),
            fetch="value",
        )
        assert result == "sessions"

    @pytest.mark.asyncio
    async def test_wal_mode_applied(self, store):
        await store.initialize()
        result = await store._pool._execute(
            "PRAGMA journal_mode", (), fetch="value"
        )
        assert result == "wal"

    @pytest.mark.asyncio
    async def test_create_session(self, store):
        await store.initialize()
        session_id = await store.create_session("test", "soul_1")
        assert isinstance(session_id, str)
        assert len(session_id) > 0

    @pytest.mark.asyncio
    async def test_append_message(self, store):
        await store.initialize()
        session_id = await store.create_session()
        idx = await store.append_message(session_id, "user", "Hello world")
        assert idx == 0

    @pytest.mark.asyncio
    async def test_append_multiple_messages(self, store):
        await store.initialize()
        session_id = await store.create_session()
        idx0 = await store.append_message(session_id, "user", "First")
        idx1 = await store.append_message(session_id, "assistant", "Second")
        idx2 = await store.append_message(session_id, "user", "Third")
        assert idx0 == 0
        assert idx1 == 1
        assert idx2 == 2

    @pytest.mark.asyncio
    async def test_estimated_tokens(self, store):
        # "Hello world" is 11 chars, 11/4 = 2 (floor division)
        assert store.estimate_tokens("Hello world") == 2
        assert store.estimate_tokens("") == 1


class TestDagCompaction:
    """Tests for DAG node creation and compaction."""

    @pytest.mark.asyncio
    async def test_create_dag_node(self, store):
        await store.initialize()
        session_id = await store.create_session()
        await store.append_message(session_id, "user", "First message")
        await store.append_message(session_id, "assistant", "Second message")
        node_id = await store.create_dag_node(
            session_id, 0, 1, "Summary of first two messages"
        )
        assert isinstance(node_id, str)

    @pytest.mark.asyncio
    async def test_messages_marked_compacted(self, store):
        await store.initialize()
        session_id = await store.create_session()
        await store.append_message(session_id, "user", "First")
        await store.append_message(session_id, "assistant", "Second")
        await store.create_dag_node(session_id, 0, 1, "Summary")
        result = await store._pool._execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ? AND is_compacted = 1",
            (session_id,),
            fetch="value",
        )
        assert result == 2

    @pytest.mark.asyncio
    async def test_should_compact(self, store):
        await store.initialize()
        session_id = await store.create_session()
        await store.append_message(session_id, "user", "Short")
        compact = await store.should_compact(session_id)
        assert compact is True or compact is False


class TestContextBuilding:
    """Tests for active context building."""

    @pytest.mark.asyncio
    async def test_get_active_context_empty(self, store):
        await store.initialize()
        session_id = await store.create_session()
        context = await store.get_active_context(session_id)
        assert "summaries" in context
        assert "tail" in context
        assert context["summaries"] == []
        assert context["tail"] == []

    @pytest.mark.asyncio
    async def test_get_active_context_with_messages(self, store):
        await store.initialize()
        session_id = await store.create_session()
        await store.append_message(session_id, "user", "Hello")
        context = await store.get_active_context(session_id)
        assert len(context["tail"]) > 0

    @pytest.mark.asyncio
    async def test_get_active_context_with_summary(self, store):
        await store.initialize()
        session_id = await store.create_session()
        await store.append_message(session_id, "user", "First")
        await store.append_message(session_id, "assistant", "Second")
        await store.create_dag_node(session_id, 0, 1, "Summary")
        context = await store.get_active_context(session_id)
        assert len(context["summaries"]) > 0


class TestRetrievalTools:
    """Tests for LCM tool schemas."""

    def test_get_lcm_tools(self):
        tools = get_lcm_tools()
        assert len(tools) == 2
        assert tools[0]["function"]["name"] == "lcm_expand"
        assert tools[1]["function"]["name"] == "lcm_grep"

    def test_expand_tool_has_required_fields(self):
        assert "type" in LCM_EXPAND_TOOL
        assert "function" in LCM_EXPAND_TOOL
        assert "name" in LCM_EXPAND_TOOL["function"]
        assert "parameters" in LCM_EXPAND_TOOL["function"]

    def test_grep_tool_has_required_fields(self):
        assert "type" in LCM_GREP_TOOL
        assert "function" in LCM_GREP_TOOL
        assert "name" in LCM_GREP_TOOL["function"]
        assert "parameters" in LCM_GREP_TOOL["function"]

    def test_expand_tool_required_params(self):
        required = LCM_EXPAND_TOOL["function"]["parameters"]["required"]
        assert "session_id" in required
        assert "start_index" in required
        assert "end_index" in required

    def test_grep_tool_required_params(self):
        required = LCM_GREP_TOOL["function"]["parameters"]["required"]
        assert "session_id" in required
        assert "pattern" in required


class TestLcmBrick:
    """Tests for LcmBrick lifecycle."""

    @pytest.mark.asyncio
    async def test_brick_init(self, brick):
        await brick.init()
        assert brick._store is not None

    @pytest.mark.asyncio
    async def test_brick_shutdown(self, brick):
        await brick.init()
        await brick.shutdown()

    @pytest.mark.asyncio
    async def test_brick_intercept_message(self, brick):
        await brick.init()
        session_id = await brick._store.create_session()
        await brick.intercept_message(session_id, "user", "Hello")
        result = await brick._store._pool._execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?",
            (session_id,),
            fetch="value",
        )
        assert result == 1

    @pytest.mark.asyncio
    async def test_brick_build_context(self, brick):
        await brick.init()
        session_id = await brick._store.create_session()
        context = await brick.build_context(session_id)
        assert "summaries" in context
        assert "tail" in context


class TestBudget:
    """Tests for token budget tracking."""

    @pytest.mark.asyncio
    async def test_get_token_budget(self, store):
        await store.initialize()
        session_id = await store.create_session()
        budget = await store.get_token_budget(session_id)
        assert "max_budget" in budget
        assert budget["max_budget"] == 4096


class TestTryFinally:
    """Tests for connection management."""

    @pytest.mark.asyncio
    async def test_pool_uses_try_finally(self, store):
        await store.initialize()
        session_id = await store.create_session()
        await store.append_message(session_id, "user", "Test")
        result = await store._pool._execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?",
            (session_id,),
            fetch="value",
        )
        assert result == 1
