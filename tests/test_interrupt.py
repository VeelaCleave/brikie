"""Tests for mid-turn interrupt / steer (#28).

The primitive (request_stop / inject_steer / _drain_steer / the stop-check in
the agent loop) and the opt-in input watcher's stop-vs-steer routing.
"""

from __future__ import annotations

import asyncio

from brikie.config.types import BrickState
from brikie.kernel.event_loop import EventLoop
from brikie.kernel.hooks import HookDispatcher
from brikie.kernel.registry import (
    BrickRegistry,
    InterfaceBrick,
    ProviderBrick,
    ToolBrick,
)
from brikie.kernel.state import StateManager


class FakeProvider(ProviderBrick):
    """Returns a tool call each round until stopped — i.e. never finishes on
    its own, so only an interrupt ends the loop."""

    def __init__(self):
        self.calls = 0

    @property
    def name(self):
        return "fake_provider"

    @property
    def state(self):
        return BrickState.ACTIVE

    async def init(self): ...
    async def shutdown(self): ...

    async def get_completion(self, messages, tools):
        self.calls += 1
        return ("working", [{"id": f"c{self.calls}",
                             "function": {"name": "noop", "arguments": "{}"}}], {})


class FakeTool(ToolBrick):
    tools = [{"type": "function", "function": {"name": "noop", "parameters": {}}}]

    @property
    def name(self):
        return "fake_tool"

    @property
    def state(self):
        return BrickState.ACTIVE

    async def init(self): ...
    async def shutdown(self): ...
    async def execute(self, name, args):
        return "ok"


class SilentIface(InterfaceBrick):
    def __init__(self):
        self.infos = []

    @property
    def name(self):
        return "iface"

    @property
    def state(self):
        return BrickState.ACTIVE

    async def init(self): ...
    async def shutdown(self): ...
    async def get_input(self):
        await asyncio.sleep(60)
        return ""
    async def output(self, msg): ...
    async def render_info(self, title, body):
        self.infos.append((title, body))


def _loop(**kw):
    reg = BrickRegistry()
    reg.register(FakeProvider())
    reg.register(FakeTool())
    reg.register(SilentIface())
    return EventLoop(registry=reg, state=StateManager(),
                     hooks=HookDispatcher(), **kw)


class TestPrimitive:
    async def test_inject_and_drain_steer(self):
        loop = _loop()
        loop.inject_steer("focus on the parser")
        loop.inject_steer("")            # blank ignored
        loop._drain_steer()
        users = [m for m in loop._message_history if m.role == "user"]
        assert len(users) == 1
        assert "focus on the parser" in users[0].content
        assert "interjected" in users[0].content
        # Draining again is a no-op (queue emptied).
        loop._drain_steer()
        assert sum(1 for m in loop._message_history if m.role == "user") == 1

    async def test_request_stop_halts_agent_loop(self):
        loop = _loop()
        loop.request_stop()
        await loop._agent_loop()          # would otherwise loop forever
        last = loop._message_history[-1]
        assert last.role == "assistant" and "Stopped at your request" in last.content
        # It stopped on the first round, before calling the provider.
        prov = loop._registry.get_all(ProviderBrick)[0]
        assert prov.calls == 0

    async def test_stop_mid_run_ends_it(self):
        # Deterministic: the provider trips the stop on its 2nd call, so the
        # 3rd round's top-of-loop check halts it cleanly.
        loop = _loop()
        prov = loop._registry.get_all(ProviderBrick)[0]
        orig = prov.get_completion

        async def gc(messages, tools):
            r = await orig(messages, tools)
            if prov.calls == 2:
                loop.request_stop()
            return r
        prov.get_completion = gc          # type: ignore[assignment]

        await loop._agent_loop()
        assert any("Stopped at your request" in m.content
                   for m in loop._message_history if m.role == "assistant")
        assert prov.calls == 2            # halted before a 3rd provider call


class TestWatcherRouting:
    async def _drive(self, scripted_input):
        loop = _loop()
        seq = iter(scripted_input)

        async def fake_capture():
            try:
                return next(seq)
            except StopIteration:
                await asyncio.sleep(60)
            return ""
        loop._capture_input = fake_capture  # type: ignore[assignment]

        async def fake_agent():
            for _ in range(200):
                if loop._stop_requested:
                    return
                await asyncio.sleep(0.005)
        agent = asyncio.create_task(fake_agent())
        watcher = asyncio.create_task(loop._watch_for_interrupt(agent))
        await asyncio.wait_for(agent, timeout=3)
        watcher.cancel()
        try:
            await watcher
        except (asyncio.CancelledError, Exception):
            pass
        return loop

    async def test_stop_word_requests_stop(self):
        loop = await self._drive(["/stop"])
        assert loop._stop_requested is True

    async def test_other_text_becomes_steer(self):
        loop = _loop()
        seq = iter(["use a regex instead", "/stop"])

        async def fake_capture():
            try:
                return next(seq)
            except StopIteration:
                await asyncio.sleep(60)
            return ""
        loop._capture_input = fake_capture  # type: ignore[assignment]

        async def fake_agent():
            for _ in range(200):
                if loop._stop_requested:
                    return
                await asyncio.sleep(0.005)
        agent = asyncio.create_task(fake_agent())
        watcher = asyncio.create_task(loop._watch_for_interrupt(agent))
        await asyncio.wait_for(agent, timeout=3)
        watcher.cancel()
        try:
            await watcher
        except (asyncio.CancelledError, Exception):
            pass
        # The non-stop text was queued as a steer before the stop landed.
        assert "use a regex instead" in loop._steer or any(
            "use a regex instead" in m.content for m in loop._message_history)


class TestDefaultOff:
    def test_interruptible_off_by_default(self):
        assert _loop()._interruptible is False

    def test_interruptible_opt_in(self):
        assert _loop(interruptible=True)._interruptible is True
