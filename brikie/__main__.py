"""Brikie — Baseplate entry point.

Boots the kernel, loads a Build Set of bricks, and runs the async event
loop.  No concrete brick is imported by name: the Build Set manifest
decides exactly which bricks are seated and how they are configured.

Optional CLI flags (--model, --base-url, --api-key) override the
provider configuration from the Build Set; there are no provider
defaults baked into the kernel.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from brikie.config.default_soul import DEFAULT_SYSTEM_PROMPT
from brikie.kernel.event_loop import EventLoop
from brikie.kernel.hooks import HookDispatcher
from brikie.kernel.registry import BrickRegistry, ProviderBrick
from brikie.kernel.state import StateManager

logger = logging.getLogger(__name__)

_BUILD_SETS_DIR = Path(__file__).resolve().parent / "bricks" / "build" / "sets"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the Baseplate entry point."""
    parser = argparse.ArgumentParser(
        prog="brikie",
        description="Modular agentic harness with Brick architecture",
    )
    parser.add_argument(
        "--set",
        default="default",
        help="Build Set name (e.g. minimal, local, default, afk) or a path "
             "to a Build Set JSON.  Default: default.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override the provider model from the Build Set",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Override the provider API key from the Build Set",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Override the provider base URL from the Build Set",
    )
    parser.add_argument(
        "--onboard",
        action="store_true",
        help="Rerun the first-run provider setup wizard",
    )
    parser.add_argument(
        "--log",
        default=None,
        metavar="LEVEL",
        help="Log level to stderr (debug, info, warning). The gateway "
             "service uses 'info' so its logs reach journald.",
    )
    parser.add_argument(
        "--continue",
        dest="resume",
        action="store_true",
        help="Resume the previous conversation (needs a memory brick, "
             "e.g. BRK-600)",
    )
    parser.add_argument(
        "--preset",
        default=None,
        metavar="NAME",
        help="Apply a provider preset (anthropic, openai, openrouter, "
             "groq, ollama, lmstudio, vllm) over the Build Set — ideal "
             "for sandboxed/scripted runs where env vars carry the keys",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default=None,
        help="'config' to (re)run setup, or 'gateway <status|logs|stop|"
             "restart>' to manage the background chat service. Omit to "
             "start the agent.",
    )
    parser.add_argument(
        "action",
        nargs="?",
        default=None,
        help=argparse.SUPPRESS,  # sub-action for 'gateway'
    )
    return parser.parse_args(argv)


