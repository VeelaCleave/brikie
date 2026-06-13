"""SwarmToolBrick (BRK-470) — delegate scoped work to parallel sub-agents.

The Swarm tier (goals #4): instead of doing every step itself in one ever-
growing context, the coordinator agent fans a set of *scoped* tasks out to
ephemeral, role-specialized sub-agents that run concurrently, each in its
own isolated and bounded context, and reports their findings back. When a
sub-agent finishes, its context is discarded — only the report returns. This
is how an agent investigates ten files, or builds and reviews in parallel,
without blowing its own window.

The full tier lands here:
- **Swarm** — ``swarm_dispatch`` runs N sub-agents at once (bounded by a
  parallelism cap), then aggregates their reports.
- **Routing** — each task carries a ``role`` that selects a system prompt.
  Built-in roles (researcher / coder / reviewer / generalist) are joined by
  any loaded **soul** persona (mason, dreamer, …), so the user's installed
  souls become delegatable roles. Unknown roles fall back to generalist.
- **Collaboration** — three layers:
  1. a shared ``context`` briefing + the active goal (#1) auto-attached to
     every sub-agent, so the fan-out stays aligned;
  2. a shared **blackboard** — sub-agents call ``swarm_share`` / ``swarm_inbox``
     to pass findings to each other *while running* (real inter-agent
     messaging, not just fan-out/fan-in);
  3. an **auto-reviewer** pass — a successful ``coder`` sub-agent's work is
     automatically handed to a reviewer sub-agent that returns PASS/FAIL with
     specifics, before the result comes back.
- **Goal integration** — the dispatch and each sub-agent's outcome are
  logged into the active goal's append-only progress log, so delegated work
  is visible in ``goal_status``.

Containment & safety:
- Each sub-agent gets the *same* Tool Bricks as the parent **except** the
  swarm tools themselves — so a sub-agent can't recursively dispatch more
  swarms (no runaway fan-out; clean, shallow GC).
- Sub-agent tool calls pass through the same PRE_TOOL/POST_TOOL hooks, so
  the CommandFirewall (BRK-800) and Watchdog (BRK-820) gate them exactly
  as they gate the coordinator.
- Per-subagent step and context budgets keep each worker bounded.

Kernel purity (AGENTS #1): the runner and blackboard live in
``brikie.kernel.subagent`` and import nothing from bricks. This brick wires
them from the registry; souls arrive via the duck-typed ``set_souls`` the
kernel calls. The dependency points brick → kernel, never the reverse.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from brikie.bricks.tool.base import ToolBrick
from brikie.bricks.tool.swarm.swarm_store import SwarmStore
from brikie.kernel.registry import ProviderBrick, ToolBrick as ToolBrickABC
from brikie.kernel.registry import InterfaceBrick
from brikie.kernel.subagent import (
    DEFAULT_CONTEXT_BUDGET,
    DEFAULT_MAX_STEPS,
    CostBudget,
    SubAgentResult,
    SubAgentRunner,
    SwarmBlackboard,
    run_swarm,
)

logger = logging.getLogger(__name__)

# Role → system-prompt specialization. The role routes a task to the kind
# of sub-agent best suited to it. Each prompt is deliberately generic (no
# provider/model assumptions) and reinforces the verify-then-report contract
# the runner depends on. Loaded souls (set_souls) extend this set at runtime.
_ROLE_PROMPTS: Dict[str, str] = {
    "researcher": (
        "You are a Researcher sub-agent. Investigate the task using your "
        "read-only tools (reading files, searching, browsing). Do NOT make "
        "changes — gather facts and synthesize them. Your report must state "
        "concrete findings (paths, names, values, conclusions), not a plan."
    ),
    "coder": (
        "You are a Coder sub-agent. Implement exactly the scoped change you "
        "are given, then VERIFY it (run the relevant tests/linter or re-read "
        "what you wrote). Do not expand scope. Report what you changed and "
        "the verification result."
    ),
    "reviewer": (
        "You are a Reviewer sub-agent. Critically inspect the work or code in "
        "scope. Find real problems — bugs, missed requirements, risks — and "
        "report them specifically with evidence. If it is sound, say so "
        "plainly. Do not rewrite it yourself."
    ),
    "generalist": (
        "You are a focused worker sub-agent. Complete exactly the task you "
        "are given using your tools, verify the result, and report concisely."
    ),
}
_DEFAULT_ROLE = "generalist"

# Collaboration tools handed ONLY to sub-agents (never at the top level), so
# they can talk to each other without being able to dispatch a sub-swarm.
_MESSAGING_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "swarm_share",
            "description": (
                "Share a finding, fact, or interim result with the OTHER "
                "sub-agents working alongside you in this swarm. Use it when "
                "you discover something the others need."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {"type": "string", "description": "The finding to share."},
                },
                "required": ["note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "swarm_inbox",
            "description": (
                "Read notes the other sub-agents in this swarm have shared so "
                "far. Check it before duplicating work they may have done."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


class SwarmToolBrick(ToolBrick):
    BRICK_NUMBER = "BRK-470"
    """Tool Brick that delegates scoped tasks to parallel sub-agents.

    Args:
        registry: The BrickRegistry (auto-injected) — source of the provider
            and the tool bricks the sub-agents inherit.
        hooks: The HookDispatcher (auto-injected) so security stays in path
            for sub-agent tool calls. Optional; without it tools still run.
        db_path: SQLite audit log for swarm runs.
        max_steps: Per-subagent provider⇄tool round budget.
        max_parallel: How many sub-agents may hit the provider at once.
        max_tasks: Hard ceiling on tasks per dispatch (fan-out cap).
        context_budget: Per-subagent token budget for its isolated history.
        auto_review: When True (default), a successful coder sub-agent's
            work is auto-reviewed before the result returns.
    """

    tools: List[Dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": "swarm_dispatch",
                "description": (
                    "Delegate one or more scoped subtasks to parallel "
                    "sub-agents and get their reports back. Use this to "
                    "fan out independent work — investigate several files at "
                    "once, or build-and-review in parallel — WITHOUT bloating "
                    "your own context: each sub-agent works in its own "
                    "isolated context and only its report returns to you. "
                    "Sub-agents can message each other (swarm_share/inbox) "
                    "while running, and a coder's work is auto-reviewed. Give "
                    "each task a 'role' (researcher, coder, reviewer, "
                    "generalist, or a loaded soul — see swarm_roles) and a "
                    "self-contained 'task' description."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tasks": {
                            "type": "array",
                            "description": "The subtasks to delegate, run concurrently.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "role": {
                                        "type": "string",
                                        "description": "The kind of sub-agent for this task (researcher, coder, reviewer, generalist, or a loaded soul role; call swarm_roles to list).",
                                    },
                                    "task": {
                                        "type": "string",
                                        "description": "A self-contained description of what this sub-agent must do. It does NOT see your conversation — include all needed detail.",
                                    },
                                },
                                "required": ["task"],
                            },
                        },
                        "context": {
                            "type": "string",
                            "description": "Optional shared briefing given to every sub-agent (the common background they all need).",
                        },
                        "review": {
                            "type": "boolean",
                            "description": "Auto-review coder sub-agents' work (default true). Set false to skip the reviewer pass.",
                        },
                    },
                    "required": ["tasks"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "swarm_status",
                "description": "Review recent swarm dispatches and their outcomes (an audit log of delegated work).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "How many recent runs to show (default 5)."},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "swarm_roles",
                "description": "List the sub-agent roles available for swarm_dispatch (built-in roles plus any loaded soul personas) and what each is for.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
    ]

    def __init__(
        self,
        registry: Any = None,
        hooks: Any = None,
        db_path: str = "swarm.db",
        max_steps: int = DEFAULT_MAX_STEPS,
        max_parallel: int = 4,
        max_tasks: int = 8,
        context_budget: int = DEFAULT_CONTEXT_BUDGET,
        auto_review: bool = True,
        subagent_timeout: float = 180.0,
        max_total_tokens: int = 0,
    ) -> None:
        super().__init__()
        self._name = "swarm"
        self._registry = registry
        self._hooks = hooks
        self._store = SwarmStore(db_path)
        self._max_steps = max_steps
        self._max_parallel = max_parallel
        self._max_tasks = max_tasks
        self._context_budget = context_budget
        self._auto_review = auto_review
        # Per-sub-agent wall-clock deadline (0 = none) — a hung tool can't
        # hang the swarm. A swarm-wide token ceiling (0 = unlimited).
        self._subagent_timeout = subagent_timeout
        self._max_total_tokens = max_total_tokens
        # role -> system prompt, contributed by loaded souls (set_souls).
        self._soul_roles: Dict[str, str] = {}

    @property
    def name(self) -> str:
        return self._name

    async def init(self) -> None:
        await self._store.initialize()
        await super().init()

    async def shutdown(self) -> None:
        await self._store.shutdown()
        await super().shutdown()

    def set_souls(self, souls: Dict[str, Any]) -> None:
        """Turn loaded soul personas into delegatable sub-agent roles.

        Called by the kernel during warm-up (duck-typed). Each soul's name
        becomes a role whose system prompt is the soul's persona — so a user
        who installs the Mason soul can dispatch ``role: "mason"`` sub-agents.
        Built-in roles are not overridden by a soul of the same name.
        """
        for soul_name, soul in (souls or {}).items():
            prompt = getattr(soul, "system_prompt", "") or ""
            key = str(soul_name).strip().lower()
            if key and prompt and key not in _ROLE_PROMPTS:
                self._soul_roles[key] = prompt
        if self._soul_roles:
            logger.info("Swarm gained %d soul role(s): %s",
                        len(self._soul_roles), ", ".join(self._soul_roles))

    async def execute(self, name: str, args: Dict[str, Any]) -> Any:
        if name == "swarm_dispatch":
            return await self._swarm_dispatch(args)
        if name == "swarm_status":
            return await self._swarm_status(args)
        if name == "swarm_roles":
            return self._swarm_roles()
        raise KeyError(f"Unknown tool: {name}")

    # ------------------------------------------------------------------
    # Roles
    # ------------------------------------------------------------------

    def _role_prompts(self) -> Dict[str, str]:
        """Built-in roles merged with any soul-contributed roles."""
        return {**_ROLE_PROMPTS, **self._soul_roles}

    def _role_prompt(self, role: str) -> str:
        return self._role_prompts().get(role, _ROLE_PROMPTS[_DEFAULT_ROLE])

    def _swarm_roles(self) -> Dict[str, Any]:
        roles = self._role_prompts()
        return {
            "roles": roles,
            "soul_roles": sorted(self._soul_roles),
            "builtin_roles": sorted(_ROLE_PROMPTS),
        }

    async def _swarm_status(self, args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            limit = int(args.get("limit", 5))
        except (TypeError, ValueError):
            limit = 5
        runs = await self._store.recent_runs(max(1, min(limit, 50)))
        return {"recent_runs": runs} if runs else {"message": "no swarm runs yet"}

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def _swarm_dispatch(self, args: Dict[str, Any]) -> Dict[str, Any]:
        raw_tasks = args.get("tasks")
        if not isinstance(raw_tasks, list) or not raw_tasks:
            return {"error": "swarm_dispatch needs a non-empty 'tasks' array of {role, task}."}

        tasks = self._normalize_tasks(raw_tasks)
        if not tasks:
            return {"error": "no task had a non-empty 'task' description."}
        if len(tasks) > self._max_tasks:
            return {"error": f"too many tasks ({len(tasks)}); the fan-out cap is {self._max_tasks}."}

        provider = self._first_provider()
        if provider is None:
            return {"error": "no provider available — cannot run sub-agents."}

        shared = str(args.get("context", "")).strip()
        goal = await self._active_goal()
        do_review = bool(args.get("review", self._auto_review))

        # A shared blackboard only when there is more than one collaborator.
        board = SwarmBlackboard() if len(tasks) > 1 else None
        briefing = self._build_briefing(shared, goal, collaborate=board is not None)

        base_schemas = self._subagent_tool_schemas()
        sub_schemas = base_schemas + (_MESSAGING_TOOLS if board else [])

        run_id = await self._store.start_run(goal or shared, len(tasks))
        roles_summary = ", ".join(role for role, _ in tasks)
        await self._log_goal("swarm.dispatch",
                             f"delegated {len(tasks)} task(s) to: {roles_summary}")

        # Shared across the whole dispatch: a token ceiling and a live event
        # sink so the user watches sub-agents work instead of staring at
        # silence for the minutes a dispatch can take.
        budget = CostBudget(self._max_total_tokens)
        sink = self._make_event_sink()
        await sink({"kind": "dispatch", "role": "swarm",
                    "count": len(tasks), "roles": roles_summary})

        runners_and_tasks = []
        for i, (role, task_text) in enumerate(tasks):
            sender = f"{role}#{i + 1}"
            execute_tool = self._make_tool_executor(board=board, sender=sender)
            runner = SubAgentRunner(
                provider=provider,
                tool_schemas=sub_schemas,
                execute_tool=execute_tool,
                hooks=self._hooks,
                max_steps=self._max_steps,
                context_budget=self._context_budget,
                label=role,
                on_event=sink,
                budget=budget,
            )
            full_task = f"{briefing}{task_text}" if briefing else task_text
            runners_and_tasks.append((runner, self._role_prompt(role), full_task))

        logger.info("Swarm %s dispatching %d sub-agent(s).", run_id, len(tasks))
        results = await run_swarm(runners_and_tasks,
                                  max_parallel=self._max_parallel,
                                  timeout=self._subagent_timeout)

        if do_review:
            await self._auto_review_coders(results, provider, base_schemas,
                                           sink=sink, budget=budget)

        # Persist + surface each outcome in the active goal's progress log.
        for i, result in enumerate(results):
            await self._store.record_task(run_id, i, result)
            verdict = "ok" if result.ok else "failed"
            if result.reviewed:
                verdict += f"; review {'PASS' if result.review_ok else 'FAIL'}"
            await self._log_goal(f"swarm.{result.role}",
                                 f"{verdict} — {result.report[:160]}")

        ok_count = sum(1 for r in results if r.ok)
        tokens_in = sum(r.tokens_in for r in results)
        tokens_out = sum(r.tokens_out for r in results)
        summary = (f"{ok_count}/{len(results)} sub-agent(s) completed "
                   f"successfully ({tokens_in + tokens_out} tokens)")
        if budget.exceeded():
            summary += " — token budget reached"
        await self._store.finish_run(run_id, ok_count, summary)
        await sink({"kind": "summary", "role": "swarm", "text": summary})

        out: Dict[str, Any] = {
            "run_id": run_id,
            "summary": summary,
            "tokens": {"in": tokens_in, "out": tokens_out,
                       "budget_reached": budget.exceeded()},
            "results": [r.to_dict() for r in results],
        }
        if board:
            shared_notes = board.snapshot()
            if shared_notes:
                out["shared_notes"] = shared_notes
        return out

    async def _auto_review_coders(
        self,
        results: List[SubAgentResult],
        provider: Any,
        base_schemas: List[Dict[str, Any]],
        sink: Any = None,
        budget: Any = None,
    ) -> None:
        """Auto-spawn a reviewer for each successful coder result.

        The reviewer inspects the coder's work (it has the same read tools)
        and returns a PASS/FAIL verdict with specifics, attached to the
        coder's result. Reviewers don't get the blackboard — they judge a
        finished artifact, not collaborate.
        """
        targets = [
            (i, r) for i, r in enumerate(results) if r.role == "coder" and r.ok
        ]
        if not targets:
            return

        review_runners = []
        for _i, r in targets:
            runner = SubAgentRunner(
                provider=provider,
                tool_schemas=base_schemas,
                execute_tool=self._make_tool_executor(),
                hooks=self._hooks,
                max_steps=self._max_steps,
                context_budget=self._context_budget,
                label="reviewer",
                on_event=sink,
                budget=budget,
            )
            review_task = (
                "Review another sub-agent's completed work for correctness "
                "and completeness. Inspect it (read the relevant files or run "
                "checks) — do not take the report at face value.\n\n"
                f"The task it was given:\n{r.task}\n\n"
                f"Its report:\n{r.report}\n\n"
                "Finish with a verdict line: 'REVIEW: PASS' if the work is "
                "correct and complete, or 'REVIEW: FAIL — <specific problems>' "
                "if not."
            )
            review_runners.append((runner, _ROLE_PROMPTS["reviewer"], review_task))

        reviews = await run_swarm(review_runners, max_parallel=self._max_parallel,
                                  timeout=self._subagent_timeout)
        for (idx, _r), rev in zip(targets, reviews):
            results[idx].reviewed = True
            results[idx].review = rev.report.strip()
            results[idx].review_ok = "REVIEW: PASS" in rev.report.upper()
            # Fold the reviewer's token cost into the reviewed result so the
            # dispatch total stays honest.
            results[idx].tokens_in += rev.tokens_in
            results[idx].tokens_out += rev.tokens_out

    # ------------------------------------------------------------------
    # Live event streaming
    # ------------------------------------------------------------------

    def _make_event_sink(self):
        """Build an async sink that narrates sub-agent events to interfaces.

        A dispatch can run for minutes; this turns it from a black box into a
        live feed. Each Interface Brick that exposes ``render_swarm_event``
        gets structured events; others fall back to plain ``output``. Best-
        effort — a noisy or slow interface never blocks the swarm.
        """
        registry = self._registry

        def _line(ev: Dict[str, Any]) -> str:
            kind, role = ev.get("kind"), ev.get("role", "?")
            if kind == "dispatch":
                return f"⇶ swarm: dispatching {ev.get('count')} sub-agent(s) → {ev.get('roles')}"
            if kind == "start":
                return f"  ▸ {role} started: {ev.get('task', '')[:80]}"
            if kind == "tool":
                return f"    {role} → {ev.get('tool')}"
            if kind == "blocked":
                return f"    {role} ⛔ {ev.get('tool')} blocked by security"
            if kind == "timeout":
                return f"  ⏱ {role} timed out after {ev.get('seconds')}s"
            if kind == "done":
                mark = "✓" if ev.get("ok") else "✗"
                return f"  {mark} {role} {'done' if ev.get('ok') else 'failed'}"
            if kind == "summary":
                return f"⇶ swarm: {ev.get('text')}"
            return f"  {role}: {kind}"

        async def sink(ev: Dict[str, Any]) -> None:
            if registry is None:
                return
            text = _line(ev)
            for iface in registry.get_all(InterfaceBrick):
                try:
                    if hasattr(iface, "render_swarm_event"):
                        await iface.render_swarm_event(ev.get("role", "?"),
                                                       ev.get("kind", ""), text)
                    elif hasattr(iface, "output"):
                        await iface.output(text)
                except Exception:
                    logger.debug("swarm event sink: interface failed",
                                 exc_info=True)

        return sink

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _normalize_tasks(self, raw_tasks: List[Any]) -> List[tuple]:
        """Validate/clean the task list into [(role, task), ...]."""
        valid_roles = self._role_prompts()
        out: List[tuple] = []
        for item in raw_tasks:
            if isinstance(item, dict):
                task_text = str(item.get("task", "")).strip()
                role = str(item.get("role", "") or "").strip().lower()
            elif isinstance(item, str):
                task_text, role = item.strip(), ""
            else:
                continue
            if not task_text:
                continue
            if role not in valid_roles:
                role = _DEFAULT_ROLE
            out.append((role, task_text))
        return out

    @staticmethod
    def _build_briefing(shared: str, goal: str, collaborate: bool = False) -> str:
        """Assemble the common preamble prepended to every sub-agent's task."""
        parts: List[str] = []
        if goal:
            parts.append(f"Overall goal: {goal}")
        if shared:
            parts.append(f"Shared context: {shared}")
        if collaborate:
            parts.append(
                "You are one of several sub-agents on this. Call swarm_inbox "
                "to read what others have found, and swarm_share to pass on "
                "anything they need."
            )
        if not parts:
            return ""
        return "\n".join(parts) + "\n\nYour task:\n"

    def _first_provider(self) -> Optional[Any]:
        if self._registry is None:
            return None
        providers = self._registry.get_all(ProviderBrick)
        return providers[0] if providers else None

    async def _active_goal(self) -> str:
        """The active goal as 'title: detail', via the duck-typed capability."""
        if self._registry is None:
            return ""
        for brick in self._registry._bricks.values():
            getter = getattr(brick, "active_goal_context", None)
            if getter is None:
                continue
            try:
                return await getter() or ""
            except Exception:
                return ""
        return ""

    async def _log_goal(self, kind: str, detail: str) -> None:
        """Record a progress event in the active goal, if a goal brick is seated.

        Duck-typed: probes for ``log_progress`` (the GoalBrick exposes it).
        Best-effort — a missing or inactive goal is a silent no-op.
        """
        if self._registry is None:
            return
        for brick in self._registry._bricks.values():
            logger_fn = getattr(brick, "log_progress", None)
            if logger_fn is None:
                continue
            try:
                await logger_fn(kind, detail)
            except Exception:
                logger.debug("goal progress log failed", exc_info=True)
            return

    def _subagent_tool_schemas(self) -> List[Dict[str, Any]]:
        """Tool schemas for sub-agents: every Tool Brick's tools except ours.

        Excluding the swarm tools is what prevents a sub-agent from
        dispatching its own swarm (no recursive fan-out).
        """
        schemas: List[Dict[str, Any]] = []
        if self._registry is None:
            return schemas
        for tool in self._registry.get_all(ToolBrickABC):
            if tool is self:
                continue
            for s in getattr(tool, "tools", None) or []:
                schemas.append(s)
        return schemas

    def _make_tool_executor(self, board: Any = None, sender: str = ""):
        """Build the (name, args) -> str executor backed by the registry.

        When a ``board`` is supplied, the collaboration tools
        (swarm_share / swarm_inbox) are handled in-process against it;
        everything else routes to the first non-swarm Tool Brick that
        advertises the call. Errors are returned as strings (never raised)
        so one bad call can't crash a sub-agent.
        """
        registry = self._registry

        async def execute_tool(name: str, args: Dict[str, Any]) -> str:
            if board is not None and name == "swarm_share":
                note = str(args.get("note", "")).strip()
                if not note:
                    return "swarm_share needs a non-empty 'note'."
                await board.post(sender, note)
                return "Shared with the swarm."
            if board is not None and name == "swarm_inbox":
                msgs = await board.read(exclude_sender=sender)
                if not msgs:
                    return "No notes from other sub-agents yet."
                return "\n".join(f"[{m['from']}] {m['note']}" for m in msgs)

            if registry is None:
                return f"No registry available to run tool '{name}'"
            for tool in registry.get_all(ToolBrickABC):
                if tool is self:
                    continue
                names = {
                    s.get("function", {}).get("name")
                    for s in getattr(tool, "tools", None) or []
                }
                if name in names:
                    try:
                        return str(await tool.execute(name, args))
                    except KeyError:
                        continue
                    except Exception as exc:
                        return f"Tool error ({type(exc).__name__}): {exc}"
            return f"No tool brick provides '{name}'"

        return execute_tool
