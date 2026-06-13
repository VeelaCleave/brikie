"""Phase 5 — durable / resumable swarms.

A swarm can't survive a process restart, and a crash mid-dispatch used to
leave a run 'running' forever and leak git worktrees. These verify that:
- completed work is persisted incrementally and idempotently,
- a restart flags orphaned runs honestly (status done + orphaned),
- leftover swarm worktrees are pruned.
"""

from __future__ import annotations

import subprocess


from brikie.bricks.tool.swarm import workspace as ws_mod
from brikie.bricks.tool.swarm.swarm_store import SwarmStore
from brikie.kernel.subagent import SubAgentResult


def _result(role="coder", ok=True, report="did it"):
    return SubAgentResult(role=role, task="t", ok=ok, report=report,
                          steps=1, tool_calls=0)


class TestOrphanReconcile:
    async def test_running_run_is_orphaned_on_restart(self, tmp_path):
        db = str(tmp_path / "swarm.db")
        s1 = SwarmStore(db)
        await s1.initialize()
        run_id = await s1.start_run("a goal", 2)   # started, never finished…
        await s1.shutdown()

        # …process "restarts": a fresh store reconciles the orphan.
        s2 = SwarmStore(db)
        await s2.initialize()
        n = await s2.reconcile_orphans()
        assert n == 1
        runs = await s2.recent_runs()
        assert runs[0]["run_id"] == run_id
        assert runs[0]["orphaned"] is True
        assert runs[0]["status"] == "done"     # no longer dangling 'running'
        # A second reconcile is a no-op (nothing left running).
        assert await s2.reconcile_orphans() == 0
        await s2.shutdown()

    async def test_finished_run_not_orphaned(self, tmp_path):
        s = SwarmStore(str(tmp_path / "swarm.db"))
        await s.initialize()
        run_id = await s.start_run("g", 1)
        await s.finish_run(run_id, 1, "1/1 done")
        assert await s.reconcile_orphans() == 0
        assert (await s.recent_runs())[0]["orphaned"] is False
        await s.shutdown()


class TestIncrementalPersistence:
    async def test_record_task_is_idempotent(self, tmp_path):
        s = SwarmStore(str(tmp_path / "swarm.db"))
        await s.initialize()
        run_id = await s.start_run("g", 1)
        # Recorded once mid-wave, then again with enriched state at the end.
        await s.record_task(run_id, 0, _result(report="first"))
        await s.record_task(run_id, 0, _result(report="final, reviewed"))
        tasks = await s.run_tasks(run_id)
        assert len(tasks) == 1                       # replaced, not duplicated
        assert tasks[0]["report"] == "final, reviewed"
        await s.shutdown()


class TestWorktreePrune:
    async def test_prunes_only_swarm_worktrees(self, tmp_path):
        repo = tmp_path / "proj"
        repo.mkdir()
        for c in (["init", "-q"], ["config", "user.email", "t@t.t"],
                  ["config", "user.name", "t"]):
            subprocess.run(["git", *c], cwd=repo, check=True, capture_output=True)
        (repo / "f").write_text("x\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "i"], cwd=repo, check=True,
                       capture_output=True)

        # A stale swarm worktree (as a crash would leave) + an unrelated one.
        stale = await ws_mod.provision(repo, "coder#1")
        assert stale.isolated and stale.path.exists()
        other = tmp_path / "mywork"
        subprocess.run(["git", "worktree", "add", "--detach", str(other), "HEAD"],
                       cwd=repo, check=True, capture_output=True)

        removed = await ws_mod.prune_swarm_worktrees(repo)
        assert removed == 1                          # only the swarm one
        assert not stale.path.exists()
        assert other.exists()                        # unrelated worktree untouched

    async def test_prune_non_git_is_noop(self, tmp_path):
        assert await ws_mod.prune_swarm_worktrees(tmp_path) == 0
