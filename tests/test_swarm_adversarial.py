"""Phase 6 — adversarial verification for the swarm.

Hostile conditions, not happy paths: a hung tool, a provider that dies
mid-swarm, a big concurrent fan-out, mid-flight cancellation, and tools that
raise. The point is to prove the swarm stays bounded, isolates failures, and
never wedges — so if one of these ever regresses, a test goes red.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import pytest

from brikie.bricks.tool.swarm.swarm_brick import SwarmToolBrick
from brikie.kernel.registry import ProviderBrick, ToolBrick
from brikie.kernel.subagent import SubAgentRunner, run_swarm


def _call(name, args):
    return {"id": "c1", "function": {"name": name, "arguments": json.dumps(args)}}


def _runner(provider, execute_tool, **kw):
    return SubAgentRunner(provider=provider, tool_schemas=[],
                          execute_tool=execute_tool, **kw)


class TestHungTool:
    async def test_hung_tool_hits_wall_clock_timeout(self):
        # The provider is fine; a TOOL hangs forever. Only the wall-clock
        # deadline saves the sub-agent (the step budget never fires).
        class Prov:
            name = "p"
            async def get_completion(self, m, t):
                return ("calling", [_call("slow_tool", {})], {})

        async def hung_tool(name, args):
            await asyncio.sleep(60)
            return "never"

        r = _runner(Prov(), hung_tool, label="x")
        results = await run_swarm([(r, "sys", "task")], timeout=0.2)
        assert results[0].ok is False and results[0].error == "timeout"


class TestProviderDiesMidSwarm:
    async def test_one_provider_failure_does_not_sink_dispatch(self, tmp_path):
        class FlakyProvider:
            name = "flaky"
            async def get_completion(self, messages, tools):
                if "BOOM" in messages[1]["content"]:
                    raise RuntimeError("provider exploded")
                return ("fine. TASK COMPLETE", [], {})

        class Reg:
            def __init__(self, p):
                self._providers = [p]
                self._tools = []
                self._bricks = {}
            def get_all(self, cls):
                return self._providers if cls is ProviderBrick else (
                    self._tools if cls is ToolBrick else [])

        reg = Reg(FlakyProvider())
        brick = SwarmToolBrick(registry=reg, db_path=str(tmp_path / "s.db"),
                               isolate_coders=False)
        reg._tools.append(brick)
        await brick.init()
        out = await brick.execute("swarm_dispatch", {
            "tasks": [
                {"role": "generalist", "task": "normal work"},
                {"role": "generalist", "task": "BOOM this one explodes"},
            ],
            "review": False,
        })
        oks = [r["ok"] for r in out["results"]]
        assert oks == [True, False]            # failure isolated to its agent
        assert "1/2" in out["summary"]
        await brick.shutdown()


class TestLoadAndParallelismCap:
    async def test_many_agents_complete_and_cap_holds(self):
        class ConcProvider:
            name = "c"
            def __init__(self):
                self.active = 0
                self.max_active = 0
            async def get_completion(self, m, t):
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                await asyncio.sleep(0.02)
                self.active -= 1
                return ("done. TASK COMPLETE", [], {})

        prov = ConcProvider()

        async def noop(name, args):
            return "ok"

        items = [(_runner(prov, noop, label=f"a{i}"), "sys", f"task {i}")
                 for i in range(8)]
        results = await run_swarm(items, max_parallel=3)
        assert len(results) == 8 and all(r.ok for r in results)
        assert prov.max_active <= 3            # the semaphore actually bounds it
        assert prov.max_active > 1             # …and it did run concurrently


class TestCancellation:
    async def test_cancel_propagates_to_children(self):
        cancelled = {"n": 0}

        class HangProvider:
            name = "h"
            async def get_completion(self, m, t):
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    cancelled["n"] += 1
                    raise

        items = [(_runner(HangProvider(), None, label=f"a{i}"), "sys", "t")
                 for i in range(3)]
        task = asyncio.create_task(run_swarm(items, max_parallel=3))
        await asyncio.sleep(0.05)              # let them all start
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Every in-flight sub-agent was cancelled — none left dangling.
        assert cancelled["n"] == 3


class TestToolErrorRobustness:
    async def test_tool_raising_is_caught_and_loop_continues(self):
        class Prov:
            name = "p"
            def __init__(self):
                self.calls = 0
            async def get_completion(self, messages, tools):
                self.calls += 1
                if self.calls == 1:
                    return ("try", [_call("boom_tool", {})], {})
                # Second round: the tool error came back; finish gracefully.
                assert any(m.get("role") == "tool" for m in messages)
                return ("recovered. TASK COMPLETE", [], {})

        async def raising_tool(name, args):
            raise ValueError("kaboom")

        prov = Prov()
        res = await _runner(prov, raising_tool, label="x").run("sys", "t")
        assert res.ok is True
        assert prov.calls == 2                 # it saw the error and continued

    async def test_unparseable_tool_args_do_not_crash(self):
        seen: List[Dict[str, Any]] = []

        class Prov:
            name = "p"
            def __init__(self):
                self.calls = 0
            async def get_completion(self, messages, tools):
                self.calls += 1
                if self.calls == 1:
                    # Malformed JSON arguments → runner coerces to {}.
                    return ("x", [{"id": "c1", "function": {
                        "name": "t", "arguments": "{not valid json"}}], {})
                return ("done. TASK COMPLETE", [], {})

        async def tool(name, args):
            seen.append(args)
            return "ok"

        res = await _runner(Prov(), tool, label="x").run("sys", "t")
        assert res.ok is True
        assert seen == [{}]                    # bad args became an empty dict
