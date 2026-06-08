"""Tests for the MemPalace Brick — Spatial/Temporal Knowledge Graph."""

import asyncio
import os
import tempfile

import pytest

from brikie.bricks.memory.mempalace.entity_extractor import EntityExtractor
from brikie.bricks.memory.mempalace.mempalace_brick import MempalaceBrick
from brikie.bricks.memory.mempalace.mempalace_store import MempalaceStore
from brikie.bricks.memory.mempalace.tools import get_mempalace_tools


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
    """Create a MempalaceStore instance."""
    return MempalaceStore(tmp_db)


@pytest.fixture
def brick(tmp_db):
    """Create a MempalaceBrick instance."""
    return MempalaceBrick(tmp_db)


@pytest.fixture
def extractor():
    """Create an EntityExtractor instance."""
    return EntityExtractor()


class TestMempalaceStore:
    """Tests for MempalaceStore CRUD operations."""

    @pytest.mark.asyncio
    async def test_initialize_creates_tables(self, store):
        await store.initialize()
        result = await store._pool._execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'entities'",
            (),
            fetch="value",
        )
        assert result == "entities"

    @pytest.mark.asyncio
    async def test_wal_mode_applied(self, store):
        await store.initialize()
        result = await store._pool._execute(
            "PRAGMA journal_mode", (), fetch="value"
        )
        assert result == "wal"

    @pytest.mark.asyncio
    async def test_upsert_entity(self, store):
        await store.initialize()
        entity_id = await store.upsert_entity(
            name="test-entity",
            entity_type="concept",
            session_id="session-1",
            description="A test entity",
        )
        assert isinstance(entity_id, str)
        assert len(entity_id) > 0

    @pytest.mark.asyncio
    async def test_get_entity(self, store):
        await store.initialize()
        entity_id = await store.upsert_entity(
            name="test-entity",
            entity_type="concept",
            session_id="session-1",
        )
        entity = await store.get_entity(entity_id)
        assert entity is not None
        assert entity["name"] == "test-entity"

    @pytest.mark.asyncio
    async def test_get_entity_by_name(self, store):
        await store.initialize()
        await store.upsert_entity(
            name="unique-entity",
            entity_type="person",
            session_id="session-1",
        )
        entity = await store.get_entity_by_name("unique-entity")
        assert entity is not None
        assert entity["name"] == "unique-entity"

    @pytest.mark.asyncio
    async def test_get_entities_by_type(self, store):
        await store.initialize()
        await store.upsert_entity(
            name="alice",
            entity_type="person",
            session_id="session-1",
        )
        await store.upsert_entity(
            name="bob",
            entity_type="person",
            session_id="session-1",
        )
        entities = await store.get_entities_by_type("person")
        assert len(entities) == 2

    @pytest.mark.asyncio
    async def test_upsert_triple(self, store):
        await store.initialize()
        subject_id = await store.upsert_entity(
            name="subject",
            entity_type="concept",
            session_id="session-1",
        )
        object_id = await store.upsert_entity(
            name="object",
            entity_type="concept",
            session_id="session-1",
        )
        triple_id = await store.upsert_triple(
            subject_id=subject_id,
            predicate="relates_to",
            object_id=object_id,
            confidence=0.8,
        )
        assert isinstance(triple_id, str)

    @pytest.mark.asyncio
    async def test_get_entity_triples(self, store):
        await store.initialize()
        subject_id = await store.upsert_entity(
            name="subject",
            entity_type="concept",
            session_id="session-1",
        )
        object_id = await store.upsert_entity(
            name="object",
            entity_type="concept",
            session_id="session-1",
        )
        await store.upsert_triple(
            subject_id=subject_id,
            predicate="relates_to",
            object_id=object_id,
        )
        triples = await store.get_entity_triples(subject_id)
        assert len(triples) == 1

    @pytest.mark.asyncio
    async def test_get_all_entities(self, store):
        await store.initialize()
        await store.upsert_entity(
            name="entity-1",
            entity_type="concept",
            session_id="session-1",
        )
        entities = await store.get_all_entities()
        assert len(entities) >= 1

    @pytest.mark.asyncio
    async def test_get_all_triples(self, store):
        await store.initialize()
        subject_id = await store.upsert_entity(
            name="subject",
            entity_type="concept",
            session_id="session-1",
        )
        object_id = await store.upsert_entity(
            name="object",
            entity_type="concept",
            session_id="session-1",
        )
        await store.upsert_triple(
            subject_id=subject_id,
            predicate="relates_to",
            object_id=object_id,
        )
        triples = await store.get_all_triples()
        assert len(triples) >= 1

    @pytest.mark.asyncio
    async def test_get_entity_count(self, store):
        await store.initialize()
        count = await store.get_entity_count()
        assert count == 0
        await store.upsert_entity(
            name="entity-1",
            entity_type="concept",
            session_id="session-1",
        )
        count = await store.get_entity_count()
        assert count == 1

    @pytest.mark.asyncio
    async def test_get_triple_count(self, store):
        await store.initialize()
        count = await store.get_triple_count()
        assert count == 0


