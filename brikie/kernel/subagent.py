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
from typing import Any, Awaitable, Callable, Dict, List

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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "task": self.task,
            "ok": self.ok,
            "report": self.report,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "tools_used": self.tools_used,
            "blocked": self.blocked,
            "error": self.error,
        }


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
    ) -> None:
        self._provider = provider
        self._tool_schemas = tool_schemas
        self._execute_tool = execute_tool
        self._hooks = hooks
        self._max_steps = max(1, max_steps)
        self._context_budget = context_budget
        self._label = label

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

        for step in range(self._max_steps):
            self._compact(messages)
            try:
                content, raw_calls = await self._complete(messages)
            except Exception as exc:  # provider blew up — bounded failure
                logger.warning("[%s] provider error: %s", self._label, exc)
                return SubAgentResult(
                    role=self._label, task=task, ok=False,
                    report=f"sub-agent could not run: {exc}",
                    steps=step, tool_calls=total_tool_calls,
                    tools_used=tools_used, blocked=blocked, error=str(exc),
                )

            if not raw_calls:
                ok = _DONE_MARKER in content and _FAIL_MARKER not in content
                return SubAgentResult(
                    role=self._label, task=task, ok=ok,
                    report=content.strip() or "(no report)",
                    steps=step + 1, tool_calls=total_tool_calls,
                    tools_used=tools_used, blocked=blocked,
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

        return SubAgentResult(
            role=self._label, task=task, ok=False,
            report=(
                f"step budget ({self._max_steps}) exhausted before the task "
                "was reported complete."
            ),
            steps=self._max_steps, tool_calls=total_tool_calls,
            tools_used=tools_used, blocked=blocked,
            error="step_budget_exhausted",
        )

    # ------------------------------------------------------------------
    # Provider + tools
    # ------------------------------------------------------------------

    async def _complete(self, messages: List[Dict[str, Any]]):
        """One provider call; normalize 2-/3-tuple returns to (content, calls)."""
        result = await self._provider.get_completion(messages, self._tool_schemas)
        content = result[0] or ""
        raw_calls = result[1] or []
        if not content and len(result) >= 3:
            # Reasoning-only models park the answer in the thinking channel.
            content = (result[2] or {}).get("reasoning", "") or ""
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
                continue
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
) -> List[SubAgentResult]:
    """Run several sub-agents concurrently, bounded by a parallelism cap.

    Each entry is ``(runner, system_prompt, task)``. Results return in the
    same order as the input. A semaphore caps how many sub-agents hit the
    provider at once so a big fan-out doesn't stampede the model server.
    """
    sem = asyncio.Semaphore(max(1, max_parallel))

    async def _one(runner: SubAgentRunner, system: str, task: str) -> SubAgentResult:
        async with sem:
            return await runner.run(system, task)

    return await asyncio.gather(*(
        _one(runner, system, task)
        for runner, system, task in runners_and_tasks
    ))
