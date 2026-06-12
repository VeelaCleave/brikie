"""Hermes TUI Interface Brick — split-panel terminal UI with status bar."""

from __future__ import annotations

import json
import shutil
import sys
import time
from typing import Any, Dict, List, Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.align import Align
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

from brikie.bricks.interface.base import InterfaceBrick

_HERMES_ASCII = """
   ╔══════════════════════════════════════════╗
   ║  ██████╗ ██████╗ ██╗██╗  ██╗██╗███████╗ ║
   ║  ██╔══██╗██╔══██╗██║██║ ██╔╝██║██╔════╝ ║
   ║  ██████╔╝██████╔╝██║█████╔╝ ██║█████╗   ║
   ║  ██╔══██╗██╔══██╗██║██╔═██╗ ██║██╔══╝   ║
   ║  ██████╔╝██║  ██║██║██║  ██╗██║███████╗ ║
   ║  ╚═════╝ ╚═╝  ╚═╝╚═╝╚═╝  ╚═╝╚═╝╚══════╝ ║
   ║       Modular Agentic Harness             ║
   ╚══════════════════════════════════════════╝
"""


class HermesTUI:
    """Manages terminal conversation rendering with status bar."""

    def __init__(
        self,
        model_name: str = "unknown",
        stdin_lines: Optional[List[str]] = None,
    ) -> None:
        self._model_name = model_name
        self._token_count = 0
        self._tool_count = 0
        self._memory_status = "—"
        self._brick_count = 0
        self._messages: List[Dict[str, Any]] = []
        self._console = Console(highlight=False, force_terminal=sys.stdout.isatty())
        self._session = self._create_session()
        self._stdin_lines = stdin_lines or []

    def _create_session(self) -> Optional[PromptSession]:
        """Create the prompt_toolkit session for input if terminal."""
        import sys as _sys
        if not _sys.stdin.isatty():
            return None

        bindings = KeyBindings()

        @bindings.add("c-c")
        def _exit(event):
            raise KeyboardInterrupt

        @bindings.add("c-l")
        def _clear(event):
            self._messages.clear()
            self._console.clear()

        return PromptSession(
            history=InMemoryHistory(),
            key_bindings=bindings,
            complete_while_typing=False,
            enable_history_search=True,
        )

    def set_model(self, name: str) -> None:
        self._model_name = name

    def update_token_count(self, count: int) -> None:
        self._token_count = count

    def set_memory_status(self, status: str) -> None:
        self._memory_status = status

    def set_brick_count(self, count: int) -> None:
        self._brick_count = count

    def render_status_bar(self) -> Panel:
        """Build the bottom status bar."""
        model_text = Text.assemble(
            (" Model: ", "dim"), (self._model_name, "bold cyan"),
        )
        tokens_text = Text.assemble(
            (" Tokens: ", "dim"), (f"{self._token_count:,}", "bold green"),
        )
        mem_text = Text.assemble(
            (" Memory: ", "dim"), (self._memory_status, "bold yellow"),
        )
        bricks_text = Text.assemble(
            (" Bricks: ", "dim"), (f"{self._brick_count}", "bold magenta"),
        )
        tool_text = Text.assemble(
            (" Tools: ", "dim"), (f"{self._tool_count}", "bold blue"),
        )
        bar = Text.assemble(
            model_text, " │ ", tokens_text, " │ ", mem_text,
            " │ ", bricks_text, " │ ", tool_text,
        )
        return Panel(
            bar,
            style="white on #1a1a2e",
            border_style="bright_black",
            height=3,
            padding=(0, 1),
            subtitle="[dim]Ctrl-C to exit | Ctrl-L to clear[/]",
        )

    def add_user_message(self, content: str) -> None:
        self._messages.append({"role": "user", "content": content, "tool_calls": [], "ts": time.time()})

    def add_assistant_message(self, content: str) -> None:
        self._messages.append({"role": "assistant", "content": content, "tool_calls": [], "ts": time.time()})

    def add_tool_call(self, name: str, args: Dict[str, Any], result: str) -> None:
        self._messages.append({
            "role": "tool_call", "content": result,
            "func_name": name, "func_args": args, "ts": time.time(),
        })
        self._tool_count += 1

    def add_assistant_with_tools(self, tool_calls: List[Dict[str, Any]]) -> None:
        self._messages.append({"role": "assistant", "content": "", "tool_calls": tool_calls, "ts": time.time()})

    def render_conversation(self) -> Panel:
        """Build the main conversation panel."""
        lines: List[Text] = []
        for msg in self._messages:
            role = msg["role"]
            if role == "user":
                lines.append(Text("  ┌─ You", style="bold cyan"))
                for line in msg["content"].split("\n"):
                    lines.append(Text(f"  │ {line}", style="cyan"))
                lines.append(Text("  └─", style="bold cyan"))
                lines.append(Text(""))

            elif role == "assistant":
                tool_calls = msg.get("tool_calls", [])
                if tool_calls:
                    lines.append(Text("  ┌─ Agent", style="bold green"))
                    for tc in tool_calls:
                        func = tc.get("function", tc)
                        name = func.get("name", tc.get("name", "?"))
                        raw_args = func.get("arguments", tc.get("args", {}))
                        if isinstance(raw_args, str):
                            try:
                                raw_args = json.loads(raw_args)
                            except (json.JSONDecodeError, TypeError):
                                raw_args = {"raw": raw_args[:80]}
                        arg_items = list(raw_args.items())[:4]
                        arg_str = ", ".join(f"{k}={v}" for k, v in arg_items)
                        if len(raw_args) > 4:
                            arg_str += "…"
                        lines.append(Text(f"  │ 🔧 [bold yellow]{name}({arg_str})[/]", no_wrap=True))
                    lines.append(Text("  └─", style="bold green"))
                else:
                    lines.append(Text("  ┌─ Agent", style="bold green"))
                    for line in msg["content"].split("\n"):
                        lines.append(Text(f"  │ {line}", style="green"))
                    lines.append(Text("  └─", style="bold green"))
                lines.append(Text(""))

            elif role == "tool_call":
                name = msg.get("func_name", "?")
                result = msg.get("content", "")
                truncated = result[:180] + "…" if len(result) > 180 else result
                lines.append(Text(f"    ✓ [{name}] {truncated}", style="dim cyan"))
                lines.append(Text(""))

        if not lines:
            lines.append(Text("  Welcome to Brikie. Type a message to begin.", style="dim italic"))

        max_rows = max(8, shutil.get_terminal_size().lines - 12)
        return Panel(
            Align.left(Text.assemble(*lines[:max_rows * 3])),
            title="[bold cyan]Conversation[/]",
            border_style="blue",
            padding=(1, 2),
            height=max_rows,
        )

    def full_render(self) -> None:
        """Render the complete TUI layout."""
        self._console.clear()
        self._console.print(f"[bold cyan]{_HERMES_ASCII}[/bold cyan]", highlight=False)
        self._console.print(Rule(style="dim"))
        self.render_conversation()
        self.render_status_bar()

    def render(self) -> None:
        """Re-render conversation and status (after content changes)."""
        term_width = shutil.get_terminal_size().columns
        self._console.print(Rule(style="dim", width=term_width))
        self._console.print(self.render_conversation())
        self._console.print(self.render_status_bar())

    async def get_input(self) -> str:
        """Get user input from the prompt bar."""
        import sys as _sys
        try:
            if self._session is None:
                if self._stdin_lines:
                    line = self._stdin_lines.pop(0)
                    if line == "/exit" or line == "":
                        return "/exit"
                    return line
                return "/exit"
            from prompt_toolkit.formatted_text import FormattedText
            ft = FormattedText([("class:prompt", "You ")])
            text = await self._session.prompt_async(
                message=ft,
            )
            return text.strip()
        except (EOFError, KeyboardInterrupt):
            return "/exit"

    def render_banner(self) -> None:
        """Print startup banner."""
        self._console.print(f"[bold cyan]{_HERMES_ASCII}[/bold cyan]", highlight=False)
        self._console.print(Rule(style="dim"))
        self._console.print()

    def shutdown(self) -> None:
        """Clean shutdown message."""
        self._console.print()
        self._console.print("[bold red]Brikie shutting down.[/bold red]")


