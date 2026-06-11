"""Asynchronous state manager for the Brikie Baseplate kernel.

Provides thread-safe, versioned key-value storage backed by asyncio.Lock.
Used by Bricks and middleware to persist state across the event loop.
"""

import asyncio
import copy
from typing import Any, Dict, List

_sentinel = object()


class StateManager:
    """Versioned, async-safe state container.

    All public methods are coroutine-safe and use a single asyncio.Lock
    to serialize reads and writes across concurrent Brick operations.
    """

    def __init__(self) -> None:
        self._versioned_dict: Dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str, default: Any = _sentinel) -> Any:
        async with self._lock:
            if key in self._versioned_dict:
                return self._versioned_dict[key]
            if default is not _sentinel:
                return default
            raise KeyError(key)

    async def set(self, key: str, value: Any) -> None:
        """Store or update a value by key.

        Args:
            key: The state key to set.
            value: The value to store (deep-copied on write).
        """
        async with self._lock:
            self._versioned_dict[key] = value

    async def snapshot(self) -> Dict[str, Any]:
        """Take a deep-copy snapshot of the entire state.

        Returns:
            A new dict containing all keys and their current values.
        """
        async with self._lock:
            return copy.deepcopy(self._versioned_dict)

    async def keys(self) -> List[str]:
        """Return a list of all state keys.

        Returns:
            List of key names currently in the state manager.
        """
        async with self._lock:
            return list(self._versioned_dict.keys())
