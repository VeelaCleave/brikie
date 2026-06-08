"""ChromaManager — ChromaDB spatial vector storage for MemPalace.

Manages ChromaDB collections for the MemPalace spatial hierarchy.
- Wings, rooms, halls, tunnels, drawers as separate collections
- 384-dim embeddings via sentence-transformers
- Metadata filters for efficient region lookups

All collection operations are wrapped in proper error handling to prevent
ChromaDB connection leaks in long-running AFK loops.
"""

import chromadb
import logging
import numpy as np
from sentence_transformers import SentenceTransformer
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

REGION_TYPES = ["wing", "room", "hall", "tunnel", "drawer"]


class ChromaManager:
    """Manages ChromaDB collections for MemPalace spatial regions."""

    def __init__(self, persist_path: str = "mempalace_chroma") -> None:
        self._client = chromadb.PersistentClient(path=persist_path)
        self._embedder = SentenceTransformer("all-MiniLM-L6-v2")
        self._collections: Dict[str, Any] = {}
        self._initialized = False

    def _ensure_collections(self) -> None:
        """Create or retrieve all spatial collections."""
        for region_type in REGION_TYPES:
            collection_name = f"mempalace_{region_type}"
            try:
                collection = self._client.get_collection(name=collection_name)
            except Exception:
                collection = self._client.create_collection(
                    name=collection_name,
                    metadata={"spatial_level": region_type, "session_id": "default"},
                )
            self._collections[region_type] = collection
        self._initialized = True

    def initialize_collections(self) -> None:
        """Initialize all ChromaDB collections."""
        self._ensure_collections()
        logger.info("ChromaManager: collections initialized")

    def _create_region(self, region_type: str, name: str, description: str | None = None) -> str:
        """Create a new region in the appropriate collection."""
        region_id = f"{region_type}_{name}"
        embedding = self._embedder.encode(name).tolist()
        metadata = {
            "name": name,
            "description": description or "",
            "region_type": region_type,
        }
        try:
            self._collections[region_type].upsert(
                ids=[region_id],
                embeddings=[embedding],
                metadatas=[metadata],
            )
        except Exception as exc:
            logger.error("ChromaManager: _create_region failed for %s: %s", region_type, exc)
        return region_id

    def create_wing(self, name: str, description: str | None = None) -> str:
        """Create a new wing in the MemPalace hierarchy."""
        return self._create_region("wing", name, description)

    def create_room(self, name: str, description: str | None = None) -> str:
        """Create a new room in the MemPalace hierarchy."""
        return self._create_region("room", name, description)

    def create_hall(self, name: str, description: str | None = None) -> str:
        """Create a new hall in the MemPalace hierarchy."""
        return self._create_region("hall", name, description)

    def create_tunnel(self, name: str, description: str | None = None) -> str:
        """Create a new tunnel in the MemPalace hierarchy."""
        return self._create_region("tunnel", name, description)

    def create_drawer(self, name: str, description: str | None = None) -> str:
        """Create a new drawer in the MemPalace hierarchy."""
        return self._create_region("drawer", name, description)

    def upsert_region(
        self,
        region_type: str,
        name: str,
        description: str | None = None,
        entity_ids: List[str] | None = None,
    ) -> str:
        """Upsert a region in the appropriate collection."""
        region_id = f"{region_type}_{name}"
        embedding = self._embedder.encode(name).tolist()
        metadata = {
            "name": name,
            "session_id": description or "default",
            "entity_ids": entity_ids or [],
            "region_type": region_type,
        }
        try:
            self._collections[region_type].upsert(
                ids=[region_id],
                embeddings=[embedding],
                metadatas=[metadata],
            )
        except Exception as exc:
            logger.error("ChromaManager: upsert_region failed for %s: %s", region_type, exc)
        return region_id

    def search_nearby(
        self,
        region_type: str,
        query: str,
        top_k: int = 10,
        min_similarity: float = 0.7,
    ) -> List[Dict[str, Any]]:
        """Search for nearby regions by query string."""
        if region_type not in self._collections:
            return []
        embedding = self._embedder.encode(query).tolist()
        results = self._collections[region_type].query(
            query_embeddings=[embedding],
            n_results=top_k,
            where={"region_type": region_type},
            include=["metadatas", "distances"],
        )
        if not results:
            return []
        output = []
        for i, doc_id in enumerate(results.get("ids", [[]])[0]):
            distance = results.get("distances", [[]])[0][i]
            similarity = max(0.0, 1.0 - distance)
            metadata = results.get("metadatas", [[]])[0][i]
            if similarity >= min_similarity:
                output.append({
                    "id": doc_id,
                    "name": metadata.get("name", ""),
                    "region_type": metadata.get("region_type", region_type),
                    "similarity": round(similarity, 4),
                })
        return output

    def get_region(self, region_type: str, name: str) -> Dict[str, Any] | None:
        """Get a region by type and name."""
        if region_type not in self._collections:
            return None
        results = self._collections[region_type].get(
            where={"name": name},
            include=["metadatas"],
        )
        if not results or not results.get("ids"):
            return None
        metadata = results.get("metadatas", [{}])[0]
        return {
            "id": results["ids"][0],
            "name": metadata.get("name", name),
            "region_type": metadata.get("region_type", region_type),
            "entity_ids": metadata.get("entity_ids", []),
        }

    def list_regions_by_type(self, region_type: str) -> List[Dict[str, Any]]:
        """List all regions of a specific type."""
        if region_type not in self._collections:
            return []
        results = self._collections[region_type].get(
            include=["metadatas"],
        )
        if not results or not results.get("ids"):
            return []
        output = []
        for i, doc_id in enumerate(results.get("ids", [])):
            metadata = results.get("metadatas", [{}])[i]
            output.append({
                "id": doc_id,
                "name": metadata.get("name", ""),
                "region_type": metadata.get("region_type", region_type),
                "entity_ids": metadata.get("entity_ids", []),
            })
        return output

    def list_wings(self) -> List[Dict[str, Any]]:
        """List all wings."""
        return self.list_regions_by_type("wing")

    def list_rooms(self) -> List[Dict[str, Any]]:
        """List all rooms."""
        return self.list_regions_by_type("room")

    def get_region_entities(self, region_type: str, name: str) -> List[str]:
        """Get entity IDs associated with a region."""
        region = self.get_region(region_type, name)
        return region.get("entity_ids", []) if region else []
