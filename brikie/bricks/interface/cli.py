"""CLI Interface Brick using rich for color-coded output.

Provides an interactive terminal interface with colored prompts and messages.
"""

from typing import Any, Dict

from brikie.bricks.interface.base import InterfaceBrick
from rich.console import Console
from rich.text import Text


class CLIBrick(InterfaceBrick):
    """Command-line interface Brick using rich for formatting.

    Color scheme:
    - User input: Blue
    - Assistant output: Green
    - Tool results: Cyan
    """

    def __init__(self) -> None:
        super().__init__()
        self._name = "cli"
        self._console = Console()
        self._color_map: Dict[str, str] = {
            "user": "blue",
            "assistant": "green",
            "tool": "cyan",
            "system": "magenta",
        }

    @property
    def name(self) -> str:
        return self._name

    async def init(self) -> None:
        """Initialize the CLI interface."""
        self._console.print(
            Text("Brikie CLI Brick ready.", style="bold green")
        )
        super().init()

    async def shutdown(self) -> None:
        """Gracefully release CLI resources."""
        self._console.print(
            Text("CLI Brick shutting down.", style="bold red")
        )
        super().shutdown()

    async def get_input(self) -> str:
        """Capture user input from stdin with color-coded prompt."""
        text = self._console.input(
            Text("You", style="bold blue"),
        )
        return text

    async def output(self, msg: str) -> None:
        """Print assistant output to stdout with color-coded styling."""
        self._console.print(
            Text(msg, style="bold green")
        )
