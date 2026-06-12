"""Tests for the MemPalace Brick — Spatial/Temporal Knowledge Graph."""

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

    @pytest.mark.asyncio
    async def test_upsert_triple_deduplicates(self, store):
        """Identical (subject, predicate, object) updates the existing row."""
        await store.initialize()
        s = await store.upsert_entity(name="bus", entity_type="concept")
        o = await store.upsert_entity(name="kernel", entity_type="concept")
        first = await store.upsert_triple(s, "depends_on", o, confidence=0.6)
        second = await store.upsert_triple(s, "depends_on", o, confidence=0.85)
        assert first == second
        assert await store.get_triple_count() == 1
        triples = await store.get_all_triples()
        assert triples[0]["confidence"] == 0.85

    @pytest.mark.asyncio
    async def test_upsert_entity_deduplicates_by_name(self, store):
        """Repeated upserts of the same name update one row, not insert many."""
        await store.initialize()
        first = await store.upsert_entity(name="redis", entity_type="concept")
        second = await store.upsert_entity(name="Redis", entity_type="tool")
        assert first == second
        assert await store.get_entity_count() == 1

    @pytest.mark.asyncio
    async def test_upsert_entity_upgrades_generic_type(self, store):
        """'concept' is upgraded to a specific type; specific types are kept."""
        await store.initialize()
        entity_id = await store.upsert_entity(name="redis", entity_type="concept")
        await store.upsert_entity(name="redis", entity_type="tool")
        entity = await store.get_entity(entity_id)
        assert entity["entity_type"] == "tool"
        # A later generic mention must not downgrade it back
        await store.upsert_entity(name="redis", entity_type="concept")
        entity = await store.get_entity(entity_id)
        assert entity["entity_type"] == "tool"

    @pytest.mark.asyncio
    async def test_upsert_entity_preserves_description(self, store):
        """A None description on re-upsert keeps the existing one."""
        await store.initialize()
        entity_id = await store.upsert_entity(
            name="redis", entity_type="tool", description="in-memory store"
        )
        await store.upsert_entity(name="redis", entity_type="tool")
        entity = await store.get_entity(entity_id)
        assert entity["description"] == "in-memory store"

    @pytest.mark.asyncio
    async def test_search_entities(self, store):
        """search_entities builds valid SQL and filters by pattern/type."""
        await store.initialize()
        await store.upsert_entity(name="redis", entity_type="tool")
        await store.upsert_entity(name="alice", entity_type="person")
        results = await store.search_entities(name_pattern="red")
        assert len(results) == 1
        assert results[0]["name"] == "redis"
        results = await store.search_entities(entity_type="person")
        assert len(results) == 1
        assert results[0]["name"] == "alice"

    @pytest.mark.asyncio
    async def test_ensure_region_is_idempotent(self, store):
        await store.initialize()
        first = await store.ensure_region("wing", "project-alpha")
        second = await store.ensure_region("wing", "project-alpha")
        assert first == second
        regions = await store.get_regions_by_type("wing")
        assert len(regions) == 1

    @pytest.mark.asyncio
    async def test_map_entity_places_in_hierarchy(self, store):
        await store.initialize()
        entity_id = await store.upsert_entity(name="redis", entity_type="tool")
        await store.map_entity(entity_id, "project-alpha", "general", "hall_facts")
        # Idempotent for the same location
        await store.map_entity(entity_id, "project-alpha", "general", "hall_facts")
        spatial = await store.get_spatial_map_for_entity(entity_id)
        assert spatial["wing"] == "project-alpha"
        assert spatial["hall"] == "hall_facts"
        entities = await store.get_entities_in_wing("project-alpha")
        assert len(entities) == 1


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

    def test_stop_words_are_not_entities(self, extractor):
        """Sentence starters and pronouns must not become person entities."""
        content = (
            "Let me explain. The system works. Your code is fine. "
            "This should help. Please review it. Now we wait."
        )
        result = extractor.extract(content)
        names = {e.name for e in result.entities}
        for noise in ("let", "the", "your", "this", "please", "now", "should"):
            assert noise not in names

    def test_sentence_initial_capital_is_not_a_person(self, extractor):
        content = "Refactor the parser. Tomorrow brings new tasks."
        result = extractor.extract(content)
        person_names = {
            e.name for e in result.entities if e.entity_type.value == "person"
        }
        assert "refactor" not in person_names
        assert "tomorrow" not in person_names

    def test_mid_sentence_name_is_a_person(self, extractor):
        content = "We asked Alice about the schema."
        result = extractor.extract(content)
        people = [e for e in result.entities if e.entity_type.value == "person"]
        assert [e.name for e in people] == ["alice"]

    def test_multi_word_name_at_sentence_start(self, extractor):
        content = "John Doe reviewed the patch."
        result = extractor.extract(content)
        people = {e.name for e in result.entities if e.entity_type.value == "person"}
        assert "john doe" in people

    def test_entity_claimed_by_specific_type_first(self, extractor):
        """'Redis' is a tool — never duplicated as a person."""
        content = "We migrated the cache to Redis last week."
        result = extractor.extract(content)
        redis = [e for e in result.entities if e.name == "redis"]
        assert len(redis) == 1
        assert redis[0].entity_type.value == "tool"

    def test_bare_verbs_are_not_entities(self, extractor):
        content = "We decided to ship it. The release was completed and merged."
        result = extractor.extract(content)
        names = {e.name for e in result.entities}
        for verb in ("decided", "completed", "merged"):
            assert verb not in names

    def test_triple_terms_strip_articles(self, extractor):
        content = "The engine depends on the bus."
        result = extractor.extract(content)
        deps = [t for t in result.triples if t.predicate == "depends_on"]
        assert len(deps) == 1
        assert deps[0].subject == "engine"
        assert deps[0].object == "bus"

    def test_triple_with_pronoun_subject_is_dropped(self, extractor):
        content = "It depends on the weather."
        result = extractor.extract(content)
        assert all(t.subject != "it" for t in result.triples)

    def test_creates_pattern_captures_object_not_predicate(self, extractor):
        """Regression: the 'creates' pattern must not store the verb as object."""
        content = "The loader creates registries."
        result = extractor.extract(content)
        creates = [t for t in result.triples if t.predicate == "creates"]
        assert len(creates) == 1
        assert creates[0].subject == "loader"
        assert creates[0].object == "registries"


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
    async def test_brick_intercept_populates_spatial_map(self, brick):
        """Entities extracted from a message land in the spatial hierarchy."""
        await brick.init()
        await brick.intercept_message(
            "test-session", "user",
            "In project-alpha, Redis was completed as the cache layer.",
        )
        wings = await brick._store.get_regions_by_type("wing")
        assert any(w["name"] == "project-alpha" for w in wings)
        entities = await brick._store.get_entities_in_wing("project-alpha")
        assert any(e["name"] == "redis" for e in entities)

    @pytest.mark.asyncio
    async def test_brick_intercept_deduplicates_entities(self, brick):
        """Mentioning the same entity twice yields one row."""
        await brick.init()
        await brick.intercept_message("s1", "user", "We rely on Redis heavily.")
        await brick.intercept_message("s1", "user", "Tune Redis for performance.")
        entities = await brick._store.search_entities(name_pattern="redis")
        assert len(entities) == 1

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
