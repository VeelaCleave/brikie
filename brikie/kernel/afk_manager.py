from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List, Optional

from brikie.config.types import AFKMode
from brikie.kernel.registry import BrickRegistry, InterfaceBrick

if TYPE_CHECKING:
    from brikie.bricks.interface.event_bus import InternalEventBusBrick
    from brikie.bricks.soul.base import SoulBrick

logger = logging.getLogger(__name__)


class AFKManager:
    """Manages the /afk toggle between interactive CLI and event-bus modes.

    On enter_afk_mode:
    1. Unregisters the CLI Interface Brick.
    2. Registers the InternalEventBusBrick.
    3. Registers soul queues on the event bus.
    4. Sets state mode to AFK.

    On exit_afk_mode:
    - Reverses the swap (re-registers CLI, unregisters event bus).
    """

    def __init__(
        self,
        registry: BrickRegistry,
        cli_brick: Optional[InterfaceBrick] = None,
        event_bus: "Optional[InternalEventBusBrick]" = None,
    ) -> None:
        self._registry = registry
        self._cli_brick = cli_brick
        if event_bus is None:
            # Lazy import keeps the kernel free of brick imports at module
            # load; callers (e.g. __main__) normally inject the bus.
            from brikie.bricks.interface.event_bus import InternalEventBusBrick
            event_bus = InternalEventBusBrick()
        self._event_bus = event_bus
        self._mode: AFKMode = AFKMode.INTERACTIVE

    @property
    def mode(self) -> AFKMode:
        return self._mode

    @property
    def event_bus(self) -> InternalEventBusBrick:
        return self._event_bus

    async def enter_afk_mode(self, souls: Optional[List[SoulBrick]] = None) -> None:
        """Switch from interactive CLI to AFK event-bus mode."""
        if self._mode == AFKMode.AFK:
            return

        # Find and unregister CLI interface
        interfaces = self._registry.get_all(InterfaceBrick)
        for iface in interfaces:
            if iface.name == "cli":
                try:
                    self._registry.unregister(iface.name)
                    logger.info("Unregistered CLI interface: %s", iface.name)
                except KeyError:
                    pass

        # Register the event bus as an InterfaceBrick
        await self._event_bus.init()
        self._registry.register(self._event_bus)

        # Register soul queues
        if souls:
            for soul in souls:
                self._event_bus.register_soul(soul.name)

        self._mode = AFKMode.AFK
        logger.info("AFK mode entered with %d soul(s).", len(souls or []))

    async def exit_afk_mode(self) -> None:
        """Switch from AFK event-bus mode back to interactive CLI."""
        if self._mode == AFKMode.INTERACTIVE:
            return

        # Unregister event bus
        try:
            await self._event_bus.shutdown()
            self._registry.unregister("event_bus")
        except KeyError:
            pass

        # Re-register CLI if we have it
        if self._cli_brick is not None:
            await self._cli_brick.init()
            self._registry.register(self._cli_brick)

        self._mode = AFKMode.INTERACTIVE
        logger.info("AFK mode exited — returned to interactive CLI.")
