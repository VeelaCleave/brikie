"""Unit tests for the Brikie Baseplate kernel components."""

import pytest
from typing import Any, Dict, List

from brikie.config.types import BrickState, HookType
from brikie.kernel.hooks import HookDispatcher
from brikie.kernel.registry import (
    BrickRegistry,
    InterfaceBrick,
    ProviderBrick,
    ToolBrick,
)
from brikie.kernel.state import StateManager


# ---------------------------------------------------------------------------
# Test-only Brick stubs (kernel tests only)
# ---------------------------------------------------------------------------


class _TestProvider(ProviderBrick):
    """Minimal ProviderBrick for testing registry operations."""

    @property
    def name(self) -> str:
        return "test-provider"

    @property
    def state(self) -> BrickState:
        return BrickState.ACTIVE

    async def init(self) -> None:
        pass

    async def get_completion(
        self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
    ) -> tuple[str, List[Dict[str, Any]]]:
        return ("response", [])

    async def shutdown(self) -> None:
        pass


class _TestInterface(InterfaceBrick):
    """Minimal InterfaceBrick for testing registry operations."""

    @property
    def name(self) -> str:
        return "test-interface"

    @property
    def state(self) -> BrickState:
        return BrickState.ACTIVE

    async def init(self) -> None:
        pass

    async def get_input(self) -> str:
        return "hello"

    async def output(self, msg: str) -> None:
        pass

    async def shutdown(self) -> None:
        pass


class _TestTool(ToolBrick):
    """Minimal ToolBrick for testing registry operations."""

    @property
    def name(self) -> str:
        return "test-tool"

    @property
    def state(self) -> BrickState:
        return BrickState.ACTIVE

    async def init(self) -> None:
        pass

    async def execute(self, name: str, args: Dict[str, Any]) -> Any:
        return {"result": name}

    async def shutdown(self) -> None:
        pass


# ---------------------------------------------------------------------------
# StateManager
# ---------------------------------------------------------------------------


class TestStateManager:
    """Verify get / set / snapshot / keys behavior."""

    @pytest.mark.asyncio
    async def test_set_and_get(self, state_manager: StateManager):
        await state_manager.set("foo", "bar")
        value = await state_manager.get("foo")
        assert value == "bar"

    @pytest.mark.asyncio
    async def test_get_missing_key_raises_key_error(self, state_manager: StateManager):
        with pytest.raises(KeyError, match="missing"):
            await state_manager.get("missing")

    @pytest.mark.asyncio
    async def test_overwrite_value(self, state_manager: StateManager):
        await state_manager.set("x", 1)
        await state_manager.set("x", 2)
        assert await state_manager.get("x") == 2

    @pytest.mark.asyncio
    async def test_snapshot_returns_deep_copy(self, state_manager: StateManager):
        await state_manager.set("data", {"nested": [1, 2, 3]})
        snap1 = await state_manager.snapshot()
        snap1["data"]["nested"].append(4)
        snap2 = await state_manager.snapshot()
        assert snap2["data"]["nested"] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_snapshot_isolation(self, state_manager: StateManager):
        await state_manager.set("a", 1)
        snap = await state_manager.snapshot()
        await state_manager.set("b", 2)
        assert "b" not in snap
        assert snap == {"a": 1}

    @pytest.mark.asyncio
    async def test_keys(self, state_manager: StateManager):
        await state_manager.set("a", 1)
        await state_manager.set("b", 2)
        keys = await state_manager.keys()
        assert set(keys) == {"a", "b"}

    @pytest.mark.asyncio
    async def test_keys_empty(self, state_manager: StateManager):
        assert await state_manager.keys() == []


# ---------------------------------------------------------------------------
# HookDispatcher
# ---------------------------------------------------------------------------


