"""Event Bus Diagnostics Collector — aggregates log events for the Dreamer and Improvement Bricks.

While TokenLoggerBrick and ToolTracerBrick handle emission of granular
events, this brick is the **aggregator and router** that:

1. Collects every log entry from the internal event bus
2. Computes running diagnostics (average latency, error rate, token burn rate)
3. Exposes a queryable API for the Dreamer Soul during AFK negotiation
4. Provides the event stream that Improvement Bricks consume

This is NOT a hook consumer itself — it listens to other Logging Bricks'
output via a shared in-memory event bus channel.

Design:
- In-memory ring buffer of the last N log entries (configurable, default 10,000)
- Rolling window aggregations (1min, 5min, 30min, 1hr, session-total)
- Pub/sub channel for real-time Improvement Brick subscription
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from brikie.bricks.logging.base import LogEntry, LogLevel, LoggingBrick
from brikie.config.types import HookType

logger = logging.getLogger(__name__)

# Default ring buffer size: 10 000 entries
_DEFAULT_BUF_SIZE = 10_000


@dataclass
class RollingWindowStats:
    """Computed statistics over a rolling time window."""

    window_label: str = ""  # e.g. "1min", "5min", "session"
    total_llm_calls: int = 0
    total_tool_calls: int = 0
    token_input_count: int = 0
    token_output_count: int = 0
    token_cost: float = 0.0
    tool_success_count: int = 0
    tool_failure_count: int = 0
    avg_llm_latency_ms: float = 0.0
    avg_tool_latency_ms: float = 0.0
    error_count: int = 0
    warning_count: int = 0


@dataclass
class Subscriber:
    """A registered subscriber for real-time log events."""

    callback: Callable[[LogEntry], None]
    event_type_filter: Optional[str] = None  # None = all events
    level_filter: Optional[LogLevel] = None  # None = all levels


class DiagnosticsCollectorBrick(LoggingBrick):
    """Aggregates log events and exposes diagnostics to the Dreamer.

    This brick is the **central nervous system** for the AFK feedback
    loop.  Improvement Bricks subscribe to its event stream, and the
    Dreamer Soul calls its aggregation methods during /afk negotiation.

    The emit() method is called by other Logging Bricks *or* by middleware
    hook callbacks that generate diagnostic events directly.

    Key methods for the Dreamer:
    - get_last_n_events(n): Last N raw log entries.
    - get_window_stats(minutes): Rolling window aggregations.
    - subscribe(callback, event_type_filter, level_filter): Register a
      real-time subscriber (used by Improvement Bricks).
    """

    def __init__(
        self,
        ring_buffer_size: int = _DEFAULT_BUF_SIZE,
    ) -> None:
        super().__init__()
        self._name = "diagnostics_collector"

        # Ring buffer for recent log entries
        self._ring_buffer: List[LogEntry] = []
        self._ring_max = ring_buffer_size
        self._ring_offset = 0  # for O(1) circular overwrite

        # Rolling window accumulators (reset periodically)
        self._window_start: Dict[str, datetime] = {}
        self._window_accums: Dict[str, "RollingWindowStats"] = defaultdict(
            RollingWindowStats
        )

        # Subscribers for real-time event streaming
        self._subscribers: List[Subscriber] = []
        self._sub_lock = asyncio.Lock()

        # Track whether we're the primary diagnostics collector
        self._is_primary = False

    # ── Override emit to also route to subscribers ───────────────────

    def emit(self, entry: LogEntry) -> None:
        """Emit a log entry and notify subscribers.

        Overrides the base class to add subscriber dispatch before
        enqueueing for persistence.

        Args:
            entry: Fully populated log entry.
        """
        # O(1) ring buffer insertion
        if len(self._ring_buffer) < self._ring_max:
            self._ring_buffer.append(entry)
        else:
            idx = self._ring_offset % self._ring_max
            self._ring_buffer[idx] = entry
            self._ring_offset += 1

        # Update rolling windows
        self._update_windows(entry)

        # Notify subscribers asynchronously
        for sub in self._subscribers:
            if sub.event_type_filter and sub.event_type_filter != entry.event_type:
                continue
            if sub.level_filter and sub.level_filter != entry.level:
                continue
            try:
                sub.callback(entry)
            except Exception:
                logger.exception(
                    "DiagnosticsCollectorBrick: subscriber callback failed."
                )

        # Now enqueue for persistence (base class handles this)
        super().emit(entry)

    # ── Hook callbacks ───────────────────────────────────────────────

    async def get_hook_callbacks(
        self,
    ) -> Dict[HookType, List[callable]]:  # noqa: F821
        """Register ALL hook points for comprehensive diagnostics.

        This brick hooks every stage to provide full event bus visibility.
        """

        async def on_pre_parse(data: Any) -> None:
            self.emit(
                LogEntry(
                    source=self._name,
                    event_type="pre_parse",
                    level=LogLevel.DEBUG,
                    payload={"data_preview": str(data)[:200]},
                )
            )

        async def on_pre_llm(data: Any) -> None:
            self.emit(
                LogEntry(
                    source=self._name,
                    event_type="pre_llm",
                    level=LogLevel.DEBUG,
                    payload={"message_count": self._count_messages(data)},
                )
            )

        async def on_post_llm(data: Any) -> None:
            content = data.get("content", "") if isinstance(data, dict) else ""
            self.emit(
                LogEntry(
                    source=self._name,
                    event_type="post_llm",
                    level=LogLevel.INFO,
                    payload={
                        "response_length": len(content),
                        "has_tool_calls": bool(
                            data.get("tool_calls") if isinstance(data, dict) else False
                        ),
                    },
                )
            )

        async def on_pre_tool(data: Any) -> None:
            tool_names = self._extract_tool_names(data)
            self.emit(
                LogEntry(
                    source=self._name,
                    event_type="pre_tool",
                    level=LogLevel.INFO,
                    payload={"tool_names": tool_names, "tool_count": len(tool_names)},
                )
            )

        async def on_post_tool(data: Any) -> None:
            tool_results = self._extract_tool_results(data)
            self.emit(
                LogEntry(
                    source=self._name,
                    event_type="post_tool",
                    level=LogLevel.INFO,
                    payload={"tool_results": tool_results},
                )
            )

        async def on_post_tool_call(data: Any) -> None:
            self.emit(
                LogEntry(
                    source=self._name,
                    event_type="post_tool_call",
                    level=LogLevel.DEBUG,
                    payload={},
                )
            )

        return {
            HookType.PRE_PARSE: [on_pre_parse],
            HookType.PRE_LLM: [on_pre_llm],
            HookType.POST_LLM: [on_post_llm],
            HookType.PRE_TOOL: [on_pre_tool],
            HookType.POST_TOOL: [on_post_tool],
            HookType.POST_TOOL_CALL: [on_post_tool_call],
        }

    # ── Pub/sub: Improvement Brick integration ───────────────────────

    async def subscribe(
        self,
        callback: Callable[[LogEntry], None],
        event_type_filter: Optional[str] = None,
        level_filter: Optional[LogLevel] = None,
    ) -> Callable[[], None]:
        """Register a subscriber for real-time log events.

        Args:
            callback: Async or sync callable accepting a LogEntry.
            event_type_filter: If set, only receive events of this type.
            level_filter: If set, only receive events at this level or above.

        Returns:
            A callable that unsubscribes the callback.
        """
        sub = Subscriber(
            callback=callback,
            event_type_filter=event_type_filter,
            level_filter=level_filter,
        )
        async with self._sub_lock:
            self._subscribers.append(sub)

        def unsubscribe() -> None:
            async def _remove() -> None:
                async with self._sub_lock:
                    if sub in self._subscribers:
                        self._subscribers.remove(sub)

            try:
                # If we're in an async context, schedule it
                import asyncio

                try:
                    asyncio.ensure_future(_remove())
                except RuntimeError:
                    pass
            except Exception:
                pass

        return unsubscribe

    # ── Dreamer API ──────────────────────────────────────────────────

    async def get_last_n_events(
        self, n: int = 100,
        event_type: Optional[str] = None,
        level: Optional[LogLevel] = None,
    ) -> List[LogEntry]:
        """Retrieve the last N log entries from the ring buffer.

        Args:
            n: Maximum number of entries to return.
            event_type: Optional event type filter.
            level: Optional minimum level filter.

        Returns:
            List of LogEntry objects, most recent first.
        """
        if not self._ring_buffer:
            return []

        # Get the most recent entries (up to n)
        total = len(self._ring_buffer)
        start = max(0, total - n)
        entries = list(reversed(self._ring_buffer[start:total]))

        # Apply filters
        if event_type:
            entries = [e for e in entries if e.event_type == event_type]
        if level:
            entries = [e for e in entries if e.level == level]

        return entries

    async def get_window_stats(
        self,
        window_minutes: int = 5,
    ) -> RollingWindowStats:
        """Compute rolling window statistics.

        Args:
            window_minutes: Size of the window in minutes.

        Returns:
            RollingWindowStats aggregated over the window.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        window_label = f"{window_minutes}min"

        stats = RollingWindowStats(window_label=window_label)

        for entry in self._ring_buffer:
            try:
                ts = datetime.fromisoformat(entry.timestamp)
                if ts < cutoff:
                    continue
            except (ValueError, TypeError):
                continue

            self._accumulate_entry(stats, entry)

        return stats

    async def get_session_stats(self) -> RollingWindowStats:
        """Get session-wide aggregated statistics.

        Returns:
            RollingWindowStats for the entire session.
        """
        stats = RollingWindowStats(window_label="session")
        for entry in self._ring_buffer:
            self._accumulate_entry(stats, entry)
        return stats

    # ── Internal helpers ─────────────────────────────────────────────

    def _update_windows(self, entry: LogEntry) -> None:
        """Update rolling window accumulators for each window duration."""
        now = datetime.now(timezone.utc)

        for window_minutes in (1, 5, 30, 60):
            label = f"{window_minutes}min"
            last = self._window_start.get(label)

            if last is None or (now - last) > timedelta(minutes=window_minutes):
                # Reset window
                self._window_start[label] = now
                self._window_accums[label] = RollingWindowStats(
                    window_label=label
                )

            stats = self._window_accums[label]
            self._accumulate_entry(stats, entry)

    @staticmethod
    def _accumulate_entry(stats: RollingWindowStats, entry: LogEntry) -> None:
        """Accumulate a single log entry into the given stats object."""
        if entry.event_type == "token_usage":
            stats.total_llm_calls += 1
            p = entry.payload
            stats.token_input_count += p.get("input_tokens", 0)
            stats.token_output_count += p.get("output_tokens", 0)
            stats.token_cost += p.get("cost_total", 0.0)
            # Running average for LLM latency
            latency = p.get("latency_ms", 0.0)
            if latency > 0:
                stats.avg_llm_latency_ms = (
                    stats.avg_llm_latency_ms * (stats.total_llm_calls - 1) + latency
                ) / stats.total_llm_calls

        elif entry.event_type == "tool_call_result":
            stats.total_tool_calls += 1
            p = entry.payload
            if p.get("success", True):
                stats.tool_success_count += 1
            else:
                stats.tool_failure_count += 1
            tool_latency = p.get("duration_ms", 0.0)
            if tool_latency > 0:
                stats.avg_tool_latency_ms = (
                    stats.avg_tool_latency_ms * (stats.total_tool_calls - 1)
                    + tool_latency
                ) / stats.total_tool_calls

        # Severity tracking
        if entry.level == LogLevel.ERROR:
            stats.error_count += 1
        elif entry.level == LogLevel.WARNING:
            stats.warning_count += 1

    @staticmethod
    def _count_messages(data: Any) -> int:
        """Count messages in hook data."""
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict):
            return len(data.get("messages", []))
        return 0

    @staticmethod
    def _extract_tool_names(data: Any) -> List[str]:
        """Extract tool names from hook data."""
        if isinstance(data, list):
            names = []
            for tc in data:
                if hasattr(tc, "name"):
                    names.append(tc.name)
                elif isinstance(tc, dict):
                    names.append(tc.get("name", ""))
            return names
        return []

    @staticmethod
    def _extract_tool_results(data: Any) -> List[Dict[str, Any]]:
        """Extract tool call results summary from hook data."""
        if isinstance(data, list):
            results = []
            for tc in data:
                if hasattr(tc, "name") and hasattr(tc, "result"):
                    results.append(
                        {
                            "name": tc.name,
                            "success": tc.result is not None
                            and "error" not in (tc.result or "").lower(),
                            "result_length": len(tc.result or ""),
                        }
                    )
            return results
        return []
