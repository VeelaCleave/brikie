"""First-run onboarding — get a working provider in under a minute.

Triggered the first time ``brikie`` boots the default set on an
interactive terminal (and never again once ``~/.brikie/onboarded``
exists, or when provider CLI overrides are given). Probes for local
model servers (Ollama, LM Studio, vLLM), sniffs conventional API-key
environment variables, and turns one keystroke into a working provider
config written into the default Build Set.

Rerun any time with ``brikie --onboard``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.text import Text

from brikie.config.provider_presets import (
    PRESETS,
    ProviderPreset,
    detect_env_keys,
    detect_local_servers,
    preset_config,
)

ACCENT = "#ff762e"
ACCENT_SOFT = "#ff9e4f"

MARKER = Path.home() / ".brikie" / "onboarded"


def should_onboard(args: argparse.Namespace) -> bool:
    """True when this boot should run the first-run wizard.

    Only for the default set, on a real terminal, with no provider
    overrides on the CLI, and only when the user hasn't onboarded before.
    """
    if getattr(args, "onboard", False):
        return True
    if args.set != "default":
        return False
    if args.model or args.base_url or args.api_key or getattr(args, "preset", None):
        return False
    if MARKER.exists():
        return False
    return sys.stdin.isatty() and sys.stdout.isatty()


def maybe_onboard(args: argparse.Namespace, sets_dir: Path) -> None:
    """Run the wizard when appropriate; never raises into the boot path."""
    if not should_onboard(args):
        return
    try:
        run_onboarding(sets_dir)
    except (KeyboardInterrupt, EOFError):
        print("\nsetup skipped — run `brikie --onboard` any time.")


def run_onboarding(sets_dir: Path) -> None:
    """Interactive provider setup; writes the default set and the marker."""
    console = Console(highlight=False)
    console.print()
    console.print(Text(" ▀▄▀▄▀▄  welcome to brikie  ▄▀▄▀▄▀", style=f"bold {ACCENT}"))
    console.print(Text("   one quick question: where does your model live?",
                       style="italic #c8855a"))
    console.print()

    local = detect_local_servers()
    env_keys = set(detect_env_keys())

    options = _ordered_options(local, env_keys)
    for i, preset in enumerate(options, start=1):
        note = ""
        if preset.name in local:
            count = len(local[preset.name])
            note = f"  [green]● running now ({count} model{'s' if count != 1 else ''})[/]"
        elif preset.name in env_keys:
            note = f"  [green]● ${preset.key_env} found[/]"
        console.print(
            f"  {i}. [bold]{preset.label}[/]  [dim]{preset.blurb}[/]{note}"
        )

    default_idx = 1
    raw = console.input(
        f"\npick a number [dim](enter for[/] [{ACCENT}]{default_idx}. "
        f"{options[0].label}[/][dim])[/]: "
    ).strip()
    try:
        idx = int(raw) if raw else default_idx
        preset = options[idx - 1] if 1 <= idx <= len(options) else options[0]
    except ValueError:
        preset = options[0]

    model = _pick_model(console, preset, local.get(preset.name, []))
    if preset.auth == "oauth":
        # Sign-in-with-ChatGPT: pick it, click the link, you're in. No key.
        api_key = None
        _do_oauth_login(console)
    else:
        api_key = _pick_key(console, preset, env_keys)

    config = preset_config(preset, model)
    if api_key is not None:
        config["api_key"] = api_key

    _write_default_set(sets_dir, config)
    MARKER.parent.mkdir(parents=True, exist_ok=True)
    MARKER.write_text(f"{preset.name}\n")

    _connect_chat(console, sets_dir)

    console.print(
        f"\n[bold {ACCENT_SOFT}]done[/] — {preset.label} / "
        f"[bold]{config['model']}[/] saved. "
        f"[dim](rerun this any time with `brikie config`)[/]\n"
    )


def run_config(sets_dir: Path) -> int:
    """``brikie config`` — re-run setup (provider + chat) any time."""
    try:
        run_onboarding(sets_dir)
        return 0
    except (KeyboardInterrupt, EOFError):
        print("\nconfig cancelled.")
        return 130


def _do_oauth_login(console: Console) -> None:
    """Run the ChatGPT sign-in flow inline: open a link, wait, done."""
    import asyncio

    from brikie.auth.openai_oauth import (
        OAuthError,
        load_tokens,
        run_login_flow,
    )

    if load_tokens() is not None:
        console.print("  [green]● already signed in to OpenAI[/] "
                      "[dim](re-run `brikie login openai` to switch accounts)[/]")
        return
    console.print("  [dim]opening your browser to sign in with ChatGPT…[/]")
    try:
        asyncio.run(run_login_flow())
        console.print("  [green]✓ signed in with ChatGPT[/]")
    except OAuthError as exc:
        console.print(f"  [yellow]sign-in didn't finish: {exc}[/]")
        console.print("  [dim]you can retry any time with `brikie login openai`[/]")
    except KeyboardInterrupt:
        console.print("  [yellow]sign-in cancelled[/] "
                      "[dim](retry with `brikie login openai`)[/]")


def _connect_chat(console: Console, sets_dir: Path) -> None:
    """Optional onboarding step: connect a chat platform with just a token."""
    from brikie.connect import CHAT_PLATFORMS, add_interface_to_set, save_env_var

    console.print(Text("\nChat from your phone? (optional)", style=f"bold {ACCENT_SOFT}"))
    platforms = list(CHAT_PLATFORMS.values())
    for i, plat in enumerate(platforms, start=1):
        console.print(f"  {i}. [bold]{plat['label']}[/]  [dim]{plat['make_bot']}[/]")
    raw = console.input(
        f"  [dim]pick a number, or enter to[/] [{ACCENT}]skip[/]: "
    ).strip()
    if not raw or not raw.isdigit() or not (1 <= int(raw) <= len(platforms)):
        return
    plat = platforms[int(raw) - 1]

    if plat["post_step"]:
        console.print(f"  [dim]first: {plat['post_step']}[/]")
    token = console.input(f"  paste your {plat['label']} bot token: ").strip()
    if not token:
        console.print("  [yellow]no token — skipped.[/]")
        return

    save_env_var(plat["token_env"], token)
    add_interface_to_set(sets_dir, "default", plat["brk"])
    console.print(
        f"  [green]✓ {plat['label']} connected[/] [dim](token saved; the "
        f"first person to message the bot becomes its owner)[/]"
    )

    # The whole point: run it as a background service, not a terminal.
    from brikie.gateway import install_service, make_gateway_set

    raw = console.input(
        f"  run {plat['label']} now as a background service? "
        f"[dim](enter for[/] [{ACCENT}]yes[/][dim])[/]: "
    ).strip().lower()
    if raw in ("", "y", "yes"):
        gw_set = make_gateway_set(sets_dir, "default", plat["brk"])
        ok, msg = install_service(gw_set)
        if ok:
            console.print(f"  [green]✓ {plat['label']} gateway is live[/] — "
                          f"[dim]close this window, the bot stays online.[/]")
            console.print(f"  [dim]{msg}[/]")
        else:
            console.print(f"  [yellow]couldn't start the service: {msg}[/]")
            console.print("  [dim]start it later with: brikie gateway restart[/]")


# ──────────────────────────────────────────────────────────────────────
# Steps
# ──────────────────────────────────────────────────────────────────────


def _ordered_options(
    local: dict[str, list[str]], env_keys: set[str]
) -> list[ProviderPreset]:
    """Presets sorted: running local servers, then found keys, then the rest."""
    running = [p for p in PRESETS.values() if p.name in local]
    keyed = [p for p in PRESETS.values() if p.name in env_keys and p.name not in local]
    rest = [
        p for p in PRESETS.values()
        if p.name not in local and p.name not in env_keys
    ]
    return running + keyed + rest


def _pick_model(
    console: Console, preset: ProviderPreset, available: list[str]
) -> str:
    """Choose a model: detected list when we have one, else the default."""
    if available:
        console.print(f"\nmodels on your {preset.label}:")
        shown = available[:8]
        for i, model in enumerate(shown, start=1):
            console.print(f"  {i}. {model}")
        raw = console.input(
            f"pick a number or type a name [dim](enter for[/] "
            f"[{ACCENT}]{shown[0]}[/][dim])[/]: "
        ).strip()
        if not raw:
            return shown[0]
        if raw.isdigit() and 1 <= int(raw) <= len(shown):
            return shown[int(raw) - 1]
        return raw

    suggestion = preset.default_model
    prompt = (
        f"\nmodel name [dim](enter for[/] [{ACCENT}]{suggestion}[/][dim])[/]: "
        if suggestion else "\nmodel name: "
    )
    while True:
        raw = console.input(prompt).strip()
        if raw:
            return raw
        if suggestion:
            return suggestion
        console.print("  [red]this provider needs a model name.[/]")


def _pick_key(
    console: Console, preset: ProviderPreset, env_keys: set[str]
) -> str | None:
    """Resolve the API key strategy. None = keep preset_config's default."""
    if not preset.key_env:
        return None  # local server — "not-needed"
    if preset.name in env_keys:
        console.print(
            f"\n[green]using the key from ${preset.key_env}[/] "
            f"[dim](nothing stored on disk)[/]"
        )
        return None  # keep the env: reference
    console.print(
        f"\n[dim]tip: `export {preset.key_env}=...` keeps the key out of "
        f"config files entirely.[/]"
    )
    pasted = console.input(
        f"paste your {preset.label} API key: ", password=True
    ).strip()
    if not pasted:
        console.print(
            f"[yellow]no key given — saved as env:{preset.key_env}; export "
            f"it before chatting.[/]"
        )
        return None
    return pasted


def _write_default_set(sets_dir: Path, provider_config: dict[str, str]) -> None:
    """Replace the BRK-200 entry's config in the default Build Set."""
    path = sets_dir / "default.json"
    data = json.loads(path.read_text())
    for entry in data.get("bricks", []):
        if isinstance(entry, dict) and entry.get("brk") == "BRK-200":
            entry["config"] = provider_config
            break
    else:
        data.setdefault("bricks", []).insert(
            0, {"brk": "BRK-200", "config": provider_config}
        )
    path.write_text(json.dumps(data, indent=2) + "\n")