class TestEntityExtractor:
    """Tests for EntityExtractor."""

    def test_extract_entities_from_text(self, extractor):
        content = "Alice decided to use SQLite for the project."
        result = extractor.extract(content)
        assert len(result.entities) > 0

    def test_extract_triples_from_text(self, extractor):
        content = "The project depends on SQLite."
        result = extractor.extract(content)
        assert len(result.triples) > 0

    def test_extract_spatial_mapping(self, extractor):
        content = "In project-alpha, we decided to use the new middleware."
        result = extractor.extract(content)
        assert result.wing is not None
        assert result.room is not None
        assert result.hall is not None

    def test_extract_batch(self, extractor):
        messages = [
            {"content": "Alice created a new feature.", "session_id": "s1"},
            {"content": "The project depends on Redis.", "session_id": "s1"},
        ]
        results = extractor.extract_batch(messages)
        assert len(results) == 2

    def test_extractor_handles_empty_content(self, extractor):
        result = extractor.extract("")
        assert isinstance(result.entities, list)
        assert isinstance(result.triples, list)

    def test_extractor_handles_long_content(self, extractor):
        content = "This is a long piece of content. " * 100
        result = extractor.extract(content)
        assert isinstance(result.entities, list)


class TestToolSchemas:
    """Tests for MemPalace tool schemas."""

    def test_get_mempalace_tools(self):
        tools = get_mempalace_tools()
        assert len(tools) == 5

    def test_tool_names(self):
        tools = get_mempalace_tools()
        names = [t["function"]["name"] for t in tools]
        assert "mempalace_query" in names
        assert "mempalace_traverse" in names
        assert "mempalace_entities" in names
        assert "mempalace_triples" in names
        assert "mempalace_inject" in names

    def test_tool_has_required_fields(self):
        tools = get_mempalace_tools()
        for tool in tools:
            assert "type" in tool
            assert "function" in tool
            assert "name" in tool["function"]
            assert "parameters" in tool["function"]
            assert "description" in tool["function"]


class TestMempalaceBrick:
    """Tests for MempalaceBrick lifecycle."""

    @pytest.mark.asyncio
    async def test_brick_init(self, brick):
        await brick.init()
        assert brick._store is not None
        assert brick._extractor is not None

    @pytest.mark.asyncio
    async def test_brick_shutdown(self, brick):
        await brick.init()
        await brick.shutdown()

    @pytest.mark.asyncio
    async def test_brick_has_tools(self, brick):
        await brick.init()
        assert brick.tools is not None
        assert len(brick.tools) == 5

    @pytest.mark.asyncio
    async def test_brick_intercept_message(self, brick):
        await brick.init()
        session_id = "test-session"
        await brick.intercept_message(
            session_id, "user", "Alice decided to use SQLite."
        )
        # Check that entities were extracted and stored
        entities = await brick._store.get_all_entities()
        assert len(entities) > 0

    @pytest.mark.asyncio
    async def test_brick_build_context(self, brick):
        await brick.init()
        session_id = "test-session"
        context = await brick.build_context(session_id)
        assert "mempalace" in context

    @pytest.mark.asyncio
    async def test_brick_execute_tool(self, brick):
        await brick.init()
        result = await brick.execute("mempalace_entities", {"type": "concept"})
        assert "entities" in result
        assert "count" in result


class TestTryFinally:
    """Tests for connection management."""

    @pytest.mark.asyncio
    async def test_pool_uses_try_finally(self, store):
        await store.initialize()
        await store.upsert_entity(
            name="test-entity",
            entity_type="concept",
            session_id="session-1",
        )
        result = await store._pool._execute(
            "SELECT COUNT(*) FROM entities",
            (),
            fetch="value",
        )
        assert result == 1
