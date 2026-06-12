"""Tests for session resume (brikie --continue)."""

from __future__ import annotations

import pytest

from brikie.bricks.memory.lcm.lcm_brick import LcmBrick
from brikie.kernel.event_loop import EventLoop
from brikie.kernel.hooks import HookDispatcher
from brikie.kernel.registry import BrickRegistry
from brikie.kernel.state import StateManager


@pytest.fixture
async def lcm(tmp_path):
    brick = LcmBrick(db_path=str(tmp_path / "lcm.db"))
    await brick.init()
    yield brick
    await brick.shutdown()


class TestLcmHistory:
    async def test_active_messages_round_trip(self, lcm):
        await lcm.intercept_message("default", "user", "hello")
        await lcm.intercept_message("default", "assistant", "hi there")
        history = await lcm.load_history("default")
        assert [(m["role"], m["content"]) for m in history] == [
            ("user", "hello"), ("assistant", "hi there"),
        ]

    async def test_history_ordered_by_index(self, lcm):
        for i in range(5):
            await lcm.intercept_message("default", "user", f"msg{i}")
        history = await lcm.load_history("default")
        assert [m["content"] for m in history] == [f"msg{i}" for i in range(5)]

    async def test_empty_session_history(self, lcm):
        assert await lcm.load_history("default") == []
        assert await lcm._store.has_session("default") is False


class TestEventLoopResume:
    async def _loop(self, tmp_path, resume: bool):
        registry = BrickRegistry()
        lcm = LcmBrick(db_path=str(tmp_path / "lcm.db"))
        registry.register(lcm)
        loop = EventLoop(
            registry=registry, state=StateManager(), hooks=HookDispatcher(),
            resume=resume,
        )
        return loop, lcm

    async def test_resume_restores_history(self, tmp_path):
        # First "session": write some turns.
        _, lcm = await self._loop(tmp_path, resume=False)
        await lcm.init()
        await lcm.intercept_message("default", "user", "remember 42")
        await lcm.intercept_message("default", "assistant", "noted: 42")
        await lcm.shutdown()

        # Second run with --continue restores them into history.
        loop, _ = await self._loop(tmp_path, resume=True)
        await loop._phase_warm_up()
        try:
            contents = [m.content for m in loop._message_history]
            assert contents == ["remember 42", "noted: 42"]
            assert loop._resumed_count == 2
        finally:
            await loop._phase_shutdown()

    async def test_no_resume_starts_empty(self, tmp_path):
        _, lcm = await self._loop(tmp_path, resume=False)
        await lcm.init()
        await lcm.intercept_message("default", "user", "old message")
        await lcm.shutdown()

        loop, _ = await self._loop(tmp_path, resume=False)
        await loop._phase_warm_up()
        try:
            assert loop._message_history == []
            assert loop._resumed_count == 0
        finally:
            await loop._phase_shutdown()

    async def test_resume_with_no_memory_brick_is_safe(self):
        registry = BrickRegistry()
        loop = EventLoop(
            registry=registry, state=StateManager(), hooks=HookDispatcher(),
            resume=True,
        )
        await loop._restore_history()  # must not raise
        assert loop._message_history == []
        assert loop._resumed_count == 0
