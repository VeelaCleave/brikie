"""Asynchronous event loop for the Brikie Baseplate kernel.

Orchestrates the two-phase lifecycle:
  1. WARM_UP — await all bricks reach ACTIVE state.
  2. ACTIVE — main loop: input → hooks → provider → tool execution → output.

The loop routes user input through middleware hooks, calls the Provider
Brick for LLM completion, processes tool calls, and renders responses
through Interface Bricks.
"""

import json
import logging
from typing import Any, Dict, List

from brikie.config.types import HookEvent, HookType, Message, ToolCall
from brikie.kernel.hooks import HookDispatcher
from brikie.kernel.registry import BrickRegistry, InterfaceBrick, ProviderBrick, ToolBrick
from brikie.kernel.state import StateManager

logger = logging.getLogger(__name__)


class EventLoop:
    """Core event loop driving the Baseplate lifecycle.

    Two-phase execution:
    - WARM_UP: Initialize every registered Brick.
    - ACTIVE: Repeatedly capture input, dispatch hooks, call the LLM,
      execute tool calls, and render output.
    """

    def __init__(
        self,
        registry: BrickRegistry,
        state: StateManager,
        hooks: HookDispatcher,
    ) -> None:
        self._registry = registry
        self._state = state
        self._hooks = hooks
        self._message_history: List[Message] = []

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Execute the two-phase event loop.

        Phase 1 (WARM_UP):
        - Call init() on every registered Brick so they reach ACTIVE state.

        Phase 2 (ACTIVE):
        - Continuously: capture input → PRE_PARSE → PRE_LLM → LLM
          → POST_LLM → (tool calls: PRE_TOOL → execute → POST_TOOL →
          POST_TOOL_CALL) → render output.

        Exits on KeyboardInterrupt.
        """
        await self._phase_warm_up()

        interfaces = self._registry.get_all(InterfaceBrick)
        providers = self._registry.get_all(ProviderBrick)

        if not interfaces:
            logger.warning("No InterfaceBrick registered.")
        if not providers:
            logger.warning("No ProviderBrick registered.")

        try:
            while True:
                await self._turn()
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt — shutting down.")

    # ------------------------------------------------------------------
    # Phase 1 — Warm Up
    # ------------------------------------------------------------------

    async def _phase_warm_up(self) -> None:
        """Initialize every brick; each brick sets its own state to ACTIVE."""
        bricks = list(self._registry._bricks.values())
        for brick in bricks:
            logger.info("Warming up brick: %s", brick.name)
            await brick.init()
        logger.info("Warm-up complete: %d brick(s) active.", len(bricks))

    # ------------------------------------------------------------------
    # Phase 2 — Single Turn
    # ------------------------------------------------------------------

    async def _turn(self) -> None:
        """Execute one full input→output turn."""
        # --- Input ---
        user_text = await self._capture_input()
        if not user_text:
            return

        # Create user message and append to history
        user_msg = Message(role="user", content=user_text)
        self._message_history.append(user_msg)

        # PRE_PARSE
        hook_event = HookEvent(
            hook_type=HookType.PRE_PARSE,
            data=user_msg,
            brick_name="event_loop",
        )
        await self._hooks.dispatch(hook_event.hook_type, hook_event)

        # PRE_LLM
        hook_event = HookEvent(
            hook_type=HookType.PRE_LLM,
            data=self._message_history,
            brick_name="event_loop",
        )
        await self._hooks.dispatch(hook_event.hook_type, hook_event)

        # --- LLM Completion ---
        tool_schemas = self._collect_tool_schemas()
        messages_dicts = self._messages_to_dicts()
        content, raw_calls = await self._call_providers(tool_schemas)

        # POST_LLM
        hook_event = HookEvent(
            hook_type=HookType.POST_LLM,
            data={"content": content, "tool_calls": raw_calls},
            brick_name="event_loop",
        )
        await self._hooks.dispatch(hook_event.hook_type, hook_event)

        # --- Tool Execution (if any) ---
        if raw_calls:
            await self._handle_tools(raw_calls)
            return

        # No tools — store assistant message and render
        assistant_msg = Message(role="assistant", content=content)
        self._message_history.append(assistant_msg)
        await self._render_output(content)

    async def _handle_tools(
        self,
        raw_calls: List[Dict[str, Any]],
        depth: int = 0,
    ) -> None:
        """Process tool calls, then get the LLM to continue."""
        if depth > 10:
            logger.warning("Tool-call depth limit reached (10).")
            assistant_msg = Message(
                role="assistant",
                content="Tool-call loop detected.",
            )
            self._message_history.append(assistant_msg)
            await self._render_output("Tool-call loop detected.")
            return

        # Convert raw tool-call dicts to ToolCall objects
        tool_calls = self._raw_to_tool_calls(raw_calls)

        # PRE_TOOL
        hook_event = HookEvent(
            hook_type=HookType.PRE_TOOL,
            data=tool_calls,
            brick_name="event_loop",
        )
        await self._hooks.dispatch(hook_event.hook_type, hook_event)

        # Execute tools
        tool_calls = await self.process_tool_calls(tool_calls)

        # POST_TOOL
        hook_event = HookEvent(
            hook_type=HookType.POST_TOOL,
            data=tool_calls,
            brick_name="event_loop",
        )
        await self._hooks.dispatch(hook_event.hook_type, hook_event)

        # Store tool results in conversation
        for tc in tool_calls:
            tool_msg = Message(
                role="tool",
                content=str(tc.result) if tc.result else "null",
                tool_call_id=tc.name,
            )
            self._message_history.append(tool_msg)

        # POST_TOOL_CALL
        hook_event = HookEvent(
            hook_type=HookType.POST_TOOL_CALL,
            data=tool_calls,
            brick_name="event_loop",
        )
        await self._hooks.dispatch(hook_event.hook_type, hook_event)

        # Continue: LLM processes tool results
        tool_schemas = self._collect_tool_schemas()
        content, more_raw = await self._call_providers(tool_schemas)

        if more_raw:
            await self._handle_tools(more_raw, depth + 1)
            return

        # Final response
        assistant_msg = Message(role="assistant", content=content)
        self._message_history.append(assistant_msg)
        await self._render_output(content)

    # ------------------------------------------------------------------
    # Tool Execution
    # ------------------------------------------------------------------

    async def process_tool_calls(self, tool_calls: List[ToolCall]) -> List[ToolCall]:
        """Execute each tool call using registered ToolBricks.

        Finds the ToolBrick that provides each tool and runs it.
        Updates the ToolCall.result field in-place.
        """
        tools = self._registry.get_all(ToolBrick)
        for tc in tool_calls:
            executed = False
            for tool_brick in tools:
                if hasattr(tool_brick, "tools") and tool_brick.tools is not None:
                    if any(
                        s.get("function", {}).get("name") == tc.name
                        for s in tool_brick.tools
                    ):
                        logger.info("Executing tool %s via brick %s", tc.name, tool_brick.name)
                        result = await tool_brick.execute(tc.name, tc.args)
                        tc.result = str(result)
                        executed = True
                        break
                # Fallback: try executing directly
                if not executed:
                    try:
                        result = await tool_brick.execute(tc.name, tc.args)
                        tc.result = str(result)
                        executed = True
                        break
                    except (KeyError, ValueError):
                        continue

            if not executed:
                tc.result = f"No ToolBrick found for tool '{tc.name}'"
                logger.warning("Unmatched tool call: %s", tc.name)

        return tool_calls

    def _raw_to_tool_calls(self, raw: List[Dict[str, Any]]) -> List[ToolCall]:
        """Convert raw provider tool-call dicts to ToolCall objects."""
        result: List[ToolCall] = []
        for item in raw:
            # OpenAI format: {"type": "function", "function": {"name": ..., "arguments": ...}}
            if "function" in item:
                func = item["function"]
                name = func.get("name", "")
                args_raw = func.get("arguments", "{}")
                try:
                    args = json.loads(args_raw)
                except (json.JSONDecodeError, TypeError):
                    args = args_raw
                result.append(ToolCall(name=name, args=args))
            # Generic format: {"name": ..., "args": ...}
            elif "name" in item:
                result.append(ToolCall(name=item["name"], args=item.get("args", {})))
        return result

    # ------------------------------------------------------------------
    # Message serialization
    # ------------------------------------------------------------------

    def _messages_to_dicts(self) -> List[Dict[str, Any]]:
        """Serialize the message history to provider-compatible dicts."""
        return [
            {
                "role": m.role,
                "content": m.content,
                "tool_call_id": m.tool_call_id,
            }
            for m in self._message_history
        ]

    # ------------------------------------------------------------------
    # Tool Schema Collection
    # ------------------------------------------------------------------

    def _collect_tool_schemas(self) -> List[Dict[str, Any]]:
        """Collect tool schemas from all Tool Bricks that expose them."""
        tools = self._registry.get_all(ToolBrick)
        schemas: List[Dict[str, Any]] = []
        for tool in tools:
            if hasattr(tool, "tools") and tool.tools is not None:
                schemas.extend(tool.tools)
        return schemas

    # ------------------------------------------------------------------
    # Provider Routing
    # ------------------------------------------------------------------

    async def _call_providers(
        self,
        tool_schemas: List[Dict[str, Any]],
    ) -> tuple[str, List[Dict[str, Any]]]:
        """Route messages to the first available Provider.

        Returns (content, raw_tool_call_dicts).
        """
        providers = self._registry.get_all(ProviderBrick)
        messages = self._messages_to_dicts()
        for provider in providers:
            try:
                return await provider.get_completion(messages, tool_schemas)
            except Exception as exc:
                logger.error("Provider %s failed: %s", provider.name, exc)
        return "", []

    # ------------------------------------------------------------------
    # Input / Output
    # ------------------------------------------------------------------

    async def _capture_input(self) -> str:
        """Capture input from the first responsive Interface Brick."""
        interfaces = self._registry.get_all(InterfaceBrick)
        for iface in interfaces:
            text = await iface.get_input()
            if text:
                return text
        return ""

    async def _render_output(self, content: str) -> None:
        """Render output through every registered Interface Brick."""
        interfaces = self._registry.get_all(InterfaceBrick)
        for iface in interfaces:
            await iface.output(content)