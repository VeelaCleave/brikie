"""Dummy Tool Brick for testing and demonstration.

Provides simple tools (calculator, reverse_string) that demonstrate
the ToolBrick interface with OpenAI-compatible schemas.
"""

import json
import logging
from typing import Any, Dict, List

from brikie.bricks.tool.base import ToolBrick

logger = logging.getLogger(__name__)


class DummyToolBrick(ToolBrick):
    BRICK_NUMBER = "BRK-400"
    """A collection of simple tools for testing the Baseplate pipeline.

    Tools:
        - calculator: Evaluate a basic arithmetic expression.
        - reverse_string: Reverse the character order of a string.
    """

    tools: List[Dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": "calculator",
                "description": "Evaluate a basic arithmetic expression.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "expression": {
                            "type": "string",
                            "description": "Arithmetic expression (e.g., '2 + 2 * 3').",
                        },
                    },
                    "required": ["expression"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "reverse_string",
                "description": "Reverse the character order of a string.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "The string to reverse.",
                        },
                    },
                    "required": ["text"],
                },
            },
        },
    ]

    def __init__(self) -> None:
        super().__init__()
        self._name = "dummy_tool"

    @property
    def name(self) -> str:
        return self._name

    async def execute(self, name: str, args: Dict[str, Any]) -> Any:
        """Execute one of the dummy tools by name.

        Args:
            name: Tool name ('calculator' or 'reverse_string').
            args: Tool arguments.

        Returns:
            The tool's result.

        Raises:
            KeyError: If the tool name is not recognized.
            ValueError: If required arguments are missing or malformed.
        """
        if name == "calculator":
            return self._calculate(args)
        elif name == "reverse_string":
            return self._reverse_string(args)
        else:
            raise KeyError(f"Unknown tool: {name}")

    def _calculate(self, args: Dict[str, Any]) -> str:
        """Evaluate an arithmetic expression.

        Supports basic Python expression evaluation: +, -, *, /, **, ().
        """
        expression = args.get("expression")
        if not isinstance(expression, str):
            raise ValueError(f"calculator: 'expression' must be a string, got {type(expression).__name__}")

        try:
            result = eval(expression, {"__builtins__": {}}, {"_": None})
            return str(result)
        except Exception as exc:
            logger.warning("Calculator error for '%s': %s", expression, exc)
            return f"Error: {exc}"

    def _reverse_string(self, args: Dict[str, Any]) -> str:
        """Reverse the character order of a string."""
        text = args.get("text")
        if not isinstance(text, str):
            raise ValueError(f"reverse_string: 'text' must be a string, got {type(text).__name__}")
        return text[::-1]
