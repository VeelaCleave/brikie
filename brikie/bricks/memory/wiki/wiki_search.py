"""BM25 Search Engine for the LLM Wiki Brick.

Provides full-text search over wiki pages using two separate
BM25Okapi indexes: one for body text and one for frontmatter fields.
Scores are combined with 0.7 weight for body and 0.3 for frontmatter.
"""

import logging
import re
from typing import AsyncGenerator, List, Tuple

from rank_bm25 import BM25Okapi

from brikie.bricks.memory.wiki.wiki_store import WikiStore

logger = logging.getLogger(__name__)


class WikiSearcher:
    """BM25-based search engine layered on top of a WikiStore instance.

    Two indexes are maintained:
    - Body index: tokenized body text of each page
    - Frontmatter index: tokenized frontmatter fields (title, status, tags, source)

    Search combines both scores: final = 0.7 * body_score + 0.3 * frontmatter_score
    """

    def __init__(self, store: WikiStore) -> None:
        self._store = store
        self._body_corpus: List[List[str]] = []
        self._fm_corpus: List[List[str]] = []
        self._page_ids: List[str] = []
        self._body_bm25: BM25Okapi | None = None
        self._fm_bm25: BM25Okapi | None = None

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """Lowercase text, strip punctuation, split on word boundaries."""
        return re.findall(r"\w+", text.lower())

    async def rebuild_index(self) -> None:
        """Iterate all pages and rebuild both BM25 indexes from scratch."""
        self._body_corpus.clear()
        self._fm_corpus.clear()
        self._page_ids.clear()

        page_list = []
        async for page in self._store.iter_pages():
            page_list.append(page)

        for page in page_list:
            self._page_ids.append(page["id"])

            # Tokenize body text
            body_tokens = self._tokenize(page.get("body", ""))
            self._body_corpus.append(body_tokens)

            # Tokenize frontmatter: title, status, tags, source
            fm_parts: List[str] = [
                page.get("title", ""),
                page.get("status", ""),
                page.get("source", ""),
            ]
            for tag in page.get("tags", []):
                fm_parts.append(tag)
            fm_tokens = self._tokenize(" ".join(fm_parts))
            self._fm_corpus.append(fm_tokens)

        # Build BM25 instances
        self._body_bm25 = BM25Okapi(self._body_corpus)
        self._fm_bm25 = BM25Okapi(self._fm_corpus)

        logger.info(
            "WikiSearcher: index rebuilt with %d pages",
            len(self._page_ids),
        )

    async def search(
        self,
        query: str,
        limit: int = 10,
        status: str | None = None,
        tags: List[str] | None = None,
    ) -> List[Tuple[dict, float]]:
        """Search the wiki by BM25 score, optionally filtering by status and tags.

        Returns a list of (page_dict, combined_score) tuples sorted by score descending.
        """
        if not self._page_ids:
            return []

        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        # Compute raw BM25 scores for every page
        body_scores = self._body_bm25.get_scores(query_tokens)
        fm_scores = self._fm_bm25.get_scores(query_tokens)

        # Build indexed scores: (id, combined_score)
        indexed_scores: List[Tuple[str, float]] = []
        for i, page_id in enumerate(self._page_ids):
            combined = 0.7 * body_scores[i] + 0.3 * fm_scores[i]
            indexed_scores.append((page_id, combined))

        # Filter by status/tags if requested
        if status is not None or tags is not None:
            allowed = set()
            filtered_pages = await self._store.list_pages(
                status=status, tags=tags
            )
            for pg in filtered_pages:
                allowed.add(pg["id"])

            indexed_scores = [
                (pid, score) for pid, score in indexed_scores if pid in allowed
            ]

        # Filter out non-matching results (0.0 means no token overlap)
        indexed_scores = [
            (pid, score) for pid, score in indexed_scores if score != 0.0
        ]

        # Sort by descending score, take top `limit`
        indexed_scores.sort(key=lambda x: x[1], reverse=True)
        top = indexed_scores[:limit]

        # Fetch full page dicts for the top results
        results: List[Tuple[dict, float]] = []
        for pid, score in top:
            page = await self._store.get_page(pid)
            if page is not None:
                results.append((page, score))

        return results
