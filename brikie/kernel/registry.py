"""Abstract Brick interfaces and the Brick Registry.

Defines the ABCs that every Provider, Interface, and Tool Brick must implement.
The BrickRegistry manages hot-swappable Brick instances by name and type.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Type, TypeVar

from brikie.config.types import BrickState

# ---------------------------------------------------------------------------
# Abstract Base Classes
# ---------------------------------------------------------------------------

class ProviderBrick(ABC):
    """Interface Bricks implement to translate between Baseplate messages and LLM APIs."""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def state(self) -> BrickState:
        ...

    @abstractmethod
    async def init(self) -> None:
        """Initialize the provider (auth, connection pools, etc.)."""
        ...

    @abstractmethod
    async def get_completion(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> tuple[str, List[Dict[str, Any]]]:
        """Send a message list to the LLM and receive a text response + tool calls.

        Args:
            messages: Standardized message objects (role, content, tool_call_id).
            tools: Tool schemas available to the provider.

        Returns:
            Tuple of (text response, list of tool-call dicts).
        """
        ...

    @abstractmethod
    async def shutdown(self) -> None:
        """Gracefully release resources (connections, threads, etc.)."""
        ...


class InterfaceBrick(ABC):
    """Interface Bricks implement to capture input and render output."""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def state(self) -> BrickState:
        ...

    @abstractmethod
    async def init(self) -> None:
        """Initialize the interface (mount sockets, open TTY, etc.)."""
        ...

    @abstractmethod
    async def get_input(self) -> str:
        """Capture the next unit of input from the medium."""
        ...

    @abstractmethod
    async def output(self, msg: str) -> None:
        """Render a message to the output medium."""
        ...

    @abstractmethod
    async def shutdown(self) -> None:
        """Gracefully release interface resources."""
        ...


class ToolBrick(ABC):
    """Tool Bricks implement to let the agent act on its environment."""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def state(self) -> BrickState:
        ...

    @abstractmethod
    async def init(self) -> None:
        """Initialize the tool (download binaries, open DBs, etc.)."""
        ...

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

    @abstractmethod
    async def shutdown(self) -> None:
        """Gracefully release tool resources."""
        ...


# ---------------------------------------------------------------------------
# Brick Registry
# ---------------------------------------------------------------------------

# Import MemoryBrick from the memory module to avoid duplication.
from brikie.bricks.memory.memory_brick import MemoryBrick

Brick = ProviderBrick | InterfaceBrick | ToolBrick | MemoryBrick
BrickT = TypeVar("BrickT", bound=Brick)


class BrickRegistry:
    """Manages the lifecycle and lookup of Bricks by name and type.

    Supports hot-swapping: Bricks are tracked by canonical name, and
    can be retrieved by their ABC type.
    """

    def __init__(self) -> None:
        self._bricks: Dict[str, Brick] = {}

    def register(self, brick: Brick) -> None:
        """Register a Brick instance.

        Args:
            brick: A Brick instance (any ABC subtype).
        """
        self._bricks[brick.name] = brick

    def get_all(self, brick_type: Type[BrickT]) -> List[BrickT]:
        """Retrieve all registered Bricks of a specific type.

        Args:
            brick_type: One of ProviderBrick, InterfaceBrick, or ToolBrick.

        Returns:
            List of bricks that are instances of `brick_type`.
        """
        return [b for b in self._bricks.values() if isinstance(b, brick_type)]  # type: ignore

    def get(self, name: str) -> Brick:
        """Retrieve a Brick by its canonical name.

        Args:
            name: The brick's `name` property value.

        Returns:
            The registered Brick instance.

        Raises:
            KeyError: If no Brick with the given name is registered.
        """
        if name not in self._bricks:
            raise KeyError(name)
        return self._bricks[name]

    def clear(self) -> None:
        """Remove all registered Bricks."""
        self._bricks.clear()
