"""Integration tests — full pipeline: input → hook → provider → tool → output."""

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
# Mock bricks for integration tests
# ---------------------------------------------------------------------------


class MockProviderBrick(ProviderBrick):
    """Provider that returns a text response and an optional tool call."""

    def __init__(self, response: str = "Hello", tool_calls: List[Dict[str, Any]] | None = None):
        self._response = response
        self._tool_calls = tool_calls or []

    @property
    def name(self) -> str:
        return "mock-provider"

    @property
    def state(self) -> BrickState:
        return BrickState.ACTIVE

    async def init(self) -> None:
        pass

    async def get_completion(
        self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
    ) -> tuple[str, List[Dict[str, Any]]]:
        return (self._response, self._tool_calls)

    async def shutdown(self) -> None:
        pass


class MockInterfaceBrick(InterfaceBrick):
    """Interface that captures output and yields pre-set inputs."""

    def __init__(self):
        self._inputs: List[str] = []
        self._outputs: List[str] = []
        self._current_input_index = 0

    @property
    def name(self) -> str:
        return "mock-interface"

    @property
    def state(self) -> BrickState:
        return BrickState.ACTIVE

    async def init(self) -> None:
        pass

    def set_inputs(self, inputs: List[str]) -> None:
        self._inputs = inputs

    async def get_input(self) -> str:
        if self._current_input_index >= len(self._inputs):
            return ""
        inp = self._inputs[self._current_input_index]
        self._current_input_index += 1
        return inp

    async def output(self, msg: str) -> None:
        self._outputs.append(msg)

    async def shutdown(self) -> None:
        pass


class MockToolBrick(ToolBrick):
    """Tool that records execute calls and returns results."""

    @property
    def name(self) -> str:
        return "mock-tool"

    @property
    def state(self) -> BrickState:
        return BrickState.ACTIVE

    async def init(self) -> None:
        pass

    async def execute(self, name: str, args: Dict[str, Any]) -> Any:
        return {"tool": name, "args": args, "result": f"{name}({args})"}

    async def shutdown(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------


async def run_pipeline(
    interface: MockInterfaceBrick,
    provider: MockProviderBrick,
    tool: MockToolBrick,
    hooks: HookDispatcher,
    state: StateManager,
) -> List[str]:
    """Run a single iteration of the Baseplate pipeline.

    Flow: get_input → dispatch hooks → get_completion → execute tools → output.
    """
    # 1. Get input from interface
    user_input = await interface.get_input()
    await state.set("user_input", user_input)

    # 2. Dispatch hooks (pre-parse through post-tool-call)
    await hooks.dispatch_all(user_input)

    # 3. Send to provider
    messages = [{"role": "user", "content": user_input}]
    tools = [{"name": "calculator", "args": {"op": str, "x": int, "y": int}}]
    response, tool_calls = await provider.get_completion(messages, tools)

    # 4. Execute any tool calls
    tool_results = []
    for tc in tool_calls:
        result = await tool.execute(tc["name"], tc.get("args", {}))
        tool_results.append(result)

    # 5. Output response
    await interface.output(response)

    return interface._outputs


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestFullPipeline:
    """Test input → hook → provider → tool → output pipeline."""

    @pytest.mark.asyncio
    async def test_basic_pipeline(self):
        """Test the full pipeline with a simple provider response."""
        registry = BrickRegistry()
        state = StateManager()
        hooks = HookDispatcher()

        provider = MockProviderBrick(response="Hello, World!")
        interface = MockInterfaceBrick()
        interface.set_inputs(["Say hello"])
        tool = MockToolBrick()

        registry.register(provider)
        registry.register(interface)
        registry.register(tool)

        outputs = await run_pipeline(interface, provider, tool, hooks, state)

        assert outputs == ["Hello, World!"]
        assert await state.get("user_input") == "Say hello"

    @pytest.mark.asyncio
    async def test_pipeline_with_tool_call(self):
        """Test the pipeline when the provider returns a tool call."""
        registry = BrickRegistry()
        state = StateManager()
        hooks = HookDispatcher()

        provider = MockProviderBrick(
            response="Calling calculator...",
            tool_calls=[{"name": "calculator", "args": {"op": "add", "x": 2, "y": 3}}],
        )
        interface = MockInterfaceBrick()
        interface.set_inputs(["Add 2 and 3"])
        tool = MockToolBrick()

        registry.register(provider)
        registry.register(interface)
        registry.register(tool)

        outputs = await run_pipeline(interface, provider, tool, hooks, state)

        assert outputs == ["Calling calculator..."]

    @pytest.mark.asyncio
    async def test_hooks_intercept_data(self):
        """Test that hooks receive and can transform data."""
        hooks = HookDispatcher()
        captured = []

        async def capture_hook(data):
            captured.append(data)
            return data

        hooks.register(HookType.PRE_PARSE, capture_hook)
        hooks.register(HookType.PRE_LLM, capture_hook)

        # Dispatch and verify
        await hooks.dispatch_all("test-data")
        assert captured == ["test-data", "test-data"]

    @pytest.mark.asyncio
    async def test_registry_lookup(self):
        """Test looking up bricks by type from the registry."""
        registry = BrickRegistry()

        provider = MockProviderBrick(response="ok")
        interface = MockInterfaceBrick()
        tool = MockToolBrick()

        registry.register(provider)
        registry.register(interface)
        registry.register(tool)

        providers = registry.get_all(ProviderBrick)
        interfaces = registry.get_all(InterfaceBrick)
        tools = registry.get_all(ToolBrick)

        assert len(providers) == 1
        assert len(interfaces) == 1
        assert len(tools) == 1
        assert providers[0].name == "mock-provider"
        assert interfaces[0].name == "mock-interface"
        assert tools[0].name == "mock-tool"

    @pytest.mark.asyncio
    async def test_multiple_inputs(self):
        """Test that the interface yields multiple inputs sequentially."""
        interface = MockInterfaceBrick()
        interface.set_inputs(["first", "second", "third"])

        assert await interface.get_input() == "first"
        assert await interface.get_input() == "second"
        assert await interface.get_input() == "third"
        assert await interface.get_input() == ""  # Exhausted

    @pytest.mark.asyncio
    async def test_state_persists_across_pipeline(self):
        """Test that state is shared between pipeline stages."""
        state = StateManager()

        await state.set("step", 1)
        assert await state.get("step") == 1

        await state.set("step", 2)
        assert await state.get("step") == 2

        snapshot = await state.snapshot()
        assert snapshot["step"] == 2

    @pytest.mark.asyncio
    async def test_hook_with_tool_response(self):
        """Test that hooks fire before tool execution."""
        hooks = HookDispatcher()
        hook_order = []

        async def order_hook(data):
            hook_order.append(data)
            return data

        hooks.register(HookType.PRE_TOOL, order_hook)
        hooks.register(HookType.POST_TOOL, order_hook)

        await hooks.dispatch(HookType.PRE_TOOL, "pre_tool_event")
        await hooks.dispatch(HookType.POST_TOOL, "post_tool_event")

        assert hook_order == ["pre_tool_event", "post_tool_event"]
