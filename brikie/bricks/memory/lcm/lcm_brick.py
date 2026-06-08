"""LCM Brick — Lossless Context Management."""

from typing import Any, Dict

from brikie.config.types import BrickState
from brikie.bricks.memory.lcm.lcm_store import LcmStore
from brikie.bricks.memory.memory_brick import MemoryBrick


class LcmBrick(MemoryBrick):
    """Lossless Context Management Brick."""

    def __init__(self, db_path: str = "lcm.db") -> None:
        super().__init__()
        self._name = "lcm"
        self._store = LcmStore(db_path)

    async def init(self) -> None:
        await self._store.initialize()
        self._state = BrickState.ACTIVE

    async def shutdown(self) -> None:
        await self._store.shutdown()
        self._state = BrickState.WARM_UP

    async def intercept_message(
        self, session_id: str, role: str, content: str
    ) -> None:
        await self._store.append_message(session_id, role, content)

    async def build_context(self, session_id: str) -> Dict[str, Any]:
        return await self._store.get_active_context(session_id)
