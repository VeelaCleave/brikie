"""Tests for the GoalBrick — the persistent long-running goal system.

Exercises the real SQLite store (a temp goals.db), the full lifecycle,
and cross-session resume (a second brick instance over the same file
sees the same goal).
"""

from __future__ import annotations

import pytest

from brikie.bricks.tool.goals.goal_brick import GoalBrick


@pytest.fixture
async def brick(tmp_path):
    b = GoalBrick(db_path=str(tmp_path / "goals.db"))
    await b.init()
    yield b
    await b.shutdown()


class TestGoalLifecycle:
    async def test_set_and_status(self, brick):
        res = await brick.execute("goal_set", {
            "title": "Ship the goal system", "detail": "with tests"})
        assert res["status"] == "active"
        status = await brick.execute("goal_status", {})
        assert status["title"] == "Ship the goal system"
        assert status["detail"] == "with tests"
        assert status["progress"] == "0/0 subtasks done"

    async def test_subtask_flow_tracks_progress(self, brick):
        await brick.execute("goal_set", {"title": "Build it"})
        a = await brick.execute("goal_add_subtask", {"title": "design"})
        await brick.execute("goal_add_subtask", {"title": "implement"})
        status = await brick.execute("goal_status", {})
        assert status["progress"] == "0/2 subtasks done"
        assert [s["title"] for s in status["subtasks"]] == ["design", "implement"]

        done = await brick.execute("goal_complete_subtask", {
            "subtask_id": a["subtask_id"], "note": "done well"})
        assert done["progress"] == "1/2 subtasks done"

    async def test_active_goal_is_default_target(self, brick):
        await brick.execute("goal_set", {"title": "first"})
        await brick.execute("goal_set", {"title": "second"})
        # subtask with no goal_id attaches to the most recent active goal
        await brick.execute("goal_add_subtask", {"title": "t"})
        status = await brick.execute("goal_status", {})
        assert status["title"] == "second"
        assert len(status["subtasks"]) == 1

    async def test_close_goal(self, brick):
        await brick.execute("goal_set", {"title": "temp"})
        res = await brick.execute("goal_close", {"outcome": "done"})
        assert res["status"] == "done"
        # closed goal is no longer the active default
        status = await brick.execute("goal_status", {})
        assert status["active_goal"] is None

    async def test_goal_list_filters_by_status(self, brick):
        await brick.execute("goal_set", {"title": "a"})
        await brick.execute("goal_set", {"title": "b"})
        await brick.execute("goal_close", {"outcome": "abandoned"})  # closes 'b'
        active = await brick.execute("goal_list", {"status": "active"})
        assert [g["title"] for g in active] == ["a"]


class TestResumeAcrossSessions:
    async def test_second_instance_sees_persisted_goal(self, tmp_path):
        db = str(tmp_path / "goals.db")
        b1 = GoalBrick(db_path=db)
        await b1.init()
        await b1.execute("goal_set", {"title": "long haul", "detail": "days"})
        await b1.execute("goal_add_subtask", {"title": "phase 1"})
        await b1.shutdown()

        # a fresh "session" over the same file resumes the goal
        b2 = GoalBrick(db_path=db)
        await b2.init()
        try:
            status = await b2.execute("goal_status", {})
            assert status["title"] == "long haul"
            assert status["subtasks"][0]["title"] == "phase 1"
            # the progress log carried over
            kinds = [e["kind"] for e in status["recent"]]
            assert "created" in kinds and "subtask_added" in kinds
        finally:
            await b2.shutdown()


class TestValidation:
    async def test_set_requires_title(self, brick):
        with pytest.raises(ValueError, match="title"):
            await brick.execute("goal_set", {})

    async def test_subtask_without_goal_errors_cleanly(self, brick):
        res = await brick.execute("goal_add_subtask", {"title": "orphan"})
        assert "error" in res

    async def test_complete_unknown_subtask(self, brick):
        await brick.execute("goal_set", {"title": "g"})
        res = await brick.execute("goal_complete_subtask", {"subtask_id": "nope"})
        assert "error" in res

    async def test_close_bad_outcome(self, brick):
        await brick.execute("goal_set", {"title": "g"})
        with pytest.raises(ValueError, match="outcome"):
            await brick.execute("goal_close", {"outcome": "maybe"})

    async def test_unknown_tool_raises_keyerror(self, brick):
        with pytest.raises(KeyError):
            await brick.execute("goal_teleport", {})
