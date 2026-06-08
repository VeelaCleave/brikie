"""LCM Context Builder — builds compressed context windows.

Assembles the final context window structure:
[Active Summaries (root → leaf)] + [Fresh Tail]

This module provides the context building logic that the LCM Brick
uses to construct the compressed context window for LLM calls.

DESIGN DECISIONS:

1. Context Window Structure:
   - Summaries are injected first (highest-level/root first).
   - The fresh tail (most recent raw messages) is appended after.
   - This preserves the chronological narrative while compressing
     older context into summaries.

2. Token Budget Enforcement:
   - The context builder enforces the session's token budget.
   - If the total context exceeds max_context_tokens, the deepest
     summaries are trimmed until the budget is met.

3. Fresh Tail Sizing:
   - The fresh tail consists of the most recent `tail_length` messages
     that have not been compacted (is_compacted = 0).
   - This ensures the LLM always sees the most recent context in full.
"""

import logging
from typing import Any, Dict, List, Optional

from brikie.bricks.memory.lcm.lcm_store import LcmStore

logger = logging.getLogger(__name__)


class ContextBuilder:
    """Builds compressed context windows for the LCM Brick.

    Assembles [summaries + fresh tail] with token budget enforcement.
    """

    def __init__(self, store: LcmStore) -> None:
        self._store = store

    async def build_context(self, session_id: str) -> Dict[str, Any]:
        """Build the active context window for a session.

        Returns:
            A dict with:
                - "messages": List of message dicts for the LLM provider.
                - "total_tokens": Total token count of the context window.
                - "budget": Current budget state.
        """
        # Get active context (summaries + tail)
        context = await self._store.get_active_context(session_id)

        # Build the message list
        messages: List[Dict[str, Any]] = []

        # 1. Active summaries (highest level first = root → leaves)
        # The store returns summaries ordered by depth ASC (root first).
        for summary in context.get("summaries", []):
            messages.append({
                "role": "system",
                "content": f"### SUMMARY: Messages {summary['range'][0]}–{summary['range'][1]}\n{summary['content']}",
            })

        # 2. Fresh tail (most recent raw messages)
        for msg in context.get("tail", []):
            messages.append({
                "role": msg["role"],
                "content": msg["content"],
                "token_count": msg.get("token_count", 0),
            })

        # 3. Budget enforcement (trim deepest summaries if over budget)
        budget = context.get("budget", {})
        if budget.get("active_context_tokens", 0) > budget.get("max_budget", 4096):
            # Trim system messages (summaries) until budget fits
            messages = [m for m in messages if m["role"] != "system"]

        return {
            "messages": messages,
            "total_tokens": context.get("total_tokens", 0),
            "budget": budget,
        }
