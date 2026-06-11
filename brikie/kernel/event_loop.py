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
from brikie.bricks.improvement.base import ImprovementBrick
from brikie.bricks.logging.base import LoggingBrick
from brikie.bricks.memory.memory_brick import MemoryBrick
from brikie.bricks.security.base import SecurityBrick

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
        improvement_bricks: List[ImprovementBrick] | None = None,
        security_bricks: List[SecurityBrick] | None = None,
    ) -> None:
        self._registry = registry
        self._state = state
        self._hooks = hooks
        self._message_history: List[Message] = []
        self._improvement_bricks = improvement_bricks or []
        self._security_bricks = security_bricks or []

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

        # Register memory brick hooks for LCM integration
        self._register_memory_hooks()

        # Register logging brick hooks for diagnostics
        self._register_logging_hooks()

        # Register improvement brick hooks for auto-fix
        self._register_improvement_hooks()

        # Register security brick hooks for firewalling and sandboxing
        self._register_security_hooks()

    def _register_memory_hooks(self) -> None:
        """Register MemoryBrick callbacks with the hook dispatcher.

        Memory Bricks intercept PRE_LLM and POST_LLM hooks to:
        - Store incoming/outgoing messages in the immutable store
        - Build compressed context windows for LLM calls
        - Trigger DAG compaction when budget thresholds are exceeded
        """
        memory_bricks = self._registry.get_all(MemoryBrick)
        for brick in memory_bricks:
            # Register PRE_LLM hook — intercept incoming messages
            self._hooks.register(
                HookType.PRE_LLM,
                lambda data, b=brick: self._memory_pre_llm(b, data),
            )
            # Register POST_LLM hook — intercept outgoing messages
            self._hooks.register(
                HookType.POST_LLM,
                lambda data, b=brick: self._memory_post_llm(b, data),
            )
            logger.info("Registered memory hooks for brick: %s", brick.name)

    async def _memory_pre_llm(self, brick: MemoryBrick, data: Any) -> None:
        """Handle PRE_LLM hook for memory bricks.

        Intercepts the message history and stores each message in LCM.
        Builds compressed context before LLM calls.
        """
        # Session ID — use "default" if not set in state
        session_id = self._state.get("session_id", "default")
        # Ensure session exists and build context
        context = await brick.build_context(session_id)
        # Store is handled by the brick's intercept_message
        # No need to modify the event flow here
        return context

    def _register_logging_hooks(self) -> None:
        """Register LoggingBrick hook callbacks with the hook dispatcher.

        Logging Bricks expose their callbacks via get_hook_callbacks().
        The EventLoop reads these and registers each callback at the
        appropriate hook stage during warm-up.
        """
        logging_bricks = self._registry.get_all(LoggingBrick)
        for brick in logging_bricks:
            callbacks = brick.get_hook_callbacks()
            for hook_type, cb_list in callbacks.items():
                for cb in cb_list:
                    self._hooks.register(hook_type, cb)
            logger.info(
                "Registered %d hook callback(s) for logging brick: %s",
                sum(len(v) for v in callbacks.values()),
                brick.name,
            )

    def _register_improvement_hooks(self) -> None:
        """Register ImprovementBrick hook callbacks with the hook dispatcher.

        Improvement Bricks are passed explicitly to EventLoop (not registered
        in the BrickRegistry's Brick union) because they need access to the
        registry for re-executing fixed tool calls.
        """
        for brick in self._improvement_bricks:
            callbacks = brick.get_hook_callbacks()
            for hook_type, cb_list in callbacks.items():
                for cb in cb_list:
                    self._hooks.register(hook_type, cb)
            logger.info(
                "Registered improvement brick: %s (%d callback(s))",
                brick.name,
                sum(len(v) for v in callbacks.values()),
            )

    def _register_security_hooks(self) -> None:
        """Register SecurityBrick hook callbacks with the hook dispatcher.

        Security Bricks hook PRE_TOOL to evaluate tool calls before
        execution and potentially BLOCK dangerous commands.
        """
        for brick in self._security_bricks:
            callbacks = brick.get_hook_callbacks()
            for hook_type, cb_list in callbacks.items():
                for cb in cb_list:
                    self._hooks.register(hook_type, cb)
            logger.info(
                "Registered security brick: %s (%d callback(s))",
                brick.name,
                sum(len(v) for v in callbacks.values()),
            )

    async def _memory_post_llm(self, brick: MemoryBrick, data: Any) -> None:
        """Handle POST_LLM hook for memory bricks.

        Stores the assistant's response in LCM and checks if compaction
        is needed based on budget thresholds.
        """
        session_id = self._state.get("session_id", "default")
        if isinstance(data, dict):
            content = data.get("content", "")
            await brick.intercept_message(session_id, "assistant", content)
        return None

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
            self._message_history.append(
                Message(role="assistant", content="Tool-call loop detected.")
            )
            await self._render_output("Tool-call loop detected.")
            return

        # Store the assistant message with tool calls in history
        assistant_msg = Message(
            role="assistant",
            content="",
            tool_calls=raw_calls,
        )
        self._message_history.append(assistant_msg)

        # Convert raw tool-call dicts to ToolCall objects, preserving call IDs
        tool_calls = self._raw_to_tool_calls(raw_calls)

        # PRE_TOOL
        await self._hooks.dispatch(HookType.PRE_TOOL, HookEvent(
            hook_type=HookType.PRE_TOOL,
            data=tool_calls,
            brick_name="event_loop",
        ))

        # Execute tools
        tool_calls = await self.process_tool_calls(tool_calls)

        # POST_TOOL
        await self._hooks.dispatch(HookType.POST_TOOL, HookEvent(
            hook_type=HookType.POST_TOOL,
            data=tool_calls,
            brick_name="event_loop",
        ))

        # Store tool results — use the actual call ID from the LLM response
        for tc in tool_calls:
            self._message_history.append(Message(
                role="tool",
                content=str(tc.result) if tc.result else "null",
                tool_call_id=tc.tool_call_id or tc.name,
            ))

        # POST_TOOL_CALL
        await self._hooks.dispatch(HookType.POST_TOOL_CALL, HookEvent(
            hook_type=HookType.POST_TOOL_CALL,
            data=tool_calls,
            brick_name="event_loop",
        ))

        # Continue: LLM processes tool results
        content, more_raw = await self._call_providers(self._collect_tool_schemas())

        if more_raw:
            await self._handle_tools(more_raw, depth + 1)
            return

        self._message_history.append(Message(role="assistant", content=content))
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
        """Convert raw provider tool-call dicts to ToolCall objects.

        Preserves the OpenAI ``id`` field (e.g. ``call_abc123``) so tool
        results can be matched back to the LLM's original tool call.
        """
        result: List[ToolCall] = []
        for item in raw:
            call_id = item.get("id", "")
            # OpenAI format: {"id": "call_xxx", "type": "function", "function": {...}}
            if "function" in item:
                func = item["function"]
                name = func.get("name", "")
                args_raw = func.get("arguments", "{}")
                try:
                    args = json.loads(args_raw)
                except (json.JSONDecodeError, TypeError):
                    args = args_raw
                result.append(ToolCall(name=name, args=args, tool_call_id=call_id))
            # Generic format: {"name": ..., "args": ...}
            elif "name" in item:
                result.append(ToolCall(
                    name=item["name"],
                    args=item.get("args", {}),
                    tool_call_id=call_id,
                ))
        return result

    # ------------------------------------------------------------------
    # Message serialization
    # ------------------------------------------------------------------

    def _messages_to_dicts(self) -> List[Dict[str, Any]]:
        """Serialize the message history to provider-compatible dicts.

        Follows the OpenAI /chat/completions wire format:
        - ``role: "assistant"`` with ``tool_calls`` includes a ``tool_calls`` array
        - ``role: "tool"`` includes ``tool_call_id`` matching the call
        - Other roles pass ``content`` and ``role`` verbatim
        """
        result: List[Dict[str, Any]] = []
        for m in self._message_history:
            if m.role == "tool":
                result.append({
                    "role": "tool",
                    "content": m.content,
                    "tool_call_id": m.tool_call_id or "",
                })
            elif m.role == "assistant" and m.tool_calls:
                result.append({
                    "role": "assistant",
                    "content": m.content or None,
                    "tool_calls": [
                        {
                            "id": tc.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": tc.get("function", {}).get("name", ""),
                                "arguments": tc.get("function", {}).get("arguments", "{}"),
                            },
                        }
                        for tc in m.tool_calls
                    ],
                })
            else:
                result.append({
                    "role": m.role,
                    "content": m.content,
                })
        return result

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