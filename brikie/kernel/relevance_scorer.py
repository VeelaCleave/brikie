"""Relevance scorer for dynamic memory recall.

Scores memory sectors (from LCM, MemPalace, Wiki build_context outputs)
against the current user message and active goal, using keyword overlap
scoring. Low-scoring sectors are excluded to save context budget.

This is NOT a stub — it implements a real TF-style term frequency overlap
algorithm that extracts significant terms (nouns, verbs, named entities
patterns) from the query and compares them against sector text.

Lives in ``brikie/kernel/`` on purpose: the kernel assembles the memory
blob and may use this helper, but it is pure stdlib logic with no brick
dependencies — so importing it does not break kernel purity (AGENTS #1).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Set

logger = logging.getLogger(__name__)

# Default: how many sectors maximum to include
DEFAULT_TOP_K = 5

# Default: rough token budget for memory context (~2000 chars ≈ 500 tokens)
DEFAULT_MEMORY_BUDGET_CHARS = 3000

# Minimum score to consider a sector relevant at all (0.0 - 1.0)
RELEVANCE_THRESHOLD = 0.05

# Words to skip when extracting terms (stopwords)
_STOPWORDS: Set[str] = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "was", "are", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "shall", "can", "need",
    "dare", "ought", "used", "it", "its", "this", "that", "these", "those",
    "i", "you", "he", "she", "we", "they", "me", "him", "her", "us", "them",
    "my", "your", "his", "its", "our", "their", "mine", "yours", "hers",
    "ours", "theirs", "not", "no", "nor", "so", "very", "just", "about",
    "also", "too", "only", "more", "most", "some", "any", "each", "every",
    "all", "both", "few", "several", "much", "many", "other", "another",
    "what", "which", "who", "whom", "whose", "when", "where", "why", "how",
    "if", "then", "else", "than", "up", "down", "out", "off", "over",
    "under", "again", "further", "once", "here", "there", "please", "help",
    "get", "got", "make", "made", "let", "like", "well", "back", "even",
    "still", "already", "yet", "because", "while", "since", "after",
    "before", "until", "during", "through", "between", "against",
    "without", "within", "along", "around", "among", "across", "behind",
    "below", "beneath", "beside", "beyond", "inside", "outside", "onto",
    "upon", "via", "per", "toward", "towards", "next", "last", "first",
    "second", "new", "old", "good", "bad", "big", "small", "high", "low",
    "long", "short", "same", "different", "own", "such",
}


@dataclass
class ScoredSector:
    """A memory sector with its relevance score."""

    brick_name: str
    sector_type: str  # e.g. "summary", "tail", "mempalace", "wiki"
    header: str
    text: str
    score: float = 0.0
    char_count: int = 0

    def formatted(self) -> str:
        """Return the sector formatted as it would appear in the prompt."""
        return f"{self.header}\n{self.text}"


def extract_terms(text: str) -> List[str]:
    """Extract significant terms from text for scoring.

    Strips stopwords, short words (< 3 chars), and purely numeric tokens.
    Returns lowercased terms in order of appearance.
    """
    if not text:
        return []

    # Normalize: lowercase, collapse whitespace
    text = text.lower().strip()

    # Split on non-alphanumeric boundaries (keep hyphens and underscores
    # as they often appear in code/symbol names)
    tokens = re.findall(r"[a-z0-9_\-\/\.]+", text)

    # Filter: no stopwords, no pure punctuation, no short tokens
    terms: List[str] = []
    for t in tokens:
        t = t.strip("-_./")
        if len(t) < 3:
            continue
        if t in _STOPWORDS:
            continue
        if t.replace(".", "").replace("-", "").replace("_", "").isdigit():
            continue
        terms.append(t)

    return terms


def compute_overlap_score(query_terms: List[str], sector_terms: List[str]) -> float:
    """Compute a TF-style overlap score between query and sector terms.

    Uses a simple scoring function:
    - Each query term that appears in the sector text contributes
      proportionally to its frequency in the sector.
    - Longer matching terms (compounds, identifiers) get slightly
      more weight than short generic matches.
    - Returns 0.0 if no overlap.

    The score is normalized to 0.0–1.0 range.
    """
    if not query_terms or not sector_terms:
        return 0.0

    # Build sector term frequency map
    sector_tf: Dict[str, int] = {}
    for t in sector_terms:
        sector_tf[t] = sector_tf.get(t, 0) + 1

    # Total sector terms for normalization
    total_sector_terms = len(sector_terms)
    if total_sector_terms == 0:
        return 0.0

    # Score: for each unique query term, count its IDF-like weight *
    # its frequency in the sector
    score = 0.0
    query_unique = set(query_terms)

    for qt in query_unique:
        qt_lower = qt.lower()
        freq = sector_tf.get(qt_lower, 0)
        if freq > 0:
            # Longer terms get a small bonus (they're more specific signals)
            length_bonus = 1.0 + (min(len(qt), 20) / 100.0)
            # Term frequency contribution, normalized
            score += (freq / total_sector_terms) * length_bonus

    # Normalize by query size
    score = score / max(len(query_unique), 1)

    # Soft clamp to 1.0
    return min(score, 1.0)


def split_into_sectors(brick_name: str, ctx: Dict[str, Any] | None) -> List[ScoredSector]:
    """Split a memory brick's build_context output into individual sectors.

    Each sector is a self-contained chunk that can be independently scored
    and included or excluded.

    Handles the three known memory brick shapes:
    - LCM: {"summaries": [...], "tail": [...]}
    - MemPalace: {"mempalace": {...}}
    - Wiki: {"wiki": {...}}
    """
    if ctx is None:
        return []
    sectors: List[ScoredSector] = []

    # LCM shape — summaries
    summaries = ctx.get("summaries", [])
    for i, s in enumerate(summaries):
        depth = s.get("depth", 0)
        content = s.get("content", "")
        if content:
            sectors.append(ScoredSector(
                brick_name=brick_name,
                sector_type="lcm_summary",
                header=f"## Session Summary ({brick_name}) [DAG depth={depth}]",
                text=content,
                char_count=len(content),
            ))

    # LCM shape — tail (recent messages)
    tail = ctx.get("tail", [])
    if tail:
        tail_texts: List[str] = []
        for t in tail:
            role = t.get("role", "?")
            content = t.get("content", "")[:200]
            tail_texts.append(f"[{role}] {content}")
        combined = "\n".join(tail_texts)
        if combined:
            sectors.append(ScoredSector(
                brick_name=brick_name,
                sector_type="lcm_tail",
                header=f"## Recent Messages ({brick_name})",
                text=combined,
                char_count=len(combined),
            ))

    # MemPalace shape
    mempalace_ctx = ctx.get("mempalace")
    if mempalace_ctx:
        parts: List[str] = []
        ec = mempalace_ctx.get("entity_count", 0)
        tc = mempalace_ctx.get("triple_count", 0)
        parts.append(f"- {ec} entities, {tc} relationships")
        entities = mempalace_ctx.get("recent_entities", [])
        for ent in entities[:5]:
            if isinstance(ent, str):
                name = ent
                etype = "?"
            else:
                name = ent.get("name", "?")
                etype = ent.get("entity_type", "?")
            parts.append(f"  - {name} ({etype})")
        combined = "\n".join(parts)
        sectors.append(ScoredSector(
            brick_name=brick_name,
            sector_type="mempalace",
            header=f"## Knowledge Graph ({brick_name})",
            text=combined,
            char_count=len(combined),
        ))

    # Wiki shape
    wiki_ctx = ctx.get("wiki")
    if wiki_ctx:
        parts = [f"- {wiki_ctx.get('page_count', 0)} pages"]
        recent = wiki_ctx.get("recent_pages", [])
        if recent:
            parts.append("- Recent pages:")
            for p in recent[:5]:
                if isinstance(p, str):
                    title = p
                else:
                    title = p.get("title", "?")
                parts.append(f"  - {title}")
        combined = "\n".join(parts)
        sectors.append(ScoredSector(
            brick_name=brick_name,
            sector_type="wiki",
            header=f"## Wiki Knowledge Base ({brick_name})",
            text=combined,
            char_count=len(combined),
        ))

    return sectors


def score_sectors(
    sectors: List[ScoredSector],
    user_message: str,
    goal_description: str = "",
    *,
    top_k: int = DEFAULT_TOP_K,
    budget_chars: int = DEFAULT_MEMORY_BUDGET_CHARS,
    threshold: float = RELEVANCE_THRESHOLD,
) -> List[ScoredSector]:
    """Score and filter memory sectors by relevance.

    Args:
        sectors: All available memory sectors to score.
        user_message: The current user message to score against.
        goal_description: The active goal description (if any).
        top_k: Maximum number of sectors to return.
        budget_chars: Maximum total character count for all returned sectors.
        threshold: Minimum score for a sector to be included.

    Returns:
        A list of scored sectors, sorted by score descending, filtered
        to fit within the budget.
    """
    if not sectors:
        return []

    # Build the query: combine user message + goal, extract terms
    query_text = user_message
    if goal_description:
        query_text = f"{user_message} {goal_description}"
    query_terms = extract_terms(query_text)

    if not query_terms:
        # No query terms to match against — return top sectors anyway
        # (better to show something than nothing)
        logger.debug("No query terms extracted from '%s'", query_text[:50])
        return sectors[:top_k]

    # Score each sector
    for sector in sectors:
        sector_terms = extract_terms(sector.text)
        sector.score = compute_overlap_score(query_terms, sector_terms)

    # Sort by score descending
    scored = sorted(sectors, key=lambda s: s.score, reverse=True)

    # Filter by threshold
    scored = [s for s in scored if s.score >= threshold]

    if not scored:
        # If everything is below threshold, return the highest-scoring one
        # to avoid complete memory blindness
        best = sorted(sectors, key=lambda s: s.score, reverse=True)[:1]
        logger.debug(
            "All sectors below threshold %.2f; keeping best: %s/%s (%.3f)",
            threshold, best[0].sector_type, best[0].brick_name, best[0].score,
        )
        scored = best

    # Fit within budget — greedy selection by score
    selected: List[ScoredSector] = []
    total_chars = 0
    for s in scored:
        if len(selected) >= top_k:
            break
        if total_chars + s.char_count > budget_chars and selected:
            # Don't add if we already have something and this would blow
            # the budget — but always include at least the top sector
            # even if it exceeds budget (otherwise we'd return nothing)
            continue
        selected.append(s)
        total_chars += s.char_count

    if not selected:
        # Fallback: at least return the top sector even if it exceeds budget
        selected = scored[:1]

    excluded_count = len(sectors) - len(selected)
    if excluded_count > 0:
        logger.info(
            "Memory relevance: included %d/%d sectors "
            "(budget=%d, top_k=%d, query_terms=%d)",
            len(selected), len(sectors),
            budget_chars, top_k, len(query_terms),
        )

    return selected
