"""Tests for boot recovery: loader quarantine, warm-up quarantine, and
last-known-good tracking."""

from __future__ import annotations

import json
from typing import Any, Dict

import pytest

from brikie import recovery
from brikie.bricks.build.loader import BuildLoader, BuildSetError
from brikie.config.types import BrickState
from brikie.kernel.event_loop import EventLoop
from brikie.kernel.hooks import HookDispatcher
from brikie.kernel.registry import BrickRegistry, ToolBrick


def _set(tmp_path, name, brks):
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps({"name": name, "bricks": [{"brk": b} for b in brks]}))
    return str(p)


# ──────────────────────────────────────────────────────────────────────
# Loader quarantine
# ──────────────────────────────────────────────────────────────────────


class TestLoaderResilience:
    def test_strict_mode_still_raises(self, tmp_path):
        path = _set(tmp_path, "bad", ["BRK-300", "BRK-999"])
        loader = BuildLoader(BrickRegistry())
        with pytest.raises(BuildSetError):
            loader.load(path)  # resilient=False (default)

    def test_resilient_quarantines_bad_brick(self, tmp_path):
        path = _set(tmp_path, "mix", ["BRK-300", "BRK-999", "BRK-200"])
        registry = BrickRegistry()
        build = BuildLoader(registry).load(path, resilient=True)
        # the unknown brick is quarantined; the good ones loaded
        assert [q[0] for q in build.quarantined] == ["BRK-999"]
        from brikie.kernel.registry import InterfaceBrick, ProviderBrick
        assert registry.get_all(InterfaceBrick)
        assert registry.get_all(ProviderBrick)

    def test_resilient_minimum_stack_still_enforced(self, tmp_path):
        # only a broken provider → no provider survives → validate fails
        path = _set(tmp_path, "noprov", ["BRK-300", "BRK-999"])
        registry = BrickRegistry()
        loader = BuildLoader(registry)
        loader.load(path, resilient=True)
        with pytest.raises(BuildSetError, match="minimum stack"):
            loader.validate_minimum_stack()


# ──────────────────────────────────────────────────────────────────────
# Warm-up quarantine
# ──────────────────────────────────────────────────────────────────────


class _ExplodingInitTool(ToolBrick):
    BRICK_NUMBER = "BRK-9994"
    tools: list = []

    @property
    def name(self) -> str:
        return "exploding_init"

    @property
    def state(self) -> BrickState:
        return BrickState.WARM_UP

    async def init(self) -> None:
        raise RuntimeError("kaboom during init")

    async def execute(self, name: str, args: Dict[str, Any]) -> Any:
        return None

    async def shutdown(self) -> None:
        pass


class _GoodTool(ToolBrick):
    BRICK_NUMBER = "BRK-9993"
    tools: list = []

    @property
    def name(self) -> str:
        return "good_tool"

    @property
    def state(self) -> BrickState:
        return BrickState.ACTIVE

    async def init(self) -> None:
        pass

    async def execute(self, name: str, args: Dict[str, Any]) -> Any:
        return None

    async def shutdown(self) -> None:
        pass


class TestWarmUpQuarantine:
    async def test_bad_init_quarantined_others_survive(self):
        registry = BrickRegistry()
        registry.register(_ExplodingInitTool())
        registry.register(_GoodTool())
        loop = EventLoop(
            registry=registry, state=__import__(
                "brikie.kernel.state", fromlist=["StateManager"]
            ).StateManager(), hooks=HookDispatcher(),
        )
        await loop._phase_warm_up()
        try:
            assert ("exploding_init", "kaboom during init") in [
                (n, e.split(":")[-1].strip() if ":" in e else e)
                for n, e in loop._quarantined
            ] or any("exploding_init" == n for n, _ in loop._quarantined)
            # the good brick is still registered and active
            assert "good_tool" in registry._bricks
            assert "exploding_init" not in registry._bricks
        finally:
            await loop._phase_shutdown()


# ──────────────────────────────────────────────────────────────────────
# Last-known-good
# ──────────────────────────────────────────────────────────────────────


class TestLastKnownGood:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(recovery, "LAST_GOOD_FILE", tmp_path / "last-good-set")

    def test_record_and_read(self, tmp_path):
        s = tmp_path / "myset.json"
        s.write_text("{}")
        recovery.record_good_set(str(s))
        assert recovery.last_good_set() == str(s.resolve())

    def test_none_when_unset(self):
        assert recovery.last_good_set() is None

    def test_none_when_file_gone(self, tmp_path):
        recovery.record_good_set(str(tmp_path / "vanished.json"))
        assert recovery.last_good_set() is None  # target no longer exists

    def test_summarize_quarantine(self):
        assert recovery.summarize_quarantine([]) == ""
        msg = recovery.summarize_quarantine([("foo", "boom")])
        assert "foo" in msg and "boom" in msg

    def test_write_minimal_set(self, tmp_path):
        p = recovery.write_minimal_set(tmp_path / "safe.json")
        data = json.loads(p.read_text())
        brks = [b["brk"] for b in data["bricks"]]
        assert brks == ["BRK-300", "BRK-200"]