class CLIBrick(InterfaceBrick):
    BRICK_NUMBER = "BRK-300"
    """Hermes-style TUI with conversation panels and status bar."""

    def __init__(self) -> None:
        super().__init__()
        self._name = "cli"
        self._tui: Optional[HermesTUI] = None
        self._model_name = "loading…"
        self._pending_tool_calls: List[Dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    async def init(self) -> None:
        """Initialize the TUI."""
        self._tui = HermesTUI(model_name=self._model_name)
        self._tui.render_banner()
        await super().init()

    async def shutdown(self) -> None:
        """Gracefully release TUI resources."""
        if self._tui:
            self._tui.shutdown()
        await super().shutdown()

    def set_provider_info(self, model: str, provider: str) -> None:
        self._model_name = model
        if self._tui:
            self._tui.set_model(model)

    def set_brick_count(self, count: int) -> None:
        if self._tui:
            self._tui.set_brick_count(count)

    async def get_input(self) -> str:
        """Capture user input from the TUI."""
        if self._tui is None:
            return ""
        try:
            return await self._tui.get_input()
        except KeyboardInterrupt:
            return "/exit"

    async def output(self, msg: str) -> None:
        """Render agent output through the TUI."""
        if self._tui is None or not msg:
            return

        if msg.startswith("[system error]"):
            self._tui.add_assistant_message(f"⚠ System Error: {msg}")
        elif msg.startswith("[AFK mode]"):
            self._tui.add_assistant_message(msg)
        elif msg.startswith("Persistent tool loop"):
            self._tui.add_assistant_message(f"⚠ {msg}")
        elif msg.startswith("Tool-call loop"):
            return
        else:
            self._tui.add_assistant_message(msg)

    async def render_user_message(self, content: str) -> None:
        if self._tui:
            self._tui.add_user_message(content)

    async def render_assistant_response(self, content: str) -> None:
        if self._tui:
            self._tui.add_assistant_message(content)

    async def render_tool_calls(self, raw_calls: List[Dict[str, Any]]) -> None:
        """Display tool calls from the assistant before execution."""
        if self._tui and raw_calls:
            self._pending_tool_calls = raw_calls
            self._tui.add_assistant_with_tools(raw_calls)

    async def render_tool_result(self, name: str, args: Dict[str, Any], result: str) -> None:
        if self._tui:
            self._tui.add_tool_call(name, args, result)

    def render_colored(self, role: str, msg: str) -> None:
        """Legacy single-line rendering."""
        if self._tui is None:
            return
        style_map = {
            "user": "bold cyan",
            "assistant": "bold green",
            "agent": "bold green",
            "tool": "bold yellow",
            "system": "bold magenta",
            "error": "bold red",
        }
        style = style_map.get(role, "white")
        self._tui._console.print(f"[{style}]{role}[/] {msg}")
