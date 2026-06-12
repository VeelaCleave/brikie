"""Brikie CLI Interface Brick — transcript-style terminal UI.

A flowing, scrollback-friendly TUI in the style of modern agent CLIs:

- Gradient ASCII banner with brick motif on boot
- Startup summary panel (provider, bricks, tools)
- Markdown-rendered agent responses
- Model thinking shown as dim italic blocks
- Tool calls and results traced inline
- Spinner while the model is working
- Status toolbar (model / tokens / bricks / tools) while typing
- Plain-text mode when stdin/stdout is not a TTY (pipes, CI)

The Baseplate event loop talks to this brick through the InterfaceBrick
ABC (``get_input`` / ``output``) plus optional richer methods it probes
with ``hasattr``: ``render_assistant_response``, ``render_thinking``,
``render_tool_calls``, ``render_tool_result``, ``render_startup``,
``render_info``, ``render_error``, ``set_busy``, ``update_usage``,
``clear_screen``.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.status import Status
from rich.text import Text

from brikie.bricks.interface.base import InterfaceBrick

__all__ = ["CLIBrick"]

_VERSION = "0.1.0"

_BANNER_LINES = [
    " ██████╗ ██████╗ ██╗██╗  ██╗██╗███████╗",
    " ██╔══██╗██╔══██╗██║██║ ██╔╝██║██╔════╝",
    " ██████╔╝██████╔╝██║█████╔╝ ██║█████╗  ",
    " ██╔══██╗██╔══██╗██║██╔═██╗ ██║██╔══╝  ",
    " ██████╔╝██║  ██║██║██║  ██╗██║███████╗",
    " ╚═════╝ ╚═╝  ╚═╝╚═╝╚═╝  ╚═╝╚═╝╚══════╝",
]

# Brick-orange → ember gradient, one colour per banner row.
_BANNER_GRADIENT = ["#ff9e4f", "#ff8a3d", "#ff762e", "#f96322", "#ef5016", "#e03e0b"]

_BRICK_ROW = " ▀▄▀▄▀▄▀▄▀▄▀▄▀▄▀▄▀▄▀▄▀▄▀▄▀▄▀▄▀▄▀▄▀▄▀▄▀"

_SLASH_COMMANDS = ["/help", "/bricks", "/afk", "/clear", "/exit", "/quit"]

_PROMPT_STYLE = Style.from_dict({
    "prompt": "#ff762e bold",
    "bottom-toolbar": "#9a9a9a bg:#1c1c28",
    "bottom-toolbar.key": "#ff9e4f bg:#1c1c28 bold",
    "auto-suggestion": "#5a5a5a",
})

_MAX_RESULT_PREVIEW = 400
_MAX_ARG_PREVIEW = 70


def _shorten(value: str, limit: int) -> str:
    value = value.replace("\n", "\\n")
    if len(value) > limit:
        return value[: limit - 1] + "…"
    return value


def _format_args(args: Any) -> str:
    """Render tool-call arguments as a compact ``key=value`` list."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            return _shorten(args, _MAX_ARG_PREVIEW)
    if not isinstance(args, dict):
        return _shorten(str(args), _MAX_ARG_PREVIEW)
    parts = []
    for key, val in args.items():
        text = val if isinstance(val, str) else json.dumps(val, default=str)
        parts.append(f"{key}={_shorten(text, _MAX_ARG_PREVIEW)}")
    return ", ".join(parts)


