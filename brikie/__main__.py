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
    return parser.parse_args(argv)


async def main() -> None:
    """Bootstrap the Baseplate kernel and run the event loop."""
    args = parse_args()

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

    loader = BuildLoader(registry)
    try:
        build = loader.load(set_path)
        loader.validate_minimum_stack()
    except BuildSetError as exc:
        print(f"[brikie] Error loading Build Set: {exc}", file=sys.stderr)
        sys.exit(1)

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


def entry_point() -> None:
    """Synchronous entry point for ``brikie`` CLI command."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    entry_point()
