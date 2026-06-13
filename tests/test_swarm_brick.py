"""Tests for SwarmToolBrick (BRK-470) — role-based parallel delegation,
recursion guard, security wiring, and the observability audit store.
"""

from __future__ import annotations

from typing import Any, Dict, List

from brikie.bricks.tool.swarm.swarm_brick import _ROLE_PROMPTS, SwarmToolBrick
from brikie.kernel.registry import ProviderBrick, ToolBrick


class FinishingProvider:
    """Always returns a clean final answer — sub-agents complete at once."""

    def __init__(self, reply: str = "did the work. TASK COMPLETE") -> None:
        self.name = "finishing"
        self.reply = reply
        self.calls = 0

    async def get_completion(self, messages, tools):
        self.calls += 1
        return (self.reply, [], {})


class FakeToolBrick:
    def __init__(self, name: str, tool_names: List[str]) -> None:
        self._name = name
        self.tools = [
            {"type": "function", "function": {"name": n, "parameters": {}}}
            for n in tool_names
        ]
        self.executed: List[tuple] = []

    @property
    def name(self) -> str:
        return self._name

    async def execute(self, name: str, args: Dict[str, Any]) -> Any:
        self.executed.append((name, args))
        return f"{name} ran"


class GoalCarrier:
    """A brick exposing the duck-typed active_goal_context() the kernel and
    swarm anchor to (without importing GoalBrick)."""

    name = "goals"

    def __init__(self, goal: str) -> None:
        self._goal = goal

    async def active_goal_context(self) -> str:
        return self._goal


class FakeRegistry:
    def __init__(self, providers: List[Any], tools: List[Any], extra: List[Any] = None) -> None:
        self._providers = providers
        self._tools = tools
        self._bricks = {}
        for b in providers + tools + (extra or []):
            self._bricks[getattr(b, "name", str(id(b)))] = b

    def get_all(self, cls):
        if cls is ProviderBrick:
            return self._providers
        if cls is ToolBrick:
            return self._tools
        return []


async def _make_brick(tmp_path, provider=None, tools=None, extra=None):
    provider = provider or FinishingProvider()
    tools = tools if tools is not None else [FakeToolBrick("files", ["read_file"])]
    reg = FakeRegistry([provider], tools, extra=extra)
    brick = SwarmToolBrick(registry=reg, hooks=None,
                           db_path=str(tmp_path / "swarm.db"))
    # register self in the fake registry's tool list (mirrors real boot)
    reg._tools.append(brick)
    reg._bricks["swarm"] = brick
    await brick.init()
    return brick, reg, provider


class TestRoles:
    async def test_swarm_roles_lists_all(self, tmp_path):
        brick, *_ = await _make_brick(tmp_path)
        out = brick._swarm_roles()
        assert set(out["roles"]) == set(_ROLE_PROMPTS)
        await brick.shutdown()

    async def test_unknown_role_falls_back_to_generalist(self, tmp_path):
        brick, *_ = await _make_brick(tmp_path)
        tasks = brick._normalize_tasks([{"role": "wizard", "task": "x"}])
        assert tasks == [("generalist", "x")]
        await brick.shutdown()

    async def test_bare_string_task_allowed(self, tmp_path):
        brick, *_ = await _make_brick(tmp_path)
        assert brick._normalize_tasks(["just do this"]) == [("generalist", "just do this")]
        await brick.shutdown()


class TestRecursionGuard:
    async def test_subagents_cannot_see_swarm_tools(self, tmp_path):
        brick, reg, _ = await _make_brick(
            tmp_path, tools=[FakeToolBrick("files", ["read_file", "write_file"])]
        )
        schemas = brick._subagent_tool_schemas()
        names = {s["function"]["name"] for s in schemas}
        assert "read_file" in names and "write_file" in names
        # The swarm's own tools must be excluded → no recursive fan-out.
        assert "swarm_dispatch" not in names
        assert "swarm_status" not in names
        await brick.shutdown()

    async def test_executor_excludes_self_and_routes(self, tmp_path):
        files = FakeToolBrick("files", ["read_file"])
        brick, *_ = await _make_brick(tmp_path, tools=[files])
        ex = brick._make_tool_executor()
        out = await ex("read_file", {"path": "/x"})
        assert out == "read_file ran"
        assert files.executed == [("read_file", {"path": "/x"})]
        # A swarm tool name is never routable from inside a sub-agent.
        missing = await ex("swarm_dispatch", {"tasks": []})
        assert "No tool brick" in missing
        await brick.shutdown()


class TestDispatch:
    async def test_validation_empty_tasks(self, tmp_path):
        brick, *_ = await _make_brick(tmp_path)
        out = await brick.execute("swarm_dispatch", {"tasks": []})
        assert "error" in out
        await brick.shutdown()

    async def test_validation_too_many_tasks(self, tmp_path):
        brick, *_ = await _make_brick(tmp_path)
        brick._max_tasks = 2
        out = await brick.execute("swarm_dispatch",
                                  {"tasks": [{"task": str(i)} for i in range(5)]})
        assert "error" in out and "fan-out cap" in out["error"]
        await brick.shutdown()

    async def test_no_provider_errors(self, tmp_path):
        reg = FakeRegistry([], [])
        brick = SwarmToolBrick(registry=reg, db_path=str(tmp_path / "s.db"))
        await brick.init()
        out = await brick.execute("swarm_dispatch", {"tasks": [{"task": "x"}]})
        assert "error" in out and "no provider" in out["error"]
        await brick.shutdown()

    async def test_dispatch_runs_and_persists(self, tmp_path):
        prov = FinishingProvider("done. TASK COMPLETE")
        brick, reg, _ = await _make_brick(tmp_path, provider=prov)
        out = await brick.execute("swarm_dispatch", {
            "tasks": [
                {"role": "researcher", "task": "investigate A"},
                {"role": "coder", "task": "build B"},
            ],
        })
        assert "run_id" in out
        assert len(out["results"]) == 2
        assert all(r["ok"] for r in out["results"])
        assert out["results"][0]["role"] == "researcher"
        assert out["results"][1]["role"] == "coder"
        assert "2/2" in out["summary"]
        # The provider was actually called once per sub-agent.
        assert prov.calls == 2

        # Audit log records the run.
        status = await brick.execute("swarm_status", {})
        assert status["recent_runs"][0]["task_count"] == 2
        assert status["recent_runs"][0]["ok_count"] == 2
        assert status["recent_runs"][0]["status"] == "done"
        await brick.shutdown()

    async def test_active_goal_is_briefed_to_subagents(self, tmp_path):
        # The goal must reach the sub-agent's task (collaboration anchor).
        captured: List[str] = []

        class CapturingProvider:
            name = "cap"
            async def get_completion(self, messages, tools):
                captured.append(messages[1]["content"])  # the user/task msg
                return ("ok. TASK COMPLETE", [], {})

        goal = GoalCarrier("Ship the Swarm tier: parallel sub-agents")
        brick, *_ = await _make_brick(
            tmp_path, provider=CapturingProvider(), extra=[goal]
        )
        await brick.execute("swarm_dispatch", {"tasks": [{"task": "do a thing"}]})
        assert captured
        assert "Ship the Swarm tier" in captured[0]
        assert "do a thing" in captured[0]
        await brick.shutdown()
