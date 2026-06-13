"""Phase 2 — isolated coder workspaces (git worktrees).

Proves the correctness property that motivated this phase: two coders editing
the same path do NOT clobber each other, because each works in its own
worktree; their patches are reconciled back into the real tree, with an
overlapping change surfaced as a conflict rather than silently merged.

Uses a real throwaway git repo (git is required to run brikie anyway).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from brikie.bricks.tool.file_tools import ShellToolBrick
from brikie.bricks.tool.swarm import workspace as ws_mod
from brikie.bricks.tool.swarm.swarm_brick import SwarmToolBrick
from brikie.kernel.registry import ProviderBrick, ToolBrick


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """A minimal git repo with one committed file."""
    root = tmp_path / "proj"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@t.t"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("hello\n")
    _git(["add", "-A"], root)
    _git(["commit", "-qm", "init"], root)
    return root


class TestWorkspaceModule:
    async def test_provision_isolated_and_cleanup(self, repo):
        ws = await ws_mod.provision(repo, "coder#1")
        assert ws.isolated is True
        assert ws.path != repo and ws.path.exists()
        assert (ws.path / "README.md").read_text() == "hello\n"   # full checkout
        await ws.cleanup()
        assert not ws.path.exists()

    async def test_non_git_is_not_isolated(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        ws = await ws_mod.provision(plain, "coder#1")
        assert ws.isolated is False
        assert ws.path == plain.resolve()
        await ws.cleanup()           # no-op, must not raise

    async def test_diff_captures_changes_and_apply_lands_them(self, repo):
        ws = await ws_mod.provision(repo, "coder#1")
        (ws.path / "new.py").write_text("print('hi')\n")
        diff = await ws.diff()
        assert "new.py" in diff and "print('hi')" in diff
        # The real tree doesn't have it yet (isolation)…
        assert not (repo / "new.py").exists()
        ok, detail = await ws.apply_to(repo)
        assert ok is True
        assert (repo / "new.py").read_text() == "print('hi')\n"   # …until applied
        await ws.cleanup()


class TestConcurrencyCorrectness:
    async def test_two_coders_same_path_do_not_clobber(self, repo):
        # Each coder edits README.md in its OWN worktree, via a root-scoped
        # ShellToolBrick — the exact wiring the swarm gives a coder.
        ws1 = await ws_mod.provision(repo, "coder#1")
        ws2 = await ws_mod.provision(repo, "coder#2")
        t1 = ShellToolBrick(root=str(ws1.path), allowed_dirs=[str(ws1.path)])
        t2 = ShellToolBrick(root=str(ws2.path), allowed_dirs=[str(ws2.path)])
        await t1.execute("write_file", {"filePath": "README.md", "content": "FROM-ONE\n"})
        await t2.execute("write_file", {"filePath": "README.md", "content": "FROM-TWO\n"})

        # Physically isolated: neither overwrote the other or the real tree.
        assert (ws1.path / "README.md").read_text() == "FROM-ONE\n"
        assert (ws2.path / "README.md").read_text() == "FROM-TWO\n"
        assert (repo / "README.md").read_text() == "hello\n"

        d1, d2 = await ws1.diff(), await ws2.diff()
        assert "FROM-ONE" in d1 and "FROM-TWO" in d2

        # Reconcile: first applies; the second OVERLAPS and is a conflict,
        # NOT a silent clobber.
        ok1, _ = await ws1.apply_to(repo)
        ok2, detail2 = await ws2.apply_to(repo)
        assert ok1 is True
        assert ok2 is False and detail2          # surfaced, with a reason
        assert (repo / "README.md").read_text() == "FROM-ONE\n"   # winner stands
        await ws1.cleanup()
        await ws2.cleanup()


# ── Swarm-level integration ────────────────────────────────────────────────

class _WriteProvider:
    """A coder that writes one file then reports done (state-based, so it's
    safe under concurrency)."""

    name = "writer"

    def __init__(self, filename: str, content: str) -> None:
        self.filename = filename
        self.content = content

    async def get_completion(self, messages, tools):
        if any(m.get("role") == "tool" for m in messages):
            return ("Wrote it. TASK COMPLETE", [], {})
        call = {"id": "c1", "function": {
            "name": "write_file",
            "arguments": json.dumps({"filePath": self.filename, "content": self.content}),
        }}
        return ("writing", [call], {})


class _Reg:
    def __init__(self, provider, tools):
        self._providers = [provider]
        self._tools = tools
        self._bricks = {}

    def get_all(self, cls):
        if cls is ProviderBrick:
            return self._providers
        if cls is ToolBrick:
            return self._tools
        return []


class TestSwarmIntegration:
    async def test_coder_changes_isolated_then_applied(self, repo, tmp_path):
        reg = _Reg(_WriteProvider("feature.py", "VALUE = 1\n"), [])
        brick = SwarmToolBrick(registry=reg, db_path=str(tmp_path / "s.db"),
                               isolate_coders=True, workspace_root=str(repo),
                               max_steps=3)
        reg._tools.append(brick)
        await brick.init()
        out = await brick.execute("swarm_dispatch", {
            "tasks": [{"role": "coder", "task": "add feature.py"}],
            "review": False,
        })
        res = out["results"][0]
        assert res["isolated"] is True
        assert res["workspace_applied"] is True
        # The change actually landed in the real tree, and no worktree leaked.
        assert (repo / "feature.py").read_text() == "VALUE = 1\n"
        assert "feature.py" not in _git_worktrees(repo)
        await brick.shutdown()

    async def test_overlapping_coders_one_applies_one_conflicts(self, repo, tmp_path):
        reg = _Reg(_WriteProvider("shared.py", "X = 1\n"), [])
        brick = SwarmToolBrick(registry=reg, db_path=str(tmp_path / "s.db"),
                               isolate_coders=True, workspace_root=str(repo),
                               max_steps=3)
        reg._tools.append(brick)
        await brick.init()
        out = await brick.execute("swarm_dispatch", {
            "tasks": [
                {"role": "coder", "task": "create shared.py"},
                {"role": "coder", "task": "create shared.py too"},
            ],
            "review": False,
        })
        applied = [r["workspace_applied"] for r in out["results"]]
        conflicts = [bool(r.get("workspace_conflict")) for r in out["results"]]
        assert sum(applied) == 1            # exactly one landed
        assert sum(conflicts) == 1          # the other was a surfaced conflict
        assert (repo / "shared.py").exists()
        await brick.shutdown()


def _git_worktrees(repo: Path) -> str:
    return subprocess.run(["git", "worktree", "list"], cwd=str(repo),
                          capture_output=True, text=True).stdout