class TestHookDispatcher:
    """Verify register / dispatch / dispatch_all behavior."""

    def test_register_adds_callback(self, hook_dispatcher: HookDispatcher):
        async def callback(data):
            return data

        hook_dispatcher.register(HookType.PRE_PARSE, callback)
        assert len(hook_dispatcher._callbacks[HookType.PRE_PARSE]) == 1

    def test_register_multiple_callbacks(self, hook_dispatcher: HookDispatcher):
        async def cb1(data):
            return data

        async def cb2(data):
            return data

        hook_dispatcher.register(HookType.PRE_PARSE, cb1)
        hook_dispatcher.register(HookType.PRE_PARSE, cb2)
        assert len(hook_dispatcher._callbacks[HookType.PRE_PARSE]) == 2

    @pytest.mark.asyncio
    async def test_dispatch_returns_results(self, hook_dispatcher: HookDispatcher):
        async def callback(data):
            return data.upper()

        hook_dispatcher.register(HookType.PRE_LLM, callback)
        results = await hook_dispatcher.dispatch(HookType.PRE_LLM, "hello")
        assert results == ["HELLO"]

    @pytest.mark.asyncio
    async def test_dispatch_multiple_callbacks(self, hook_dispatcher: HookDispatcher):
        async def cb1(data):
            return data * 2

        async def cb2(data):
            return data + "!"

        hook_dispatcher.register(HookType.POST_TOOL, cb1)
        hook_dispatcher.register(HookType.POST_TOOL, cb2)
        results = await hook_dispatcher.dispatch(HookType.POST_TOOL, "x")
        assert results == ["xx", "x!"]

    @pytest.mark.asyncio
    async def test_dispatch_empty_hook(self, hook_dispatcher: HookDispatcher):
        results = await hook_dispatcher.dispatch(HookType.PRE_PARSE, "data")
        assert results == []

    @pytest.mark.asyncio
    async def test_dispatch_all_runs_every_hook(self, hook_dispatcher: HookDispatcher):
        async def callback(data):
            return data

        for ht in HookType:
            hook_dispatcher.register(ht, callback)

        results = await hook_dispatcher.dispatch_all("event")
        assert len(results) == len(HookType)
        for ht in HookType:
            assert results[ht] == ["event"]


# ---------------------------------------------------------------------------
# BrickRegistry
# ---------------------------------------------------------------------------


class TestBrickRegistry:
    """Verify register / get_all / get / clear behavior."""

    def test_register_and_get_provider(self, brick_registry: BrickRegistry):
        provider = _TestProvider()
        brick_registry.register(provider)
        retrieved = brick_registry.get("test-provider")
        assert retrieved is provider

    def test_register_and_get_tool(self, brick_registry: BrickRegistry):
        tool = _TestTool()
        brick_registry.register(tool)
        retrieved = brick_registry.get("test-tool")
        assert retrieved is tool

    def test_get_missing_raises_key_error(self, brick_registry: BrickRegistry):
        with pytest.raises(KeyError, match="missing"):
            brick_registry.get("missing")

    def test_get_all_filters_by_type(self, brick_registry: BrickRegistry):
        brick_registry.register(_TestProvider())
        brick_registry.register(_TestInterface())
        brick_registry.register(_TestTool())

        providers = brick_registry.get_all(ProviderBrick)
        interfaces = brick_registry.get_all(InterfaceBrick)
        tools = brick_registry.get_all(ToolBrick)

        assert len(providers) == 1
        assert len(interfaces) == 1
        assert len(tools) == 1

    def test_get_all_empty(self, brick_registry: BrickRegistry):
        assert brick_registry.get_all(ProviderBrick) == []

    def test_register_overwrite_same_name(self, brick_registry: BrickRegistry):
        p1 = _TestProvider()
        p2 = _TestProvider()
        brick_registry.register(p1)
        brick_registry.register(p2)
        assert brick_registry.get("test-provider") is p2

    def test_clear(self, brick_registry: BrickRegistry):
        brick_registry.register(_TestProvider())
        brick_registry.clear()
        assert len(brick_registry._bricks) == 0


# ──────────────────────────────────────────────────────────────────────
# Tool error containment
# ──────────────────────────────────────────────────────────────────────


class _ExplodingToolBrick(ToolBrick):
    """A tool brick whose execute() raises an arbitrary exception."""

    BRICK_NUMBER = "BRK-9998"
    tools = [{
        "type": "function",
        "function": {"name": "explode", "parameters": {"type": "object"}},
    }]

    @property
    def name(self) -> str:
        return "exploder"

    @property
    def state(self) -> BrickState:
        return BrickState.ACTIVE

    async def init(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def execute(self, name: str, args: Dict[str, Any]) -> Any:
        raise RuntimeError("kaboom")


class TestToolErrorContainment:
    """One failing tool call must never crash the event loop (AGENTS.md)."""

    async def test_exception_is_settled_as_structured_error(self):
        from brikie.config.types import ToolCall
        from brikie.kernel.event_loop import EventLoop

        registry = BrickRegistry()
        registry.register(_ExplodingToolBrick())
        loop = EventLoop(
            registry=registry, state=StateManager(), hooks=HookDispatcher()
        )

        calls = await loop.process_tool_calls(
            [ToolCall(name="explode", args={}, tool_call_id="t1")]
        )

        assert calls[0].result is not None
        assert "RuntimeError" in calls[0].result
        assert "kaboom" in calls[0].result
