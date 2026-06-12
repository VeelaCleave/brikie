"""MemPalace Store — SQLite-backed knowledge graph store.

Implements the core data layer for the MemPalace Brick.
- Entity/relationship (triple) CRUD
- Spatial mapping (wing → room → hall → tunnel → drawer)
- Temporal filtering (valid_from/valid_to)

All database operations are wrapped in strict try/finally blocks to prevent
connection leaks in long-running AFK loops.
"""

import logging
import uuid
from pathlib import Path

from brikie.bricks.memory.sqlite_pool import VersionedConnectionPool

logger = logging.getLogger(__name__)


class MempalaceConnectionPool(VersionedConnectionPool):
    """Manages SQLite connections for the MemPalace store."""

    SCHEMA_VERSION = 1
    MIGRATIONS = {}
    DB_FILENAME = "mempalace.db"
    def __init__(self, db_path: str) -> None:
        super().__init__(db_path)

    def _get_schema_path(self) -> Path:
        module_dir = Path(__file__).resolve().parent
        return module_dir / "schema.sql"


class MempalaceStore:
    """Core MemPalace data store for entities, triples, and spatial mapping."""

    def __init__(self, db_path: str = "mempalace.db") -> None:
        self._pool = MempalaceConnectionPool(db_path)
        self._db_path = db_path

    async def initialize(self) -> None:
        await self._pool.initialize()

    async def shutdown(self) -> None:
        await self._pool.shutdown()

    async def upsert_entity(
        self,
        name: str,
        entity_type: str,
        session_id: str | None = None,
        description: str | None = None,
        valid_from: str | None = None,
        valid_to: str | None = None,
    ) -> str:
        """Create or update an entity in the knowledge graph."""
        entity_id = str(uuid.uuid4())
        await self._pool._insert(
            """INSERT INTO entities (id, name, entity_type, session_id, description,
                                      valid_from, valid_to, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc'))""",
            (entity_id, name, entity_type, session_id, description, valid_from, valid_to),
        )
        return entity_id

    async def get_entity(self, entity_id: str) -> dict | None:
        """Retrieve a single entity by ID."""
        row = await self._pool._execute(
            "SELECT id, name, entity_type, session_id, description, created_at, valid_from, valid_to "
            "FROM entities WHERE id = ?",
            (entity_id,),
            fetch="one",
        )
        if row is None:
            return None
        return {
            "id": row[0],
            "name": row[1],
            "entity_type": row[2],
            "session_id": row[3],
            "description": row[4],
            "created_at": row[5],
            "valid_from": row[6],
            "valid_to": row[7],
        }

    async def get_entity_by_name(self, name: str) -> dict | None:
        """Retrieve an entity by name."""
        row = await self._pool._execute(
            "SELECT id, name, entity_type, session_id, description, created_at, valid_from, valid_to "
            "FROM entities WHERE name = ? ORDER BY created_at DESC LIMIT 1",
            (name,),
            fetch="one",
        )
        if row is None:
            return None
        return {
            "id": row[0],
            "name": row[1],
            "entity_type": row[2],
            "session_id": row[3],
            "description": row[4],
            "created_at": row[5],
            "valid_from": row[6],
            "valid_to": row[7],
        }

    async def upsert_triple(
        self,
        subject_id: str,
        predicate: str,
        object_id: str,
        confidence: float = 0.8,
        source_message_index: int | None = None,
        valid_from: str | None = None,
        valid_to: str | None = None,
    ) -> str:
        """Create or update a triple relationship. Returns triple_id."""
        triple_id = str(uuid.uuid4())
        await self._pool._insert(
            """INSERT INTO triples (id, subject_id, predicate, object_id,
                                    valid_from, valid_to, confidence, source_message_index, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc'))""",
            (triple_id, subject_id, predicate, object_id, valid_from, valid_to, confidence,
             source_message_index),
        )
        return triple_id

    async def get_entity_triples(self, entity_id: str) -> list[dict]:
        """Get all triples where the entity is either subject or object."""
        rows = await self._pool._execute(
            "SELECT id, subject_id, predicate, object_id, confidence, valid_from, valid_to "
            "FROM triples WHERE subject_id = ? OR object_id = ? ORDER BY created_at DESC",
            (entity_id, entity_id),
            fetch="all",
        )
        result = []
        if rows:
            for row in rows:
                result.append({
                    "id": row[0],
                    "subject_id": row[1],
                    "predicate": row[2],
                    "object_id": row[3],
                    "confidence": row[4],
                    "valid_from": row[5],
                    "valid_to": row[6],
                })
        return result

    async def get_entities_by_type(self, entity_type: str) -> list[dict]:
        """Get all entities of a specific type."""
        rows = await self._pool._execute(
            "SELECT id, name, entity_type, session_id, description, created_at, valid_from, valid_to "
            "FROM entities WHERE entity_type = ? ORDER BY created_at DESC",
            (entity_type,),
            fetch="all",
        )
        result = []
        if rows:
            for row in rows:
                result.append({
                    "id": row[0],
                    "name": row[1],
                    "entity_type": row[2],
                    "session_id": row[3],
                    "description": row[4],
                    "created_at": row[5],
                    "valid_from": row[6],
                    "valid_to": row[7],
                })
        return result

    async def get_entities_in_wing(self, wing: str) -> list[dict]:
        """Get all entities mapped to a specific wing."""
        rows = await self._pool._execute(
            """SELECT e.id, e.name, e.entity_type, e.session_id, e.description, e.created_at,
                      e.valid_from, e.valid_to
               FROM entities e
               INNER JOIN spatial_map sm ON e.id = sm.entity_id
               WHERE sm.wing = ?
               ORDER BY sm.distance ASC""",
            (wing,),
            fetch="all",
        )
        result = []
        if rows:
            for row in rows:
                result.append({
                    "id": row[0],
                    "name": row[1],
                    "entity_type": row[2],
                    "session_id": row[3],
                    "description": row[4],
                    "created_at": row[5],
                    "valid_from": row[6],
                    "valid_to": row[7],
                })
        return result

    async def get_triples_by_subject(self, subject_id: str) -> list[dict]:
        """Get all triples with a specific subject."""
        rows = await self._pool._execute(
            "SELECT id, subject_id, predicate, object_id, confidence, valid_from, valid_to "
            "FROM triples WHERE subject_id = ? ORDER BY created_at DESC",
            (subject_id,),
            fetch="all",
        )
        result = []
        if rows:
            for row in rows:
                result.append({
                    "id": row[0],
                    "subject_id": row[1],
                    "predicate": row[2],
                    "object_id": row[3],
                    "confidence": row[4],
                    "valid_from": row[5],
                    "valid_to": row[6],
                })
        return result

    async def get_triples_by_predicate(self, predicate: str) -> list[dict]:
        """Get all triples with a specific predicate."""
        rows = await self._pool._execute(
            "SELECT id, subject_id, predicate, object_id, confidence, valid_from, valid_to "
            "FROM triples WHERE predicate = ? ORDER BY created_at DESC",
            (predicate,),
            fetch="all",
        )
        result = []
        if rows:
            for row in rows:
                result.append({
                    "id": row[0],
                    "subject_id": row[1],
                    "predicate": row[2],
                    "object_id": row[3],
                    "confidence": row[4],
                    "valid_from": row[5],
                    "valid_to": row[6],
                })
        return result

    async def get_spatial_map_for_entity(self, entity_id: str) -> dict | None:
        """Get the spatial location of an entity."""
        row = await self._pool._execute(
            "SELECT id, entity_id, wing, room, hall, tunnel, drawer, distance "
            "FROM spatial_map WHERE entity_id = ? ORDER BY distance ASC LIMIT 1",
            (entity_id,),
            fetch="one",
        )
        if row is None:
            return None
        return {
            "id": row[0],
            "entity_id": row[1],
            "wing": row[2],
            "room": row[3],
            "hall": row[4],
            "tunnel": row[5],
            "drawer": row[6],
            "distance": row[7],
        }

    async def create_region(
        self,
        region_type: str,
        name: str,
        parent_id: str | None = None,
        description: str | None = None,
        chroma_collection: str | None = None,
        chroma_document_id: str | None = None,
    ) -> str:
        """Create a new spatial region."""
        region_id = str(uuid.uuid4())
        await self._pool._insert(
            """INSERT INTO spatial_regions (id, region_type, name, parent_id, description,
                                            chroma_collection, chroma_document_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (region_id, region_type, name, parent_id, description, chroma_collection, chroma_document_id),
        )
        return region_id

    async def get_regions_by_type(self, region_type: str) -> list[dict]:
        """Get all regions of a specific type."""
        rows = await self._pool._execute(
            "SELECT id, region_type, name, parent_id, description, created_at "
            "FROM spatial_regions WHERE region_type = ? ORDER BY name ASC",
            (region_type,),
            fetch="all",
        )
        result = []
        if rows:
            for row in rows:
                result.append({
                    "id": row[0],
                    "region_type": row[1],
                    "name": row[2],
                    "parent_id": row[3],
                    "description": row[4],
                    "created_at": row[5],
                })
        return result

    async def search_spatial_by_wing_room(self, wing: str, room: str) -> list[dict]:
        """Search for entities in a specific wing and room."""
        rows = await self._pool._execute(
            """SELECT e.id, e.name, e.entity_type, e.description, sm.wing, sm.room, sm.distance
               FROM entities e
               INNER JOIN spatial_map sm ON e.id = sm.entity_id
               WHERE sm.wing = ? AND sm.room = ?
               ORDER BY sm.distance ASC""",
            (wing, room),
            fetch="all",
        )
        result = []
        if rows:
            for row in rows:
                result.append({
                    "entity_id": row[0],
                    "entity_name": row[1],
                    "entity_type": row[2],
                    "description": row[3],
                    "wing": row[4],
                    "room": row[5],
                    "distance": row[6],
                })
        return result

    async def get_temporal_range(self, entity_id: str, start: str, end: str) -> list[dict]:
        """Get triples within a temporal range."""
        rows = await self._pool._execute(
            "SELECT id, subject_id, predicate, object_id, confidence, valid_from, valid_to "
            "FROM triples WHERE (subject_id = ? OR object_id = ?) "
            "AND valid_from >= ? AND valid_to <= ? ORDER BY valid_from ASC",
            (entity_id, entity_id, start, end),
            fetch="all",
        )
        result = []
        if rows:
            for row in rows:
                result.append({
                    "id": row[0],
                    "subject_id": row[1],
                    "predicate": row[2],
                    "object_id": row[3],
                    "confidence": row[4],
                    "valid_from": row[5],
                    "valid_to": row[6],
                })
        return result

    async def get_all_entities(
        self,
        entity_type: str | None = None,
        limit: int = 50,
        sort_by: str = "recency",
    ) -> list[dict]:
        """Get all entities with optional type filter."""
        query = "SELECT id, name, entity_type, session_id, description, created_at, valid_from, valid_to FROM entities"
        params: list = []
        if entity_type:
            query += " WHERE entity_type = ?"
            params.append(entity_type)
        if sort_by == "recency":
            query += " ORDER BY created_at DESC"
        elif sort_by == "alphabetical":
            query += " ORDER BY name ASC"
        query += " LIMIT ?"
        params.append(limit)
        rows = await self._pool._execute(query, tuple(params), fetch="all")
        result = []
        if rows:
            for row in rows:
                result.append({
                    "id": row[0],
                    "name": row[1],
                    "entity_type": row[2],
                    "session_id": row[3],
                    "description": row[4],
                    "created_at": row[5],
                    "valid_from": row[6],
                    "valid_to": row[7],
                })
        return result

    async def search_entities(
        self,
        name_pattern: str | None = None,
        entity_type: str | None = None,
    ) -> list[dict]:
        """Search entities by name pattern and type."""
        query = "SELECT id, name, entity_type, session_id, description, created_at "
        "FROM entities WHERE 1=1"
        params: list = []
        if name_pattern:
            query += " AND name LIKE ?"
            params.append(f"%{name_pattern}%")
        if entity_type:
            query += " AND entity_type = ?"
            params.append(entity_type)
        query += " ORDER BY created_at DESC LIMIT 50"
        rows = await self._pool._execute(query, tuple(params), fetch="all")
        result = []
        if rows:
            for row in rows:
                result.append({
                    "id": row[0],
                    "name": row[1],
                    "entity_type": row[2],
                    "session_id": row[3],
                    "description": row[4],
                    "created_at": row[5],
                })
        return result

    async def get_all_triples(
        self,
        subject_id: str | None = None,
        predicate: str | None = None,
        object_id: str | None = None,
        current_only: bool = False,
        reverse: bool = False,
        limit: int = 100,
    ) -> list[dict]:
        """Get triples with optional filters."""
        conditions = ["1=1"]
        params: list = []
        if subject_id and subject_id != "*":
            if reverse:
                conditions.append("object_id = ?")
            else:
                conditions.append("subject_id = ?")
            params.append(subject_id)
        if predicate and predicate != "*":
            conditions.append("predicate = ?")
            params.append(predicate)
        if object_id and object_id != "*":
            if reverse:
                conditions.append("subject_id = ?")
            else:
                conditions.append("object_id = ?")
            params.append(object_id)
        if current_only:
            conditions.append("(valid_to IS NULL OR valid_to > strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc'))")
        query = "SELECT id, subject_id, predicate, object_id, confidence, valid_from, valid_to FROM triples WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = await self._pool._execute(query, tuple(params), fetch="all")
        result = []
        if rows:
            for row in rows:
                result.append({
                    "id": row[0],
                    "subject_id": row[1],
                    "predicate": row[2],
                    "object_id": row[3],
                    "confidence": row[4],
                    "valid_from": row[5],
                    "valid_to": row[6],
                })
        return result

    async def get_entity_count(self) -> int:
        """Get total entity count."""
        return (await self._pool._execute(
            "SELECT COUNT(*) FROM entities", (), fetch="value"
        )) or 0

    async def get_triple_count(self) -> int:
        """Get total triple count."""
        return (await self._pool._execute(
            "SELECT COUNT(*) FROM triples", (), fetch="value"
        )) or 0

    async def get_recent_entities(self, limit: int = 10) -> list[dict]:
        """Get the most recently created entities."""
        rows = await self._pool._execute(
            "SELECT id, name, entity_type, session_id, description, created_at, valid_from, valid_to "
            "FROM entities ORDER BY created_at DESC LIMIT ?",
            (limit,),
            fetch="all",
        )
        result = []
        if rows:
            for row in rows:
                result.append({
                    "id": row[0],
                    "name": row[1],
                    "entity_type": row[2],
                    "session_id": row[3],
                    "description": row[4],
                    "created_at": row[5],
                    "valid_from": row[6],
                    "valid_to": row[7],
                })
        return result
