"""GoalBrick (BRK-460) — long-running, persistent goals for the agent.

The spine of autonomous, multi-session work: the agent sets a high-level
goal, breaks it into linked subtasks, and works through them over hours
or days. Everything persists to SQLite, so a paused, crashed, or
resumed session picks up exactly where it left off — no heartbeat
required.

Tools:
    goal_set              start (or restate) the goal being worked on
    goal_status           the active goal, its subtasks, and recent log
    goal_add_subtask      append a subtask to the goal
    goal_complete_subtask mark a subtask done (with an optional note)
    goal_list             list goals, optionally by status
    goal_close            finish a goal (done) or drop it (abandoned)

When a tool omits ``goal_id``, it targets the most recently updated
active goal — so the agent rarely needs to track ids by hand.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from brikie.bricks.tool.base import ToolBrick
from brikie.bricks.tool.goals.goal_store import GoalStore

logger = logging.getLogger(__name__)

_VALID_SUBTASK_STATUS = {"pending", "active", "done", "blocked"}


class GoalBrick(ToolBrick):
    BRICK_NUMBER = "BRK-460"
    """Tool Brick exposing the persistent goal system.

    Args:
        db_path: SQLite file for goals (default ``goals.db`` in the cwd).
    """

    tools: List[Dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": "goal_set",
                "description": "Set the high-level goal you're working toward. Persists across sessions; call goal_status later to resume it. Use this when the user gives you a substantial, multi-step objective.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Short goal statement."},
                        "detail": {"type": "string", "description": "Fuller description, constraints, definition of done."},
                    },
                    "required": ["title"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "goal_status",
                "description": "Get the active goal, its subtasks, and recent progress — call this at the start of a session to resume, or any time to re-anchor on what you're doing.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "goal_id": {"type": "string", "description": "Specific goal id; defaults to the active goal."},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "goal_add_subtask",
                "description": "Break the goal down by appending a concrete subtask.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "What the subtask accomplishes."},
                        "goal_id": {"type": "string", "description": "Defaults to the active goal."},
                    },
                    "required": ["title"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "goal_complete_subtask",
                "description": "Mark a subtask done once you've actually finished and verified it.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "subtask_id": {"type": "string", "description": "The subtask id (from goal_status)."},
                        "note": {"type": "string", "description": "Optional note on the outcome."},
                    },
                    "required": ["subtask_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "goal_list",
                "description": "List goals, optionally filtered by status (active, paused, done, abandoned).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "enum": ["active", "paused", "done", "abandoned"]},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "goal_close",
                "description": "Close a goal — 'done' when achieved, 'abandoned' when dropping it.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "outcome": {"type": "string", "enum": ["done", "abandoned"]},
                        "goal_id": {"type": "string", "description": "Defaults to the active goal."},
                    },
                    "required": ["outcome"],
                },
            },
        },
    ]

    def __init__(self, db_path: str = "goals.db") -> None:
        super().__init__()
        self._name = "goals"
        self._store = GoalStore(db_path)

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
        """Dispatch a goal tool.

        Raises:
            KeyError: Unknown tool name (lets another brick claim it).
        """
        if name == "goal_set":
            return await self._goal_set(args)
        elif name == "goal_status":
            return await self._goal_status(args)
        elif name == "goal_add_subtask":
            return await self._goal_add_subtask(args)
        elif name == "goal_complete_subtask":
            return await self._goal_complete_subtask(args)
        elif name == "goal_list":
            return await self._goal_list(args)
        elif name == "goal_close":
            return await self._goal_close(args)
        raise KeyError(f"Unknown tool: {name}")

    # ------------------------------------------------------------------
    # Tool handlers
    # ------------------------------------------------------------------

    async def _goal_set(self, args: Dict[str, Any]) -> Dict[str, Any]:
        title = str(args.get("title", "")).strip()
        if not title:
            raise ValueError("goal_set: 'title' is required")
        goal_id = await self._store.create_goal(title, str(args.get("detail", "")))
        return {"goal_id": goal_id, "title": title, "status": "active",
                "message": "goal set — break it into subtasks with goal_add_subtask"}

    async def _goal_status(self, args: Dict[str, Any]) -> Dict[str, Any]:
        goal = await self._resolve_goal(args.get("goal_id"))
        if goal is None:
            return {"active_goal": None,
                    "message": "no active goal — set one with goal_set"}
        subtasks = await self._store.list_subtasks(goal["id"])
        events = await self._store.recent_events(goal["id"], limit=10)
        done = sum(1 for s in subtasks if s["status"] == "done")
        return {
            "goal_id": goal["id"],
            "title": goal["title"],
            "detail": goal["detail"],
            "status": goal["status"],
            "progress": f"{done}/{len(subtasks)} subtasks done",
            "subtasks": subtasks,
            "recent": events,
        }

    async def _goal_add_subtask(self, args: Dict[str, Any]) -> Dict[str, Any]:
        title = str(args.get("title", "")).strip()
        if not title:
            raise ValueError("goal_add_subtask: 'title' is required")
        goal = await self._resolve_goal(args.get("goal_id"))
        if goal is None:
            return {"error": "no active goal — set one with goal_set first"}
        subtask_id = await self._store.add_subtask(goal["id"], title)
        return {"subtask_id": subtask_id, "goal_id": goal["id"], "title": title,
                "status": "pending"}

    async def _goal_complete_subtask(self, args: Dict[str, Any]) -> Dict[str, Any]:
        subtask_id = str(args.get("subtask_id", "")).strip()
        if not subtask_id:
            raise ValueError("goal_complete_subtask: 'subtask_id' is required")
        goal_id = await self._store.set_subtask_status(
            subtask_id, "done", str(args.get("note", ""))
        )
        if goal_id is None:
            return {"error": f"no subtask with id '{subtask_id}'"}
        subtasks = await self._store.list_subtasks(goal_id)
        done = sum(1 for s in subtasks if s["status"] == "done")
        return {"subtask_id": subtask_id, "status": "done",
                "progress": f"{done}/{len(subtasks)} subtasks done"}

    async def _goal_list(self, args: Dict[str, Any]) -> List[Dict[str, Any]]:
        status = args.get("status")
        goals = await self._store.list_goals(status)
        return [
            {"goal_id": g["id"], "title": g["title"], "status": g["status"],
             "updated_at": g["updated_at"]}
            for g in goals
        ]

    async def _goal_close(self, args: Dict[str, Any]) -> Dict[str, Any]:
        outcome = str(args.get("outcome", "")).strip()
        if outcome not in ("done", "abandoned"):
            raise ValueError("goal_close: 'outcome' must be 'done' or 'abandoned'")
        goal = await self._resolve_goal(args.get("goal_id"))
        if goal is None:
            return {"error": "no active goal to close"}
        await self._store.set_status(goal["id"], outcome)
        return {"goal_id": goal["id"], "title": goal["title"], "status": outcome}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _resolve_goal(self, goal_id: Optional[str]) -> Optional[dict]:
        """The named goal, or the most recent active goal when omitted."""
        if goal_id:
            return await self._store.get_goal(str(goal_id))
        return await self._store.latest_active_goal()
