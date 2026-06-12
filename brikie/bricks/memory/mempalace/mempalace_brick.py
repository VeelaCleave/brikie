"""MemPalace Brick — Spatial/Temporal Knowledge Graph Memory Brick.

Implements both MemoryBrick (auto-extraction) and ToolBrick (agent tools)
interfaces. The brick automatically extracts entities and triples from
messages via the event bus, and exposes 5 read-only tools for the agent
to query the MemPalace hierarchy.
"""

import logging
from typing import Any, Dict, List

from brikie.config.types import BrickState
from brikie.bricks.memory.memory_brick import MemoryBrick
from brikie.bricks.memory.mempalace.entity_extractor import EntityExtractor
from brikie.bricks.memory.mempalace.mempalace_store import MempalaceStore
from brikie.bricks.memory.mempalace.tools import get_mempalace_tools
from brikie.kernel.registry import ToolBrick

logger = logging.getLogger(__name__)


class MempalaceBrick(MemoryBrick, ToolBrick):
    BRICK_NUMBER = "BRK-610"
    """MemPalace Memory Brick with entity extraction and spatial mapping.

    Implements:
    - MemoryBrick: Auto-extracts entities/triples from messages
    - ToolBrick: Exposes 5 read-only tools for the agent
    """

    def __init__(self, db_path: str = "mempalace.db") -> None:
        super().__init__()
        self._name = "mempalace"
        self._store = MempalaceStore(db_path)
        self._extractor = EntityExtractor()
        self._initialized = False
        self._tools = get_mempalace_tools()

    @property
    def tools(self) -> List[Dict[str, Any]]:
        """Return the 5 MemPalace tool schemas."""
        return self._tools


    async def init(self) -> None:
        """Initialize the MemPalace store."""
        await self._store.initialize()
        self._initialized = True
        self._state = BrickState.ACTIVE
        logger.info("MempalaceBrick: initialized at %s", self._store._db_path)

    async def shutdown(self) -> None:
        """Shutdown the MemPalace store."""
        await self._store.shutdown()
        self._initialized = False
        self._state = BrickState.WARM_UP
        logger.info("MempalaceBrick: shutdown complete")

    async def intercept_message(
        self, session_id: str, role: str, content: str
    ) -> None:
        """Intercept messages and auto-extract entities/triples.

        Extracts entities, triples, and spatial mappings from the message,
        then stores them in the knowledge graph.
        """
        if not self._initialized:
            return

        # Extract entities and triples
        result = self._extractor.extract(content, session_id)

        # Store entities and place them in the spatial hierarchy
        entity_ids: List[str] = []
        for entity in result.entities:
            entity_id = await self._store.upsert_entity(
                name=entity.name,
                entity_type=entity.entity_type.value,
                session_id=session_id,
                description=entity.description,
            )
            entity_ids.append(entity_id)

        if entity_ids:
            wing_id = await self._store.ensure_region("wing", result.wing)
            await self._store.ensure_region("room", result.room, parent_id=wing_id)
            await self._store.ensure_region("hall", result.hall, parent_id=wing_id)
            for entity_id in entity_ids:
                await self._store.map_entity(
                    entity_id, result.wing, result.room, result.hall
                )

        # Store triples
        for triple in result.triples:
            subject_id = await self._get_or_create_entity(
                triple.subject, session_id
            )
            object_id = await self._get_or_create_entity(
                triple.object, session_id
            )
            if subject_id and object_id:
                await self._store.upsert_triple(
                    subject_id=subject_id,
                    predicate=triple.predicate,
                    object_id=object_id,
                    confidence=triple.confidence,
                )

    async def _get_or_create_entity(
        self, name: str, session_id: str
    ) -> str | None:
        """Get an entity by name or create one if it doesn't exist."""
        entity = await self._store.get_entity_by_name(name)
        if entity:
            return entity["id"]

        entity_id = await self._store.upsert_entity(
            name=name,
            entity_type="concept",
            session_id=session_id,
            description=f"Extracted entity: {name}",
        )
        return entity_id

    async def build_context(self, session_id: str) -> Dict[str, Any]:
        """Build context from MemPalace knowledge graph.

        Returns a summary of the current state of the knowledge graph
        including recent entities for context injection.
        """
        entity_count = await self._store.get_entity_count()
        triple_count = await self._store.get_triple_count()
        recent_entities = await self._store.get_recent_entities(limit=10)

        return {
            "mempalace": {
                "entity_count": entity_count,
                "triple_count": triple_count,
                "recent_entities": recent_entities,
                "session_id": session_id,
            },
        }

    async def execute(self, name: str, args: Dict[str, Any]) -> Any:
        """Execute a MemPalace tool.

        Handles the 5 read-only tools:
        - mempalace_query: Semantic search
        - mempalace_traverse: Spatial navigation
        - mempalace_entities: Entity queries
        - mempalace_triples: Triple queries
        - mempalace_inject: Context injection
        """
        if name == "mempalace_query":
            return await self._handle_mempalace_query(args)
        elif name == "mempalace_traverse":
            return await self._handle_mempalace_traverse(args)
        elif name == "mempalace_entities":
            return await self._handle_mempalace_entities(args)
        elif name == "mempalace_triples":
            return await self._handle_mempalace_triples(args)
        elif name == "mempalace_inject":
            return await self._handle_mempalace_inject(args)
        else:
            return {"error": f"Unknown tool: {name}"}

    # ------------------------------------------------------------------
    # Tool Handlers
    # ------------------------------------------------------------------

    async def _handle_mempalace_query(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle mempalace_query tool — semantic search."""
        query = args.get("query", "")
        wing = args.get("wing")
        room = args.get("room")

        results: List[Dict[str, Any]] = []

        # Search entities by name pattern
        if query:
            entities = await self._store.search_entities(name_pattern=query)
            results.extend([
                {
                    "type": "entity",
                    "name": e["name"],
                    "entity_type": e["entity_type"],
                    "description": e.get("description", ""),
                }
                for e in entities
            ])

        # If wing/room specified, filter by spatial location
        if wing and room:
            spatial_results = await self._store.search_spatial_by_wing_room(wing, room)
            results.extend([
                {
                    "type": "spatial",
                    "entity_name": r["entity_name"],
                    "wing": r["wing"],
                    "room": r["room"],
                    "distance": r["distance"],
                }
                for r in spatial_results
            ])

        return {
            "query": query,
            "results": results[:20],
            "count": len(results),
        }

    async def _handle_mempalace_traverse(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle mempalace_traverse tool — spatial navigation."""
        action = args.get("action", "list_wings")

        if action == "list_wings":
            wings = await self._store.get_regions_by_type("wing")
            return {"wings": wings}

        elif action == "list_rooms":
            rooms = await self._store.get_regions_by_type("room")
            return {"rooms": rooms}

        elif action == "list_halls":
            halls = await self._store.get_regions_by_type("hall")
            return {"halls": halls}

        elif action in ("enter_room", "enter_hall", "tunnel", "path"):
            wing = args.get("wing")
            room = args.get("room")
            hall = args.get("hall")

            results: List[Dict[str, Any]] = []
            if wing:
                entities = await self._store.get_entities_in_wing(wing)
                results.extend(entities)
            if room:
                if wing:
                    spatial = await self._store.search_spatial_by_wing_room(wing, room)
                    results.extend(spatial)

            return {
                "action": action,
                "wing": wing,
                "room": room,
                "hall": hall,
                "results": results,
            }

        return {"action": action, "results": []}

    async def _handle_mempalace_entities(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle mempalace_entities tool — entity queries."""
        entity_type = args.get("type")
        top_k = args.get("top_k", 20)
        sort_by = args.get("sort_by", "recency")

        entities = await self._store.get_all_entities(
            entity_type=entity_type,
            limit=top_k,
            sort_by=sort_by,
        )

        return {
            "entities": entities,
            "count": len(entities),
            "type_filter": entity_type,
        }

    async def _handle_mempalace_triples(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle mempalace_triples tool — triple queries."""
        subject = args.get("subject")
        predicate = args.get("predicate")
        obj = args.get("object")
        current_only = args.get("current_only", False)
        reverse = args.get("reverse", False)

        triples = await self._store.get_all_triples(
            subject_id=subject,
            predicate=predicate,
            object_id=obj,
            current_only=current_only,
            reverse=reverse,
        )

        return {
            "triples": triples,
            "count": len(triples),
        }

    async def _handle_mempalace_inject(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle mempalace_inject tool — context injection."""
        wing = args.get("wing")
        max_tokens = args.get("max_tokens", 1000)

        results: List[Dict[str, Any]] = []

        if wing:
            entities = await self._store.get_entities_in_wing(wing)
            results.extend(entities)
        else:
            entities = await self._store.get_all_entities(limit=50)
            results.extend(entities)

        # Truncate to fit token budget
        truncated = self._truncate_to_tokens(results, max_tokens)

        return {
            "wing": wing,
            "injected": truncated,
            "token_budget": max_tokens,
            "count": len(truncated),
        }

    def _truncate_to_tokens(
        self, items: List[Dict[str, Any]], max_tokens: int
    ) -> List[Dict[str, Any]]:
        """Truncate items to fit within token budget."""
        total_tokens = 0
        result: List[Dict[str, Any]] = []

        for item in items:
            # Estimate tokens (rough: 1 char = 0.25 tokens)
            item_tokens = sum(
                len(str(v)) for v in item.values() if v
            ) // 4

            if total_tokens + item_tokens > max_tokens:
                break

            result.append(item)
            total_tokens += item_tokens

        return result
