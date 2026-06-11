"""CLI Interface Brick — color-coded terminal I/O via rich."""

from typing import Dict

from brikie.bricks.interface.base import InterfaceBrick
from rich.console import Console


class CLIBrick(InterfaceBrick):
    BRICK_NUMBER = "BRK-300"
    """Command-line interface Brick using rich for formatting.

    Color scheme: user=blue, agent=green, tool=cyan, system=magenta.
    """

    COLORS: Dict[str, str] = {
        "user": "bold blue",
        "assistant": "bold green",
        "agent": "bold green",
        "tool": "bold cyan",
        "system": "bold magenta",
    }

    def __init__(self) -> None:
        super().__init__()
        self._name = "cli"
        self._console = Console()

    @property
    def name(self) -> str:
        return self._name

    async def init(self) -> None:
        """Initialize the CLI interface."""
        self._console.print("[bold green]Brikie CLI Brick ready.[/bold green]")
        await super().init()

    async def shutdown(self) -> None:
        """Gracefully release CLI resources."""
        self._console.print("[bold red]CLI Brick shutting down.[/bold red]")
        await super().shutdown()

    async def get_input(self) -> str:
        """Capture user input from stdin with color-coded prompt."""
        text = self._console.input("[bold blue]You[/bold blue] ")
        return text

    async def output(self, msg: str) -> None:
        """Print agent output to stdout with color-coded styling."""
        self._console.print(f"[bold green]Agent[/bold green] {msg}")

    def render_colored(self, role: str, msg: str) -> None:
        """Print a message with role-specific color.

        Args:
            role: Message role ('user', 'assistant', 'agent', 'tool', 'system').
            msg: The message content to display.
        """
        style = self.COLORS.get(role, "white")
        self._console.print(f"[{style}]{role}[/] {msg}")
