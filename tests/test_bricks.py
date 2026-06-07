"""Tests for Brick ABC compliance and DummyToolBrick functionality."""

import pytest
from abc import ABC
from typing import Any, Dict, List

from brikie.config.types import BrickState
from brikie.kernel.registry import InterfaceBrick, ProviderBrick, ToolBrick


# ---------------------------------------------------------------------------
# DummyToolBrick — reference implementation for tool tests
# ---------------------------------------------------------------------------


class DummyToolBrick(ToolBrick):
    """Minimal tool brick with `calculator` and `reverse_string` methods."""

    _TOOLS = ("calculator", "reverse_string")

    @property
    def name(self) -> str:
        return "dummy-tool"

    @property
    def state(self) -> BrickState:
        return BrickState.ACTIVE

    async def init(self) -> None:
        pass

    async def execute(self, name: str, args: Dict[str, Any]) -> Any:
        if name == "calculator":
            return self.calculator(args)
        elif name == "reverse_string":
            return self.reverse_string(args)
        raise ValueError(f"Unknown tool: {name}")

    async def shutdown(self) -> None:
        pass

    @staticmethod
    def calculator(args: Dict[str, Any]) -> int:
        """Evaluate `op(x, y)` where op ∈ {add, sub, mul, div}."""
        op = args["op"]
        x = args["x"]
        y = args["y"]
        if op == "add":
            return x + y
        elif op == "sub":
            return x - y
        elif op == "mul":
            return x * y
        elif op == "div":
            return x / y
        raise ValueError(f"Unknown op: {op}")

    @staticmethod
    def reverse_string(args: Dict[str, Any]) -> str:
        """Reverse the string in `args['text']`."""
        return args["text"][::-1]


# ---------------------------------------------------------------------------
# DummyToolBrick tests
# ---------------------------------------------------------------------------


class TestDummyToolBrick:
    """Verify DummyToolBrick.calculator and reverse_string."""

    def test_calculator_add(self):
        result = DummyToolBrick.calculator({"op": "add", "x": 3, "y": 4})
        assert result == 7

    def test_calculator_sub(self):
        result = DummyToolBrick.calculator({"op": "sub", "x": 10, "y": 3})
        assert result == 7

    def test_calculator_mul(self):
        result = DummyToolBrick.calculator({"op": "mul", "x": 3, "y": 3})
        assert result == 9

    def test_calculator_div(self):
        result = DummyToolBrick.calculator({"op": "div", "x": 10, "y": 2})
        assert result == 5

    def test_calculator_unknown_op(self):
        with pytest.raises(ValueError, match="Unknown op: pow"):
            DummyToolBrick.calculator({"op": "pow", "x": 2, "y": 3})

    def test_reverse_string(self):
        result = DummyToolBrick.reverse_string({"text": "hello"})
        assert result == "olleh"

    def test_reverse_string_palindrome(self):
        result = DummyToolBrick.reverse_string({"text": "racecar"})
        assert result == "racecar"

    def test_reverse_string_empty(self):
        result = DummyToolBrick.reverse_string({"text": ""})
        assert result == ""

    @pytest.mark.asyncio
    async def test_execute_delegates_to_calculator(self):
        tool = DummyToolBrick()
        result = await tool.execute("calculator", {"op": "add", "x": 1, "y": 2})
        assert result == 3

    @pytest.mark.asyncio
    async def test_execute_delegates_to_reverse_string(self):
        tool = DummyToolBrick()
        result = await tool.execute("reverse_string", {"text": "abc"})
        assert result == "cba"


# ---------------------------------------------------------------------------
# ABC compliance — ProviderBrick
# ---------------------------------------------------------------------------


class TestProviderBrickABC:
    """Verify ProviderBrick is a proper ABC."""

    def test_is_abc(self):
        assert issubclass(ProviderBrick, ABC)

    def test_concrete_subclass_is_instantiable(self):
        class ConcreteProvider(ProviderBrick):
            @property
            def name(self) -> str:
                return "concrete-provider"

            @property
            def state(self) -> BrickState:
                return BrickState.ACTIVE

            async def init(self) -> None:
                pass

            async def get_completion(self, messages, tools):
                return ("ok", [])

            async def shutdown(self) -> None:
                pass

        p = ConcreteProvider()
        assert p.name == "concrete-provider"

    def test_missing_get_completion_raises(self):
        class IncompleteProvider(ProviderBrick):
            @property
            def name(self) -> str:
                return "incomplete"

            @property
            def state(self) -> BrickState:
                return BrickState.ACTIVE

            async def init(self) -> None:
                pass

            async def shutdown(self) -> None:
                pass

        # get_completion is missing → should not be instantiable
        with pytest.raises(TypeError):
            IncompleteProvider()


# ---------------------------------------------------------------------------
# ABC compliance — InterfaceBrick
# ---------------------------------------------------------------------------


class TestInterfaceBrickABC:
    """Verify InterfaceBrick is a proper ABC."""

    def test_is_abc(self):
        assert issubclass(InterfaceBrick, ABC)

    def test_concrete_subclass_is_instantiable(self):
        class ConcreteInterface(InterfaceBrick):
            @property
            def name(self) -> str:
                return "concrete-interface"

            @property
            def state(self) -> BrickState:
                return BrickState.ACTIVE

            async def init(self) -> None:
                pass

            async def get_input(self) -> str:
                return "hi"

            async def output(self, msg: str) -> None:
                pass

            async def shutdown(self) -> None:
                pass

        i = ConcreteInterface()
        assert i.name == "concrete-interface"

    def test_missing_get_input_raises(self):
        class IncompleteInterface(InterfaceBrick):
            @property
            def name(self) -> str:
                return "incomplete"

            @property
            def state(self) -> BrickState:
                return BrickState.ACTIVE

            async def init(self) -> None:
                pass

            async def output(self, msg: str) -> None:
                pass

            async def shutdown(self) -> None:
                pass

        # get_input is missing
        with pytest.raises(TypeError):
            IncompleteInterface()


# ---------------------------------------------------------------------------
# ABC compliance — ToolBrick
# ---------------------------------------------------------------------------


class TestToolBrickABC:
    """Verify ToolBrick is a proper ABC."""

    def test_is_abc(self):
        assert issubclass(ToolBrick, ABC)

    def test_concrete_subclass_is_instantiable(self):
        class ConcreteTool(ToolBrick):
            @property
            def name(self) -> str:
                return "concrete-tool"

            @property
            def state(self) -> BrickState:
                return BrickState.ACTIVE

            async def init(self) -> None:
                pass

            async def execute(self, name: str, args) -> Any:
                return "done"

            async def shutdown(self) -> None:
                pass

        t = ConcreteTool()
        assert t.name == "concrete-tool"

    def test_missing_execute_raises(self):
        class IncompleteTool(ToolBrick):
            @property
            def name(self) -> str:
                return "incomplete"

            @property
            def state(self) -> BrickState:
                return BrickState.ACTIVE

            async def init(self) -> None:
                pass

            async def shutdown(self) -> None:
                pass

        # execute is missing
        with pytest.raises(TypeError):
            IncompleteTool()
