"""Sub-agent runner — an isolated, bounded agent loop the kernel can spawn.

This is the substrate under the Swarm tier (goals #4): a coordinator agent
delegates a scoped task to an ephemeral *sub-agent* that runs its own short
agent loop — its own message history, its own step and context budget — and
reports back a single concise result. When it finishes, its context is
discarded (clean GC): nothing leaks back into the parent's window except the
report.

Why a separate runner rather than reusing the main event loop:
- **Isolation.** Each sub-agent reasons in a fresh, scoped context. A
  ten-file research dig doesn't bloat the parent's prompt — only the
  findings come back. This is the per-subagent context limit the roadmap
  asks for.
- **Containment.** A sub-agent gets a step budget and a token budget; it
  can't run away. The parent stays responsive.
- **Security stays in path.** The runner dispatches the same PRE_TOOL /
  POST_TOOL hooks the main loop does, so the CommandFirewall and Watchdog
  gate a sub-agent's tool calls exactly as they gate the parent's. A
  PRE_TOOL veto (a hook that pre-settles ``tc.result``) is honoured: the
  call is reported as blocked, never executed.

Kernel purity (AGENTS #1): this module imports nothing from
``brikie.bricks``. The provider, the tool executor, and the hook dispatcher
are all passed in as duck-typed callables/objects. The caller (the
SwarmToolBrick) wires them from the registry; the kernel stays generic.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from brikie.config.types import HookEvent, HookType, ToolCall

logger = logging.getLogger(__name__)

# A sub-agent is meant to be a short, focused worker — not a second main
# loop. Tight defaults keep fan-out cheap and bounded.
DEFAULT_MAX_STEPS = 12
DEFAULT_CONTEXT_BUDGET = 16000

# When a sub-agent's isolated history exceeds the budget, the oldest tool
# results are truncated to this many characters so inference stays fast
# without dropping the structure of the conversation.
_TOOL_RESULT_CAP = 600

# Signals the sub-agent uses to declare it is finished.
_DONE_MARKER = "TASK COMPLETE"
_FAIL_MARKER = "TASK FAILED"


# A callable that actually runs a tool by name and returns its string
# result. The SwarmToolBrick supplies one backed by the registry's Tool
# Bricks (minus the swarm tools themselves, so a sub-agent can't recurse).
ToolExecutor = Callable[[str, Dict[str, Any]], Awaitable[str]]

# A callback the runner emits lifecycle events to so a long swarm isn't a
# black box. Each event is a dict: {"kind", "role", ...}. Kinds: "start",
# "tool", "blocked", "done", "timeout". Fire-and-forget; failures are swallowed.
EventSink = Callable[[Dict[str, Any]], Awaitable[None]]


def _estimate_tokens(text: Any) -> int:
    """Cheap, dependency-free token estimate (~4 chars/token)."""
    if not isinstance(text, str):
        return 0
    return (len(text) + 3) // 4


@dataclass
class SubAgentResult:
    """The outcome of one sub-agent run — all that returns to the parent."""

    role: str
    task: str
    ok: bool
    report: str
    steps: int = 0
    tool_calls: int = 0
    tools_used: List[str] = field(default_factory=list)
    blocked: List[str] = field(default_factory=list)
    error: str = ""
    # Real token accounting (summed from provider usage meta).
    tokens_in: int = 0
    tokens_out: int = 0
    # Set when an auto-reviewer sub-agent inspected this result (coder pass).
    reviewed: bool = False
    review_ok: bool = False
    review: str = ""
    revisions: int = 0          # how many review→revise rounds this took
    # Set for an isolated coder (Phase 2): its captured patch and how it
    # landed back in the real tree.
    isolated: bool = False
    workspace_diff: str = ""
    workspace_applied: bool = False
    workspace_conflict: str = ""

    def to_dict(self) -> Dict[str, Any]:
        out = {
            "role": self.role,
            "task": self.task,
            "ok": self.ok,
            "report": self.report,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "tools_used": self.tools_used,
            "blocked": self.blocked,
            "error": self.error,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
        }
        if self.reviewed:
            out["reviewed"] = True
            out["review_ok"] = self.review_ok
            out["review"] = self.review
            if self.revisions:
                out["revisions"] = self.revisions
        if self.isolated:
            out["isolated"] = True
            out["workspace_applied"] = self.workspace_applied
            if self.workspace_conflict:
                out["workspace_conflict"] = self.workspace_conflict
            if self.workspace_diff:
                # A compact signal, not the whole patch, to keep the model's
                # context lean — the full patch is on the result object.
                out["workspace_diff_lines"] = self.workspace_diff.count("\n")
        return out


class SwarmBlackboard:
    """A shared, append-only message board for one swarm dispatch.

    This is what turns fan-out/fan-in into genuine collaboration: while the
    sub-agents of a dispatch run concurrently, each can ``post`` a finding
    and ``read`` what the others have shared. A long-running researcher can
    surface a fact mid-run that a coder picks up before it finishes. Scoped
    to a single dispatch and discarded with it (no cross-run leakage).
    """

    def __init__(self) -> None:
        self._messages: List[Dict[str, str]] = []
        self._lock = asyncio.Lock()

    async def post(self, sender: str, note: str) -> None:
        async with self._lock:
            self._messages.append({"from": sender, "note": note})

    async def read(self, exclude_sender: str = "") -> List[Dict[str, str]]:
        async with self._lock:
            return [m for m in self._messages if m["from"] != exclude_sender]

    def snapshot(self) -> List[Dict[str, str]]:
        """All messages posted during the run (for the audit trail)."""
        return list(self._messages)


class CostBudget:
    """A shared, cooperative token ceiling for one swarm dispatch.

    A fan-out can burn a lot of tokens fast. Each sub-agent checks the
    shared budget before every provider call and stops early once the
    swarm's combined usage crosses the ceiling — a real cap, not a
    post-hoc tally. ``max_tokens <= 0`` means unlimited.
    """

    def __init__(self, max_tokens: int = 0) -> None:
        self.max_tokens = max_tokens
        self.used = 0

    def add(self, tokens: int) -> None:
        self.used += max(0, tokens)

    def exceeded(self) -> bool:
        return self.max_tokens > 0 and self.used >= self.max_tokens

    def remaining(self) -> int:
        return max(0, self.max_tokens - self.used) if self.max_tokens > 0 else -1


class SubAgentRunner:
    """Runs one scoped task as an isolated, bounded sub-agent.

    Args:
        provider: A Provider Brick (duck-typed: ``get_completion(messages,
            tools)`` returning ``(content, tool_calls[, meta])``).
        tool_schemas: OpenAI-format tool schemas the sub-agent may call.
        execute_tool: Async callable ``(name, args) -> str`` that runs a
            tool and returns its result. Backed by the registry's Tool
            Bricks (swarm tools excluded to prevent recursion).
        hooks: Optional HookDispatcher (duck-typed ``dispatch(type, event)``)
            so security bricks gate the sub-agent's tool calls. None = no
            middleware (tools still run).
        max_steps: Provider⇄tool rounds before the sub-agent is stopped.
        context_budget: Estimated-token ceiling for the isolated history;
            over it, old tool results are truncated.
        label: A short name for logs (e.g. the role).
        on_event: Optional async sink for lifecycle events (live streaming).
        budget: Optional shared CostBudget — the sub-agent stops early when
            the swarm's combined token usage crosses the ceiling.
    """

    def __init__(
        self,
        provider: Any,
        tool_schemas: List[Dict[str, Any]],
        execute_tool: ToolExecutor,
        hooks: Any = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        context_budget: int = DEFAULT_CONTEXT_BUDGET,
        label: str = "subagent",
        on_event: Optional[EventSink] = None,
        budget: Optional[CostBudget] = None,
    ) -> None:
        self._provider = provider
        self._tool_schemas = tool_schemas
        self._execute_tool = execute_tool
        self._hooks = hooks
        self._max_steps = max(1, max_steps)
        self._context_budget = context_budget
        self._label = label
        self._on_event = on_event
        self._budget = budget
        self._tokens_in = 0
        self._tokens_out = 0

    @property
    def label(self) -> str:
        return self._label

    async def _emit(self, kind: str, **fields: Any) -> None:
        """Surface a lifecycle event to the sink (best-effort)."""
        if self._on_event is None:
            return
        try:
            await self._on_event({"kind": kind, "role": self._label, **fields})
        except Exception:
            logger.debug("[%s] event sink failed (%s)", self._label, kind,
                         exc_info=True)

    async def run(self, system_prompt: str, task: str) -> SubAgentResult:
        """Drive the sub-agent to a final report (or a bounded failure)."""
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": (
                f"{task}\n\n"
                "Work the task with your tools, verify the result, and finish "
                f"with a concise report. End your final message with "
                f"'{_DONE_MARKER}' on success or '{_FAIL_MARKER}: <reason>' if "
                "you cannot complete it."
            )},
        ]
        tools_used: List[str] = []
        blocked: List[str] = []
        total_tool_calls = 0

        await self._emit("start", task=task[:120])

        for step in range(self._max_steps):
            # Cooperative cost ceiling: stop before spending more once the
            # swarm's combined token usage has crossed the budget.
            if self._budget is not None and self._budget.exceeded():
                return self._result(
                    task, ok=False, steps=step, tool_calls=total_tool_calls,
                    tools_used=tools_used, blocked=blocked,
                    report="stopped: the swarm's token budget was reached.",
                    error="cost_ceiling",
                )

            self._compact(messages)
            try:
                content, raw_calls = await self._complete(messages)
            except asyncio.CancelledError:
                # A timeout/cancel from the swarm — propagate cleanly.
                await self._emit("cancelled")
                raise
            except Exception as exc:  # provider blew up — bounded failure
                logger.warning("[%s] provider error: %s", self._label, exc)
                await self._emit("done", ok=False, error=str(exc))
                return self._result(
                    task, ok=False, steps=step, tool_calls=total_tool_calls,
                    tools_used=tools_used, blocked=blocked,
                    report=f"sub-agent could not run: {exc}", error=str(exc),
                )

            if not raw_calls:
                ok = _DONE_MARKER in content and _FAIL_MARKER not in content
                await self._emit("done", ok=ok, steps=step + 1)
                return self._result(
                    task, ok=ok, steps=step + 1, tool_calls=total_tool_calls,
                    tools_used=tools_used, blocked=blocked,
                    report=content.strip() or "(no report)",
                )

            messages.append({
                "role": "assistant",
                "content": content or None,
                "tool_calls": raw_calls,
            })
            results, used, blk = await self._run_tools(raw_calls)
            total_tool_calls += len(raw_calls)
            tools_used.extend(used)
            blocked.extend(blk)
            messages.extend(results)

        await self._emit("done", ok=False, error="step_budget_exhausted")
        return self._result(
            task, ok=False, steps=self._max_steps, tool_calls=total_tool_calls,
            tools_used=tools_used, blocked=blocked,
            report=(
                f"step budget ({self._max_steps}) exhausted before the task "
                "was reported complete."
            ),
            error="step_budget_exhausted",
        )

    def _result(self, task: str, **kw: Any) -> SubAgentResult:
        """Build a SubAgentResult, folding in this run's token totals."""
        return SubAgentResult(
            role=self._label, task=task,
            tokens_in=self._tokens_in, tokens_out=self._tokens_out, **kw,
        )

    # ------------------------------------------------------------------
    # Provider + tools
    # ------------------------------------------------------------------

    async def _complete(self, messages: List[Dict[str, Any]]):
        """One provider call; normalize 2-/3-tuple returns to (content, calls).

        Also records real token usage from the provider's meta into this
        run's totals and the shared cost budget.
        """
        result = await self._provider.get_completion(messages, self._tool_schemas)
        content = result[0] or ""
        raw_calls = result[1] or []
        meta = result[2] if len(result) >= 3 else {}
        if not content and meta:
            # Reasoning-only models park the answer in the thinking channel.
            content = (meta or {}).get("reasoning", "") or ""
        usage = (meta or {}).get("usage") or {}
        tin = usage.get("prompt_tokens", 0) or 0
        tout = usage.get("completion_tokens", 0) or 0
        self._tokens_in += tin
        self._tokens_out += tout
        if self._budget is not None:
            self._budget.add(tin + tout)
        return content, raw_calls

    async def _run_tools(
        self, raw_calls: List[Dict[str, Any]],
    ):
        """Execute one batch of tool calls through the security hooks.

        Returns (tool_result_messages, tools_used, blocked_descriptions).
        A PRE_TOOL hook may pre-settle a call's ``result`` to veto it; such
        a call is reported, not executed (mirrors the main loop's contract).
        """
        tool_calls = self._raw_to_tool_calls(raw_calls)

        await self._dispatch(HookType.PRE_TOOL, tool_calls)

        used: List[str] = []
        blocked: List[str] = []
        for tc in tool_calls:
            if tc.result is not None:
                # A security hook already settled (blocked/revised) this call.
                blocked.append(f"{tc.name}: {tc.result}")
                await self._emit("blocked", tool=tc.name)
                continue
            await self._emit("tool", tool=tc.name)
            try:
                tc.result = await self._execute_tool(tc.name, tc.args)
            except Exception as exc:
                tc.result = f"Tool error ({type(exc).__name__}): {exc}"
            used.append(tc.name)

        await self._dispatch(HookType.POST_TOOL, tool_calls)

        messages = [
            {
                "role": "tool",
                "content": str(tc.result) if tc.result else "null",
                "tool_call_id": tc.tool_call_id or tc.name,
            }
            for tc in tool_calls
        ]
        return messages, used, blocked

    async def _dispatch(self, hook_type: HookType, tool_calls: List[ToolCall]) -> None:
        """Dispatch a hook stage if a dispatcher is wired (else no-op)."""
        if self._hooks is None:
            return
        try:
            await self._hooks.dispatch(hook_type, HookEvent(
                hook_type=hook_type,
                data=tool_calls,
                brick_name=f"swarm:{self._label}",
            ))
        except Exception:
            logger.exception("[%s] hook dispatch failed (%s)", self._label, hook_type)

    @staticmethod
    def _raw_to_tool_calls(raw: List[Dict[str, Any]]) -> List[ToolCall]:
        """Convert raw provider tool-call dicts into ToolCall objects."""
        result: List[ToolCall] = []
        for item in raw:
            call_id = item.get("id", "")
            if "function" in item:
                func = item["function"]
                args_raw = func.get("arguments", "{}")
                try:
                    args = json.loads(args_raw)
                except (json.JSONDecodeError, TypeError):
                    args = args_raw if isinstance(args_raw, dict) else {}
                result.append(ToolCall(
                    name=func.get("name", ""), args=args, tool_call_id=call_id,
                ))
            elif "name" in item:
                result.append(ToolCall(
                    name=item["name"], args=item.get("args", {}),
                    tool_call_id=call_id,
                ))
        return result

    # ------------------------------------------------------------------
    # Per-subagent context limit
    # ------------------------------------------------------------------

    def _compact(self, messages: List[Dict[str, Any]]) -> None:
        """Truncate old tool results in place when over the token budget.

        A sub-agent is short-lived, so a light touch is enough: keep the
        system + task and the most recent exchanges verbatim, and clip the
        bodies of older ``tool`` results (the usual bloat source) to a cap.
        Never drops messages — preserves the loop's structure.
        """
        total = sum(_estimate_tokens(m.get("content")) for m in messages)
        if total <= self._context_budget:
            return
        # Clip everything but the last few messages (the working set).
        cutoff = max(2, len(messages) - 4)
        clipped = 0
        for m in messages[:cutoff]:
            if m.get("role") == "tool":
                body = m.get("content") or ""
                if len(body) > _TOOL_RESULT_CAP:
                    m["content"] = body[:_TOOL_RESULT_CAP] + " …[truncated]"
                    clipped += 1
        if clipped:
            logger.debug("[%s] clipped %d old tool result(s) to fit budget",
                         self._label, clipped)


