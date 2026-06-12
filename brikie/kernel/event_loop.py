"""Asynchronous event loop for the Brikie Baseplate kernel.

Orchestrates the two-phase lifecycle:
  1. WARM_UP — initialize every brick, register middleware hooks.
  2. ACTIVE — main loop: input → hooks → provider → tool execution → output.

Each turn the loop routes user input through middleware hooks, then runs
an iterative agent loop: call the Provider Brick, execute any requested
tools, feed results back, and repeat until the model answers in plain
text (or the step budget runs out).

Rendering goes through a single path: the loop prefers an Interface
Brick's rich ``render_*`` methods when present and falls back to the
plain ``output()`` channel otherwise.
"""

import asyncio
import inspect
import json
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from brikie.config.types import HookEvent, HookType, Message, ToolCall
from brikie.kernel.hooks import HookDispatcher
from brikie.kernel.registry import BrickRegistry, InterfaceBrick, ProviderBrick, ToolBrick
from brikie.kernel.state import StateManager

if TYPE_CHECKING:
    from brikie.kernel.afk_manager import AFKManager

logger = logging.getLogger(__name__)

# Max provider→tool→provider iterations per user turn.
MAX_AGENT_STEPS = 500

# AFK mode defaults: bounded by default, '/afk inf' for the endless loop.
DEFAULT_AFK_CYCLES = 3
MAX_MASON_STEPS = 12

_MASON_FALLBACK_PROMPT = (
    "You are a Mason — a builder sub-agent. Execute exactly the job you "
    "are given using your tools, verify the result, and report concisely. "
    "End your final message with 'TASK COMPLETE' or 'TASK FAILED: <reason>'."
)

_HELP_TEXT = """\
/help    show this help
/bricks  list seated bricks
/clear   clear screen and conversation history
/model   show or switch the model: /model to show, /model <id> to switch
/focus   steer the Dreamer: /focus <directive>, /focus to show, /focus clear
/afk     enter autonomous AFK mode: /afk [cycles|inf] (default 3)
/exit    quit brikie (also /quit, Ctrl-C, Ctrl-D)\
"""


def _unwrap_hook_data(data: Any) -> Any:
    """Unwrap hook data that may be wrapped in a HookEvent envelope.

    The HookDispatcher passes the raw data argument to callbacks. When the
    event_loop dispatches a HookEvent, the callback receives the HookEvent
    object directly — not the inner payload. This helper peels the envelope.

    Args:
        data: Either a raw dict/list or a HookEvent wrapping the inner payload.

    Returns:
        The inner payload (dict, list, or whatever was in data.data).
    """
    if isinstance(data, HookEvent):
        return data.data
    return data


