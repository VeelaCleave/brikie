"""Memory Brick ABC — base class for the tripartite memory architecture.

Defines the interface that all Memory Bricks (LCM, MemPalace, LLM Wiki)
must implement. Inherits from the kernel's MemoryBrick ABC.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict

from brikie.config.types import BrickState


class MemoryBrick(ABC):
    """Abstract base class for Memory Bricks.

    Memory Bricks intercept the event bus to automatically preserve,
    retrieve, and synthesize context without the agent needing to call
    explicit memory tools.

    Subclasses must implement:
    - init(): Initialize the memory brick (open DB connections, etc.)
    - shutdown(): Release resources (close DB connections, etc.)
    - intercept_message(): Save a message to the immutable store.
    - build_context(): Build the active context window for a session.
    """

    def __init__(self) -> None:
        self._name: str = "base_memory"
        self._state: BrickState = BrickState.WARM_UP

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> BrickState:
        return self._state

    @abstractmethod
    async def init(self) -> None:
        """Initialize the memory brick (open DB connections, spawn workers)."""
        ...

    @abstractmethod
    async def shutdown(self) -> None:
        """Release resources (close DB connections, stop workers)."""
        ...

    @abstractmethod
    async def intercept_message(
        self, session_id: str, role: str, content: str
    ) -> None:
        """Save a message to the immutable store."""
        ...

    @abstractmethod
    async def build_context(self, session_id: str) -> Dict[str, Any]:
        """Build the active context window for a session."""
        ...
