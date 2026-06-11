from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

from brikie.bricks.interface.base import InterfaceBrick
from brikie.config.types import AFKMode, BrickState, BusEvent

logger = logging.getLogger(__name__)


class InternalEventBusBrick(InterfaceBrick):
    BRICK_NUMBER = "BRK-310"
    """Interface Brick for inter-Soul communication during AFK mode.

    Replaces the CLI interface when /afk is activated. Souls publish
    BusEvent messages to the bus, which routes them to the correct
    recipient via internal queues.

    Design:
    - Each soul has a dedicated asyncio.Queue for incoming events.
    - publish() puts an event on the recipient's queue (or all if "*").
    - consume() blocks on the caller's queue — the soul actor loop calls this.
    - get_input() returns serialised events for the provider loop.
    - output() publishes events from the orchestrator to recipients.
    """

    def __init__(self) -> None:
        super().__init__()
        self._name = "event_bus"
        self._queues: Dict[str, asyncio.Queue[BusEvent]] = {}
        self._subscribers: Dict[str, List[Callable[[BusEvent], None]]] = {}
        self._stopped = False

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def init(self) -> None:
        self._state = BrickState.ACTIVE
        logger.info("InternalEventBusBrick started.")

    async def shutdown(self) -> None:
        self._stopped = True
        self._state = BrickState.WARM_UP
        # Unblock any waiting consumers
        for queue in self._queues.values():
            queue.put_nowait(BusEvent(event_type="system.shutdown"))
        self._queues.clear()
        self._subscribers.clear()
        logger.info("InternalEventBusBrick shut down.")

    # ── Soul queue management ─────────────────────────────────────────

    def register_soul(self, soul_name: str) -> None:
        """Create a message queue for a soul.

        Must be called before the soul can consume events.
        """
        if soul_name not in self._queues:
            self._queues[soul_name] = asyncio.Queue()

    def unregister_soul(self, soul_name: str) -> None:
        """Remove a soul's message queue."""
        self._queues.pop(soul_name, None)
        self._subscribers.pop(soul_name, None)

    # ── Event publishing / routing ────────────────────────────────────

    async def publish(self, event: BusEvent) -> None:
        """Publish an event to the target soul's queue.

        If target_soul is "*", publishes to all registered souls.
        """
        if self._stopped:
            return

        targets: List[str] = []
        if event.target_soul == "*":
            targets = list(self._queues.keys())
        elif event.target_soul in self._queues:
            targets = [event.target_soul]

        for target in targets:
            queue = self._queues.get(target)
            if queue is not None:
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    logger.warning("Queue full for soul %s — dropping event", target)

        # Also notify local subscribers
        subscribers = self._subscribers.get(event.event_type, [])
        for cb in subscribers:
            try:
                cb(event)
            except Exception:
                logger.exception("Subscriber callback failed for %s", event.event_type)

    async def consume(self, soul_name: str) -> BusEvent:
        """Block until the next event arrives for the given soul.

        Returns a BusEvent or a shutdown sentinel.
        """
        queue = self._queues.get(soul_name)
        if queue is None:
            return BusEvent(
                event_type="error",
                source_soul="system",
                target_soul=soul_name,
                payload={"error": f"No queue registered for soul '{soul_name}'"},
            )
        return await queue.get()

    # ── Pub/sub for local listeners ───────────────────────────────────

    def subscribe(
        self, event_type: str, callback: Callable[[BusEvent], None]
    ) -> None:
        """Register a local subscriber for a specific event type."""
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(callback)

    def unsubscribe(
        self, event_type: str, callback: Callable[[BusEvent], None]
    ) -> None:
        """Remove a local subscriber."""
        subs = self._subscribers.get(event_type, [])
        if callback in subs:
            subs.remove(callback)

    # ── InterfaceBrick contract ──────────────────────────────────────

    async def get_input(self) -> str:
        """Block until the next event arrives, return as serialised string.

        This is the replacement for CLI.get_input() during AFK mode.
        The serialised event is parsed back in the event loop to drive
        the next turn.
        """
        if self._stopped:
            return ""

        # Wait for any event from any soul
        while not self._stopped:
            for soul_name, queue in list(self._queues.items()):
                if not queue.empty():
                    event = queue.get_nowait()
                    return f"__bus_event__:{event.event_type}|{soul_name}|{event.correlation_id}"
            await asyncio.sleep(0.05)
        return ""

    async def output(self, msg: str) -> None:
        """Route a message back through the event bus.

        During AFK mode, the orchestrator's output is published as
        a BusEvent rather than rendered to the CLI.
        """
        if self._stopped:
            return
        event = BusEvent(
            event_type="orchestrator.output",
            source_soul="sisyphus_orchestrator",
            target_soul="*",
            payload={"content": msg},
        )
        await self.publish(event)
