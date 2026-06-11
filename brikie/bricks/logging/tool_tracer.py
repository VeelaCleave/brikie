"""Tool-Call Tracing Brick — trace every tool call through its lifecycle.

Graphs the complete lifecycle of each agent-invoked tool:
  PRE_TOOL      → tool is about to be invoked (captures name, args, trace_id)
  POST_TOOL     → tool has returned a result (captures result, timing)
  POST_TOOL_CALL → tool result has been sent back to the LLM (captures LLM's next action)

This enables:
- Debugging: "which tool call failed and why?"
- Optimization: "which tools are slow/fast?"
- Dreamer analysis: "are tool calls succeeding or failing?"
- Improvement Bricks: auto-fix failed tool calls via post-tool-call hook

Persistence: JSONL files (append-only, one JSON object per line) for
simple, queryable, rotation-friendly storage.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from brikie.bricks.logging.base import LogEntry, LogLevel, LoggingBrick
from brikie.config.types import HookType, ToolCall

logger = logging.getLogger(__name__)


@dataclass
class ToolCallTrace:
    """Full trace of a single tool invocation through the lifecycle.

    Stages:
        1. PRE_TOOL       — tool requested (name, args, timestamp)
        2. POST_TOOL      — tool returned (result, success/fail, duration_ms)
        3. POST_TOOL_CALL — LLM processed the result (next_content, next_tool_calls)
    """

    trace_id: str = ""
    tool_name: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    session_id: str = ""

    # Stage 1: PRE_TOOL
    pre_tool_timestamp: str = ""

    # Stage 2: POST_TOOL
    post_tool_timestamp: str = ""
    result: Optional[str] = None
    success: bool = True
    duration_ms: float = 0.0
    error_message: str = ""

    # Stage 3: POST_TOOL_CALL
    post_tool_call_timestamp: str = ""
    llm_next_content: str = ""
    llm_next_tool_count: int = 0

    # Derived
    total_duration_ms: float = 0.0


class ToolTracerBrick(LoggingBrick):
    BRICK_NUMBER = "BRK-061"
    """Tracks every tool call through its lifecycle stages.

    Hooks into PRE_TOOL, POST_TOOL, and POST_TOOL_CALL to build a
    complete trace of each tool invocation.

    Internal state: dict[trace_id, ToolCallTrace] — in-flight traces.
    This state is transient; completed traces are persisted to JSONL.

    Schema (tool_traces.jsonl — one JSON object per line):
        {
            "trace_id": "...",
            "tool_name": "calculator",
            "args": {...},
            "session_id": "...",
            "pre_tool_timestamp": "...",
            "post_tool_timestamp": "...",
            "duration_ms": 123.45,
            "success": true,
            "error_message": "",
            "result_preview": "...",    // first 500 chars
            "llm_next_content": "...",  // first 500 chars
            "llm_next_tool_count": 0,
            "total_duration_ms": 150.0
        }
    """

    def __init__(
        self,
        log_dir: str = "~/.brikie/logs/traces",
        result_preview_max_chars: int = 500,
        max_inflight_traces: int = 100,
    ) -> None:
        super().__init__()
        self._name = "tool_tracer"
        self._log_dir = Path(log_dir).expanduser()
        self._result_preview_max_chars = result_preview_max_chars
        self._max_inflight = max_inflight_traces

        # In-flight traces keyed by trace_id
        self._in_flight: Dict[str, ToolCallTrace] = {}

        # Current session's tool calls collection (populated from hook data)
        self._current_tool_calls: List[ToolCall] = []
        self._current_trace_ids: List[str] = []

    async def init(self) -> None:
        """Ensure log directory exists."""
        self._log_dir.mkdir(parents=True, exist_ok=True)
        await super().init()

    # ── Hook callback factory ────────────────────────────────────────

    async def get_hook_callbacks(
        self,
    ) -> Dict[HookType, List[callable]]:  # noqa: F821
        """Register PRE_TOOL, POST_TOOL, and POST_TOOL_CALL callbacks."""

        async def on_pre_tool(data: Any) -> None:
            """PRE_TOOL: Start tracing each tool call.

            Expected data: list[ToolCall] or list[dict].
            """
            self._on_pre_tool(data)

        async def on_post_tool(data: Any) -> None:
            """POST_TOOL: Record tool result and timing.

            Expected data: list[ToolCall] with result fields set.
            """
            self._on_post_tool(data)

        async def on_post_tool_call(data: Any) -> None:
            """POST_TOOL_CALL: Record the LLM's response after tool results.

            Expected data: list[ToolCall] — the same tool calls, now
            with results that were fed back to the LLM.
            """
            self._on_post_tool_call(data)

        return {
            HookType.PRE_TOOL: [on_pre_tool],
            HookType.POST_TOOL: [on_post_tool],
            HookType.POST_TOOL_CALL: [on_post_tool_call],
        }

    # ── Stage handlers ───────────────────────────────────────────────

    def _on_pre_tool(self, data: Any) -> None:
        """Handle PRE_TOOL: initialize traces for each tool call."""
        tool_calls = self._normalize_tool_calls(data)
        if not tool_calls:
            return

        self._current_tool_calls = tool_calls
        self._current_trace_ids = []

        now = datetime.now(timezone.utc).isoformat()

        for tc in tool_calls:
            trace_id = tc.trace_id or str(uuid.uuid4())
            self._current_trace_ids.append(trace_id)

            # Enforce in-flight limit — evict oldest if necessary
            if len(self._in_flight) >= self._max_inflight:
                oldest_key = next(iter(self._in_flight))
                self._finalize_trace(oldest_key, "evicted")
                # NOTE: _finalize_trace already pops from _in_flight

            self._in_flight[trace_id] = ToolCallTrace(
                trace_id=trace_id,
                tool_name=tc.name,
                args=tc.args,
                pre_tool_timestamp=now,
            )

            entry = LogEntry(
                source=self._name,
                event_type="tool_call_start",
                level=LogLevel.INFO,
                payload={
                    "trace_id": trace_id,
                    "tool_name": tc.name,
                    "args": self._preview(str(tc.args)),
                },
                trace_id=trace_id,
            )
            self.emit(entry)

    def _on_post_tool(self, data: Any) -> None:
        """Handle POST_TOOL: record results for tracing tool calls."""
        tool_calls = self._normalize_tool_calls(data)
        if not tool_calls:
            return

        now = datetime.now(timezone.utc).isoformat()

        for i, tc in enumerate(tool_calls):
            trace_id = (
                self._current_trace_ids[i]
                if i < len(self._current_trace_ids)
                else tc.trace_id or str(uuid.uuid4())
            )
            trace = self._in_flight.get(trace_id)
            if trace is None:
                logger.warning("ToolTracerBrick: no in-flight trace for %s", trace_id)
                continue

            # Calculate execution duration
            if trace.pre_tool_timestamp:
                try:
                    pre_time = datetime.fromisoformat(trace.pre_tool_timestamp)
                    post_time = datetime.fromisoformat(now)
                    trace.duration_ms = (post_time - pre_time).total_seconds() * 1000
                except (ValueError, TypeError):
                    trace.duration_ms = 0.0

            trace.post_tool_timestamp = now
            trace.result = tc.result or ""
            trace.success = not self._is_error(tc.result)

            if not trace.success:
                trace.error_message = self._extract_error(tc.result)
                level = LogLevel.ERROR
            elif trace.duration_ms > 10_000:  # >10s warning threshold
                level = LogLevel.WARNING
            else:
                level = LogLevel.INFO

            entry = LogEntry(
                source=self._name,
                event_type="tool_call_result",
                level=level,
                payload={
                    "trace_id": trace_id,
                    "tool_name": trace.tool_name,
                    "duration_ms": round(trace.duration_ms, 2),
                    "success": trace.success,
                    "result_preview": self._preview(tc.result or ""),
                    "error_message": trace.error_message,
                },
                trace_id=trace_id,
            )
            self.emit(entry)

    def _on_post_tool_call(self, data: Any) -> None:
        """Handle POST_TOOL_CALL: finalize traces.

        The data here might be the tool call results list again.
        We try to extract the LLM's next response if this is a
        multi-call sequence.
        """
        now = datetime.now(timezone.utc).isoformat()

        for trace_id in self._current_trace_ids:
            trace = self._in_flight.get(trace_id)
            if trace is None:
                continue

            trace.post_tool_call_timestamp = now

            # Total duration from pre_tool to post_tool_call
            if trace.pre_tool_timestamp:
                try:
                    pre_time = datetime.fromisoformat(trace.pre_tool_timestamp)
                    post_time = datetime.fromisoformat(now)
                    trace.total_duration_ms = (
                        post_time - pre_time
                    ).total_seconds() * 1000
                except (ValueError, TypeError):
                    trace.total_duration_ms = trace.duration_ms

            self._finalize_trace(trace_id)

    # ── Trace finalization ───────────────────────────────────────────

    def _finalize_trace(
        self, trace_id: str, reason: str = "completed"
    ) -> None:
        """Persist a completed trace and remove it from in-flight state.

        Args:
            trace_id: The trace to finalize.
            reason: Why the trace is being finalized (completed, evicted, error).
        """
        trace = self._in_flight.pop(trace_id, None)
        if trace is None:
            return

        entry = LogEntry(
            source=self._name,
            event_type="tool_call_complete",
            level=(
                LogLevel.ERROR
                if not trace.success
                else LogLevel.INFO
            ),
            payload={
                "trace_id": trace_id,
                "tool_name": trace.tool_name,
                "duration_ms": round(trace.duration_ms, 2),
                "total_duration_ms": round(trace.total_duration_ms, 2),
                "success": trace.success,
                "error_message": trace.error_message,
                "result_preview": self._preview(trace.result or ""),
                "reason": reason,
            },
            trace_id=trace_id,
        )
        self.emit(entry)

    # ── Persistence (JSONL) ──────────────────────────────────────────

    async def _persist(self, entry: LogEntry) -> None:
        """Append a log entry as a JSON line.

        JSONL format is append-only, easy to rotate, and works with
        standard UNIX tools (grep, jq).
        """
        if entry.event_type not in ("tool_call_start", "tool_call_result", "tool_call_complete"):
            return

        log_file = self._log_dir / f"tool_traces_{datetime.now(timezone.utc).strftime('%Y%m%d')}.jsonl"

        line: Dict[str, Any] = {
            "timestamp": entry.timestamp,
            "source": entry.source,
            "event_type": entry.event_type,
            "level": entry.level.value,
            "payload": entry.payload,
            "session_id": entry.session_id,
            "trace_id": entry.trace_id,
        }

        try:
            with open(log_file, "a") as f:
                f.write(json.dumps(line, default=str) + "\n")
        except OSError:
            logger.exception("ToolTracerBrick: failed to write trace.")

    # ── Helpers ──────────────────────────────────────────────────────

    def _normalize_tool_calls(
        self, data: Any
    ) -> List[ToolCall]:
        """Normalize hook data to a list of ToolCall objects.

        Accepts:
        - list[ToolCall]
        - list[dict] with name/args keys
        """
        if isinstance(data, list):
            result: List[ToolCall] = []
            for item in data:
                if isinstance(item, ToolCall):
                    result.append(item)
                elif isinstance(item, dict):
                    result.append(
                        ToolCall(
                            name=item.get("name", ""),
                            args=item.get("args", {}),
                            result=item.get("result", None),
                            trace_id=item.get("trace_id", ""),
                        )
                    )
            return result
        return []

    @staticmethod
    def _preview(text: str, max_chars: int = 500) -> str:
        """Truncate text to a preview snippet."""
        if not text:
            return ""
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "..."

    @staticmethod
    def _is_error(result: Optional[str]) -> bool:
        """Heuristic: does the result look like an error?"""
        if not result:
            return False
        lowered = result.lower()
        error_indicators = [
            "error",
            "exception",
            "traceback",
            "failed",
            "failure",
            "not found",
            "permission denied",
            "no such",
        ]
        return any(indicator in lowered for indicator in error_indicators)

    @staticmethod
    def _extract_error(result: Optional[str]) -> str:
        """Extract the first relevant error message from a result."""
        if not result:
            return ""
        lines = result.split("\n")
        # Try to find the most meaningful error line
        for line in lines:
            lowered = line.lower().strip()
            for keyword in ["error:", "exception:", "traceback"]:
                if keyword in lowered:
                    return line[:300]
        # Fallback: first non-empty line
        for line in lines:
            if line.strip():
                return line[:300]
        return result[:300]