async def run_swarm(
    runners_and_tasks: List["tuple[SubAgentRunner, str, str]"],
    max_parallel: int = 4,
    timeout: float = 0,
) -> List[SubAgentResult]:
    """Run several sub-agents concurrently, bounded by a parallelism cap.

    Each entry is ``(runner, system_prompt, task)``. Results return in the
    same order as the input. A semaphore caps how many sub-agents hit the
    provider at once so a big fan-out doesn't stampede the model server.

    ``timeout`` (seconds, 0 = none) is a per-sub-agent WALL-CLOCK deadline:
    a sub-agent that exceeds it — e.g. stuck on a hung tool, where the step
    budget alone would never fire — is cancelled and returned as a bounded
    ``timeout`` failure instead of hanging the whole swarm.

    On outer cancellation (the parent turn is torn down) every in-flight
    sub-agent is cancelled cleanly; no tasks are orphaned.
    """
    sem = asyncio.Semaphore(max(1, max_parallel))

    async def _one(runner: SubAgentRunner, system: str, task: str) -> SubAgentResult:
        async with sem:
            try:
                if timeout and timeout > 0:
                    return await asyncio.wait_for(runner.run(system, task), timeout)
                return await runner.run(system, task)
            except asyncio.TimeoutError:
                logger.warning("[%s] timed out after %.0fs", runner.label, timeout)
                await runner._emit("timeout", seconds=timeout)
                return SubAgentResult(
                    role=runner.label, task=task, ok=False,
                    report=f"timed out after {timeout:.0f}s (no result).",
                    error="timeout",
                )

    tasks = [
        asyncio.ensure_future(_one(runner, system, task))
        for runner, system, task in runners_and_tasks
    ]
    try:
        return await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for t in tasks:
            t.cancel()
        # Let the cancellations settle so nothing is left dangling.
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
