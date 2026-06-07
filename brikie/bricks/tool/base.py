"""Abstract base class for Tool Bricks.

Implements the ToolBrick ABC from the kernel registry, providing a concrete
skeleton for agent tools (calculators, browsers, file systems, etc.).
"""

from abc import ABC, abstractmethod
from typing import Any, Dict

from brikie.config.types import BrickState
from brikie.kernel.registry import ToolBrick as ToolBrickABC


class ToolBrick(ToolBrickABC, ABC):
    """Concrete base class for Tool Bricks.

    Subclasses must implement `execute` and may override `init` and
    `shutdown` to manage tool-specific lifecycle (binaries, DBs, etc.).
    """

    def __init__(self) -> None:
        self._name: str = "base_tool"
        self._state: BrickState = BrickState.WARM_UP

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> BrickState:
        return self._state

    @abstractmethod
    async def execute(self, name: str, args: Dict[str, Any]) -> Any:
        """Execute a named tool with the given arguments.

        Args:
            name: The canonical tool name to invoke.
            args: Key-value arguments for the tool.

        Returns:
            The tool's output (type depends on the tool).
        """
        ...

    async def init(self) -> None:
        """Initialize the tool (download binaries, open DBs, etc.)."""
        self._state = BrickState.ACTIVE

    async def shutdown(self) -> None:
        """Gracefully release tool resources."""
        self._state = BrickState.WARM_UP
