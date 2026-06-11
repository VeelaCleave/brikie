"""CLI Interface Brick — rich terminal I/O with history and autocomplete."""

from __future__ import annotations

import shlex
from typing import Dict, List, Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from brikie.bricks.interface.base import InterfaceBrick

_COMMANDS: Dict[str, str] = {
    "/help": "Show available slash commands",
    "/afk": "Enter autonomous AFK mode",
    "/clear": "Clear the screen",
    "/exit": "Exit Brikie",
    "/set": "List loaded bricks",
    "/memory": "Show memory stats",
    "/tools": "List available tools",
}


class CLIBrick(InterfaceBrick):
    BRICK_NUMBER = "BRK-300"
    """Command-line interface Brick with history, autocomplete, and rich output."""

    STYLE = Style.from_dict({
        "prompt": "bold cyan",
        "agent": "bold green",
        "user": "bold blue",
        "tool": "bold yellow",
        "system": "bold magenta",
        "error": "bold red",
    })

    def __init__(self) -> None:
        super().__init__()
        self._name = "cli"
        self._console = Console(highlight=False)
        self._history = InMemoryHistory()
        self._session: Optional[PromptSession] = None

    @property
    def name(self) -> str:
        return self._name

    async def init(self) -> None:
        """Initialize the CLI session."""
        bindings = KeyBindings()

        @bindings.add("c-c")
        def _exit(event):
            raise KeyboardInterrupt

        self._session = PromptSession(
            history=self._history,
            auto_suggest=AutoSuggestFromHistory(),
            key_bindings=bindings,
            style=self.STYLE,
            complete_while_typing=False,
            enable_history_search=True,
        )

        self._print_banner()
        await super().init()

    async def shutdown(self) -> None:
        """Gracefully release CLI resources."""
        self._console.print()
        self._console.print("[bold red]Brikie shutting down.[/bold red]")
        await super().shutdown()

    async def get_input(self) -> str:
        """Capture user input with history and autocomplete."""
        if self._session is None:
            return ""

        try:
            if self._session is None:
                return ""
            text = await self._session.prompt_async(
                message=Text.assemble(("You ", "bold cyan")),
                bottom_toolbar=self._bottom_toolbar,
            )
            return text.strip()
        except EOFError:
            return ""
        except Exception:
            # Fallback for non-TTY / piped input
            import sys
            try:
                line = sys.stdin.readline()
                return line.strip()
            except (EOFError, KeyboardInterrupt):
                return ""

    def _bottom_toolbar(self) -> str:
        return " [/help for commands | Ctrl-C to exit]"

    async def output(self, msg: str) -> None:
        """Render agent output with formatting detection."""
        if not msg:
            return
        if msg.startswith("[system error]"):
            self._console.print(Panel(msg, border_style="red", title="System Error"))
        elif msg.startswith("[AFK mode]"):
            self._console.print(Panel(msg, border_style="magenta", title="AFK"))
        elif msg.startswith("#") or msg.startswith("- ") or msg.startswith("* "):
            self._console.print(Markdown(msg))
        elif msg.startswith("[bold") or msg.startswith("[green") or msg.startswith("[red"):
            self._console.print(msg)
        else:
            self._console.print(Text.assemble(("Agent ", "bold green"), (msg, "default")))

    def render_colored(self, role: str, msg: str) -> None:
        """Print a message with role-specific color."""
        style_map: Dict[str, str] = {
            "user": "bold blue",
            "assistant": "bold green",
            "agent": "bold green",
            "tool": "bold yellow",
            "system": "bold magenta",
            "error": "bold red",
        }
        style = style_map.get(role, "white")
        self._console.print(f"[{style}]{role}[/] {msg}")

    def render_table(self, title: str, columns: List[str], rows: List[List[str]]) -> None:
        """Render a table with the given columns and rows."""
        table = Table(title=title, border_style="cyan")
        for col in columns:
            table.add_column(col, style="cyan")
        for row in rows:
            table.add_row(*row)
        self._console.print(table)

    def render_panel(self, title: str, content: str, style: str = "cyan") -> None:
        """Render content inside a bordered panel."""
        self._console.print(Panel(content, border_style=style, title=title))

    async def show_help(self) -> None:
        """Display available slash commands."""
        self._console.print()
        self._console.print(Rule(style="cyan"))
        self._console.print("[bold cyan]Slash Commands[/bold cyan]")
        for cmd, desc in _COMMANDS.items():
            self._console.print(f"  [bold]{cmd:<10}[/bold]  {desc}")
        self._console.print(Rule(style="cyan"))

    def _print_banner(self) -> None:
        """Print startup banner."""
        self._console.print()
        self._console.print(Panel.fit(
            "[bold cyan]Brikie[/bold cyan] — Modular Agentic Harness\n"
            "[dim]Type /help for commands | Ctrl-C to exit[/dim]",
            border_style="cyan",
        ))
        self._console.print()
