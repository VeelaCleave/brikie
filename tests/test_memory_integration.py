"""Integration tests for the full memory pipeline: hook dispatch → write → read.

These tests verify the end-to-end flow that was broken:
1. POST_LLM hook dispatches HookEvent → _memory_post_llm unwraps it → intercept_message
2. User messages are intercepted via _intercept_user_message
3. build_context returns data from all three memory bricks
4. _build_memory_blob normalizes all brick shapes into the prompt context

Each test uses a real EventLoop with real memory bricks (no mocks) to
catch HookEvent/dict type mismatches and other wiring bugs.
"""

import tempfile
from pathlib import Path

import pytest

from brikie.config.types import HookEvent, HookType, Message
from brikie.kernel.event_loop import EventLoop, _unwrap_hook_data
from brikie.kernel.hooks import HookDispatcher
from brikie.kernel.registry import BrickRegistry
from brikie.kernel.state import StateManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db_dir():
    """Create a temporary directory for DB files that tests can clean up."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def registry():
    return BrickRegistry()


@pytest.fixture
def state():
    return StateManager()


@pytest.fixture
def hooks():
    return HookDispatcher()


@pytest.fixture
def mempalace_brick(temp_db_dir):
    """Real MemPalace brick writing to a temp DB."""
    from brikie.bricks.memory.mempalace.mempalace_brick import MempalaceBrick

    brick = MempalaceBrick(db_path=str(temp_db_dir / "test_mempalace.db"))
    return brick


@pytest.fixture
def wiki_brick(temp_db_dir):
    """Real Wiki brick writing to a temp DB with its own pages dir."""
    from brikie.bricks.memory.wiki.wiki_brick import WikiBrick

    brick = WikiBrick(db_path=str(temp_db_dir / "test_wiki.db"))
    # Point wiki directory to a temp subdirectory
    brick._store._wiki_dir = temp_db_dir / "wiki_pages"
    brick._store._pages_dir = brick._store._wiki_dir / "pages"
    return brick


@pytest.fixture
def lcm_brick(temp_db_dir):
    """Real LCM brick writing to a temp DB."""
    from brikie.bricks.memory.lcm.lcm_brick import LcmBrick

    brick = LcmBrick(db_path=str(temp_db_dir / "test_lcm.db"))
    return brick


# ---------------------------------------------------------------------------
# HookEvent unwrap tests
# ---------------------------------------------------------------------------


class TestUnwrapHookData:
    """Verify the _unwrap_hook_data helper works for all dispatch patterns."""

    def test_unwrap_raw_dict(self):
        """Raw dict passes through unchanged."""
        data = {"content": "hello", "tool_calls": []}
        assert _unwrap_hook_data(data) is data

    def test_unwrap_raw_list(self):
        """Raw list passes through unchanged."""
        data = [Message(role="user", content="hi")]
        assert _unwrap_hook_data(data) is data

    def test_unwrap_hook_event(self):
        """HookEvent envelope is peeled to reveal inner data."""
        inner = {"content": "hello"}
        event = HookEvent(hook_type=HookType.POST_LLM, data=inner, brick_name="test")
        assert _unwrap_hook_data(event) is inner

    def test_unwrap_hook_event_list(self):
        """HookEvent with list data peels correctly."""
        inner = [{"role": "user", "content": "hi"}]
        event = HookEvent(hook_type=HookType.PRE_LLM, data=inner, brick_name="test")
        assert _unwrap_hook_data(event) is inner


# ---------------------------------------------------------------------------
# Memory hook registration tests
# ---------------------------------------------------------------------------


class TestMemoryHookRegistration:
    """Verify that memory-capable bricks get their hooks registered."""

    async def test_memory_capable_bricks_detected(self, registry, mempalace_brick, lcm_brick):
        """Both bricks should be detected as memory-capable."""
        registry.register(mempalace_brick)
        registry.register(lcm_brick)

        loop = EventLoop(registry=registry, state=StateManager(), hooks=HookDispatcher())
        capable = loop._memory_capable_bricks()
        names = {b.name for b in capable}
        assert "mempalace" in names
        assert "lcm" in names

    async def test_memory_hooks_wired(self, registry, mempalace_brick, hooks, state):
        """Memory hooks should be registered with the dispatcher."""
        registry.register(mempalace_brick)
        loop = EventLoop(registry=registry, state=state, hooks=hooks)
        await loop._phase_warm_up()

        # Check PRE_LLM and POST_LLM have callbacks
        pre_llm_count = len(hooks._callbacks[HookType.PRE_LLM])
        post_llm_count = len(hooks._callbacks[HookType.POST_LLM])
        assert pre_llm_count >= 1, "PRE_LLM should have memory callbacks"
        assert post_llm_count >= 1, "POST_LLM should have memory callbacks"


# ---------------------------------------------------------------------------
# Write path tests — intercept_message through hook dispatch
# ---------------------------------------------------------------------------


class TestMemoryWritePath:
    """Test that messages written through hooks actually persist to DB."""

    async def test_post_llm_writes_to_mempalace(self, registry, mempalace_brick, hooks, state):
        """POST_LLM dispatch should write assistant content to MemPalace."""
        registry.register(mempalace_brick)
        loop = EventLoop(registry=registry, state=state, hooks=hooks)
        await loop._phase_warm_up()

        # Simulate a POST_LLM dispatch with HookEvent (matching real agent_loop)
        event = HookEvent(
            hook_type=HookType.POST_LLM,
            data={"content": "The authentication system depends on Redis.", "tool_calls": []},
            brick_name="event_loop",
        )
        await hooks.dispatch(HookType.POST_LLM, event)

        # Check MemPalace DB has entities and triples
        ec = await mempalace_brick._store.get_entity_count()
        tc = await mempalace_brick._store.get_triple_count()
        assert ec > 0, "MemPalace should have extracted entities from POST_LLM content"
        assert tc > 0, "MemPalace should have extracted triples from POST_LLM content"

    async def test_post_llm_writes_to_lcm(self, registry, lcm_brick, hooks, state):
        """POST_LLM dispatch should append to LCM store."""
        registry.register(lcm_brick)
        loop = EventLoop(registry=registry, state=state, hooks=hooks)
        await loop._phase_warm_up()

        event = HookEvent(
            hook_type=HookType.POST_LLM,
            data={"content": "This is an assistant response.", "tool_calls": []},
            brick_name="event_loop",
        )
        await hooks.dispatch(HookType.POST_LLM, event)

        # Check LCM has the message
        ctx = await lcm_brick.build_context("default")
        assert len(ctx.get("tail", [])) > 0, "LCM should have stored the assistant message"
        assert ctx["tail"][0]["role"] == "assistant"

    async def test_post_llm_writes_to_wiki(self, registry, wiki_brick, hooks, state):
        """POST_LLM dispatch with structured content should auto-extract a wiki page."""
        registry.register(wiki_brick)
        loop = EventLoop(registry=registry, state=state, hooks=hooks)
        await loop._phase_warm_up()

        # Wiki auto-extract requires >= 200 chars with headings
        structured_content = "# Architecture Decision\n\nWe decided to use Redis for caching.\n- Low latency\n- Battle-tested\n- Simple API\n\n```python\ncache = redis.Redis()\n```\n" + "..." * 80
        assert len(structured_content) >= 200

        event = HookEvent(
            hook_type=HookType.POST_LLM,
            data={"content": structured_content, "tool_calls": []},
            brick_name="event_loop",
        )
        await hooks.dispatch(HookType.POST_LLM, event)

        # Check Wiki has auto-extracted a page
        count = await wiki_brick._store.page_count()
        assert count > 0, "Wiki should have auto-extracted a page from structured content"

    async def test_user_message_intercepted(self, registry, mempalace_brick, lcm_brick, hooks, state):
        """User messages should be intercepted into memory bricks."""
        registry.register(mempalace_brick)
        registry.register(lcm_brick)
        loop = EventLoop(registry=registry, state=state, hooks=hooks)
        await loop._phase_warm_up()

        # Simulate user message interception
        await loop._intercept_user_message("I think we should use PostgreSQL for the main database.")

        # MemPalace should have extracted entities
        ec = await mempalace_brick._store.get_entity_count()
        assert ec > 0, "MemPalace should extract from user messages"

        # LCM should have the user message
        ctx = await lcm_brick.build_context("default")
        tails = ctx.get("tail", [])
        user_msgs = [t for t in tails if t.get("role") == "user"]
        assert len(user_msgs) > 0, "LCM should have stored the user message"

    async def test_commands_not_intercepted(self, registry, lcm_brick, hooks, state):
        """Slash commands should NOT be intercepted into memory."""
        registry.register(lcm_brick)
        loop = EventLoop(registry=registry, state=state, hooks=hooks)
        await loop._phase_warm_up()

        await loop._intercept_user_message("/help")
        await loop._intercept_user_message("/bricks")

        ctx = await lcm_brick.build_context("default")
        assert len(ctx.get("tail", [])) == 0, "Commands should not be stored in memory"


# ---------------------------------------------------------------------------
# Read path tests — build_context and _build_memory_blob
# ---------------------------------------------------------------------------


class TestMemoryReadPath:
    """Test that _build_memory_blob correctly consumes all brick shapes."""

    async def test_lcm_context_included(self, registry, lcm_brick, hooks, state):
        """LCM DAG summaries should appear in the memory blob."""
        registry.register(lcm_brick)
        loop = EventLoop(registry=registry, state=state, hooks=hooks)
        await loop._phase_warm_up()

        # Write a message to LCM first
        event = HookEvent(
            hook_type=HookType.POST_LLM,
            data={"content": "Test response for LCM context.", "tool_calls": []},
            brick_name="event_loop",
        )
        await hooks.dispatch(HookType.POST_LLM, event)

        # Build the memory blob
        blob = await loop._build_memory_blob()
        assert "Session Summary" in blob or "Recent Messages" in blob, \
            "LCM context should appear in memory blob"

    async def test_mempalace_context_included(self, registry, mempalace_brick, hooks, state):
        """MemPalace knowledge graph stats should appear in the memory blob."""
        registry.register(mempalace_brick)
        loop = EventLoop(registry=registry, state=state, hooks=hooks)
        await loop._phase_warm_up()

        # Write a message to MemPalace first
        event = HookEvent(
            hook_type=HookType.POST_LLM,
            data={"content": "The auth system depends on Redis.", "tool_calls": []},
            brick_name="event_loop",
        )
        await hooks.dispatch(HookType.POST_LLM, event)

        blob = await loop._build_memory_blob()
        assert "Knowledge Graph" in blob, \
            "MemPalace context should appear in memory blob"
        assert "entities" in blob, "Entity count should be in MemPalace context"

    async def test_wiki_context_included(self, registry, wiki_brick, hooks, state):
        """Wiki page stats should appear in the memory blob."""
        registry.register(wiki_brick)
        loop = EventLoop(registry=registry, state=state, hooks=hooks)
        await loop._phase_warm_up()

        # Write a wiki page directly (auto-extract requires long structured content)
        structured = "# Test Page\n\n" + "Content.\n" * 50
        await wiki_brick._store.upsert_page(
            title="Test Page",
            body=structured,
            status="draft",
            tags=["test"],
            source="manual",
        )

        blob = await loop._build_memory_blob()
        assert "Wiki Knowledge Base" in blob, \
            "Wiki context should appear in memory blob"
        assert "page" in blob.lower(), "Page count should be in Wiki context"

    async def test_multiple_bricks_combined(self, registry, mempalace_brick, wiki_brick, lcm_brick, hooks, state):
        """All three memory bricks should contribute to the blob."""
        registry.register(mempalace_brick)
        registry.register(wiki_brick)
        registry.register(lcm_brick)
        loop = EventLoop(registry=registry, state=state, hooks=hooks)
        await loop._phase_warm_up()

        # Seed some data
        await loop._intercept_user_message("I decided to use React for the frontend.")
        event = HookEvent(
            hook_type=HookType.POST_LLM,
            data={"content": "Great choice, React integrates well.", "tool_calls": []},
            brick_name="event_loop",
        )
        await hooks.dispatch(HookType.POST_LLM, event)

        blob = await loop._build_memory_blob()
        # At least one of the sections should appear
        sections = ["Session Summary", "Recent Messages", "Knowledge Graph", "Wiki Knowledge Base"]
        found = [s for s in sections if s in blob]
        assert len(found) >= 2, \
            f"Expected at least 2 memory sections in blob, found {found}. Blob: {blob[:200]}"


# ---------------------------------------------------------------------------
# End-to-end pipeline test
# ---------------------------------------------------------------------------


class TestMemoryPipeline:
    """Full pipeline: hook dispatch → write → build_context → _build_memory_blob."""

    async def test_full_round_trip(self, registry, mempalace_brick, lcm_brick, hooks, state):
        """A complete memory round-trip with real bricks."""
        registry.register(mempalace_brick)
        registry.register(lcm_brick)
        loop = EventLoop(registry=registry, state=state, hooks=hooks)
        await loop._phase_warm_up()

        # 1. User speaks
        await loop._intercept_user_message("Let's use event sourcing for the audit log.")

        # 2. Assistant responds
        event = HookEvent(
            hook_type=HookType.POST_LLM,
            data={
                "content": "Event sourcing is a solid choice for audit logging. "
                           "The audit log depends on PostgreSQL.",
                "tool_calls": [],
            },
            brick_name="event_loop",
        )
        await hooks.dispatch(HookType.POST_LLM, event)

        # 3. Verify LCM has both messages
        ctx = await lcm_brick.build_context("default")
        assert len(ctx.get("tail", [])) >= 2, \
            f"Expected ≥2 messages in LCM tail, got {len(ctx.get('tail', []))}"

        # 4. Verify MemPalace has entities and triples
        ec = await mempalace_brick._store.get_entity_count()
        tc = await mempalace_brick._store.get_triple_count()
        assert ec > 0, f"Expected entities in MemPalace, got {ec}"
        assert tc > 0, f"Expected triples in MemPalace, got {tc}"

        # 5. Verify memory blob is non-empty and mentions both bricks
        blob = await loop._build_memory_blob()
        assert blob, "Memory blob should not be empty after messages"
        assert "Recent Messages" in blob or "Knowledge Graph" in blob, \
            f"Blob should reference memory content. Got: {blob[:300]}"
