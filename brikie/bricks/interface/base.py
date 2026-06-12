"""Abstract base class for Interface Bricks.

Implements the InterfaceBrick ABC from the kernel registry, providing a concrete
skeleton for interfaces (CLI, Web UI, WebSocket, etc.).
"""

from abc import ABC, abstractmethod

from brikie.config.types import BrickState
from brikie.kernel.registry import InterfaceBrick as InterfaceBrickABC


class InterfaceBrick(InterfaceBrickABC, ABC):
    """Concrete base class for Interface Bricks.

    Subclasses must implement `get_input` and `output` to handle I/O through
    their respective mediums.
    """

    def __init__(self) -> None:
        self._name: str = "base_interface"
        self._state: BrickState = BrickState.WARM_UP

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> BrickState:
        return self._state

    @abstractmethod
    async def get_input(self) -> str:
        """Capture the next unit of input from the medium.

        Returns:
            The raw input string from the user or external system.
        """
        ...

    @abstractmethod
    async def output(self, msg: str) -> None:
        """Render a message to the output medium.

        Args:
            msg: The message content to display.
        """
        ...

    async def init(self) -> None:
        """Initialize the interface (mount sockets, open TTY, etc.)."""
        self._state = BrickState.ACTIVE

    async def shutdown(self) -> None:
        """Gracefully release interface resources."""
        self._state = BrickState.WARM_UP
