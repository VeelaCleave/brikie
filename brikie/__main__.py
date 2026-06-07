"""Brikie — Baseplate entry point.

Boots the kernel, registers bricks, and runs the async event loop.
"""

import argparse
import asyncio
import sys
from typing import Any, Dict, List

from brikie.config.types import HookType
from brikie.kernel.hooks import HookDispatcher
from brikie.kernel.registry import BrickRegistry, InterfaceBrick, ProviderBrick, ToolBrick
from brikie.kernel.state import StateManager

# ---------------------------------------------------------------------------
# Brick imports — resolved once companion modules are built
# ---------------------------------------------------------------------------

from brikie.bricks.interface.cli import CliInterfaceBrick
from brikie.bricks.provider.http_provider import HttpProviderBrick
from brikie.bricks.tool.dummy import DummyToolBrick


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
        default="https://api.openai.com/v1/chat/completions",
        help="Base URL for the provider (default: OpenAI)",
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
    """Bootstrap the Baseplate kernel and run the event loop."""
    args = parse_args()

    # Kernel components
    registry = BrickRegistry()
    state = StateManager()
    hooks = HookDispatcher()

    # Create bricks
    provider = HttpProviderBrick(
        model=args.model,
        api_key=args.api_key,
        base_url=args.base_url,
    )
    interface = CliInterfaceBrick()
    tool = DummyToolBrick()

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
        # Graceful shutdown
        await provider.shutdown()
        await interface.shutdown()
        await tool.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
