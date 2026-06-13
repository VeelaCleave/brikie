"""LoopDetectorBrick (BRK-910) — detect and break tool-call loops.

Monitors the POST_TOOL_CALL hook to identify looping patterns in the
agent's tool-calling sequence — repeated identical calls, oscillating
patterns between two tools, or a repeated error string. When a loop is
detected, emits a diagnostic event and offers realignment suggestions
via the ``loop_status`` tool.

Design:
- Inherits from ToolBrick (for tool registration) and implements
  get_hook_callbacks() (for middleware observation).
- Ring buffer of the last N tool calls (default 128).
- Pattern analysis runs after each hook dispatch (best-effort, <5ms).
- The agent calls ``loop_status`` to self-diagnose.
- The Dreamer reads ``dream_context()`` during AFK cycles.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from brikie.bricks.logging.base import LogEntry, LogLevel
from brikie.bricks.tool.base import ToolBrick
from brikie.config.types import HookType, ToolCall

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────

_DEFAULT_BUFFER_SIZE = 128
_LOOP_REPEAT_THRESHOLD = 4     # same tool+args N times = loop
_OSCILLATION_THRESHOLD = 6     # A→B→A→B length ≥ 6 = oscillation
_ERROR_REPEAT_THRESHOLD = 3    # same error string N times = error loop
_ERROR_WINDOW = 20             # window scanned for recurring (interleaved) errors

_LOOP_TYPE_REPEAT = "repeat"
_LOOP_TYPE_OSCILLATE = "oscillate"
_LOOP_TYPE_ERROR = "error_loop"


# ──────────────────────────────────────────────────────────────────────
# Data types
# ──────────────────────────────────────────────────────────────────────


@dataclass
class ToolCallRecord:
    """A single recorded tool call for pattern analysis.

    Attributes:
        name: Canonical tool name.
        args_hash: SHA-256 of JSON-serialized args (for comparison).
        result_preview: First 200 chars of the result (for error detection).
        success: Whether the call succeeded.
        timestamp: UTC ISO-8601 timestamp.
    """
    name: str
    args_hash: str
    result_preview: str
    success: bool
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class LoopInfo:
    """Information about a detected or current looping pattern.

    Attributes:
        loop_type: One of "repeat", "oscillate", "error_loop".
        tool_names: Tools involved in the pattern.
        count: How many times the pattern repeated.
        detail: Human-readable description.
        detected_at: UTC ISO-8601 of first detection.
    """
    loop_type: str = ""
    tool_names: List[str] = field(default_factory=list)
    count: int = 0
    detail: str = ""
    detected_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# ──────────────────────────────────────────────────────────────────────
# Tool schema for loop_status
# ──────────────────────────────────────────────────────────────────────

_LOOP_STATUS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "loop_status",
        "description": (
            "Check if you are in a tool-call loop (repeating the same "
            "calls, oscillating between two tools, or hitting the same "
            "error repeatedly). Call this when you suspect you might be "
            "spinning without making progress."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


# ──────────────────────────────────────────────────────────────────────
# Brick
# ──────────────────────────────────────────────────────────────────────


class LoopDetectorBrick(ToolBrick):
    """Detects tool-call loops and exposes status via the loop_status tool.

    This brick is both a ToolBrick (for the loop_status schema + execute
    dispatch) and a middleware hook consumer (for POST_TOOL_CALL
    monitoring). The event loop discovers the tool via _collect_tool_schemas
    and the hook callbacks via _register_brick_hooks.

    BRICK_NUMBER = "BRK-910"
    """

    BRICK_NUMBER = "BRK-910"

    tools: List[Dict[str, Any]] = [_LOOP_STATUS_SCHEMA]

    def __init__(
        self,
        registry: Any = None,
        buffer_size: int = _DEFAULT_BUFFER_SIZE,
    ) -> None:
        super().__init__()
        self._name = "loop_detector"

        # The BuildLoader injects the registry (it sees the parameter) so we
        # can find the diagnostics collector and confirm whether a goal tool
        # is loaded to anchor realignment to.
        self._registry = registry

        # Ring buffer of recent tool calls
        self._buffer: deque[ToolCallRecord] = deque(maxlen=buffer_size)

        # Latest loop info (updated only when a new loop is detected)
        self._current_loop: Optional[LoopInfo] = None

        # Active realignment: when a loop is caught we stage a nudge here.
        # The event loop drains it (``pop_realignment``) and injects it into
        # the conversation BEFORE the next model round — so a stuck local
        # model is steered without having to call ``loop_status`` itself.
        self._pending_realignment: Optional[str] = None

        # Suppression window: don't re-emit the same loop type within 30s
        self._last_alert_time: float = 0.0
        self._alert_cooldown_secs: float = 30.0

        # Diagnostics collector reference (resolved from the registry at init)
        self._diagnostics = None

    # ── Brick lifecycle ─────────────────────────────────────────────

    async def init(self) -> None:
        """Initialize the loop detector."""
        await super().init()
        self._diagnostics = self._find_diagnostics()
        logger.info(
            "LoopDetectorBrick started (buffer=%d, diagnostics=%s).",
            self._buffer.maxlen, "on" if self._diagnostics else "off",
        )

    def _find_diagnostics(self) -> Any:
        """Locate the diagnostics collector in the registry, if present.

        Duck-typed: any registered brick that exposes ``emit`` and names
        itself a diagnostics collector. Absent (minimal sets) → stays None,
        and the emit path is simply skipped.
        """
        if self._registry is None:
            return None
        bricks = getattr(self._registry, "_bricks", {})
        for brick in bricks.values():
            if brick is self:
                continue
            if hasattr(brick, "emit") and "diagnostic" in type(brick).__name__.lower():
                return brick
        return None

    # ── Realignment drain (called by the event loop) ────────────────

    def pop_realignment(self) -> Optional[str]:
        """Return and clear the pending realignment nudge, if any.

        The event loop calls this once per agent round. Returning a string
        means "inject this as a system turn now"; None means nothing staged.
        """
        nudge = self._pending_realignment
        self._pending_realignment = None
        return nudge

    async def shutdown(self) -> None:
        """Shut down the loop detector."""
        self._buffer.clear()
        self._current_loop = None
        await super().shutdown()
        logger.info("LoopDetectorBrick shut down.")

    # ── Tool dispatch ────────────────────────────────────────────────

    async def execute(self, name: str, args: Dict[str, Any]) -> Any:
        """Dispatch tool calls handled by this brick.

        Args:
            name: Tool name (``"loop_status"``).
            args: Tool arguments.

        Returns:
            The loop status dict.

        Raises:
            KeyError: Unknown tool name.
        """
        if name == "loop_status":
            return await self._handle_loop_status(args)
        raise KeyError(f"Unknown tool: {name}")

    # ── Hook callbacks ──────────────────────────────────────────────

    async def get_hook_callbacks(self) -> Dict[HookType, List[callable]]:
        """Return POST_TOOL_CALL hook callback to monitor tool calls."""

        async def on_post_tool_call(data: Any) -> None:
            """Record each tool call and detect loops."""
            inner = data
            if hasattr(data, "data"):
                inner = data.data
            if not isinstance(inner, list):
                return

            # Record each tool call
            for tc in inner:
                self._record(tc)

            # Check for loops (once per round, not per call)
            if inner:
                loop = self._detect_loop()
                if loop is not None:
                    await self._handle_loop(loop)

        return {HookType.POST_TOOL_CALL: [on_post_tool_call]}

    # ── Recording ───────────────────────────────────────────────────

    def _record(self, tc: Any) -> None:
        """Normalise a tool call (ToolCall object or dict) and record it."""
        if isinstance(tc, ToolCall) or hasattr(tc, "name"):
            name = tc.name
            args = tc.args
            result = tc.result
        elif isinstance(tc, dict):
            name = tc.get("name", "")
            args = tc.get("args", {})
            result = tc.get("result", "")
        else:
            return

        args_hash = self._hash_args(args)
        result_preview = (result or "")[:200]
        success = not self._is_error(result)

        self._buffer.append(ToolCallRecord(
            name=name,
            args_hash=args_hash,
            result_preview=result_preview,
            success=success,
        ))

    # ── Loop detection ──────────────────────────────────────────────

    def _detect_loop(self) -> Optional[LoopInfo]:
        """Analyse the buffer and return a LoopInfo if a pattern is found.

        Checks three patterns, returning the first (most severe) match:
        1. Repeated identical tool call (same name + same args hash)
        2. Oscillating pattern (A → B → A → B ...)
        3. Repeated error string
        """
        buf = list(self._buffer)
        if len(buf) < 3:
            return None

        # 1. Repeated identical calls
        info = self._check_repeated(buf)
        if info is not None:
            return info

        # 2. Oscillating pattern
        info = self._check_oscillation(buf)
        if info is not None:
            return info

        # 3. Error loop
        info = self._check_error_loop(buf)
        if info is not None:
            return info

        return None

    @staticmethod
    def _check_repeated(buf: List[ToolCallRecord]) -> Optional[LoopInfo]:
        """Check for the same tool + args repeated N+ times."""
        if len(buf) < _LOOP_REPEAT_THRESHOLD:
            return None

        recent = buf[-_LOOP_REPEAT_THRESHOLD:]
        first = recent[0]
        if not all(
            r.name == first.name and r.args_hash == first.args_hash
            for r in recent
        ):
            return None

        # Count total consecutive repeats
        count = 0
        for r in reversed(buf):
            if r.name == first.name and r.args_hash == first.args_hash:
                count += 1
            else:
                break

        return LoopInfo(
            loop_type=_LOOP_TYPE_REPEAT,
            tool_names=[first.name],
            count=count,
            detail=(
                f"Same tool call '{first.name}' with identical args "
                f"repeated {count} times — possible loop."
            ),
        )

    @staticmethod
    def _check_oscillation(buf: List[ToolCallRecord]) -> Optional[LoopInfo]:
        """Check for A → B → A → B pattern of length ≥ 6.

        True oscillation requires that the two alternating calls are
        actually *different* — different tool names OR different arg
        hashes.  Just calling the same tool with different args is not
        an oscillation.
        """
        if len(buf) < _OSCILLATION_THRESHOLD:
            return None

        recent = buf[-_OSCILLATION_THRESHOLD:]
        names = [r.name for r in recent]
        hashes = [r.args_hash for r in recent]

        # Pattern: all even indices are tool A, all odd are tool B
        if (len(set(names[0::2])) == 1 and len(set(names[1::2])) == 1
                and len(set(hashes[0::2])) == 1 and len(set(hashes[1::2])) == 1):
            # Must actually alternate between two distinct calls
            even = recent[0]
            odd = recent[1]
            same_name = even.name == odd.name
            same_args = even.args_hash == odd.args_hash
            if same_name and same_args:
                return None  # This is a repeat, not an oscillation
            return LoopInfo(
                loop_type=_LOOP_TYPE_OSCILLATE,
                tool_names=list(set(names)),
                count=_OSCILLATION_THRESHOLD,
                detail=(
                    f"Oscillating between '{names[0]}' and '{names[1]}' "
                    f"— alternating {_OSCILLATION_THRESHOLD}+ calls."
                ),
            )
        return None

    @staticmethod
    def _check_error_loop(buf: List[ToolCallRecord]) -> Optional[LoopInfo]:
        """Check for the same error string recurring N+ times in a window.

        Counts occurrences across a wider window rather than requiring the
        errors to be consecutive — an agent that retries a blocked command,
        reads a few files to diagnose, then retries again interleaves
        successes between the identical errors. The old consecutive-only
        check missed exactly that pattern (e.g. a firewall block hit
        repeatedly between diagnostic reads).
        """
        if len(buf) < _ERROR_REPEAT_THRESHOLD:
            return None

        window = buf[-_ERROR_WINDOW:]
        errors = [r for r in window if not r.success and r.result_preview]
        if len(errors) < _ERROR_REPEAT_THRESHOLD:
            return None

        # Tally identical error previews (first 80 chars) across the window.
        counts = Counter(r.result_preview[:80] for r in errors)
        err_text, count = counts.most_common(1)[0]
        if count < _ERROR_REPEAT_THRESHOLD:
            return None

        tool_names = list({
            r.name for r in errors if r.result_preview[:80] == err_text
        })
        return LoopInfo(
            loop_type=_LOOP_TYPE_ERROR,
            tool_names=tool_names,
            count=count,
            detail=f"Same error repeated {count} times: '{err_text}'",
        )

    # ── Loop handling ───────────────────────────────────────────────

    async def _handle_loop(self, loop: LoopInfo) -> None:
        """Process a detected loop: update state and emit diagnostics."""
        import time

        now = time.time()
        if now - self._last_alert_time < self._alert_cooldown_secs:
            return  # Suppress duplicate alerts
        self._last_alert_time = now

        self._current_loop = loop
        # Stage an active realignment nudge for the event loop to inject.
        self._pending_realignment = self._build_realignment_message(loop)
        logger.warning(
            "Loop detected: %s — %s", loop.loop_type, loop.detail
        )

        # Emit to diagnostics if available
        if self._diagnostics is not None and hasattr(self._diagnostics, "emit"):
            try:
                self._diagnostics.emit(LogEntry(
                    source=self._name,
                    event_type="loop_detected",
                    level=LogLevel.WARNING,
                    payload={
                        "loop_type": loop.loop_type,
                        "tool_names": loop.tool_names,
                        "count": loop.count,
                        "detail": loop.detail,
                    },
                ))
            except Exception:
                logger.exception("Failed to emit loop event to diagnostics.")

    # ── loop_status tool handler ────────────────────────────────────

    async def _handle_loop_status(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle the loop_status tool call.

        Returns:
            Dict with keys: in_loop, loop_info, recent_calls, suggestion.
        """
        result: Dict[str, Any] = {
            "in_loop": self._current_loop is not None,
            "loop_info": None,
            "recent_calls": [],
            "suggestion": None,
        }

        if self._current_loop is not None:
            result["loop_info"] = {
                "loop_type": self._current_loop.loop_type,
                "tool_names": self._current_loop.tool_names,
                "count": self._current_loop.count,
                "detail": self._current_loop.detail,
                "detected_at": self._current_loop.detected_at,
            }
            result["suggestion"] = self._build_suggestion()

        # Last 10 calls summary
        buf = list(self._buffer)
        for r in buf[-10:]:
            result["recent_calls"].append({
                "name": r.name,
                "success": r.success,
                "args_preview": r.args_hash[:12],
            })

        return result

    def _build_suggestion(self) -> str:
        """Build a realignment suggestion based on the current loop."""
        if self._current_loop is None:
            return ""

        suggestions = []

        if self._current_loop.loop_type == _LOOP_TYPE_REPEAT:
            suggestions.append(
                "You are calling the same tool with the same arguments "
                "repeatedly. Consider checking if the tool is producing "
                "the expected result, and if not, try a different approach "
                "or verify input data."
            )
        elif self._current_loop.loop_type == _LOOP_TYPE_OSCILLATE:
            suggestions.append(
                f"You are oscillating between "
                f"{' and '.join(self._current_loop.tool_names)}. "
                "Consider stepping back to reassess the problem."
            )
        elif self._current_loop.loop_type == _LOOP_TYPE_ERROR:
            suggestions.append(
                "You are hitting the same error repeatedly. Consider "
                "verifying assumptions, checking input data, or trying "
                "a fundamentally different approach."
            )

        suggestions.append(
            "Tip: call goal_status() to re-anchor on your current goal."
        )
        return " ".join(suggestions)

    def _build_realignment_message(self, loop: LoopInfo) -> str:
        """Compose the system-injected realignment the model will read next.

        Unlike ``loop_status`` (which the model has to choose to call), this
        is pushed into the conversation the moment a loop is caught — the
        whole point, since a spinning model won't self-diagnose. Phrased as
        a direct, second-person instruction so the model acts on it.
        """
        anchor = (
            " Call goal_status() to re-anchor on your active goal, then take a "
            "different concrete step."
            if self._goal_tool_available()
            else " Step back and take a different concrete step."
        )
        return (
            f"⚠️ LOOP DETECTED ({loop.loop_type}): {loop.detail} "
            f"You appear to be spinning without progress. Stop repeating the "
            f"same action.{anchor}"
        )

    def _goal_tool_available(self) -> bool:
        """True if a ``goal_status`` tool is loaded to realign against."""
        if self._registry is None:
            return False
        for brick in getattr(self._registry, "_bricks", {}).values():
            schemas = getattr(brick, "tools", None)
            if not schemas:
                continue
            for schema in schemas:
                if schema.get("function", {}).get("name") == "goal_status":
                    return True
        return False

    # ── Dream Source for AFK ────────────────────────────────────────

    async def dream_context(self) -> str:
        """Provide loop detection context for the Dreamer (Dream Source).

        The Dreamer calls this during AFK negotiation to decide whether
        to propose a fix for a detected loop.
        """
        parts: List[str] = []

        if self._current_loop is not None:
            parts.append(
                f"⚠️ Active loop detected: {self._current_loop.loop_type} "
                f"(tools: {', '.join(self._current_loop.tool_names)}, "
                f"count: {self._current_loop.count}). "
                f"{self._current_loop.detail}"
            )
            parts.append(f"Suggested action: {self._build_suggestion()}")
        else:
            buffer_snapshot = list(self._buffer)
            if buffer_snapshot:
                last_5 = buffer_snapshot[-5:]
                calls = ", ".join(
                    f"{r.name}({'ok' if r.success else 'fail'})"
                    for r in last_5
                )
                parts.append(
                    f"Last {min(5, len(buffer_snapshot))} tool calls: {calls}. "
                    "No loop detected."
                )
            else:
                parts.append("No tool calls recorded yet.")

        return "\n\n".join(parts)

    # ── Helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _hash_args(args: Any) -> str:
        """SHA-256 hash of JSON-serialized args for comparison."""
        try:
            raw = json.dumps(args, sort_keys=True, default=str)
        except (TypeError, ValueError):
            raw = str(args)
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def _is_error(result: Optional[str]) -> bool:
        """Check if a tool result looks like an error."""
        if not result:
            return False
        lowered = result.lower().strip()
        if lowered.startswith("error:"):
            return True
        if "traceback" in lowered:
            return True
        if lowered.startswith("no toolbrick found"):
            return True
        return False
