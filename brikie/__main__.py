"""Brikie — Baseplate entry point.

Boots the kernel, registers bricks, and runs the async event loop.

No concrete brick is imported by name.  Bricks are selected by:
1. CLI args (--provider, --interface, --tool)
2. Environment variables (BRIKIE_PROVIDER, BRIKIE_INTERFACE, BRIKIE_TOOL)
3. Default fallback (imported dynamically from known subpackages)
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from brikie.kernel.event_loop import EventLoop
from brikie.kernel.hooks import HookDispatcher
from brikie.kernel.registry import BrickRegistry
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
        help="Build Set name (e.g. minimal, default, afk).  Default: default.",
    )
    parser.add_argument(
        "--model",
        default="gpt-4o",
        help="LLM model name (default: gpt-4o)",
    )
    parser.add_argument(
        "--api-key",
        default="",
        help="API key for the provider",
    )
    parser.add_argument(
        "--base-url",
        default="https://api.openai.com/v1",
        help="Base URL for the provider (default: OpenAI)",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    """Bootstrap the Baseplate kernel and run the event loop.

    Bricks are loaded from a Build Set manifest.  The set specifies
    exactly which bricks to register and their configuration.
    """
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
        build = loader.load(set_path)
    except BuildSetError as exc:
        print(f"[brikie] Error loading Build Set: {exc}", file=sys.stderr)
        sys.exit(1)

    # Apply CLI overrides to all provider bricks in the set
    for brick in registry.get_all(type(registry).__class__):
        if hasattr(brick, "_model") and (args.model != "gpt-4o" or args.base_url != "https://api.openai.com/v1"):
            if args.model:
                brick._model = args.model
            if args.base_url:
                brick._base_url = args.base_url
                brick._client.base_url = args.base_url
            if args.api_key:
                brick._api_key = args.api_key

    loop = EventLoop(
        registry=registry,
        state=state,
        hooks=hooks,
    )

    try:
        await loop.run()
    except (KeyboardInterrupt, EOFError):
        print("\n[brikie] Shutting down...")


def entry_point() -> None:
    """Synchronous entry point for ``brikie`` CLI command."""
    asyncio.run(main())


if __name__ == "__main__":
    entry_point()
