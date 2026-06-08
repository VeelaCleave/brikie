"""Entity Extractor — NLP-based entity and triple extraction.

Extracts entities, triples, and spatial mappings from messages.
Uses regex patterns and lightweight NLP for zero-overhead extraction.
"""

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Entity Types
# ---------------------------------------------------------------------------

class EntityType(str, Enum):
    PERSON = "person"
    PROJECT = "project"
    TOOL = "tool"
    CONCEPT = "concept"
    DECISION = "decision"
    MILESTONE = "milestone"

# ---------------------------------------------------------------------------
# Extraction Results
# ---------------------------------------------------------------------------

@dataclass
class ExtractedEntity:
    """A single extracted entity."""
    name: str
    entity_type: EntityType
    description: str = ""
    confidence: float = 0.7

@dataclass
class ExtractedTriple:
    """A single extracted triple (subject, predicate, object)."""
    subject: str
    predicate: str
    object: str
    confidence: float = 0.7
    source_text: str = ""

@dataclass
class ExtractionResult:
    """Result of extracting entities/triples from a message."""
    entities: List[ExtractedEntity] = field(default_factory=list)
    triples: List[ExtractedTriple] = field(default_factory=list)
    wing: str = "default"
    room: str = "general"
    hall: str = "hall_events"

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# Named entity patterns (simple regex-based detection)
ENTITY_PATTERNS = [
    # People: "Alice", "Bob", "John Doe", "the user", "the developer"
    (r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b', EntityType.PERSON),
    # Projects: "project-alpha", "brikie", "the project"
    (r'\b(project[-_\w]+|brikie|agent[-_\w]+)\b', EntityType.PROJECT),
    # Tools/Libraries: "SQLite", "Playwright", "ChromaDB"
    (r'\b(SQLite|Playwright|ChromaDB|PyTorch|React|Django|Flask|Redis|PostgreSQL)\b', EntityType.TOOL),
    # Concepts: "authentication", "middleware", "context window"
    (r'\b(authentication|authorization|middleware|compaction|context\s*window|memory|orchestration)\b', EntityType.CONCEPT),
    # Decisions: "decided", "chose", "selected"
    (r'\b(decid(ed|e?s|ed\s+on|ing)|chose|selected|adopt(ed|ing)|settled\s+on)\b', EntityType.DECISION),
    # Milestones: "completed", "launched", "deployed", "merged"
    (r'\b(complet(ed|e?s|ion)|launched|deployed|merged|release(d|s))\b', EntityType.MILESTONE),
]

# Triple patterns (subject - predicate - object)
TRIPLE_PATTERNS = [
    # "X depends on Y"
    (r'(\w+(?:\s+\w+)?)\s+depends\s+on\s+(\w+(?:\s+\w+)?)', "depends_on"),
    # "X extends Y"
    (r'(\w+(?:\s+\w+)?)\s+extends\s+(\w+(?:\s+\w+)?)', "extends"),
    # "X blocks Y"
    (r'(\w+(?:\s+\w+)?)\s+blocks\s+(\w+(?:\s+\w+)?)', "blocks"),
    # "X is part of Y"
    (r'(\w+(?:\s+\w+)?)\s+is\s+part\s+of\s+(\w+(?:\s+\w+)?)', "is_part_of"),
    # "X relates to Y"
    (r'(\w+(?:\s+\w+)?)\s+relates?\s+to\s+(\w+(?:\s+\w+)?)', "relates_to"),
    # "X implements Y"
    (r'(\w+(?:\s+\w+)?)\s+implements?\s+(\w+(?:\s+\w+)?)', "implements"),
    # "X uses Y"
    (r'(\w+(?:\s+\w+)?)\s+uses?\s+(\w+(?:\s+\w+)?)', "uses"),
    # "X creates Y"
    (r'(\w+(?:\s+\w+)?)\s+(creates?|makes?)\s+(\w+(?:\s+\w+)?)', "creates"),
    # "X calls Y"
    (r'(\w+(?:\s+\w+)?)\s+calls?\s+(\w+(?:\s+\w+)?)', "calls"),
    # "X requires Y"
    (r'(\w+(?:\s+\w+)?)\s+requires?\s+(\w+(?:\s+\w+)?)', "requires"),
]

# Wing/Room/Hall patterns
WING_PATTERNS = [
    (r'\b(project[-_]\w+)\b', "wing"),
    (r'\b(domain[-_]\w+)\b', "wing"),
]

ROOM_PATTERNS = [
    (r'\b(room[-_]\w+)\b', "room"),
    (r'\b(module[-_]\w+)\b', "room"),
    (r'\b(feature[-_]\w+)\b', "room"),
]


class EntityExtractor:
    """Extracts entities and triples from messages using NLP patterns.

    Provides lightweight entity extraction without heavy dependencies.
    Uses regex patterns, keyword matching, and simple heuristics.
    """

    def __init__(self) -> None:
        self._entity_patterns = [
            (re.compile(pattern), etype) for pattern, etype in ENTITY_PATTERNS
        ]
        self._triple_patterns = [
            (re.compile(pattern), predicate) for pattern, predicate in TRIPLE_PATTERNS
        ]
        self._entity_cache: Dict[str, ExtractedEntity] = {}

    def extract(self, content: str, session_id: str = "default") -> ExtractionResult:
        """Extract entities and triples from a message.

        Args:
            content: The message content to extract from.
            session_id: Session identifier for grouping.

        Returns:
            ExtractionResult with entities, triples, and spatial mapping.
        """
        entities = self._extract_entities(content)
        triples = self._extract_triples(content, entities)
        wing, room, hall = self._extract_spatial_mapping(content, entities)

        return ExtractionResult(
            entities=entities,
            triples=triples,
            wing=wing,
            room=room,
            hall=hall,
        )

    def _extract_entities(self, content: str) -> List[ExtractedEntity]:
        """Extract named entities from content using regex patterns."""
        found: Dict[str, ExtractedEntity] = {}

        for pattern, entity_type in self._entity_patterns:
            matches = pattern.finditer(content)
            for match in matches:
                name = match.group(0).strip().lower()
                key = (name, entity_type.value)

                if key not in found:
                    # Build description from context
                    start = max(0, match.start() - 20)
                    end = min(len(content), match.end() + 20)
                    context = content[start:end].strip()

                    found[key] = ExtractedEntity(
                        name=name,
                        entity_type=entity_type,
                        description=context,
                        confidence=0.75,
                    )

        return list(found.values())

    def _extract_triples(
        self,
        content: str,
        entities: List[ExtractedEntity],
    ) -> List[ExtractedTriple]:
        """Extract triples (subject-predicate-object) from content."""
        triples: List[ExtractedTriple] = []
        entity_names = {e.name for e in entities}

        for pattern, predicate in self._triple_patterns:
            matches = pattern.finditer(content)
            for match in matches:
                groups = match.groups()
                if len(groups) >= 2:
                    subject = groups[0].strip().lower()
                    obj = groups[1].strip().lower()

                    # Boost confidence if both entities were extracted
                    confidence = 0.6
                    if subject in entity_names and obj in entity_names:
                        confidence = 0.85
                    elif subject in entity_names or obj in entity_names:
                        confidence = 0.7

                    triples.append(ExtractedTriple(
                        subject=subject,
                        predicate=predicate,
                        object=obj,
                        confidence=confidence,
                        source_text=match.group(0),
                    ))

        return triples

    def _extract_spatial_mapping(
        self,
        content: str,
        entities: List[ExtractedEntity],
    ) -> Tuple[str, str, str]:
        """Determine the spatial location (wing/room/hall) for content."""
        wing = "default"
        room = "general"
        hall = "hall_events"

        # Determine hall based on content type
        if any(kw in content.lower() for kw in ["decision", "decided", "chose"]):
            hall = "hall_decisions"
        elif any(kw in content.lower() for kw in ["milestone", "completed", "launched"]):
            hall = "hall_milestones"
        elif any(kw in content.lower() for kw in ["fact", "note", "observation"]):
            hall = "hall_facts"
        elif any(kw in content.lower() for kw in ["discovery", "found", "learned"]):
            hall = "hall_discoveries"

        # Determine wing from project references
        for pattern, _ in WING_PATTERNS:
            match = re.search(pattern, content.lower())
            if match:
                wing = match.group(1)
                break

        # Determine room from module/feature references
        for pattern, _ in ROOM_PATTERNS:
            match = re.search(pattern, content.lower())
            if match:
                room = match.group(1)
                break

        return wing, room, hall

    def extract_batch(self, messages: List[Dict[str, Any]]) -> List[ExtractionResult]:
        """Extract entities/triples from a batch of messages.

        Args:
            messages: List of message dicts with 'role' and 'content' keys.

        Returns:
            List of ExtractionResult objects, one per message.
        """
        results = []
        for msg in messages:
            content = msg.get("content", "")
            session_id = msg.get("session_id", "default")
            results.append(self.extract(content, session_id))
        return results
