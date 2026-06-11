"""Logging Brick ABC — abstract interface for all logging bricks.

Defines the base contract for LLM Token Logging, Tool-Call Tracing, and
Event Bus Diagnostics.  Every Logging Brick is a HookDispatcher consumer
that records diagnostic events and optionally persists them.

Design invariants:
1. NEVER block the main event loop — use asyncio.create_task for writes.
2. NEVER raise — catch all exceptions and log them internally.
3. Target <50 ms per hook dispatch callback.
4. All public fields use Pydantic-like dataclasses for schema stability.
"""

from __future__ import annotations

import abc
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from brikie.config.types import BrickState, HookType

logger = logging.getLogger(__name__)


class LogLevel(str, Enum):
    """Severity levels for internal diagnostic events."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass
class LogEntry:
    """Single structured log record emitted by a Logging Brick.

    Every Logging Brick emits LogEntry objects onto the internal event bus
    and/or to persistent storage.

    Attributes:
        timestamp: UTC ISO-8601 timestamp of the event.
        source: Canonical brick name that emitted this entry.
        event_type: Semantic event type (e.g. "token_usage", "tool_call_start").
        level: Severity level.
        payload: Free-form key-value data; schema depends on event_type.
        session_id: Optional session identifier for correlation.
        trace_id: Optional request-scoped trace ID linking related events.
    """

    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    source: str = ""
    event_type: str = ""
    level: LogLevel = LogLevel.INFO
    payload: Dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    trace_id: str = ""


@dataclass
class LogEvent:
    """Diagnostic event emitted onto the internal event bus.

    Improvement Bricks and the Dreamer Soul consume these in the AFK loop.
    """

    entry: LogEntry
    hook_type: Optional[HookType] = None


# ──────────────────────────────────────────────────────────────────────
# Logging Brick ABC
# ──────────────────────────────────────────────────────────────────────


class LoggingBrick(abc.ABC):
    """Abstract base class for all Logging Bricks.

    Subclasses MUST:
    - Set self._name in __init__.
    - Implement `async def init()` and `async def shutdown()`.
    - Implement hook callback factory methods that call self.emit().

    Subclasses MAY:
    - Override _persist(entry) for durable storage (JSONL, SQLite, etc.).
    """

    def __init__(self) -> None:
        self._name: str = "base_logging"
        self._state: BrickState = BrickState.WARM_UP
        self._emit_queue: asyncio.Queue[LogEntry] = asyncio.Queue()
        self._consumer_task: Optional[asyncio.Task[None]] = None

    # ── Brick lifecycle ──────────────────────────────────────────────

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> BrickState:
        return self._state

    async def init(self) -> None:
        """Initialize the logging brick and start the background consumer."""
        self._consumer_task = asyncio.create_task(self._consume_loop())
        self._state = BrickState.ACTIVE
        logger.info("Logging brick %s started.", self._name)

    async def shutdown(self) -> None:
        """Flush pending entries and stop the background consumer."""
        self._state = BrickState.WARM_UP
        if self._consumer_task is not None and not self._consumer_task.done():
            await self._emit_queue.join()  # flush
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass
        logger.info("Logging brick %s shut down.", self._name)

    # ── Event emission ───────────────────────────────────────────────

    def emit(self, entry: LogEntry) -> None:
        """Emit a log entry asynchronously.

        This is the **only** public method subclasses should call to
        register events.  It puts the entry on an internal queue so the
        caller is never blocked — the background consumer handles
        persistence and event bus dispatch.

        Args:
            entry: A fully populated LogEntry.
        """
        try:
            self._emit_queue.put_nowait(entry)
        except asyncio.QueueFull:
            logger.warning(
                "Logging brick %s emit queue full — dropping entry.",
                self._name,
            )

    async def _consume_loop(self) -> None:
        """Background loop: drain the emit queue and persist entries."""
        while True:
            try:
                entry = await self._emit_queue.get()
                try:
                    await self._on_entry(entry)
                except Exception:
                    logger.exception(
                        "Logging brick %s failed to process entry.",
                        self._name,
                    )
                finally:
                    self._emit_queue.task_done()
            except asyncio.CancelledError:
                break

    async def _on_entry(self, entry: LogEntry) -> None:
        """Process a single log entry.

        Default implementation calls _persist.  Subclasses may override
        to add event bus dispatch or additional side-effects.

        Args:
            entry: The log entry to process.
        """
        await self._persist(entry)

    async def _persist(self, entry: LogEntry) -> None:
        """Persist a log entry to durable storage.

        Default implementation is a no-op.  Subclasses should override
        this with their specific storage backend (JSONL file, SQLite,
        etc.).

        Args:
            entry: The log entry to persist.
        """
        # No-op by default — subclasses override.
        _ = entry

    # ── Hook callbacks ───────────────────────────────────────────────

    @abc.abstractmethod
    async def get_hook_callbacks(
        self,
    ) -> Dict[HookType, List[callable]]:  # noqa: F821
        """Return the hook callbacks this brick should register.

        Returns a mapping of HookType -> list of async callback functions.
        Each callback receives the hook data and returns None.

        This is called during warm-up so the EventLoop can register all
        callbacks before the active phase begins.
        """
        ...
