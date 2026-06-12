"""Brikie interactive installer — pick your bricks, get your agent.

The Ninite model: the user selects the bricks they want, the installer
writes a custom Build Set JSON, and ``brikie --set <name>`` boots exactly
that stack.  The same Build Set format is what brikie.co will generate
server-side, so this module doubles as the local reference
implementation of the web installer.

Run via:  python3 -m brikie.install
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from brikie.config.provider_presets import PRESETS, preset_config

SETS_DIR = Path(__file__).resolve().parent / "bricks" / "build" / "sets"

ACCENT = "#ff762e"
ACCENT_SOFT = "#ff9e4f"
BORDER = "#7a3a1d"


@dataclass
class CatalogEntry:
    brk: str
    label: str
    blurb: str
    default: bool = False
    config: Dict[str, Any] = field(default_factory=dict)
    preset: str | None = None  # provider preset id (BRK-200 variants)

    @property
    def value(self) -> str:
        """Unique picker value — BRK number, qualified by preset if any."""
        return f"{self.brk}@{self.preset}" if self.preset else self.brk


# The local brick catalog. brikie.co will serve this as JSON one day;
# the BRK numbers must exist in brikie.bricks.build.loader.BRICK_INDEX.
CATALOG: Dict[str, List[CatalogEntry]] = {
    "Interface Bricks (pick at least 1)": [
        CatalogEntry("BRK-300", "CLI", "Terminal interface with rich TUI rendering", default=True),
    ],
    "Provider Bricks (pick exactly 1)": [
        CatalogEntry(
            "BRK-200", p.label, p.blurb,
            default=(p.name == "ollama"),
            config=preset_config(p),
            preset=p.name,
        )
        for p in PRESETS.values()
    ],
    "Tool Bricks": [
        CatalogEntry("BRK-410", "File Tools", "bash, read/write file, glob, grep, LSP diagnostics", default=True),
        CatalogEntry("BRK-420", "CloakBrowser", "Stealth web browsing that passes bot detection", default=True),
        CatalogEntry("BRK-430", "GitHub", "Read repo issues; feeds the Dreamer triaged community requests"),
        CatalogEntry("BRK-450", "Registry Installer", "Fetch and install bricks from the central registry"),
    ],
    "Memory Bricks": [
        CatalogEntry("BRK-600", "Lossless Context (LCM)", "SQLite immutable store + DAG compaction"),
        CatalogEntry("BRK-610", "MemPalace", "Spatial vector memory + temporal knowledge graph"),
        CatalogEntry("BRK-620", "LLM Wiki", "Persistent synthesized markdown knowledge base"),
    ],
    "Logging Bricks": [
        CatalogEntry("BRK-700", "Token Logger", "Track token usage and cost per call"),
        CatalogEntry("BRK-710", "Tool Tracer", "Record every tool call and result"),
        CatalogEntry("BRK-720", "Diagnostics", "Session stats the Dreamer mines for proposals"),
    ],
    "Security Bricks": [
        CatalogEntry("BRK-800", "Command Firewall", "Block destructive shell commands"),
        CatalogEntry("BRK-810", "Sandbox", "Isolated execution environment"),
    ],
    "Improvement Bricks": [
        CatalogEntry("BRK-900", "Auto-Fixer", "Repair malformed tool calls without an LLM round-trip"),
    ],
    "Soul Bricks (needed for /afk)": [
        CatalogEntry("BRK-500", "Foreman", "Site-boss orchestrator: plans, delegates, verifies"),
        CatalogEntry("BRK-510", "Dreamer", "Lateral thinker: mines logs, proposes improvements"),
        CatalogEntry("BRK-540", "Mason", "Builder sub-agent that executes approved jobs"),
    ],
}

_REQUIRED_GROUPS = {
    "Interface Bricks (pick at least 1)",
    "Provider Bricks (pick exactly 1)",
}
_SINGLE_PICK_GROUPS = {"Provider Bricks (pick exactly 1)"}


def _print_banner(console: Console) -> None:
    console.print()
    console.print(Text(" ▀▄▀▄▀▄  brikie installer  ▄▀▄▀▄▀", style=f"bold {ACCENT}"))
    console.print(Text("   build your agent · brick by brick", style="italic #c8855a"))
    console.print()


def _show_presets(console: Console) -> List[str]:
    presets = sorted(p.stem for p in SETS_DIR.glob("*.json"))
    table = Table(border_style=BORDER, header_style=f"bold {ACCENT_SOFT}", show_lines=False)
    table.add_column("preset")
    table.add_column("bricks", justify="right")
    table.add_column("description")
    for name in presets:
        data = json.loads((SETS_DIR / f"{name}.json").read_text())
        table.add_row(name, str(len(data.get("bricks", []))), data.get("description", ""))
    console.print(table)
    return presets


def _pick_group(console: Console, group: str, entries: List[CatalogEntry]) -> List[CatalogEntry]:
    """Prompt for a multi-select within one catalog group."""
    required = group in _REQUIRED_GROUPS
    console.print(Text(f"\n{group}", style=f"bold {ACCENT_SOFT}"))
    defaults: List[int] = []
    for i, entry in enumerate(entries, start=1):
        marker = "●" if entry.default else "○"
        if entry.default:
            defaults.append(i)
        console.print(f"  {marker} {i}. [bold]{entry.label}[/]  [dim]{entry.blurb}[/]")

    default_str = ",".join(str(d) for d in defaults) if defaults else "none"
    while True:
        raw = console.input(
            f"  [dim]numbers (comma-sep), 'all', or enter for[/] [{ACCENT}]{default_str}[/]: "
        ).strip().lower()
        if not raw:
            picked = [entries[d - 1] for d in defaults]
        elif raw in ("none", "-"):
            picked = []
        elif raw == "all":
            picked = list(entries)
        else:
            try:
                idxs = [int(p) for p in raw.replace(" ", "").split(",") if p]
                picked = [entries[i - 1] for i in idxs if 1 <= i <= len(entries)]
            except ValueError:
                console.print("  [red]Didn't understand — try e.g. 1,3[/]")
                continue
        if required and not picked:
            console.print("  [red]This category needs at least one brick.[/]")
            continue
        if group in _SINGLE_PICK_GROUPS and len(picked) > 1:
            console.print(f"  [dim]keeping just the first: {picked[0].label}[/]")
            picked = picked[:1]
        return picked


def _configure_provider(console: Console, entry: CatalogEntry) -> CatalogEntry:
    """Let the user adjust provider connection settings."""
    console.print(Text("\nProvider configuration", style=f"bold {ACCENT_SOFT}"))
    cfg = dict(entry.config)
    for key, prompt in (("model", "model"), ("base_url", "base URL"), ("api_key", "API key")):
        current = cfg.get(key, "")
        raw = console.input(f"  {prompt} [dim]([/][{ACCENT}]{current}[/][dim])[/]: ").strip()
        if raw:
            cfg[key] = raw
    entry.config = cfg
    return entry


def run_installer() -> int:
    console = Console(highlight=False)
    _print_banner(console)

    console.print("Use a preset Build Set, or compose a custom one brick by brick.\n")
    presets = _show_presets(console)

    choice = console.input(
        f"\npreset name, or [{ACCENT}]custom[/] [dim](enter for[/] [{ACCENT}]default[/][dim])[/]: "
    ).strip().lower() or "default"

    if choice in presets:
        console.print(Panel(
            f"Run your agent with:\n\n  [bold {ACCENT_SOFT}]brikie --set {choice}[/]",
            border_style=BORDER, padding=(1, 2),
        ))
        return 0

    if choice != "custom":
        console.print(f"[red]Unknown preset '{choice}'.[/]")
        return 1

    selected: List[CatalogEntry] = []
    for group, entries in CATALOG.items():
        selected.extend(_pick_group(console, group, entries))

    for entry in selected:
        if entry.brk == "BRK-200":
            _configure_provider(console, entry)

    name = console.input(
        f"\nname your Build Set [dim](enter for[/] [{ACCENT}]custom[/][dim])[/]: "
    ).strip() or "custom"

    manifest = {
        "name": name,
        "description": "Custom Build Set composed with the brikie installer",
        "bricks": [
            ({"brk": e.brk, "config": e.config} if e.config else {"brk": e.brk})
            for e in selected
        ],
    }
    out_path = SETS_DIR / f"{name}.json"
    out_path.write_text(json.dumps(manifest, indent=2) + "\n")

    brick_list = ", ".join(e.label for e in selected)
    console.print(Panel(
        f"[bold]{len(selected)}[/] bricks seated: [dim]{brick_list}[/]\n"
        f"Saved to [dim]{out_path}[/]\n\n"
        f"Run your agent with:\n\n  [bold {ACCENT_SOFT}]brikie --set {name}[/]",
        title=f"[bold {ACCENT}]build set ready[/]",
        border_style=BORDER, padding=(1, 2),
    ))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run_installer())
    except (KeyboardInterrupt, EOFError):
        print("\ninstaller cancelled.")
        sys.exit(130)
