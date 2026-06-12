"""Abstract base class for Provider Bricks.

Implements the ProviderBrick ABC from the kernel registry, providing a concrete
skeleton for LLM providers (HTTP, WebSocket, local, etc.).
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List

from brikie.config.types import BrickState
from brikie.kernel.registry import ProviderBrick as ProviderBrickABC


class ProviderBrick(ProviderBrickABC, ABC):
    """Concrete base class for Provider Bricks.

    Subclasses must implement `get_completion` and may override `init` and
    `shutdown` to manage provider-specific lifecycle (connections, auth, etc.).
    """

    def __init__(self) -> None:
        self._name: str = "base_provider"
        self._state: BrickState = BrickState.WARM_UP

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> BrickState:
        return self._state

    @abstractmethod
    async def get_completion(
        self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
    ) -> tuple:
        """Send a message list to the LLM and receive a text response + tool calls.

        Args:
            messages: Standardized message objects (role, content, tool_call_id).
            tools: Tool schemas available to the provider.

        Returns:
            ``(content, tool_calls)`` or ``(content, tool_calls, meta)``.
            The optional ``meta`` dict may carry ``reasoning`` (model
            thinking), ``usage`` (token counts), and ``finish_reason``;
            the kernel renders and logs these when present.
        """
        ...

    async def init(self) -> None:
        """Initialize the provider (auth, connection pools, etc.)."""
        self._state = BrickState.ACTIVE

    async def shutdown(self) -> None:
        """Gracefully release resources (connections, threads, etc.)."""
        self._state = BrickState.WARM_UP