class EventLoop:
    """Core event loop driving the Baseplate lifecycle."""

    def __init__(
        self,
        registry: BrickRegistry,
        state: StateManager,
        hooks: HookDispatcher,
        afk_manager: "AFKManager | None" = None,
        system_prompt: Optional[str] = None,
        souls: Optional[Dict[str, Any]] = None,
        resume: bool = False,
    ) -> None:
        self._registry = registry
        self._state = state
        self._hooks = hooks
        self._message_history: List[Message] = []
        self._afk_manager = afk_manager
        self._system_prompt = system_prompt
        self._souls = souls or {}
        self._resume = resume
        self._resumed_count = 0
        self._afk_watchers: List[Any] = []
        self._tokens_in = 0
        self._tokens_out = 0
        # Pending get_input() tasks when several interfaces are seated.
        self._input_tasks: Dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Execute the two-phase event loop until interrupted."""
        await self._phase_warm_up()

        if not self._registry.get_all(InterfaceBrick):
            logger.warning("No InterfaceBrick registered.")
        if not self._registry.get_all(ProviderBrick):
            logger.warning("No ProviderBrick registered.")

        await self._announce_startup()
        if self._resumed_count:
            await self._emit_info(
                "resumed",
                f"continuing your previous conversation "
                f"({self._resumed_count} messages restored).",
            )

        try:
            while True:
                await self._turn()
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt — shutting down.")
        finally:
            await self._phase_shutdown()

    # ------------------------------------------------------------------
    # Phase 1 — Warm Up
    # ------------------------------------------------------------------

    async def _phase_warm_up(self) -> None:
        """Initialize every brick; quarantine any whose init() crashes.

        A brick that raises during ``init()`` is unregistered and logged
        rather than crashing the whole boot — the same graceful-
        degradation contract the loader applies to instantiation. This is
        the safety net under agent-authored bricks: one that breaks on
        startup is dropped, and the rest of the stack still comes up.
        """
        self._quarantined: List[tuple] = []
        for brick in list(self._registry._bricks.values()):
            logger.info("Warming up brick: %s", brick.name)
            try:
                await brick.init()
            except Exception as exc:
                logger.exception("Brick %s failed to initialize — quarantined",
                                 brick.name)
                self._quarantined.append((brick.name, str(exc)))
                try:
                    self._registry.unregister(brick.name)
                except Exception:
                    logger.debug("Could not unregister %s", brick.name)
        active = len(self._registry._bricks)
        logger.info("Warm-up complete: %d brick(s) active, %d quarantined.",
                    active, len(self._quarantined))

        self._register_memory_hooks()
        await self._register_brick_hooks()

        if self._resume:
            await self._restore_history()

    async def _phase_shutdown(self) -> None:
        """Gracefully shut down every brick (HTTP clients, DBs, TUI…)."""
        for task in self._input_tasks.values():
            task.cancel()
        self._input_tasks.clear()
        for brick in list(self._registry._bricks.values()):
            try:
                await brick.shutdown()
            except Exception:
                logger.exception("Error shutting down brick %s", brick.name)

    async def _restore_history(self) -> None:
        """Repopulate the conversation from a memory brick (``--continue``).

        Probes for any brick exposing the duck-typed ``load_history``;
        the first non-empty result becomes the in-memory history so the
        next turn literally continues the prior conversation. Older,
        compacted context still arrives via the memory blob.
        """
        session_id = await self._state.get("session_id", "default")
        for brick in self._registry._bricks.values():
            if not hasattr(brick, "load_history"):
                continue
            try:
                history = await brick.load_history(session_id)
            except Exception:
                logger.exception("Resume from %s failed", getattr(brick, "name", "?"))
                continue
            restored = [
                Message(
                    role=m.get("role", "user"),
                    content=m.get("content", "") or "",
                    tool_call_id=m.get("tool_call_id"),
                )
                for m in (history or [])
                if m.get("role") and m.get("content")
            ]
            if restored:
                self._message_history = restored
                self._resumed_count = len(restored)
                logger.info(
                    "Resumed %d message(s) from %s.",
                    len(restored), getattr(brick, "name", "memory"),
                )
                return
        logger.info("Nothing to resume — starting a fresh conversation.")

    async def _announce_startup(self) -> None:
        """Give interfaces a boot summary once everything is warm."""
        providers = self._registry.get_all(ProviderBrick)
        model = next(
            (p.model for p in providers if hasattr(p, "model")), "—"
        )
        base_url = next(
            (p.base_url for p in providers if hasattr(p, "base_url")), ""
        )
        info = {
            "model": model,
            "base_url": base_url,
            "bricks": [b.name for b in self._registry._bricks.values()],
            "tool_count": len(self._collect_tool_schemas()),
            "souls": list(self._souls.keys()),
        }
        for iface in self._registry.get_all(InterfaceBrick):
            if hasattr(iface, "render_startup"):
                await iface.render_startup(info)

    def _memory_capable_bricks(self) -> List[Any]:
        """Bricks that act as memory: expose build_context + intercept_message."""
        return [
            b for b in self._registry._bricks.values()
            if hasattr(b, "build_context") and hasattr(b, "intercept_message")
        ]

    def _register_memory_hooks(self) -> None:
        """Register memory-capable brick callbacks with the hook dispatcher."""
        for brick in self._memory_capable_bricks():
            self._hooks.register(
                HookType.PRE_LLM,
                lambda data, b=brick: self._memory_pre_llm(b, data),
            )
            self._hooks.register(
                HookType.POST_LLM,
                lambda data, b=brick: self._memory_post_llm(b, data),
            )
            logger.info("Registered memory hooks for brick: %s", brick.name)

    async def _memory_pre_llm(self, brick: Any, data: Any) -> Any:
        session_id = await self._state.get("session_id", "default")
        return await brick.build_context(session_id)

    async def _memory_post_llm(self, brick: Any, data: Any) -> None:
        """Intercept POST_LLM hook to persist assistant messages into memory bricks.

        The dispatcher wraps hook data in a HookEvent — unwrap it to get
        the inner payload dict.
        """
        session_id = await self._state.get("session_id", "default")
        payload = _unwrap_hook_data(data)
        if isinstance(payload, dict):
            content = payload.get("content", "")
            if content:
                await brick.intercept_message(session_id, "assistant", content)
        return None

    async def _intercept_user_message(self, user_text: str) -> None:
        """Persist user messages into all memory-capable bricks.

        Called at the start of _turn() before the agent loop begins.
        Only intercepts non-command, non-empty text.
        """
        if not user_text or user_text.strip().startswith("/"):
            return
        session_id = await self._state.get("session_id", "default")
        for brick in self._memory_capable_bricks():
            try:
                await brick.intercept_message(session_id, "user", user_text)
            except Exception:
                logger.exception("Failed to intercept user message in brick: %s", brick.name)

    async def _register_brick_hooks(self) -> None:
        """Discover and register middleware hooks from any capable brick.

        The kernel knows no brick categories: any registered brick that
        exposes ``get_hook_callbacks()`` (sync or async) — logging,
        improvement, security, or third-party — gets its callbacks wired
        into the dispatcher.
        """
        for brick in self._registry._bricks.values():
            getter = getattr(brick, "get_hook_callbacks", None)
            if getter is None:
                continue
            callbacks = getter()
            if inspect.isawaitable(callbacks):
                callbacks = await callbacks
            count = 0
            for hook_type, cb_list in callbacks.items():
                for cb in cb_list:
                    self._hooks.register(hook_type, cb)
                    count += 1
            logger.info("Registered %d hook callback(s) from brick: %s", count, brick.name)

    # ------------------------------------------------------------------
    # Phase 2 — Single Turn
    # ------------------------------------------------------------------

    async def _turn(self) -> None:
        """Capture one user input and drive the agent loop to completion."""
        user_text = await self._capture_input()
        if not user_text:
            return

        if await self._handle_command(user_text):
            return

        user_msg = Message(role="user", content=user_text)
        self._message_history.append(user_msg)

        for iface in self._registry.get_all(InterfaceBrick):
            if hasattr(iface, "render_user_message"):
                await iface.render_user_message(user_text)

        await self._hooks.dispatch(HookType.PRE_PARSE, HookEvent(
            hook_type=HookType.PRE_PARSE,
            data=user_msg,
            brick_name="event_loop",
        ))

        # Intercept user message into memory bricks before LLM sees it
        await self._intercept_user_message(user_text)

        await self._hooks.dispatch(HookType.PRE_LLM, HookEvent(
            hook_type=HookType.PRE_LLM,
            data=self._message_history,
            brick_name="event_loop",
        ))

        await self._agent_loop()

    async def _handle_command(self, user_text: str) -> bool:
        """Process slash commands. Returns True if the input was a command."""
        cmd = user_text.strip().lower()
        if cmd in ("/exit", "/quit"):
            raise KeyboardInterrupt
        if cmd == "/help":
            await self._emit_info("commands", _HELP_TEXT)
            return True
        if cmd == "/bricks":
            lines = []
            for brick in self._registry._bricks.values():
                brk = getattr(brick, "BRICK_NUMBER", "BRK-???")
                lines.append(f"{brk}  {brick.name}  ({type(brick).__name__})")
            if self._souls:
                lines.append("")
                lines.append(f"--- {len(self._souls)} soul(s) loaded ---")
                for name, soul_obj in self._souls.items():
                    brk = getattr(soul_obj, "BRICK_NUMBER", "BRK-???")
                    lines.append(f"  {brk}  {name}  ({type(soul_obj).__name__})")
            await self._emit_info("seated bricks", "\n".join(lines) or "none")
            return True
        if cmd == "/clear":
            self._message_history.clear()
            for iface in self._registry.get_all(InterfaceBrick):
                if hasattr(iface, "clear_screen"):
                    iface.clear_screen()
            return True
        if cmd == "/model" or cmd.startswith("/model "):
            await self._handle_model_command(user_text.strip()[len("/model"):].strip())
            return True
        if cmd == "/focus" or cmd.startswith("/focus "):
            # Preserve the operator's casing — only the command word is
            # matched lowercase.
            directive = user_text.strip()[len("/focus"):].strip()
            if not directive:
                current = await self._state.get("dreamer_focus", "")
                soul_focus = getattr(self._souls.get("dreamer"), "focus", "")
                shown = current or soul_focus
                await self._emit_info(
                    "focus",
                    f"current focus: {shown}" if shown
                    else "no focus set — the Dreamer free-associates. "
                         "Set one with /focus <directive>.",
                )
            elif directive.lower() == "clear":
                await self._state.set("dreamer_focus", "")
                await self._emit_info("focus", "focus cleared.")
            else:
                await self._state.set("dreamer_focus", directive)
                await self._emit_info(
                    "focus", f"the Dreamer will now focus on: {directive}"
                )
            return True
        if cmd == "/afk" or cmd.startswith("/afk "):
            missing = [s for s in ("dreamer", "foreman") if s not in self._souls]
            if self._afk_manager is None or missing:
                needs = f" (missing souls: {', '.join(missing)})" if missing else ""
                await self._emit_info(
                    "afk",
                    f"AFK mode is not available{needs}.\n"
                    "Load a build set that includes the dreamer and foreman "
                    "souls — e.g.  brikie --set afk",
                )
            else:
                cycles = self._parse_afk_cycles(cmd)
                if cycles is None:
                    await self._emit_info(
                        "afk", "Usage: /afk [cycles] — a number, or 'inf' for endless."
                    )
                else:
                    await self._enter_afk_mode(cycles=cycles)
            return True
        return False

    async def _handle_model_command(self, arg: str) -> None:
        """Show the active model, or switch it live across providers.

        The model id rides in each request payload, so a switch takes
        effect on the very next turn with no re-initialization.
        """
        providers = self._registry.get_all(ProviderBrick)
        if not providers:
            await self._emit_info("model", "No Provider Brick is seated.")
            return

        if not arg:
            lines = [
                f"{getattr(p, 'model', '?')}  ({getattr(p, 'base_url', '')})"
                for p in providers
            ]
            await self._emit_info(
                "model",
                "\n".join(lines) + "\n\nswitch with  /model <id>",
            )
            return

        switched = []
        for provider in providers:
            if hasattr(provider, "configure"):
                provider.configure(model=arg)
                switched.append(provider.name)
        if switched:
            for iface in self._registry.get_all(InterfaceBrick):
                if hasattr(iface, "set_provider_info"):
                    base = getattr(providers[0], "base_url", "")
                    iface.set_provider_info(arg, base)
            await self._emit_info("model", f"now using {arg}.")
        else:
            await self._emit_info(
                "model", "The seated provider can't switch models at runtime."
            )

    @staticmethod
    def _parse_afk_cycles(cmd: str) -> Optional[int]:
        """Parse '/afk', '/afk 5', '/afk inf'. None means invalid input."""
        arg = cmd.removeprefix("/afk").strip()
        if not arg:
            return DEFAULT_AFK_CYCLES
        if arg in ("inf", "infinite", "forever"):
            return 0
        try:
            n = int(arg)
            return n if n >= 0 else None
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Agent loop — provider ⇄ tools until a text answer
    # ------------------------------------------------------------------

    async def _agent_loop(self) -> None:
        """Iterate provider calls and tool executions until the model answers."""
        for _step in range(MAX_AGENT_STEPS):
            # Re-collect every round, not just per turn: a brick seated by
            # registry_install/registry_create_brick mid-turn must be
            # callable in the very next model round.
            tool_schemas = self._collect_tool_schemas()
            messages = await self._build_provider_messages()

            self._set_busy(True, "thinking…")
            try:
                content, raw_calls, meta = await self._call_providers(tool_schemas, messages)
            finally:
                self._set_busy(False)

            self._track_usage(meta)

            reasoning = meta.get("reasoning", "")
            if reasoning:
                await self._emit_thinking(reasoning)

            await self._hooks.dispatch(HookType.POST_LLM, HookEvent(
                hook_type=HookType.POST_LLM,
                data={"content": content, "tool_calls": raw_calls},
                brick_name="event_loop",
            ))

            if not raw_calls:
                if not content:
                    if meta.get("provider_failed"):
                        # The human-readable error was already rendered —
                        # a placeholder reply on top would just be noise.
                        return
                    content = "[the model returned an empty response]"
                self._message_history.append(Message(role="assistant", content=content))
                await self._emit_assistant(content)
                return

            # Model wants tools: record its message, run them, loop again.
            self._message_history.append(
                Message(role="assistant", content=content, tool_calls=raw_calls)
            )
            if content:
                await self._emit_assistant(content)
            await self._execute_tool_round(raw_calls)

        msg = f"Stopped after {MAX_AGENT_STEPS} tool steps without a final answer."
        self._message_history.append(Message(role="assistant", content=msg))
        await self._emit_assistant(msg)

    async def _execute_tool_round(self, raw_calls: List[Dict[str, Any]]) -> None:
        """Run one batch of tool calls through hooks and Tool Bricks."""
        tool_calls = self._raw_to_tool_calls(raw_calls)

        for iface in self._registry.get_all(InterfaceBrick):
            if hasattr(iface, "render_tool_calls"):
                await iface.render_tool_calls(raw_calls)

        await self._hooks.dispatch(HookType.PRE_TOOL, HookEvent(
            hook_type=HookType.PRE_TOOL,
            data=tool_calls,
            brick_name="event_loop",
        ))

        tool_calls = await self.process_tool_calls(tool_calls)

        await self._hooks.dispatch(HookType.POST_TOOL, HookEvent(
            hook_type=HookType.POST_TOOL,
            data=tool_calls,
            brick_name="event_loop",
        ))

        for tc in tool_calls:
            if tc.result and tc.name:
                for iface in self._registry.get_all(InterfaceBrick):
                    if hasattr(iface, "render_tool_result"):
                        await iface.render_tool_result(tc.name, tc.args, tc.result)

        for tc in tool_calls:
            self._message_history.append(Message(
                role="tool",
                content=str(tc.result) if tc.result else "null",
                tool_call_id=tc.tool_call_id or tc.name,
            ))

        await self._hooks.dispatch(HookType.POST_TOOL_CALL, HookEvent(
            hook_type=HookType.POST_TOOL_CALL,
            data=tool_calls,
            brick_name="event_loop",
        ))

    # ------------------------------------------------------------------
    # AFK mode
    # ------------------------------------------------------------------

    async def _enter_afk_mode(self, cycles: int = DEFAULT_AFK_CYCLES) -> None:
        """Enter autonomous AFK mode.

        Swaps the CLI for the internal event bus, starts a ForemanActor
        serving the bus, and runs the Dreamer ⇄ Foreman negotiation loop.
        Approved proposals are executed by a Mason sub-agent with the
        registered tool bricks (security hooks included).
        """
        from brikie.kernel.afk_protocol import AFKProtocolEngine
        from brikie.kernel.soul_actor import DreamerActor, ForemanActor

        providers = self._registry.get_all(ProviderBrick)
        if not providers:
            await self._emit_info("afk", "No Provider Brick available — cannot dream.")
            return
        provider = providers[0]

        dreamer_soul = self._souls.get("dreamer")
        foreman_soul = self._souls.get("foreman")

        # Interfaces captured before the swap keep narrating the
        # negotiation even while the CLI is unmounted from the registry.
        self._afk_watchers = list(self._registry.get_all(InterfaceBrick))

        label = "∞" if cycles == 0 else str(cycles)
        await self._emit_info(
            "afk",
            f"Entering autonomous loop ({label} cycle{'s' if cycles != 1 else ''}). "
            "Dreamer proposes → Foreman signs off → Masons build.",
        )

        souls = [s for s in (dreamer_soul, foreman_soul) if s is not None]
        await self._afk_manager.enter_afk_mode(souls=souls)
        bus = self._afk_manager.event_bus

        dreamer = DreamerActor(dreamer_soul, provider)
        foreman = ForemanActor(foreman_soul, provider, bus)
        foreman_task = asyncio.create_task(foreman.serve())

        diagnostics = next(
            (b for b in self._registry._bricks.values()
             if hasattr(b, "get_session_stats") and hasattr(b, "get_last_n_events")),
            None,
        )
        async def current_focus() -> str:
            stored = await self._state.get("dreamer_focus", "")
            return stored or getattr(dreamer_soul, "focus", "")

        engine = AFKProtocolEngine(
            event_bus=bus,
            dreamer_soul=dreamer_soul,
            foreman_soul=foreman_soul,
            diagnostics=diagnostics,
            on_execute=self._run_mason_task,
            dreamer_propose=dreamer.propose,
            on_stage=self._emit_afk,
            evaluation_timeout=120.0,
            dream_sources=lambda: [
                b for b in self._registry._bricks.values()
                if hasattr(b, "dream_context")
            ],
            get_focus=current_focus,
        )

        try:
            await engine.start(
                cycles=cycles,
                max_duration_seconds=0,
            )
        except asyncio.CancelledError:
            pass
        finally:
            foreman_task.cancel()
            try:
                await foreman_task
            except (asyncio.CancelledError, Exception):
                pass
            await self._afk_manager.exit_afk_mode()
            self._afk_watchers = []

        totals = engine.results
        proposals = sum(r.proposals_count for r in totals)
        executed = sum(r.executed_count for r in totals)
        failed = sum(r.failed_count for r in totals)
        await self._emit_info(
            "afk",
            f"Completed {len(totals)} cycle(s) — returned to interactive.\n"
            f"{proposals} proposal(s), {executed} built, {failed} rejected/failed.",
        )

    async def _emit_afk(self, actor: str, text: str) -> None:
        """Narrate an AFK negotiation stage through the watching interfaces."""
        watchers = self._afk_watchers or self._registry.get_all(InterfaceBrick)
        for iface in watchers:
            if hasattr(iface, "render_afk_event"):
                await iface.render_afk_event(actor, text)

    async def _run_mason_task(self, title: str, payload: Dict[str, Any]) -> bool:
        """Execute an approved proposal with a Mason sub-agent.

        The Mason runs its own bounded agent loop against the registered
        tool bricks, with PRE_TOOL/POST_TOOL hooks dispatched so security
        bricks stay in the path. Returns True only when the Mason itself
        reports verified completion.
        """
        mason_soul = self._souls.get("mason")
        system = mason_soul.system_prompt if mason_soul else _MASON_FALLBACK_PROMPT
        max_steps = MAX_MASON_STEPS
        if mason_soul:
            max_steps = mason_soul.behavioral_constraints.get("max_steps", MAX_MASON_STEPS)

        schemas = self._collect_tool_schemas()
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": (
                    f"Approved job: {title}\n\n{payload.get('description', '')}\n\n"
                    "Execute this job now."
                ),
            },
        ]

        for _step in range(max_steps):
            content, raw_calls, _meta = await self._call_providers(schemas, messages)

            if not raw_calls:
                done = "TASK COMPLETE" in content and "TASK FAILED" not in content
                summary = content.strip().splitlines()[-1] if content.strip() else "(no report)"
                await self._emit_afk("mason", summary[:200])
                return done

            messages.append({
                "role": "assistant",
                "content": content or None,
                "tool_calls": raw_calls,
            })

            tool_calls = self._raw_to_tool_calls(raw_calls)
            for tc in tool_calls:
                await self._emit_afk("mason", f"→ {tc.name}({str(tc.args)[:120]})")

            await self._hooks.dispatch(HookType.PRE_TOOL, HookEvent(
                hook_type=HookType.PRE_TOOL,
                data=tool_calls,
                brick_name="mason",
            ))
            tool_calls = await self.process_tool_calls(tool_calls)
            await self._hooks.dispatch(HookType.POST_TOOL, HookEvent(
                hook_type=HookType.POST_TOOL,
                data=tool_calls,
                brick_name="mason",
            ))

            for tc in tool_calls:
                messages.append({
                    "role": "tool",
                    "content": str(tc.result) if tc.result else "null",
                    "tool_call_id": tc.tool_call_id or tc.name,
                })

        await self._emit_afk("mason", f"step budget ({max_steps}) exhausted")
        return False

    # ------------------------------------------------------------------
    # Context assembly
    # ------------------------------------------------------------------

    async def _build_provider_messages(self) -> List[Dict[str, Any]]:
        """Assemble system prompt + memory context + conversation history."""
        messages: List[Dict[str, Any]] = []
        if self._system_prompt:
            messages.append({"role": "system", "content": self._system_prompt})

        memory_blob = await self._build_memory_blob()
        if memory_blob:
            messages.append({
                "role": "system",
                "content": f"## Memory Context\n{memory_blob}",
            })

        messages.extend(self._messages_to_dicts())
        return messages

    async def _build_memory_blob(self) -> str:
        """Collect compressed context from memory-capable bricks, if any.

        Each brick returns its own context shape. This method normalizes
        across the three memory brick types:

        - LCM (lossless context manager):
            {"summaries": [...], "tail": [...]}
        - MemPalace (knowledge graph):
            {"mempalace": {entity_count, triple_count, ...}}
        - Wiki (page store):
            {"wiki": {page_count, recent_pages, ...}}

        Each brick type gets its own section header so the LLM can
        distinguish session memory from persistent knowledge.
        """
        memory_bricks = self._memory_capable_bricks()
        if not memory_bricks:
            return ""

        session_id = await self._state.get("session_id", "default")
        context_parts: List[str] = []
        for brick in memory_bricks:
            ctx = await brick.build_context(session_id)
            if not ctx:
                continue

            brick_name = getattr(brick, "name", "memory")

            # LCM shape — DAG summaries + recent tail
            summaries = ctx.get("summaries", [])
            tail = ctx.get("tail", [])
            if summaries:
                context_parts.append(f"## Session Summary ({brick_name})")
                for s in summaries:
                    context_parts.append(
                        f"[DAG depth={s.get('depth', 0)}] {s.get('content', '')}"
                    )
            if tail:
                context_parts.append(f"## Recent Messages ({brick_name})")
                for t in tail:
                    context_parts.append(
                        f"[{t.get('role', '?')}] {t.get('content', '')[:200]}"
                    )

            # MemPalace shape — knowledge graph stats
            mempalace_ctx = ctx.get("mempalace")
            if mempalace_ctx:
                parts = [f"## Knowledge Graph ({brick_name})"]
                ec = mempalace_ctx.get("entity_count", 0)
                tc = mempalace_ctx.get("triple_count", 0)
                parts.append(f"- {ec} entities, {tc} relationships")
                # Include a few recent entities as context clues
                entities = mempalace_ctx.get("recent_entities", [])
                for ent in entities[:5]:
                    name = ent.get("name", "?")
                    etype = ent.get("entity_type", "?")
                    desc = ent.get("description", "")
                    if desc:
                        parts.append(f"  - {name} ({etype}): {desc[:120]}")
                    else:
                        parts.append(f"  - {name} ({etype})")
                context_parts.append("\n".join(parts))

            # Wiki shape — page store stats
            wiki_ctx = ctx.get("wiki")
            if wiki_ctx:
                parts = [f"## Wiki Knowledge Base ({brick_name})"]
                pc = wiki_ctx.get("page_count", 0)
                parts.append(f"- {pc} pages")
                recent = wiki_ctx.get("recent_pages", [])
                if recent:
                    parts.append("- Recent pages:")
                    for p in recent[:5]:
                        title = p.get("title", "?")
                        status = p.get("status", "?")
                        parts.append(f"  - {title} [{status}]")
                context_parts.append("\n".join(parts))

        return "\n\n".join(context_parts)

    # ------------------------------------------------------------------
    # Tool Execution
    # ------------------------------------------------------------------

    async def process_tool_calls(self, tool_calls: List[ToolCall]) -> List[ToolCall]:
        """Execute each tool call using registered ToolBricks."""
        tools = self._registry.get_all(ToolBrick)
        for tc in tool_calls:
            if tc.result is not None:
                # A PRE_TOOL hook (e.g. the command firewall) already
                # settled this call — executing it would bypass the block.
                logger.info("Skipping pre-settled tool call: %s", tc.name)
                continue
            executed = False
            for tool_brick in tools:
                tool_list = getattr(tool_brick, "tools", None)
                if tool_list is None:
                    continue
                if any(
                    s.get("function", {}).get("name") == tc.name
                    for s in tool_list
                ):
                    logger.info("Executing tool %s via brick %s", tc.name, tool_brick.name)
                    try:
                        result = await tool_brick.execute(tc.name, tc.args)
                        tc.result = str(result)
                        executed = True
                        break
                    except KeyError as exc:
                        # The brick advertises the schema but doesn't
                        # dispatch it — let another brick claim the call.
                        logger.warning(
                            "Tool %s not dispatched by %s: %s",
                            tc.name, tool_brick.name, exc,
                        )
                        continue
                    except Exception as exc:
                        # The brick matched and ran but failed. Settle the
                        # call with a structured error so the model can
                        # react — one bad tool call must never crash the
                        # loop (AGENTS.md tool contract).
                        logger.warning(
                            "Tool %s failed on %s: %s",
                            tc.name, tool_brick.name, exc,
                        )
                        tc.result = f"Tool error ({type(exc).__name__}): {exc}"
                        executed = True
                        break

            if not executed:
                tc.result = f"No ToolBrick found for tool '{tc.name}'"
                logger.warning("Unmatched tool call: %s", tc.name)

        return tool_calls

    def _raw_to_tool_calls(self, raw: List[Dict[str, Any]]) -> List[ToolCall]:
        """Convert raw provider tool-call dicts to ToolCall objects."""
        result: List[ToolCall] = []
        for item in raw:
            call_id = item.get("id", "")
            if "function" in item:
                func = item["function"]
                name = func.get("name", "")
                args_raw = func.get("arguments", "{}")
                try:
                    args = json.loads(args_raw)
                except (json.JSONDecodeError, TypeError):
                    args = args_raw
                result.append(ToolCall(name=name, args=args, tool_call_id=call_id))
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
        """Serialize history to OpenAI /chat/completions wire format."""
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
        schemas: List[Dict[str, Any]] = []
        for tool in self._registry.get_all(ToolBrick):
            if getattr(tool, "tools", None):
                schemas.extend(tool.tools)
        return schemas

    # ------------------------------------------------------------------
    # Provider Routing
    # ------------------------------------------------------------------

    async def _call_providers(
        self,
        tool_schemas: List[Dict[str, Any]],
        messages: List[Dict[str, Any]],
    ) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
        """Route messages to the first available Provider.

        Providers may return ``(content, tool_calls)`` or
        ``(content, tool_calls, meta)`` — meta carries reasoning text,
        token usage, and finish reason when available.
        """
        errors: List[str] = []
        for provider in self._registry.get_all(ProviderBrick):
            try:
                result = await provider.get_completion(messages, tool_schemas)
            except Exception as exc:
                msg = f"{provider.name}: {exc}"
                logger.error("Provider %s failed: %s", provider.name, exc)
                errors.append(msg)
                continue
            if len(result) == 3:
                return result
            content, raw_calls = result
            return content, raw_calls, {}

        if errors:
            if len(errors) == 1:
                await self._emit_error(errors[0])
            else:
                await self._emit_error("All providers failed:\n" + "\n".join(errors))
            return "", [], {"provider_failed": True}
        return "", [], {}

    def _track_usage(self, meta: Dict[str, Any]) -> None:
        usage = meta.get("usage") or {}
        self._tokens_in += usage.get("prompt_tokens", 0)
        self._tokens_out += usage.get("completion_tokens", 0)
        for iface in self._registry.get_all(InterfaceBrick):
            if hasattr(iface, "update_usage"):
                iface.update_usage(self._tokens_in, self._tokens_out)

    # ------------------------------------------------------------------
    # Input / Output
    # ------------------------------------------------------------------

    async def _capture_input(self) -> str:
        """Capture input from whichever Interface Brick speaks first.

        With one interface this is a plain await. With several (e.g. CLI
        + Telegram) their ``get_input()`` calls race; the first to
        produce text wins the turn. Pending reads persist across turns —
        an interface's input is never cancelled, only consumed later.
        """
        ifaces = self._registry.get_all(InterfaceBrick)
        if not ifaces:
            return ""
        if len(ifaces) == 1:
            return (await ifaces[0].get_input()) or ""

        for iface in ifaces:
            if iface.name not in self._input_tasks:
                self._input_tasks[iface.name] = asyncio.create_task(
                    iface.get_input(), name=f"input:{iface.name}"
                )

        done, _pending = await asyncio.wait(
            set(self._input_tasks.values()),
            return_when=asyncio.FIRST_COMPLETED,
        )
        winner = next(iter(done))
        for name, task in list(self._input_tasks.items()):
            if task is winner:
                del self._input_tasks[name]
                break
        try:
            return (winner.result() or "").strip()
        except Exception:
            logger.exception("Interface input failed")
            return ""

    def _set_busy(self, busy: bool, label: str = "thinking…") -> None:
        for iface in self._registry.get_all(InterfaceBrick):
            if hasattr(iface, "set_busy"):
                iface.set_busy(busy, label)

    async def _emit_assistant(self, content: str) -> None:
        for iface in self._registry.get_all(InterfaceBrick):
            if hasattr(iface, "render_assistant_response"):
                await iface.render_assistant_response(content)
            else:
                await iface.output(content)

    async def _emit_thinking(self, reasoning: str) -> None:
        for iface in self._registry.get_all(InterfaceBrick):
            if hasattr(iface, "render_thinking"):
                await iface.render_thinking(reasoning)

    async def _emit_info(self, title: str, body: str) -> None:
        for iface in self._registry.get_all(InterfaceBrick):
            if hasattr(iface, "render_info"):
                await iface.render_info(title, body)
            else:
                await iface.output(f"[{title}] {body}")

    async def _emit_error(self, msg: str) -> None:
        for iface in self._registry.get_all(InterfaceBrick):
            if hasattr(iface, "render_error"):
                await iface.render_error(msg)
            else:
                await iface.output(f"[system error] {msg}")