async def main() -> None:
    """Bootstrap the Baseplate kernel and run the event loop."""
    args = parse_args()

    if args.log:
        level = getattr(logging, args.log.upper(), logging.INFO)
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )

    # Persisted secrets (chat tokens, etc.) — an explicit export wins.
    from brikie.connect import load_env_file
    load_env_file()

    # First run on a real terminal? Sort the provider out before booting.
    from brikie.onboard import maybe_onboard
    maybe_onboard(args, _BUILD_SETS_DIR)

    registry = BrickRegistry()
    state = StateManager()
    hooks = HookDispatcher()

    from brikie.bricks.build.loader import BuildLoader, BuildSetError

    set_path = args.set
    if "/" not in set_path and not set_path.endswith(".json"):
        set_path = str(_BUILD_SETS_DIR / f"{set_path}.json")

    from brikie.recovery import (
        last_good_set,
        record_good_set,
        summarize_quarantine,
    )

    loader = BuildLoader(registry, hooks=hooks)
    build = None
    loaded_path = set_path
    try:
        # Resilient: a broken brick (e.g. a bad agent-authored one) is
        # quarantined so the rest of the stack still boots.
        build = loader.load(set_path, resilient=True)
        loader.validate_minimum_stack()
    except BuildSetError as exc:
        # The requested set can't even reach a minimum stack. Fall back
        # to the last set that booted cleanly, if any.
        fallback = last_good_set()
        if fallback and fallback != str(Path(set_path).resolve()):
            print(f"[brikie] '{set_path}' couldn't start ({exc}).",
                  file=sys.stderr)
            print(f"[brikie] Falling back to your last working setup: "
                  f"{fallback}", file=sys.stderr)
            registry.clear()
            try:
                build = loader.load(fallback, resilient=True)
                loader.validate_minimum_stack()
                loaded_path = fallback
            except BuildSetError as exc2:
                print(f"[brikie] Fallback also failed: {exc2}", file=sys.stderr)
                build = None
        if build is None:
            print(f"[brikie] Couldn't start: {exc}\n"
                  f"          Run `brikie config` to set up a provider.",
                  file=sys.stderr)
            sys.exit(1)

    if build.quarantined:
        print(f"[brikie] {summarize_quarantine(build.quarantined)}",
              file=sys.stderr)

    # This set reached a viable minimum stack — remember it as the
    # fallback for a future broken boot.
    record_good_set(loaded_path)

    # CLI flags override provider configuration before init().
    # --preset applies a full named recipe; --model/--base-url/--api-key
    # refine on top of it.
    preset_overrides = {}
    if args.preset:
        from brikie.config.provider_presets import PRESETS, preset_config

        preset = PRESETS.get(args.preset)
        if preset is None:
            print(
                f"[brikie] Unknown preset '{args.preset}' — choose from: "
                f"{', '.join(PRESETS)}",
                file=sys.stderr,
            )
            sys.exit(1)
        preset_overrides = preset_config(preset)

    if preset_overrides or args.model or args.base_url or args.api_key:
        for brick in registry.get_all(ProviderBrick):
            if hasattr(brick, "configure"):
                brick.configure(**preset_overrides)
                brick.configure(
                    model=args.model,
                    base_url=args.base_url,
                    api_key=args.api_key,
                )

    # Wire the AFK manager when the set ships both negotiating souls.
    afk_manager = None
    if "dreamer" in build.souls and "foreman" in build.souls:
        from brikie.kernel.afk_manager import AFKManager
        from brikie.kernel.registry import InterfaceBrick

        cli = next(
            (b for b in registry.get_all(InterfaceBrick) if b.name == "cli"), None
        )
        afk_manager = AFKManager(registry, cli_brick=cli)

    loop = EventLoop(
        registry=registry,
        state=state,
        hooks=hooks,
        afk_manager=afk_manager,
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        souls=build.souls,
        resume=args.resume,
    )

    try:
        await loop.run()
    except (KeyboardInterrupt, EOFError):
        pass


def _run_login(provider: str) -> int:
    """``brikie login openai`` — sign in with ChatGPT and save OAuth tokens."""
    if provider not in ("openai", "chatgpt"):
        print(f"[brikie] Unknown login provider '{provider}'. Try: "
              f"brikie login openai", file=sys.stderr)
        return 1
    from brikie.auth.openai_oauth import (
        BRIKIE_AUTH_PATH,
        OAuthError,
        load_tokens,
        run_login_flow,
    )
    existing = load_tokens()
    if existing is not None:
        print("[brikie] An OpenAI login is already available "
              "(reusing it; delete "
              f"{BRIKIE_AUTH_PATH} or run again to replace it).")
    try:
        asyncio.run(run_login_flow())
    except OAuthError as exc:
        print(f"[brikie] Sign-in failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[brikie] Sign-in cancelled.", file=sys.stderr)
        return 1
    print(f"[brikie] Signed in. Tokens saved to {BRIKIE_AUTH_PATH}.\n"
          "         Use the 'OpenAI (ChatGPT login)' provider — e.g.\n"
          "         brikie config  →  pick OpenAI (ChatGPT login).")
    return 0


def entry_point() -> None:
    """Synchronous entry point for ``brikie`` CLI command."""
    args = parse_args()
    if args.command == "config":
        # Setup wizard (provider + chat) — runs its own loop, never boots
        # the kernel.
        from brikie.connect import load_env_file
        from brikie.onboard import run_config
        load_env_file()
        sys.exit(run_config(_BUILD_SETS_DIR))
    if args.command == "gateway":
        from brikie.gateway import run_gateway_command
        sys.exit(run_gateway_command(args.action or "status"))
    if args.command == "login":
        sys.exit(_run_login(args.action or "openai"))
    if args.command:
        print(f"[brikie] Unknown command '{args.command}'. Did you mean 'config'?",
              file=sys.stderr)
        sys.exit(1)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    entry_point()
