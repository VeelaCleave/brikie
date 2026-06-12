"""LLM-driven soul actors for multi-head orchestration.

A SoulActor binds a Soul (persona manifest: system prompt + constraints)
to a Provider Brick, turning the static configuration into an agent that
can actually think. Phase C uses two actors during AFK mode:

- DreamerActor — mines diagnostics and formulates improvement proposals.
- ForemanActor — serves the ``foreman`` bus queue, evaluating each
  proposal against its constraints and publishing decisions back to the
  ``dreamer`` queue.

Kernel purity: souls, providers, and the event bus are duck-typed — this
module imports nothing from ``brikie.bricks``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Tuple

from brikie.config.types import BusEvent
from brikie.kernel.afk_protocol import Proposal

logger = logging.getLogger(__name__)

_VALID_DECISIONS = {"approve", "defer", "reject"}
_VALID_LEVELS = {"low", "medium", "high"}


def extract_json(text: str) -> Any:
    """Tolerantly extract a JSON object or array from LLM output.

    Handles raw JSON, markdown code fences, and JSON embedded in prose.
    Returns None when nothing parseable is found.
    """
    if not text:
        return None
    text = text.strip()

    # Strip a markdown code fence if the payload is wrapped in one.
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2:
            body = lines[1:]
            if body and body[-1].strip().startswith("```"):
                body = body[:-1]
            text = "\n".join(body).strip()

    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    # JSON embedded in prose: take the outermost bracket span that parses.
    candidates: List[Tuple[int, Any]] = []
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        start = text.find(open_ch)
        end = text.rfind(close_ch)
        if start != -1 and end > start:
            try:
                candidates.append((start, json.loads(text[start:end + 1])))
            except (json.JSONDecodeError, ValueError):
                continue
    if candidates:
        return min(candidates, key=lambda c: c[0])[1]
    return None


class SoulActor:
    """Binds a Soul persona to a Provider Brick for LLM-driven behavior."""

    def __init__(self, soul: Any, provider: Any) -> None:
        self._soul = soul
        self._provider = provider

    @property
    def name(self) -> str:
        return self._soul.name

    @property
    def soul(self) -> Any:
        return self._soul

    async def complete(self, user_content: str) -> str:
        """One system-prompt + user-message completion as this persona."""
        messages = [
            {"role": "system", "content": self._soul.system_prompt},
            {"role": "user", "content": user_content},
        ]
        result = await self._provider.get_completion(messages, [])
        content = result[0] or ""
        if not content and len(result) >= 3:
            # Reasoning models sometimes put everything in the thinking
            # channel; better to negotiate with that than with silence.
            content = (result[2] or {}).get("reasoning", "")
        return content


class DreamerActor(SoulActor):
    """Formulates improvement proposals from system diagnostics."""

    async def propose(self, context: str, max_proposals: int) -> List[Proposal]:
        """Ask the Dreamer persona for up to ``max_proposals`` proposals.

        Returns an empty list when the model produces nothing parseable —
        an honest idle cycle, never a fabricated one.
        """
        prompt = (
            f"Here is the current state of the system you inhabit:\n\n"
            f"{context}\n\n"
            f"Propose up to {max_proposals} concrete improvements — new "
            "capabilities, fixes for observed failures, efficiency gains, or "
            "quality-of-life features. Each proposal must be small enough "
            "for a single builder agent to complete with shell and file "
            "tools. When a proposal addresses an item from a listed source "
            "(e.g. a GitHub issue), set its \"source\" to that reference "
            "(e.g. \"github#42\"); otherwise omit it.\n\n"
            "Respond with ONLY a JSON array, no prose:\n"
            '[{"title": "...", "description": "...", '
            '"impact": "low|medium|high", "complexity": "low|medium|high", '
            '"source": "github#42 (optional)"}]'
        )
        raw = await self.complete(prompt)
        data = extract_json(raw)
        if not isinstance(data, list):
            logger.warning("Dreamer produced no parseable proposals: %r", raw[:200])
            return []

        proposals: List[Proposal] = []
        for item in data[:max_proposals]:
            if not isinstance(item, dict) or not item.get("title"):
                continue
            impact = str(item.get("impact", "low")).lower()
            complexity = str(item.get("complexity", "low")).lower()
            source = str(item.get("source") or "dreamer").strip() or "dreamer"
            proposals.append(Proposal(
                title=str(item["title"])[:200],
                description=str(item.get("description", "")),
                impact=impact if impact in _VALID_LEVELS else "low",
                complexity=complexity if complexity in _VALID_LEVELS else "low",
                source=source[:100],
            ))
        return proposals


class ForemanActor(SoulActor):
    """Serves the foreman bus queue, evaluating proposals from the Dreamer."""

    def __init__(self, soul: Any, provider: Any, event_bus: Any) -> None:
        super().__init__(soul, provider)
        self._bus = event_bus

    async def serve(self) -> None:
        """Consume the ``foreman`` queue until cancelled or shut down."""
        while True:
            event = await self._bus.consume("foreman")
            if event.event_type in ("system.shutdown", "error"):
                logger.info("Foreman actor stopping (%s).", event.event_type)
                return
            if event.event_type != "foreman.evaluate_proposal":
                logger.debug("Foreman ignoring event: %s", event.event_type)
                continue

            proposal = event.payload.get("proposal", {})
            attempt = event.payload.get("attempt", 1)
            try:
                decision, feedback = await self.evaluate(proposal, attempt)
            except Exception as exc:
                logger.exception("Foreman evaluation failed: %s", exc)
                decision, feedback = "reject", f"evaluation error: {exc}"

            await self._bus.publish(BusEvent(
                event_type="foreman.decision",
                source_soul="foreman",
                target_soul="dreamer",
                payload={
                    "decision": decision,
                    "feedback": feedback,
                    "proposal_id": proposal.get("proposal_id", ""),
                },
            ))

    async def evaluate(self, proposal: Dict[str, Any], attempt: int = 1) -> Tuple[str, str]:
        """Judge one proposal. Returns (decision, feedback)."""
        constraints = json.dumps(self._soul.behavioral_constraints, default=str)
        source = proposal.get("source", "dreamer")
        prompt = (
            "The Dreamer has submitted a proposal for your sign-off "
            f"(attempt {attempt}):\n\n"
            f"Title: {proposal.get('title', '')}\n"
            f"Impact: {proposal.get('impact', '?')} · "
            f"Complexity: {proposal.get('complexity', '?')} · "
            f"Source: {source}\n"
            f"Description: {proposal.get('description', '')}\n\n"
            f"Your behavioral constraints: {constraints}\n\n"
            "Approve only proposals that are concrete, in scope for a single "
            "builder agent with shell/file tools, and unlikely to destabilize "
            "the system. Defer (with actionable feedback) if the idea is good "
            "but underspecified. Reject anything vague, risky, or out of "
            "scope.\n\n"
            "Respond with ONLY a JSON object, no prose:\n"
            '{"decision": "approve|defer|reject", "feedback": "..."}'
        )
        raw = await self.complete(prompt)
        data = extract_json(raw)
        if not isinstance(data, dict):
            return "reject", f"unparseable evaluation: {raw[:120]}"

        decision = str(data.get("decision", "reject")).lower()
        if decision not in _VALID_DECISIONS:
            decision = "reject"
        return decision, str(data.get("feedback", ""))
