"""SwarmToolBrick (BRK-470) — delegate scoped work to parallel sub-agents.

The Swarm tier (goals #4): instead of doing every step itself in one ever-
growing context, the coordinator agent fans a set of *scoped* tasks out to
ephemeral, role-specialized sub-agents that run concurrently, each in its
own isolated and bounded context, and reports their findings back. When a
sub-agent finishes, its context is discarded — only the report returns. This
is how an agent investigates ten files, or builds and reviews in parallel,
without blowing its own window.

Three roadmap pieces land together here:
- **Swarm** — ``swarm_dispatch`` runs N sub-agents at once (bounded by a
  parallelism cap), then aggregates their reports.
- **Routing** — each task carries a ``role``; the role selects a system
  prompt that specializes the sub-agent (researcher / coder / reviewer /
  generalist). Unknown roles fall back to generalist.
- **Collaboration** — a shared ``context`` briefing is given to every
  sub-agent, and the active goal (#1) is auto-attached, so the fan-out
  stays aligned to one objective. Results come back together for the
  coordinator to synthesize.

Containment & safety:
- Each sub-agent gets the *same* Tool Bricks as the parent **except** the
  swarm tools themselves — so a sub-agent can't recursively dispatch more
  swarms (no runaway fan-out; clean, shallow GC).
- Sub-agent tool calls pass through the same PRE_TOOL/POST_TOOL hooks, so
  the CommandFirewall (BRK-800) and Watchdog (BRK-820) gate them exactly
  as they gate the coordinator.
- Per-subagent step and context budgets keep each worker bounded.

Kernel purity (AGENTS #1): the runner lives in ``brikie.kernel.subagent``
and imports nothing from bricks. This brick wires it from the registry —
the dependency points brick → kernel, never the reverse.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from brikie.bricks.tool.base import ToolBrick
from brikie.bricks.tool.swarm.swarm_store import SwarmStore
from brikie.kernel.registry import ProviderBrick, ToolBrick as ToolBrickABC
from brikie.kernel.subagent import (
    DEFAULT_CONTEXT_BUDGET,
    DEFAULT_MAX_STEPS,
    SubAgentRunner,
    run_swarm,
)

logger = logging.getLogger(__name__)

# Role → system-prompt specialization. The role routes a task to the kind
# of sub-agent best suited to it. Each prompt is deliberately generic (no
# provider/model assumptions) and reinforces the verify-then-report contract
# the runner depends on.
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
                    "Give each task a 'role' (researcher, coder, reviewer, or "
                    "generalist) and a self-contained 'task' description."
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
                                        "enum": ["researcher", "coder", "reviewer", "generalist"],
                                        "description": "The kind of sub-agent for this task.",
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
                "description": "List the sub-agent roles available for swarm_dispatch and what each is for.",
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

    @property
    def name(self) -> str:
        return self._name

    async def init(self) -> None:
        await self._store.initialize()
        await super().init()

    async def shutdown(self) -> None:
        await self._store.shutdown()
        await super().shutdown()

    async def execute(self, name: str, args: Dict[str, Any]) -> Any:
        if name == "swarm_dispatch":
            return await self._swarm_dispatch(args)
        if name == "swarm_status":
            return await self._swarm_status(args)
        if name == "swarm_roles":
            return self._swarm_roles()
        raise KeyError(f"Unknown tool: {name}")

    # ------------------------------------------------------------------
    # Tool handlers
    # ------------------------------------------------------------------

    def _swarm_roles(self) -> Dict[str, Any]:
        return {"roles": {r: p for r, p in _ROLE_PROMPTS.items()}}

    async def _swarm_status(self, args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            limit = int(args.get("limit", 5))
        except (TypeError, ValueError):
            limit = 5
        runs = await self._store.recent_runs(max(1, min(limit, 50)))
        return {"recent_runs": runs} if runs else {"message": "no swarm runs yet"}

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
        briefing = self._build_briefing(shared, goal)

        tool_schemas = self._subagent_tool_schemas()
        execute_tool = self._make_tool_executor()

        run_id = await self._store.start_run(goal or shared, len(tasks))

        runners_and_tasks = []
        for role, task_text in tasks:
            runner = SubAgentRunner(
                provider=provider,
                tool_schemas=tool_schemas,
                execute_tool=execute_tool,
                hooks=self._hooks,
                max_steps=self._max_steps,
                context_budget=self._context_budget,
                label=role,
            )
            system_prompt = _ROLE_PROMPTS[role]
            full_task = f"{briefing}{task_text}" if briefing else task_text
            runners_and_tasks.append((runner, system_prompt, full_task))

        logger.info("Swarm %s dispatching %d sub-agent(s).", run_id, len(tasks))
        results = await run_swarm(runners_and_tasks, max_parallel=self._max_parallel)

        for i, result in enumerate(results):
            await self._store.record_task(run_id, i, result)
        ok_count = sum(1 for r in results if r.ok)
        summary = f"{ok_count}/{len(results)} sub-agent(s) completed successfully"
        await self._store.finish_run(run_id, ok_count, summary)

        return {
            "run_id": run_id,
            "summary": summary,
            "results": [r.to_dict() for r in results],
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _normalize_tasks(self, raw_tasks: List[Any]) -> List[tuple]:
        """Validate/clean the task list into [(role, task), ...]."""
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
            if role not in _ROLE_PROMPTS:
                role = _DEFAULT_ROLE
            out.append((role, task_text))
        return out

    @staticmethod
    def _build_briefing(shared: str, goal: str) -> str:
        """Assemble the common preamble prepended to every sub-agent's task."""
        parts: List[str] = []
        if goal:
            parts.append(f"Overall goal: {goal}")
        if shared:
            parts.append(f"Shared context: {shared}")
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

    def _make_tool_executor(self):
        """Build the (name, args) -> str executor backed by the registry.

        Routes a tool call to the first non-swarm Tool Brick that advertises
        it. Errors are returned as strings (never raised) so one bad call
        can't crash a sub-agent.
        """
        registry = self._registry

        async def execute_tool(name: str, args: Dict[str, Any]) -> str:
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