class CLIBrick(InterfaceBrick):
    """Transcript-style CLI with rich rendering and a prompt toolbar."""

    BRICK_NUMBER = "BRK-300"

    def __init__(self) -> None:
        super().__init__()
        self._name = "cli"
        self._console = Console(highlight=False)
        self._session: Optional[PromptSession] = None
        self._booted = False
        self._stdin_lines: List[str] = []
        self._is_tty = sys.stdin.isatty() and sys.stdout.isatty()
        self._status: Optional[Status] = None
        # Toolbar state
        self._model_name = "—"
        self._brick_count = 0
        self._tool_count = 0
        self._tokens_in = 0
        self._tokens_out = 0

    @property
    def name(self) -> str:
        return self._name

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def init(self) -> None:
        if self._booted:
            # Re-mounted (e.g. returning from AFK mode) — keep the transcript.
            await super().init()
            return
        self._booted = True
        if self._is_tty:
            self._session = self._build_session()
        else:
            # Piped mode: consume stdin up-front, line per turn.
            self._stdin_lines = [
                line for line in sys.stdin.read().splitlines() if line.strip()
            ]
        self._print_banner()
        await super().init()

    async def shutdown(self) -> None:
        self._stop_spinner()
        if self._is_tty:
            self._console.print()
            self._console.print("[#ff762e]▀▄▀[/] [dim]brikie dismantled. bricks saved.[/]")
        await super().shutdown()

    def _build_session(self) -> PromptSession:
        bindings = KeyBindings()

        @bindings.add("c-l")
        def _clear(event) -> None:
            self._console.clear()

        return PromptSession(
            history=InMemoryHistory(),
            key_bindings=bindings,
            style=_PROMPT_STYLE,
            completer=WordCompleter(_SLASH_COMMANDS, sentence=True),
            auto_suggest=AutoSuggestFromHistory(),
            complete_while_typing=True,
            enable_history_search=True,
            bottom_toolbar=self._toolbar,
        )

    # ------------------------------------------------------------------
    # Banner & startup
    # ------------------------------------------------------------------

    def _print_banner(self) -> None:
        self._console.print()
        for line, colour in zip(_BANNER_LINES, _BANNER_GRADIENT):
            self._console.print(Text(line, style=f"bold {colour}"))
        self._console.print(Text(_BRICK_ROW, style="#7a3a1d"))
        tagline = Text()
        tagline.append("   build your agent · brick by brick", style="italic #c8855a")
        tagline.append(f"   v{_VERSION}", style="dim")
        self._console.print(tagline)
        self._console.print()

    async def render_startup(self, info: Dict[str, Any]) -> None:
        """Show the boot summary once all bricks are warm."""
        self._model_name = info.get("model", self._model_name)
        bricks: List[str] = info.get("bricks", [])
        self._brick_count = len(bricks)
        self._tool_count = info.get("tool_count", 0)

        grid = Text()
        grid.append("  model   ", style="dim")
        grid.append(f"{info.get('model', '—')}", style="bold #ff9e4f")
        if info.get("base_url"):
            grid.append(f"  ·  {info['base_url']}", style="dim")
        grid.append("\n  bricks  ", style="dim")
        grid.append(f"{self._brick_count} seated", style="bold")
        grid.append(f"  ·  {', '.join(bricks)}", style="#c8855a")
        grid.append("\n  tools   ", style="dim")
        grid.append(f"{self._tool_count} available", style="bold")
        self._console.print(Panel(
            grid,
            title="[bold #ff762e]baseplate ready[/]",
            border_style="#7a3a1d",
            padding=(0, 1),
        ))
        if self._is_tty:
            self._console.print(
                "  [dim]type a message to begin — [/]"
                "[#ff9e4f]/help[/][dim] for commands[/]"
            )
        self._console.print()

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------

    async def get_input(self) -> str:
        if not self._is_tty:
            if self._stdin_lines:
                return self._stdin_lines.pop(0)
            return "/exit"
        if self._session is None:
            return "/exit"
        try:
            prompt = FormattedText([("class:prompt", "❯ ")])
            text = await self._session.prompt_async(prompt)
            return text.strip()
        except (EOFError, KeyboardInterrupt):
            return "/exit"

    def _toolbar(self) -> FormattedText:
        tokens = f"{self._tokens_in:,}↑ {self._tokens_out:,}↓"
        return FormattedText([
            ("class:bottom-toolbar.key", f" {self._model_name} "),
            ("class:bottom-toolbar", "│"),
            ("class:bottom-toolbar", f" tok {tokens} "),
            ("class:bottom-toolbar", "│"),
            ("class:bottom-toolbar", f" bricks {self._brick_count} "),
            ("class:bottom-toolbar", "│"),
            ("class:bottom-toolbar", f" tools {self._tool_count} "),
            ("class:bottom-toolbar", "│"),
            ("class:bottom-toolbar.key", " /help "),
            ("class:bottom-toolbar", "│"),
            ("class:bottom-toolbar.key", " /exit "),
        ])

    # ------------------------------------------------------------------
    # Output — transcript rendering
    # ------------------------------------------------------------------

    async def output(self, msg: str) -> None:
        """Generic fallback channel for system-level messages."""
        if not msg:
            return
        self._stop_spinner()
        if msg.startswith("[system error]"):
            await self.render_error(msg.removeprefix("[system error]").strip())
        else:
            self._console.print(Text(f"  {msg}", style="dim"))

    async def render_user_message(self, content: str) -> None:
        # In TTY mode the prompt line itself already echoes the input.
        if not self._is_tty:
            self._console.print(Text(f"❯ {content}", style="bold cyan"))

    async def render_thinking(self, reasoning: str) -> None:
        if not reasoning.strip():
            return
        self._stop_spinner()
        header = Text("✻ thinking", style="italic #8a7a9a")
        body = Padding(Text(reasoning.strip(), style="dim italic"), (0, 0, 0, 2))
        self._console.print(Padding(Group(header, body), (1, 0, 0, 1)))

    async def render_assistant_response(self, content: str) -> None:
        if not content.strip():
            return
        self._stop_spinner()
        header = Text("✦ brikie", style="bold #ff9e4f")
        body = Padding(Markdown(content), (0, 0, 0, 2))
        self._console.print(Padding(Group(header, body), (1, 0, 1, 1)))

    async def render_tool_calls(self, raw_calls: List[Dict[str, Any]]) -> None:
        self._stop_spinner()
        for call in raw_calls:
            func = call.get("function", call)
            name = func.get("name", call.get("name", "?"))
            args = func.get("arguments", call.get("args", {}))
            line = Text("  ● ", style="#f9c74f")
            line.append(name, style="bold #f9c74f")
            line.append(f"({_format_args(args)})", style="#b08c3f")
            self._console.print(line)
        self._start_spinner("running tools…")

    async def render_tool_result(self, name: str, args: Dict[str, Any], result: str) -> None:
        self._stop_spinner()
        preview = _shorten(result.strip(), _MAX_RESULT_PREVIEW)
        line = Text("    ⎿ ", style="dim")
        line.append(preview, style="dim")
        if len(result) > _MAX_RESULT_PREVIEW:
            line.append(f"  ({len(result):,} chars)", style="dim italic")
        self._console.print(line)

    async def render_afk_event(self, actor: str, text: str) -> None:
        """Narrate one stage of the AFK negotiation in the transcript."""
        self._stop_spinner()
        styles = {
            "dreamer": "#b48ead",
            "foreman": "#ff9e4f",
            "mason": "#f9c74f",
        }
        style = styles.get(actor, "dim")
        line = Text("  ◆ ", style=style)
        line.append(f"{actor:<8}", style=f"bold {style}")
        line.append(text, style="#9a9a9a")
        self._console.print(line)

    async def render_info(self, title: str, body: str) -> None:
        self._stop_spinner()
        self._console.print(Panel(
            Text(body),
            title=f"[bold #ff9e4f]{title}[/]",
            border_style="#7a3a1d",
            padding=(0, 1),
        ))

    async def render_error(self, msg: str) -> None:
        self._stop_spinner()
        self._console.print(Panel(
            Text(msg, style="red"),
            title="[bold red]⚠ error[/]",
            border_style="red",
            padding=(0, 1),
        ))

    # ------------------------------------------------------------------
    # Spinner & status
    # ------------------------------------------------------------------

    def set_busy(self, busy: bool, label: str = "thinking…") -> None:
        if busy:
            self._start_spinner(label)
        else:
            self._stop_spinner()

    def _start_spinner(self, label: str) -> None:
        if not self._is_tty:
            return
        self._stop_spinner()
        self._status = self._console.status(
            f"[#ff9e4f]{label}[/]", spinner="dots", spinner_style="#ff762e"
        )
        self._status.start()

    def _stop_spinner(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None

    def update_usage(self, tokens_in: int, tokens_out: int) -> None:
        self._tokens_in = tokens_in
        self._tokens_out = tokens_out

    def set_provider_info(self, model: str, provider: str) -> None:
        self._model_name = model

    def clear_screen(self) -> None:
        self._console.clear()
        self._print_banner()
