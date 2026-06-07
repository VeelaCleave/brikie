"""Middleware hook dispatcher for the Brikie Baseplate kernel.

Hooks are coroutines registered by Bricks to intercept or transform data
at specific lifecycle stages. Dispatch follows the linear ordering:
PRE_PARSE → PRE_LLM → POST_LLM → PRE_TOOL → POST_TOOL → POST_TOOL_CALL.
"""

from typing import Any, Awaitable, Callable, Dict, List

from brikie.config.types import HookType


HookCallback = Callable[..., Awaitable[Any]]


class HookDispatcher:
    """Dispatches hook events to registered Brick callbacks in order.

    Each Brick registers a coroutine callback per hook type. When `dispatch` is
    called, all callbacks for that hook type are awaited sequentially.
    """

    HOOK_ORDER: List[HookType] = [
        HookType.PRE_PARSE,
        HookType.PRE_LLM,
        HookType.POST_LLM,
        HookType.PRE_TOOL,
        HookType.POST_TOOL,
        HookType.POST_TOOL_CALL,
    ]

    def __init__(self) -> None:
        self._callbacks: Dict[HookType, List[HookCallback]] = {ht: [] for ht in self.HOOK_ORDER}

    def register(self, hook_type: HookType, callback: HookCallback) -> None:
        """Register a coroutine callback for a specific hook stage.

        Args:
            hook_type: The lifecycle stage this callback intercepts.
            callback: An async function that accepts `(data, **kwargs)` and
                      optionally returns a transformed value.
        """
        self._callbacks[hook_type].append(callback)

    async def dispatch(self, hook_type: HookType, data: Any) -> List[Any]:
        """Dispatch a hook event to all registered callbacks for the stage.

        Args:
            hook_type: The lifecycle stage to trigger.
            data: The payload passed to each callback.

        Returns:
            A list of return values from each callback coroutine (in registration order).
        """
        results: List[Any] = []
        for callback in self._callbacks[hook_type]:
            result = await callback(data)
            results.append(result)
        return results

    async def dispatch_all(self, data: Any) -> Dict[HookType, List[Any]]:
        """Dispatch the same data through every hook stage in linear order.

        Useful for running the full middleware pipeline for a single event.

        Args:
            data: The payload passed through the pipeline.

        Returns:
            Mapping from each HookType to the list of callback results.
        """
        results: Dict[HookType, List[Any]] = {}
        for ht in self.HOOK_ORDER:
            results[ht] = await self.dispatch(ht, data)
        return results
