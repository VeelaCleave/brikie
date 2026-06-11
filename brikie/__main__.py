"""Brikie — Baseplate entry point.

Boots the kernel, registers bricks, and runs the async event loop.

No concrete brick is imported by name.  Bricks are selected by:
1. CLI args (--provider, --interface, --tool)
2. Environment variables (BRIKIE_PROVIDER, BRIKIE_INTERFACE, BRIKIE_TOOL)
3. Default fallback (imported dynamically from known subpackages)
"""

import argparse
import asyncio
import importlib
import logging
import os
import sys
from typing import Any, Dict, List

from brikie.config.types import HookType
from brikie.kernel.hooks import HookDispatcher
from brikie.kernel.registry import BrickRegistry, InterfaceBrick, ProviderBrick, ToolBrick
from brikie.kernel.state import StateManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Brick selection helpers
# ---------------------------------------------------------------------------

_DEFAULT_PROVIDER = "brikie.bricks.provider.http_provider.HTTPProvider"
_DEFAULT_INTERFACE = "brikie.bricks.interface.cli.CLIBrick"
_DEFAULT_TOOL = "brikie.bricks.tool.dummy.DummyToolBrick"


def _resolve_import(dotted: str) -> Any:
    """Import a class from a dotted ``module.ClassName`` string."""
    module_path, _, class_name = dotted.rpartition(".")
    mod = importlib.import_module(module_path)
    return getattr(mod, class_name)


def _pick_brick(
    cli_value: str | None,
    env_var: str,
    default: str,
) -> Any:
    """Resolve a brick class from CLI → env → default."""
    raw = cli_value or os.environ.get(env_var) or default
    try:
        return _resolve_import(raw)
    except (ImportError, AttributeError) as exc:
        logger.warning("Brick %s not found (%s); falling back to %s", raw, exc, default)
        return _resolve_import(default)


def parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the Baseplate entry point."""
    parser = argparse.ArgumentParser(
        prog="brikie",
        description="Modular agentic harness with Brick architecture",
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
    parser.add_argument(
        "--provider",
        default=None,
        help="Dotted path to a ProviderBrick class (env: BRIKIE_PROVIDER)",
    )
    parser.add_argument(
        "--interface",
        default=None,
        help="Dotted path to an InterfaceBrick class (env: BRIKIE_INTERFACE)",
    )
    parser.add_argument(
        "--tool",
        default=None,
        help="Dotted path to a ToolBrick class (env: BRIKIE_TOOL)",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Event loop
# ---------------------------------------------------------------------------


async def event_loop(
    registry: BrickRegistry,
    state: StateManager,
    hooks: HookDispatcher,
) -> None:
    """Run the Baseplate async event loop.

    Each iteration:
      1. Get input from interface bricks
      2. Dispatch through the middleware hook pipeline
      3. Send to provider bricks
      4. Execute any tool calls
      5. Output the response
    """
    providers: List[ProviderBrick] = registry.get_all(ProviderBrick)
    interfaces: List[InterfaceBrick] = registry.get_all(InterfaceBrick)
    tools: List[ToolBrick] = registry.get_all(ToolBrick)

    if not providers:
        print("[brikie] No provider bricks registered.", file=sys.stderr)
    if not interfaces:
        print("[brikie] No interface bricks registered.", file=sys.stderr)

    # Use first provider and interface for the basic loop
    provider = providers[0]
    interface = interfaces[0]

    # Build tool schema list from registered tool bricks
    tool_schemas = [
        {"name": t.name, "args": {}} for t in tools
    ]

    conversation_history: List[Dict[str, Any]] = []

    print(f"[brikie] Event loop started. Press Enter on empty line to exit.")

    while True:
        # 1. Get input
        user_input = await interface.get_input()
        if not user_input:
            break

        await state.set("user_input", user_input)
        conversation_history.append({"role": "user", "content": user_input})

        # 2. Dispatch hooks
        await hooks.dispatch_all(user_input)

        # 3. Send to provider
        response, tool_calls = await provider.get_completion(
            messages=conversation_history,
            tools=tool_schemas,
        )

        # 4. Execute tool calls
        for tc in tool_calls:
            tool_name = tc.get("name", "")
            tool_args = tc.get("args", {})
            # Find matching tool brick
            target_tool = next((t for t in tools if t.name == tool_name), tools[0])
            result = await target_tool.execute(tool_name, tool_args)
            # Store result in state
            await state.set(f"tool_result.{tool_name}", result)
            conversation_history.append({
                "role": "tool",
                "content": str(result),
                "tool_call_id": tc.get("id"),
            })

        # 5. Output response
        await interface.output(response)
        conversation_history.append({"role": "assistant", "content": response})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    """Bootstrap the Baseplate kernel and run the event loop.

    Bricks are resolved dynamically via ``_pick_brick`` — no concrete
    brick is imported by name at the top of this module.  The resolution
    chain is::

        CLI --provider/--interface/--tool
            → $BRIKIE_PROVIDER / $BRIKIE_INTERFACE / $BRIKIE_TOOL
                → built-in defaults
    """
    args = parse_args()

    # Kernel components
    registry = BrickRegistry()
    state = StateManager()
    hooks = HookDispatcher()

    # Dynamically resolve brick classes — never hardcoded
    ProviderCls = _pick_brick(args.provider, "BRIKIE_PROVIDER", _DEFAULT_PROVIDER)
    InterfaceCls = _pick_brick(args.interface, "BRIKIE_INTERFACE", _DEFAULT_INTERFACE)
    ToolCls = _pick_brick(args.tool, "BRIKIE_TOOL", _DEFAULT_TOOL)

    provider = ProviderCls(model=args.model, api_key=args.api_key, base_url=args.base_url)
    interface = InterfaceCls()
    tool = ToolCls()

    # Register bricks
    registry.register(provider)
    registry.register(interface)
    registry.register(tool)

    # Initialize bricks
    await provider.init()
    await interface.init()
    await tool.init()

    # Run the event loop
    try:
        await event_loop(registry, state, hooks)
    except (KeyboardInterrupt, EOFError):
        print("\n[brikie] Shutting down...")
    finally:
        await provider.shutdown()
        await interface.shutdown()
        await tool.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
