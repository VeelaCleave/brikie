"""Public API for brikie.kernel."""

from brikie.kernel.event_loop import EventLoop
from brikie.kernel.hooks import (
    HookCallback,
    HookDispatcher,
)
from brikie.kernel.registry import (
    BrickRegistry,
    InterfaceBrick,
    ProviderBrick,
    ToolBrick,
)
from brikie.kernel.state import StateManager

# Import LoggingBrick here so it's available from kernel for typing
from brikie.bricks.logging.base import LoggingBrick  # noqa: F401 — re-exported

__all__ = [
    "EventLoop",
    "HookCallback",
    "HookDispatcher",
    "StateManager",
    "BrickRegistry",
    "ProviderBrick",
    "InterfaceBrick",
    "ToolBrick",
    "LoggingBrick",
]
