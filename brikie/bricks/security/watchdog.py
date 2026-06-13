"""WatchdogSecurityBrick (BRK-820) — LLM-based reviewer for risky tool calls.

Intercepts the PRE_TOOL hook and asks the seated provider (LLM) whether a
high-risk tool call should be ALLOWed, BLOCKed, or REVISEd — a judgment
layer above the regex-based CommandFirewall (BRK-800).

Design:
- Subclasses SecurityBrick (BRK-160): reuses PRE_TOOL hook registration,
  per-ToolCall handling, audit logging, and the tc.result veto channel
  (process_tool_calls skips any call a PRE_TOOL hook already settled).
- Only *execution / irreversible / external* tools are candidates for
  review (bash_execute, the registry mutators, browser_evaluate). File
  writes are intentionally not default-reviewed — they're reversible and
  the firewall already covers destructive shell content. Configurable.
- bash_execute is pre-screened: a benign command (ls, cat, pytest, git
  status) skips the LLM entirely. Only commands containing risk-trigger
  words (rm, dd, curl, sudo, pip install, git push, …) cost a round-trip.
  Without this, every shell call would double the agent's latency.
- Fail-open EVERYWHERE: no provider, a provider error/timeout, or an
  unparseable verdict all resolve to ALLOW. A reviewer must never be able
  to wedge the agent (this brick exists because an over-eager gate did).
- Verdicts are cached per turn for identical tool+args. REVISE sets the
  tool result to the guidance (so the model can retry) and stages a
  pop_realignment() nudge for user-visible notification; after N revisions
  it escalates to BLOCK.
- Toggle with BRIKIE_WATCHDOG=0 (or BRIKIE_DISCIPLINE=0).

Limitation (MVP): the reviewer defaults to the *same* provider as the main
agent, so it catches egregious/risky actions but not subtle drift the main
model also rationalises. Real value scales when a stronger model is wired
to the reviewer slot.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from brikie.bricks.security.base import (
    SecurityBrick,
    SecurityDecision,
    _normalize_tool_calls,
)
from brikie.config.types import HookType

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────
# Risk policy
# ──────────────────────────────────────────────────────────────────────────

# Tool names that are candidates for LLM review. Kept tight on purpose:
# execution, irreversible, or external-effect actions only. Reviewing every
# write_file/edit_file would tax a coding agent for little gain (they're
# reversible and the firewall covers destructive shell content) — add them
# via ``risky_patterns`` if you want paranoid mode.
_DEFAULT_RISKY_PATTERNS: List[str] = [
    r"^bash_execute$",
    r"^registry_install$",
    r"^registry_create_brick$",
    r"^registry_publish$",
    r"^registry_uninstall$",
    r"^browser_evaluate$",
]

# Tools whose command argument is pre-screened so benign shell calls skip
# the LLM round-trip entirely.
_SHELL_TOOLS = {"bash_execute", "bash", "shell", "sh"}

# A shell command is only worth reviewing if it mentions something with
# real blast radius. Benign commands (ls, cat, pytest, grep, git status,
# python script.py) match none of these and are allowed without a round-trip.
_BASH_REVIEW_TRIGGERS = re.compile(
    r"\b(rm|rmdir|dd|mkfs|shred|truncate|chmod|chown|chgrp|mv|ln|"
    r"kill|pkill|killall|curl|wget|nc|ncat|ssh|scp|rsync|sudo|su|doas|"
    r"pip|pip3|npm|yarn|pnpm|apt|apt-get|dpkg|yum|dnf|brew|gem|cargo|"
    r"systemctl|service|launchctl|mount|umount|crontab|"
    r"passwd|chpasswd|useradd|userdel|usermod|groupadd|"
    r"reboot|shutdown|halt|poweroff|"
    r"iptables|ufw|firewall-cmd|"
    r"docker|kubectl|helm|terraform|"
    r"eval|exec|source)\b",
    re.IGNORECASE,
)
# Also review remote pushes, pipes-to-shell, and redirects into system dirs.
_BASH_REVIEW_EXTRA = re.compile(
    r"git\s+push|\|\s*(?:ba)?sh\b|>\s*/(?:etc|dev|usr|bin|boot|sys|var)\b",
    re.IGNORECASE,
)

# ──────────────────────────────────────────────────────────────────────────
# Verdicts
# ──────────────────────────────────────────────────────────────────────────

_VERDICT_ALLOW = "ALLOW"
_VERDICT_BLOCK = "BLOCK"
_VERDICT_REVISE = "REVISE"

# After this many REVISE verdicts for the same tool+args in one turn,
# escalate to BLOCK so the agent can't be told to "revise" forever.
_MAX_REVISIONS_PER_TURN = 3


def _disabled() -> bool:
    """True if the watchdog is turned off via env (BRIKIE_WATCHDOG/DISCIPLINE)."""
    val = os.environ.get(
        "BRIKIE_WATCHDOG", os.environ.get("BRIKIE_DISCIPLINE", "1")
    )
    return val.strip() in ("0", "false", "no")


def _hash_call(name: str, args: Dict[str, Any]) -> str:
    """Deterministic hash of a tool call for verdict caching."""
    try:
        raw = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = str(args)
    return hashlib.sha256(f"{name}:{raw}".encode()).hexdigest()


# ──────────────────────────────────────────────────────────────────────────
# Brick
# ──────────────────────────────────────────────────────────────────────────


class WatchdogSecurityBrick(SecurityBrick):
    """LLM reviewer that gates risky tool calls via provider judgment.

    BRICK_NUMBER = "BRK-820"
    """

    BRICK_NUMBER = "BRK-820"

    def __init__(
        self,
        registry: Any = None,
        risky_patterns: Optional[List[str]] = None,
    ) -> None:
        """Initialise the watchdog.

        Args:
            registry: BrickRegistry, auto-injected by the BuildLoader. Used
                to locate the provider, diagnostics collector, and goal.
            risky_patterns: Regex patterns for tool names to review.
                Defaults to execution/irreversible/external tools.
        """
        super().__init__()
        self._name = "watchdog"
        self._registry = registry
        self._risky = [
            re.compile(p) for p in (risky_patterns or _DEFAULT_RISKY_PATTERNS)
        ]
        # Per-turn caches (cleared on each PRE_TOOL dispatch).
        self._verdict_cache: Dict[str, Tuple[str, str]] = {}
        self._revision_counts: Dict[str, int] = {}
        self._diagnostics: Any = None
        self._pending_realignment: Optional[str] = None
        self._last_reason: str = "Blocked by watchdog reviewer"
        self._last_rule: str = "watchdog:default"

    # ── Lifecycle ───────────────────────────────────────────────────

    async def init(self) -> None:
        await super().init()
        self._diagnostics = self._find_diagnostics()
        logger.info(
            "WatchdogSecurityBrick started (risky=%d, diagnostics=%s, disabled=%s).",
            len(self._risky), "on" if self._diagnostics else "off", _disabled(),
        )

    async def shutdown(self) -> None:
        self._verdict_cache.clear()
        self._revision_counts.clear()
        self._pending_realignment = None
        await super().shutdown()
        logger.info("WatchdogSecurityBrick shut down.")

    # ── Hook: PRE_TOOL ──────────────────────────────────────────────

    async def get_hook_callbacks(self) -> Dict[HookType, List[callable]]:
        """Register the PRE_TOOL review callback (skips when disabled)."""
        async def on_pre_tool(data: Any) -> None:
            if _disabled():
                return
            self._verdict_cache.clear()
            await self._on_pre_tool(data)

        return {HookType.PRE_TOOL: [on_pre_tool]}

    async def _on_pre_tool(self, data: Any) -> None:
        """Review each proposed call and settle tc.result on BLOCK/REVISE."""
        for tc in _normalize_tool_calls(data):
            name = getattr(tc, "name", "")
            args = getattr(tc, "args", {}) or {}
            verdict, guidance = await self._review(name, args)

            if verdict == _VERDICT_ALLOW:
                continue  # leave tc.result None → the call executes

            if verdict == _VERDICT_REVISE:
                # Surface the guidance as the result so the model can retry
                # with corrected params this turn, and notify the user.
                self._last_reason = guidance or "Revision requested"
                self._last_rule = f"watchdog_revise:{name}"
                tc.result = (
                    f"Revise: {guidance or 'adjust this call and retry.'}"
                )
                self._pending_realignment = self._realignment_msg(name, guidance)
                self._log_blocked(tc)
            else:  # BLOCK
                self._last_reason = guidance or "Blocked by watchdog reviewer"
                self._last_rule = f"watchdog_block:{name}"
                self._log_blocked(tc)
                tc.result = self._blocked_error(tc)

    # ── Realignment drain (event loop calls this once per round) ─────

    def pop_realignment(self) -> Optional[str]:
        """Return and clear the staged REVISE nudge, if any."""
        nudge = self._pending_realignment
        self._pending_realignment = None
        return nudge

    # ── SecurityBrick contract ──────────────────────────────────────

    async def evaluate(
        self, tool_name: str, args: Dict[str, Any], session_id: str = "",
    ) -> SecurityDecision:
        """ALLOW if reviewed-allowed (or not a review target), else BLOCK.

        REVISE collapses to BLOCK here so the base contract holds; the
        richer REVISE behaviour lives in ``_on_pre_tool``.
        """
        verdict, _ = await self._review(tool_name, args)
        return (
            SecurityDecision.ALLOW if verdict == _VERDICT_ALLOW
            else SecurityDecision.BLOCK
        )

    # ── Review pipeline ─────────────────────────────────────────────

    async def _review(
        self, tool_name: str, args: Dict[str, Any],
    ) -> Tuple[str, str]:
        """Decide a verdict for a call (cache → risk screen → LLM judge)."""
        if not self._is_review_target(tool_name, args):
            return (_VERDICT_ALLOW, "")

        key = _hash_call(tool_name, args)
        if key in self._verdict_cache:
            return self._verdict_cache[key]

        verdict, guidance = await self._llm_judge(tool_name, args)

        if verdict == _VERDICT_REVISE:
            count = self._revision_counts.get(key, 0) + 1
            self._revision_counts[key] = count
            if count > _MAX_REVISIONS_PER_TURN:
                logger.warning(
                    "Watchdog: %s revised %d× — escalating to BLOCK.",
                    tool_name, count,
                )
                verdict, guidance = (
                    _VERDICT_BLOCK,
                    f"Blocked after {count} revision attempts — reassess "
                    "your approach.",
                )

        self._verdict_cache[key] = (verdict, guidance)
        return verdict, guidance

    def _is_review_target(self, tool_name: str, args: Dict[str, Any]) -> bool:
        """True if this call should be sent to the LLM reviewer."""
        if not any(p.match(tool_name) for p in self._risky):
            return False
        # Pre-screen shell commands so benign ones skip the round-trip.
        if tool_name in _SHELL_TOOLS:
            command = ""
            if isinstance(args, dict):
                command = str(args.get("command") or args.get("cmd") or "")
            return bool(
                _BASH_REVIEW_TRIGGERS.search(command)
                or _BASH_REVIEW_EXTRA.search(command)
            )
        return True

    async def _llm_judge(
        self, tool_name: str, args: Dict[str, Any],
    ) -> Tuple[str, str]:
        """Ask the provider for a verdict. Fail-open (ALLOW) on any error."""
        provider = self._find_provider()
        if provider is None:
            logger.warning("Watchdog: no provider — allowing %s.", tool_name)
            return (_VERDICT_ALLOW, "")

        goal = await self._get_goal_context()
        prompt = self._build_judgment_prompt(tool_name, args, goal)
        try:
            result = await provider.get_completion(
                [
                    {"role": "system", "content": _JUDGMENT_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                [],
            )
        except Exception as exc:
            logger.warning(
                "Watchdog judge failed for %s: %s — allowing (fail-open).",
                tool_name, exc,
            )
            self._emit("watchdog_error", {"tool_name": tool_name, "error": str(exc)})
            return (_VERDICT_ALLOW, "")

        content = result[0] if isinstance(result, tuple) else result
        verdict, guidance = self._parse_verdict(str(content or ""))
        logger.info(
            "Watchdog verdict for %s: %s — %s",
            tool_name, verdict, (guidance[:80] if guidance else "(none)"),
        )
        self._emit("watchdog_verdict", {
            "tool_name": tool_name, "verdict": verdict, "guidance": guidance,
        })
        return verdict, guidance

    # ── Prompt + parsing ────────────────────────────────────────────

    @staticmethod
    def _build_judgment_prompt(
        tool_name: str, args: Dict[str, Any], goal: str,
    ) -> str:
        args_str = json.dumps(args, indent=2, default=str)
        parts = [
            f"Proposed tool call: {tool_name}",
            f"Arguments:\n```json\n{args_str}\n```",
        ]
        if goal:
            parts.append(f"\nActive goal:\n{goal}")
        return "\n".join(parts)

    @staticmethod
    def _realignment_msg(tool_name: str, guidance: str) -> str:
        return (
            f"⚠️ Watchdog asked you to REVISE {tool_name}: "
            f"{guidance or 'adjust the call and retry.'}"
        )

    @staticmethod
    def _parse_verdict(text: str) -> Tuple[str, str]:
        """Parse a verdict, requiring it to LEAD the response.

        Accepted: ``ALLOW`` / ``BLOCK(reason: …)`` / ``REVISE(guidance: …)``,
        verdict word first. Anything else fails open to ALLOW — a reviewer
        must never block on ambiguous prose (that's what wedges agents).
        """
        cleaned = text.strip()
        if not cleaned:
            return (_VERDICT_ALLOW, "")
        upper = cleaned.upper()
        for verdict in (_VERDICT_BLOCK, _VERDICT_REVISE, _VERDICT_ALLOW):
            if upper.startswith(verdict):
                rest = cleaned[len(verdict):].strip()
                if rest.startswith("(") and rest.endswith(")"):
                    inner = rest[1:-1].strip()
                    if ":" in inner:
                        _key, _, value = inner.partition(":")
                        return verdict, value.strip()
                    return verdict, inner
                return verdict, rest.lstrip(":-—. ").strip()
        # Unparseable → fail-open.
        return (_VERDICT_ALLOW, "")

    # ── Registry helpers (duck-typed; no brick imports) ─────────────

    def _find_provider(self) -> Any:
        for brick in getattr(self._registry, "_bricks", {}).values():
            if brick is not self and hasattr(brick, "get_completion"):
                return brick
        return None

    def _find_diagnostics(self) -> Any:
        for brick in getattr(self._registry, "_bricks", {}).values():
            if brick is self:
                continue
            if hasattr(brick, "emit") and "diagnostic" in type(brick).__name__.lower():
                return brick
        return None

    async def _get_goal_context(self) -> str:
        for brick in getattr(self._registry, "_bricks", {}).values():
            getter = getattr(brick, "active_goal_context", None)
            if getter is None:
                continue
            try:
                return await getter() or ""
            except Exception:
                logger.debug("Could not read active goal.", exc_info=True)
                return ""
        return ""

    def _emit(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self._diagnostics is None:
            return
        try:
            from brikie.bricks.logging.base import LogEntry, LogLevel
            self._diagnostics.emit(LogEntry(
                source=self._name, event_type=event_type,
                level=LogLevel.INFO, payload=payload,
            ))
        except Exception:
            logger.debug("Failed to emit diagnostic.", exc_info=True)

    # ── Override audit hooks ────────────────────────────────────────

    def _block_reason(self) -> str:
        return getattr(self, "_last_reason", "Blocked by watchdog reviewer")

    def _rule_matched(self) -> str:
        return getattr(self, "_last_rule", "watchdog:default")

    @staticmethod
    def _blocked_error(tc: Any) -> str:
        name = getattr(tc, "name", "unknown")
        return (
            f"Error: Watchdog blocked '{name}'. If this was unexpected, "
            "rephrase or narrow the request and try again."
        )


# ──────────────────────────────────────────────────────────────────────────
# Reviewer system prompt
# ──────────────────────────────────────────────────────────────────────────

_JUDGMENT_SYSTEM_PROMPT = """\
You are a safety reviewer for an autonomous coding agent. Decide whether a
proposed tool call should run, given the agent's active goal.

Your ENTIRE reply must BEGIN with exactly one verdict — nothing before it:

  ALLOW
  BLOCK(reason: <why this must not run>)
  REVISE(guidance: <what to change before retrying>)

Guidance:
- ALLOW: safe, reversible, or clearly part of normal work. This is the
  default — when unsure, ALLOW. The agent must stay autonomous.
- BLOCK: destructive and irreversible (wiping data, leaking secrets,
  remote code execution, irreversible publishes) or plainly misaligned
  with the goal.
- REVISE: the intent is fine but a parameter is wrong or too broad (bad
  path, missing safety flag, overly wide scope).

Do not block normal development actions (running tests, editing project
files, installing expected dependencies, committing). Begin with the
verdict word.
"""
