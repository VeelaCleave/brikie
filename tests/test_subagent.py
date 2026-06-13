"""Tests for the SubAgentRunner — the isolated, bounded sub-agent loop
under the Swarm tier (goals #4).

Verified with scripted fake providers and a fake hook dispatcher, never by
running a live model — the contract (isolation, bounded steps, security in
path, fail-open) must be provable deterministically.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from brikie.config.types import HookType
from brikie.kernel.subagent import (
    SubAgentRunner,
    SubAgentResult,
    run_swarm,
)


def _call(name: str, args: Dict[str, Any], cid: str = "c1") -> Dict[str, Any]:
    return {"id": cid, "function": {"name": name, "arguments": json.dumps(args)}}


class ScriptedProvider:
    """Returns a scripted sequence of (content, raw_calls[, meta]) per call."""

    def __init__(self, script: List[tuple]) -> None:
        self.name = "scripted"
        self._script = script
        self.calls = 0

    async def get_completion(self, messages, tools):
        i = min(self.calls, len(self._script) - 1)
        self.calls += 1
        return self._script[i]


class RecordingExecutor:
    """An execute_tool callable that records invocations."""

    def __init__(self, result: str = "ok") -> None:
        self.invocations: List[tuple] = []
        self._result = result

    async def __call__(self, name: str, args: Dict[str, Any]) -> str:
        self.invocations.append((name, args))
        return self._result


class VetoHooks:
    """A fake dispatcher that, on PRE_TOOL, pre-settles every call's result
    — exactly the security veto channel the real firewall/watchdog use."""

    def __init__(self, verdict: str = "BLOCKED: nope") -> None:
        self.verdict = verdict
        self.dispatched: List[HookType] = []

    async def dispatch(self, hook_type, event):
        self.dispatched.append(hook_type)
        if hook_type == HookType.PRE_TOOL:
            for tc in event.data:
                tc.result = self.verdict
        return []


def _runner(provider, executor, hooks=None, **kw):
    return SubAgentRunner(
        provider=provider,
        tool_schemas=[],
        execute_tool=executor,
        hooks=hooks,
        label=kw.pop("label", "tester"),
        **kw,
    )


class TestFinish:
    async def test_immediate_success(self):
        prov = ScriptedProvider([("All done. TASK COMPLETE", [], {})])
        ex = RecordingExecutor()
        res = await _runner(prov, ex).run("sys", "do it")
        assert isinstance(res, SubAgentResult)
        assert res.ok is True
        assert "TASK COMPLETE" in res.report
        assert ex.invocations == []          # no tools needed
        assert res.tool_calls == 0

    async def test_failure_marker(self):
        prov = ScriptedProvider([("Cannot. TASK FAILED: no access", [], {})])
        res = await _runner(prov, RecordingExecutor()).run("sys", "do it")
        assert res.ok is False
        assert "no access" in res.report

    async def test_no_marker_is_not_success(self):
        prov = ScriptedProvider([("here is some prose with no marker", [], {})])
        res = await _runner(prov, RecordingExecutor()).run("sys", "do it")
        assert res.ok is False


class TestToolRounds:
    async def test_runs_a_tool_then_finishes(self):
        prov = ScriptedProvider([
            ("working", [_call("read_file", {"path": "/x"})], {}),
            ("found it. TASK COMPLETE", [], {}),
        ])
        ex = RecordingExecutor(result="file contents")
        res = await _runner(prov, ex).run("sys", "read the file")
        assert res.ok is True
        assert ex.invocations == [("read_file", {"path": "/x"})]
        assert res.tool_calls == 1
        assert res.tools_used == ["read_file"]

    async def test_two_tuple_return_is_handled(self):
        # Some providers return (content, calls) with no meta.
        prov = ScriptedProvider([("done. TASK COMPLETE", [])])
        res = await _runner(prov, RecordingExecutor()).run("sys", "x")
        assert res.ok is True

    async def test_reasoning_channel_used_when_content_empty(self):
        prov = ScriptedProvider([("", [], {"reasoning": "thought it through. TASK COMPLETE"})])
        res = await _runner(prov, RecordingExecutor()).run("sys", "x")
        assert res.ok is True
        assert "TASK COMPLETE" in res.report


class TestSecurityInPath:
    async def test_pre_tool_veto_blocks_execution(self):
        # The hook settles tc.result on PRE_TOOL → the tool must NOT run.
        prov = ScriptedProvider([
            ("trying", [_call("bash_execute", {"command": "rm -rf /"})], {}),
            ("gave up. TASK FAILED: blocked", [], {}),
        ])
        ex = RecordingExecutor()
        hooks = VetoHooks("BLOCKED: destructive")
        res = await _runner(prov, ex, hooks=hooks).run("sys", "wipe disk")
        assert ex.invocations == []                      # vetoed, never ran
        assert any("BLOCKED" in b for b in res.blocked)
        assert HookType.PRE_TOOL in hooks.dispatched
        assert HookType.POST_TOOL in hooks.dispatched

    async def test_hooks_optional(self):
        # No dispatcher wired → tools still run (no middleware).
        prov = ScriptedProvider([
            ("go", [_call("read_file", {"path": "/x"})], {}),
            ("done. TASK COMPLETE", [], {}),
        ])
        ex = RecordingExecutor()
        res = await _runner(prov, ex, hooks=None).run("sys", "x")
        assert ex.invocations == [("read_file", {"path": "/x"})]
        assert res.ok is True


class TestBounds:
    async def test_step_budget_exhausted(self):
        # Provider always asks for a tool → never finishes → bounded stop.
        prov = ScriptedProvider([("loop", [_call("read_file", {"p": 1})], {})])
        res = await _runner(prov, RecordingExecutor(), max_steps=3).run("sys", "x")
        assert res.ok is False
        assert res.error == "step_budget_exhausted"
        assert res.steps == 3
        assert prov.calls == 3

    async def test_provider_error_is_bounded_failure(self):
        class Boom:
            name = "boom"
            async def get_completion(self, messages, tools):
                raise RuntimeError("provider down")

        res = await _runner(Boom(), RecordingExecutor()).run("sys", "x")
        assert res.ok is False
        assert "provider down" in res.error

    async def test_context_compaction_truncates_old_tool_results(self):
        runner = _runner(ScriptedProvider([("x", [])]), RecordingExecutor(),
                         context_budget=10)
        big = "A" * 5000
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            {"role": "tool", "content": big},
            {"role": "tool", "content": big},
            {"role": "assistant", "content": "recent1"},
            {"role": "assistant", "content": "recent2"},
            {"role": "assistant", "content": "recent3"},
            {"role": "assistant", "content": "recent4"},
        ]
        runner._compact(messages)
        # An old tool result got clipped…
        assert "[truncated]" in messages[2]["content"]
        assert len(messages[2]["content"]) < len(big)
        # …but the recent working set is untouched.
        assert messages[-1]["content"] == "recent4"


class TestSwarmConcurrency:
    async def test_run_swarm_returns_in_order(self):
        runners = []
        for n in range(3):
            prov = ScriptedProvider([(f"agent {n}. TASK COMPLETE", [], {})])
            runners.append((_runner(prov, RecordingExecutor(), label=f"r{n}"),
                            "sys", f"task {n}"))
        results = await run_swarm(runners, max_parallel=2)
        assert [r.task for r in results] == ["task 0", "task 1", "task 2"]
        assert all(r.ok for r in results)

    async def test_run_swarm_isolates_failures(self):
        good = (_runner(ScriptedProvider([("good. TASK COMPLETE", [], {})]),
                        RecordingExecutor(), label="good"), "sys", "g")

        class Boom:
            name = "boom"
            async def get_completion(self, m, t):
                raise RuntimeError("nope")

        bad = (_runner(Boom(), RecordingExecutor(), label="bad"), "sys", "b")
        results = await run_swarm([good, bad])
        assert results[0].ok is True
        assert results[1].ok is False        # one failing agent doesn't sink the swarm
