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
    return parser.parse_args(argv)


async def main() -> None:
    """Bootstrap the Baseplate kernel and run the event loop."""
    args = parse_args()

    registry = BrickRegistry()
    state = StateManager()
    hooks = HookDispatcher()

    from brikie.bricks.build.loader import BuildLoader, BuildSetError

    set_path = args.set
    if "/" not in set_path and not set_path.endswith(".json"):
        set_path = str(_BUILD_SETS_DIR / f"{set_path}.json")

    loader = BuildLoader(registry)
    try:
        loader.load(set_path)
    except BuildSetError as exc:
        print(f"[brikie] Error loading Build Set: {exc}", file=sys.stderr)
        sys.exit(1)

    # CLI flags override provider configuration before init().
    if args.model or args.base_url or args.api_key:
        for brick in registry.get_all(ProviderBrick):
            if hasattr(brick, "configure"):
                brick.configure(
                    model=args.model,
                    base_url=args.base_url,
                    api_key=args.api_key,
                )

    loop = EventLoop(
        registry=registry,
        state=state,
        hooks=hooks,
        system_prompt=DEFAULT_SYSTEM_PROMPT,
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
