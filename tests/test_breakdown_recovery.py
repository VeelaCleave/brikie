"""Tests for breakdown recovery — a crashed turn writes a resumable dump
and the run loop survives transient failures with a circuit breaker.
"""

from __future__ import annotations


from brikie import recovery
from brikie.config.types import BrickState
from brikie.kernel.event_loop import MAX_CONSECUTIVE_BREAKDOWNS, EventLoop
from brikie.kernel.hooks import HookDispatcher
from brikie.kernel.registry import BrickRegistry, InterfaceBrick
from brikie.kernel.state import StateManager


class TestContextDump:
    def test_writes_resumable_report(self, tmp_path, monkeypatch):
        monkeypatch.setattr(recovery, "BREAKDOWN_DIR", tmp_path)
        ctx = {
            "error_type": "RuntimeError",
            "error_message": "boom",
            "traceback": "Traceback ...\nRuntimeError: boom",
            "active_goal": "Ship feature X",
            "model": "deepseek-v4-flash",
            "bricks": ["cli", "http_provider"],
            "session_id": "s1",
            "tokens_in": 10, "tokens_out": 5, "history_len": 4,
            "recent_messages": ["- **user**: hi", "- **assistant**: hello"],
            "consecutive": 1,
        }
        path = recovery.write_context_dump(ctx)
        assert path is not None and path.exists()
        text = path.read_text()
        assert "RuntimeError" in text and "boom" in text
        assert "Ship feature X" in text
        assert "brikie --continue" in text          # resume instructions
        assert "RuntimeError: boom" in text          # traceback included
        # latest.md mirrors the newest report
        assert (tmp_path / "latest.md").read_text() == text

    def test_handles_missing_fields(self, tmp_path, monkeypatch):
        monkeypatch.setattr(recovery, "BREAKDOWN_DIR", tmp_path)
        path = recovery.write_context_dump({})
        assert path is not None and path.exists()
        assert "no active goal" in path.read_text()


class _CapIface(InterfaceBrick):
    """Interface that records what was rendered to the user."""

    def __init__(self) -> None:
        self.errors = []

    @property
    def name(self) -> str:
        return "cap"

    @property
    def state(self) -> BrickState:
        return BrickState.ACTIVE

    async def init(self) -> None: ...
    async def shutdown(self) -> None: ...
    async def get_input(self) -> str:
        return ""
    async def output(self, msg: str) -> None: ...
    async def render_error(self, msg: str) -> None:
        self.errors.append(msg)


class TestRunLoopRecovery:
    async def test_dumps_and_circuit_breaks(self, tmp_path, monkeypatch):
        monkeypatch.setattr(recovery, "BREAKDOWN_DIR", tmp_path)
        reg = BrickRegistry()
        iface = _CapIface()
        reg.register(iface)
        loop = EventLoop(registry=reg, state=StateManager(), hooks=HookDispatcher())

        calls = {"n": 0}

        async def boom() -> None:
            calls["n"] += 1
            raise RuntimeError(f"turn fail {calls['n']}")

        loop._turn = boom  # type: ignore[assignment]
        await loop.run()

        # The loop stopped after the breaker tripped — it didn't spin forever.
        assert calls["n"] == MAX_CONSECUTIVE_BREAKDOWNS
        # A dump was written and the user was told (final message: stopped).
        assert (tmp_path / "latest.md").exists()
        assert iface.errors and "stopped after" in iface.errors[-1]

    async def test_recovers_when_failures_not_consecutive(self, tmp_path, monkeypatch):
        monkeypatch.setattr(recovery, "BREAKDOWN_DIR", tmp_path)
        reg = BrickRegistry()
        iface = _CapIface()
        reg.register(iface)
        loop = EventLoop(registry=reg, state=StateManager(), hooks=HookDispatcher())

        seq = iter([
            "boom", "ok", "boom", "ok", "boom", "ok", "boom", "stop",
        ])

        async def scripted() -> None:
            step = next(seq)
            if step == "boom":
                raise RuntimeError("transient")
            if step == "stop":
                raise KeyboardInterrupt
            # "ok" → a clean turn that resets the counter

        loop._turn = scripted  # type: ignore[assignment]
        await loop.run()

        # 4 separate breakdowns, each reset by a good turn → never hit the
        # breaker, so the user got recovery (not stopped) messages.
        assert len(iface.errors) == 4
        assert all("recovered" in e for e in iface.errors)
