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

from dataclasses import dataclass, field
from pathlib import Path

from brikie.bricks.tool.base import ToolBrick
from brikie.bricks.tool.file_tools import ShellToolBrick
from brikie.bricks.tool.swarm import workspace as ws_mod
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


@dataclass
class _SwarmTask:
    """One normalized task in a dispatch (with its id + dependencies)."""

    index: int
    role: str
    task: str
    id: str
    depends_on: List[str] = field(default_factory=list)

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
                                    "id": {
                                        "type": "string",
                                        "description": "Optional short id for this task, so others can depend on it.",
                                    },
                                    "depends_on": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Ids of tasks that must finish first; this task runs after them and receives their outputs. Use for pipelines (e.g. a coder that depends on a researcher's findings).",
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
        isolate_coders: bool = True,
        workspace_root: Optional[str] = None,
        max_revisions: int = 1,
    ) -> None:
        super().__init__()
        self._name = "swarm"
        # Phase 2: each coder works in its own git worktree (when in a repo),
        # so parallel coders can't clobber one shared tree. workspace_root
        # defaults to the process cwd at dispatch time.
        self._isolate_coders = isolate_coders
        self._workspace_root = workspace_root
        # Phase 3: a failed review feeds back to the coder for up to this many
        # bounded retry rounds before the result returns as-is.
        self._max_revisions = max_revisions
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
        # role -> system prompt, and role -> behavioral_constraints,
        # contributed by loaded souls (set_souls).
        self._soul_roles: Dict[str, str] = {}
        self._soul_constraints: Dict[str, dict] = {}

    @property
    def name(self) -> str:
        return self._name

    async def init(self) -> None:
        await self._store.initialize()
        # Durability: a process restart means any prior dispatch is dead.
        # Flag orphaned runs honestly and clean up worktrees they leaked.
        try:
            n = await self._store.reconcile_orphans()
            if n:
                logger.info("Marked %d orphaned swarm run(s) from a prior crash.", n)
            await ws_mod.prune_swarm_worktrees(Path(self._workspace_root or Path.cwd()))
        except Exception:
            logger.debug("swarm durability reconcile failed", exc_info=True)
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
                bc = getattr(soul, "behavioral_constraints", None)
                if isinstance(bc, dict):
                    self._soul_constraints[key] = bc
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

    def _role_max_steps(self, role: str) -> int:
        """A soul role's step budget honours its behavioral_constraints."""
        ms = (self._soul_constraints.get(role) or {}).get("max_steps")
        try:
            return int(ms) if ms else self._max_steps
        except (TypeError, ValueError):
            return self._max_steps

    def _role_tool_schemas(self, role: str,
                           schemas: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Restrict a soul role's tools to its ``allowed_tools`` constraint, if any."""
        allowed = (self._soul_constraints.get(role) or {}).get("allowed_tools")
        if not allowed:
            return schemas
        allow = set(allowed)
        return [s for s in schemas
                if s.get("function", {}).get("name") in allow]

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

        # Dependency-ordered scheduling: a dependent task runs only after its
        # prerequisites finish (and receives their output) — no inbox race.
        waves, plan_error = self._plan_waves(tasks)
        if plan_error:
            return {"error": plan_error}

        provider = self._first_provider()
        if provider is None:
            return {"error": "no provider available — cannot run sub-agents."}

        shared = str(args.get("context", "")).strip()
        goal = await self._active_goal()
        do_review = bool(args.get("review", self._auto_review))

        # A shared blackboard only when more than one collaborator runs in the
        # SAME wave (cross-wave data flows via dependency outputs instead).
        has_parallel = any(len(w) > 1 for w in waves)
        board = SwarmBlackboard() if has_parallel else None
        briefing = self._build_briefing(shared, goal, collaborate=board is not None)

        base_schemas = self._subagent_tool_schemas()
        sub_schemas = base_schemas + (_MESSAGING_TOOLS if board else [])

        run_id = await self._store.start_run(goal or shared, len(tasks))
        roles_summary = ", ".join(t.role for t in tasks)
        wave_note = f" in {len(waves)} wave(s)" if len(waves) > 1 else ""
        await self._log_goal("swarm.dispatch",
                             f"delegated {len(tasks)} task(s){wave_note} to: {roles_summary}")

        # Shared across the whole dispatch: a token ceiling and a live event
        # sink so the user watches sub-agents work instead of staring at
        # silence for the minutes a dispatch can take.
        budget = CostBudget(self._max_total_tokens)
        sink = self._make_event_sink()
        await sink({"kind": "dispatch", "role": "swarm",
                    "count": len(tasks), "roles": roles_summary})

        repo_root = Path(self._workspace_root or Path.cwd())
        workspaces: Dict[int, Any] = {}        # task index → Workspace (coders)

        # Provision coder workspaces up front (worktrees at HEAD).
        provisioned: Dict[int, tuple] = {}
        for t in tasks:
            provisioned[t.index] = await self._provision_coder(
                t.role, f"{t.role}#{t.index + 1}", repo_root, workspaces, t.index)

        logger.info("Swarm %s dispatching %d sub-agent(s) across %d wave(s).",
                    run_id, len(tasks), len(waves))
        results: List[Any] = [None] * len(tasks)
        for w, wave in enumerate(waves):
            if len(waves) > 1:
                await sink({"kind": "wave", "role": "swarm",
                            "text": f"wave {w + 1}/{len(waves)}: "
                                    + ", ".join(tasks[i].id for i in wave)})
            runners_and_tasks = []
            for idx in wave:
                t = tasks[idx]
                sender = f"{t.role}#{idx + 1}"
                workspace_tool, iso_note = provisioned[idx]
                execute_tool = self._make_tool_executor(
                    board=board, sender=sender, workspace_tool=workspace_tool)
                runner = SubAgentRunner(
                    provider=provider,
                    tool_schemas=self._role_tool_schemas(t.role, sub_schemas),
                    execute_tool=execute_tool,
                    hooks=self._hooks,
                    max_steps=self._role_max_steps(t.role),
                    context_budget=self._context_budget,
                    label=t.role,
                    on_event=sink,
                    budget=budget,
                )
                body = iso_note + self._dep_section(t, tasks, results) + t.task
                full_task = f"{briefing}{body}" if briefing else body
                runners_and_tasks.append((runner, self._role_prompt(t.role), full_task))
            wave_results = await run_swarm(
                runners_and_tasks, max_parallel=self._max_parallel,
                timeout=self._subagent_timeout)
            for idx, res in zip(wave, wave_results):
                results[idx] = res
                # Durability: persist each result the moment its wave lands,
                # so a crash in a later wave doesn't lose completed work.
                await self._store.record_task(run_id, idx, res)

        # Capture each isolated coder's patch BEFORE review (so the reviewer
        # judges the actual diff) and before it's applied.
        for i, ws in workspaces.items():
            results[i].isolated = ws.isolated
            if ws.isolated:
                results[i].workspace_diff = await ws.diff()

        if do_review:
            await self._review_and_revise(results, workspaces, provider,
                                          base_schemas, sink, budget)

        # Reconcile isolated work back into the real tree: apply each
        # successful coder's patch sequentially; an overlapping one is
        # reported as a conflict, never silently merged. Then clean up.
        await self._reconcile_workspaces(workspaces, results, repo_root, sink)

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

    async def _review_and_revise(
        self,
        results: List[SubAgentResult],
        workspaces: Dict[int, Any],
        provider: Any,
        base_schemas: List[Dict[str, Any]],
        sink: Any,
        budget: Any,
    ) -> None:
        """Review every successful coder, then revise the failures (bounded).

        A coder whose review fails is re-run IN ITS OWN WORKSPACE with the
        reviewer's objections, then re-reviewed — up to ``max_revisions``
        rounds. So 'REVIEW: FAIL' actually drives a fix instead of being a
        cosmetic verdict.
        """
        targets = [i for i, r in enumerate(results) if r.role == "coder" and r.ok]
        if not targets:
            return
        await self._review_coders(results, targets, provider, base_schemas,
                                  sink, budget)

        for _round in range(self._max_revisions):
            failing = [
                i for i in targets
                if results[i].ok and results[i].reviewed and not results[i].review_ok
            ]
            if not failing:
                break
            await self._revise_coders(results, failing, workspaces, provider,
                                      base_schemas, sink, budget)
            # Re-collect each revised coder's diff, then re-review it.
            for i in failing:
                ws = workspaces.get(i)
                if ws is not None and ws.isolated:
                    results[i].workspace_diff = await ws.diff()
            await self._review_coders(results, failing, provider, base_schemas,
                                      sink, budget)

    async def _review_coders(
        self,
        results: List[SubAgentResult],
        indices: List[int],
        provider: Any,
        base_schemas: List[Dict[str, Any]],
        sink: Any,
        budget: Any,
    ) -> None:
        """Review the coder results at *indices* (only those still ok)."""
        targets = [i for i in indices if results[i].ok]
        if not targets:
            return
        review_runners = []
        for i in targets:
            r = results[i]
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
            diff_section = ""
            if r.isolated and r.workspace_diff.strip():
                diff_section = (
                    "\nThe exact changes it made (unified diff), which will be "
                    "applied to the tree if you approve:\n```diff\n"
                    f"{r.workspace_diff[:6000]}\n```\n")
            review_task = (
                "Review another sub-agent's completed work for correctness "
                "and completeness. Inspect it (read the relevant files or run "
                "checks) — do not take the report at face value.\n\n"
                f"The task it was given:\n{r.task}\n\n"
                f"Its report:\n{r.report}\n"
                f"{diff_section}\n"
                "Finish with a verdict line: 'REVIEW: PASS' if the work is "
                "correct and complete, or 'REVIEW: FAIL — <specific problems>' "
                "if not."
            )
            review_runners.append((runner, _ROLE_PROMPTS["reviewer"], review_task))

        reviews = await run_swarm(review_runners, max_parallel=self._max_parallel,
                                  timeout=self._subagent_timeout)
        for i, rev in zip(targets, reviews):
            results[i].reviewed = True
            results[i].review = rev.report.strip()
            results[i].review_ok = "REVIEW: PASS" in rev.report.upper()
            results[i].tokens_in += rev.tokens_in
            results[i].tokens_out += rev.tokens_out

    async def _revise_coders(
        self,
        results: List[SubAgentResult],
        indices: List[int],
        workspaces: Dict[int, Any],
        provider: Any,
        base_schemas: List[Dict[str, Any]],
        sink: Any,
        budget: Any,
    ) -> None:
        """Re-run failing coders with the reviewer's feedback, in-place.

        Each runs in its OWN existing worktree (its prior changes are still
        there), so it refines rather than starting over. Results are replaced
        with the revision, carrying forward the cumulative token cost and an
        incremented revision count.
        """
        revise_runners = []
        for i in indices:
            r = results[i]
            ws = workspaces.get(i)
            workspace_tool = None
            if ws is not None and ws.isolated:
                workspace_tool = ShellToolBrick(
                    root=str(ws.path), allowed_dirs=[str(ws.path)])
                await workspace_tool.init()
            runner = SubAgentRunner(
                provider=provider,
                tool_schemas=base_schemas,
                execute_tool=self._make_tool_executor(workspace_tool=workspace_tool),
                hooks=self._hooks,
                max_steps=self._max_steps,
                context_budget=self._context_budget,
                label="coder",
                on_event=sink,
                budget=budget,
            )
            revise_task = (
                "Your previous attempt at this task was reviewed and REJECTED. "
                "Revise it to fully address the feedback. Your earlier changes "
                "are already in your workspace — read the files, then fix the "
                "problems. Use relative paths.\n\n"
                f"The task:\n{r.task}\n\n"
                f"Reviewer feedback (what to fix):\n{r.review}\n\n"
                "Finish with a concise report ending in TASK COMPLETE or "
                "TASK FAILED: <reason>."
            )
            revise_runners.append((runner, _ROLE_PROMPTS["coder"], revise_task))
            await sink({"kind": "revise", "role": "coder",
                        "text": f"revising coder#{i + 1} after a failed review"})

        revised = await run_swarm(revise_runners, max_parallel=self._max_parallel,
                                  timeout=self._subagent_timeout)
        for i, rev in zip(indices, revised):
            old = results[i]
            rev.role = "coder"
            rev.isolated = old.isolated
            rev.revisions = old.revisions + 1
            rev.tokens_in += old.tokens_in        # cumulative dispatch cost
            rev.tokens_out += old.tokens_out
            results[i] = rev

    # ------------------------------------------------------------------
    # Isolated coder workspaces (Phase 2)
    # ------------------------------------------------------------------

    async def _provision_coder(
        self, role: str, sender: str, repo_root: Any,
        workspaces: Dict[int, Any], index: int,
    ) -> tuple:
        """Provision an isolated workspace for a coder; (workspace_tool, note).

        Returns a root-scoped ShellToolBrick the coder's file/shell tools are
        routed to (or None when not isolating) and a task-preamble note. Non-
        coder roles, or when isolation is off/unavailable, get (None, "").
        """
        if role != "coder" or not self._isolate_coders:
            return None, ""
        ws = await ws_mod.provision(repo_root, sender)
        workspaces[index] = ws
        if not ws.isolated:
            return None, ""
        tool = ShellToolBrick(root=str(ws.path), allowed_dirs=[str(ws.path)])
        await tool.init()
        note = (
            "You are working in an ISOLATED copy of the project (your own git "
            "worktree). Use RELATIVE file paths. Your changes are captured and "
            "merged back into the real tree only if they don't conflict with "
            "another sub-agent's — so stay within the scope you were given.\n\n"
        )
        return tool, note

    async def _reconcile_workspaces(
        self, workspaces: Dict[int, Any], results: List[SubAgentResult],
        repo_root: Any, sink: Any,
    ) -> None:
        """Apply each successful coder's patch to the real tree, then clean up.

        Patches are applied in order; the first to touch a hunk wins and any
        overlapping patch is recorded as a conflict (the real tree is left
        untouched by a failed apply). Worktrees are always removed.
        """
        for i, ws in workspaces.items():
            r = results[i]
            try:
                if ws.isolated and r.ok and r.workspace_diff.strip():
                    applied, detail = await ws.apply_to(repo_root)
                    r.workspace_applied = applied
                    if not applied:
                        r.workspace_conflict = detail
                    await sink({
                        "kind": "workspace", "role": r.role,
                        "text": (f"{r.role} changes "
                                 + ("merged" if applied else f"CONFLICT: {detail[:80]}")),
                    })
            except Exception:
                logger.exception("workspace reconcile failed for task %d", i)
            finally:
                await ws.cleanup()

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

    def _normalize_tasks(self, raw_tasks: List[Any]) -> List[_SwarmTask]:
        """Validate/clean the raw task list into _SwarmTask objects.

        Each task gets a stable id (its given ``id`` or ``t<index>``) and an
        optional ``depends_on`` list of other task ids — the basis for
        dependency-ordered scheduling.
        """
        valid_roles = self._role_prompts()
        out: List[_SwarmTask] = []
        for item in raw_tasks:
            if isinstance(item, dict):
                task_text = str(item.get("task", "")).strip()
                role = str(item.get("role", "") or "").strip().lower()
                tid = str(item.get("id", "") or "").strip()
                deps_raw = item.get("depends_on") or item.get("after") or []
                depends_on = [str(d).strip() for d in deps_raw
                              if str(d).strip()] if isinstance(deps_raw, list) else []
            elif isinstance(item, str):
                task_text, role, tid, depends_on = item.strip(), "", "", []
            else:
                continue
            if not task_text:
                continue
            if role not in valid_roles:
                role = _DEFAULT_ROLE
            idx = len(out)
            out.append(_SwarmTask(
                index=idx, role=role, task=task_text,
                id=tid or f"t{idx}", depends_on=depends_on,
            ))
        return out

    @staticmethod
    def _plan_waves(tasks: List[_SwarmTask]) -> tuple:
        """Topologically order tasks into concurrency waves.

        Returns (waves, error). ``waves`` is a list of lists of task indices:
        every task in wave N depends only on tasks in waves < N, so a
        dependent never starts before — or races — its prerequisite. Returns
        an error string for an unknown dependency id or a dependency cycle.
        """
        id_to_index = {t.id: t.index for t in tasks}
        # Resolve dep ids → indices; reject unknown references.
        deps: Dict[int, set] = {}
        for t in tasks:
            resolved = set()
            for dep in t.depends_on:
                if dep == t.id:
                    return [], f"task '{t.id}' cannot depend on itself."
                if dep not in id_to_index:
                    return [], f"task '{t.id}' depends on unknown id '{dep}'."
                resolved.add(id_to_index[dep])
            deps[t.index] = resolved

        waves: List[List[int]] = []
        done: set = set()
        remaining = set(range(len(tasks)))
        while remaining:
            ready = sorted(i for i in remaining if deps[i] <= done)
            if not ready:
                stuck = ", ".join(tasks[i].id for i in sorted(remaining))
                return [], f"dependency cycle among tasks: {stuck}."
            waves.append(ready)
            done |= set(ready)
            remaining -= set(ready)
        return waves, ""

    @staticmethod
    def _dep_section(task: _SwarmTask, tasks: List[_SwarmTask],
                     results: List[Any]) -> str:
        """The prerequisite outputs injected into a dependent task's prompt."""
        if not task.depends_on:
            return ""
        id_to_index = {t.id: t.index for t in tasks}
        blocks = []
        for dep in task.depends_on:
            di = id_to_index.get(dep)
            r = results[di] if di is not None else None
            if r is not None:
                blocks.append(f"### Output from '{dep}' ({r.role}):\n{r.report}")
        if not blocks:
            return ""
        return ("Outputs from the sub-agents you depend on (already complete):\n\n"
                + "\n\n".join(blocks) + "\n\n---\n\n")

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

    def _make_tool_executor(self, board: Any = None, sender: str = "",
                            workspace_tool: Any = None):
        """Build the (name, args) -> str executor backed by the registry.

        When a ``board`` is supplied, the collaboration tools
        (swarm_share / swarm_inbox) are handled in-process against it. When a
        ``workspace_tool`` (a root-scoped ShellToolBrick) is supplied, this
        coder's file/shell tools route to THAT instance so its edits land in
        its isolated worktree, not the shared tree. Everything else routes to
        the first non-swarm Tool Brick that advertises the call. Errors are
        returned as strings (never raised) so one bad call can't crash a
        sub-agent.
        """
        registry = self._registry
        ws_names = {
            s.get("function", {}).get("name")
            for s in (getattr(workspace_tool, "tools", None) or [])
        } if workspace_tool is not None else set()

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

            # An isolated coder's file/shell tools hit its own worktree.
            if name in ws_names:
                try:
                    return str(await workspace_tool.execute(name, args))
                except Exception as exc:
                    return f"Tool error ({type(exc).__name__}): {exc}"

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
