"""Per-coder isolated workspaces for the swarm (Phase 2).

Parallel ``coder`` sub-agents must not edit one shared working tree — two of
them touching the same file would clobber each other through the single
process cwd. Each coder instead gets its own **git worktree** checked out at
HEAD; its file tools are rooted there (see ``ShellToolBrick(root=…)``), so its
edits are physically isolated. When it finishes, its changes are captured as
a patch and applied back to the real tree **only if they don't conflict** —
an overlapping change is surfaced, never silently merged.

Honest boundaries:
- Isolation requires a git repo. Outside one, ``provision`` returns a
  non-isolated workspace (the shared tree) and says so — brikie does not
  pretend to isolate when it can't.
- Patches are applied sequentially; the first wins a contested hunk and the
  rest are reported as conflicts for the coordinator to reconcile. No
  three-way auto-merge (that would risk silent corruption).
"""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


async def _git(args: list, cwd: Path) -> Tuple[int, str, str]:
    """Run a git command; return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def is_git_repo(root: Path) -> bool:
    try:
        code, out, _ = await _git(["rev-parse", "--is-inside-work-tree"], root)
    except FileNotFoundError:        # git not installed
        return False
    return code == 0 and out.strip() == "true"


def _slug(label: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", label)[:24] or "agent"


class Workspace:
    """A coder's working directory — an isolated git worktree, or the shared
    tree when isolation isn't available."""

    def __init__(self, path: Path, repo_root: Optional[Path], isolated: bool) -> None:
        self.path = path
        self.repo_root = repo_root
        self.isolated = isolated

    async def diff(self) -> str:
        """The patch of everything changed in this worktree vs HEAD."""
        if not self.isolated or self.repo_root is None:
            return ""
        await _git(["add", "-A"], self.path)
        code, out, _ = await _git(["diff", "--cached"], self.path)
        return out if code == 0 else ""

    async def apply_to(self, target: Path) -> Tuple[bool, str]:
        """Apply this workspace's patch to *target*; (ok, detail).

        Returns (True, "no changes") for an empty diff, (True, "applied") on
        success, or (False, <git error>) when it conflicts — in which case
        *target* is left untouched (git apply is all-or-nothing).
        """
        patch = await self.diff()
        if not patch.strip():
            return True, "no changes"
        proc = await asyncio.create_subprocess_exec(
            "git", "apply", "--whitespace=nowarn", "-", cwd=str(target),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _out, err = await proc.communicate(patch.encode())
        if proc.returncode == 0:
            return True, "applied"
        return False, (err.decode(errors="replace").strip() or "patch did not apply")

    async def cleanup(self) -> None:
        """Remove the worktree (clean GC). No-op for a shared workspace."""
        if self.isolated and self.repo_root is not None:
            try:
                await _git(["worktree", "remove", "--force", str(self.path)],
                           self.repo_root)
            except Exception:
                logger.debug("worktree cleanup failed for %s", self.path,
                             exc_info=True)


async def prune_swarm_worktrees(repo_root: Path) -> int:
    """Remove leftover swarm worktrees from a crashed dispatch; return count.

    Safe to call at startup: no dispatch is in flight then, so any worktree
    whose path was created by this module (``brikie-swarm-*``) is stale.
    """
    repo_root = Path(repo_root).resolve()
    if not await is_git_repo(repo_root):
        return 0
    code, out, _ = await _git(["worktree", "list", "--porcelain"], repo_root)
    if code != 0:
        return 0
    removed = 0
    for line in out.splitlines():
        if not line.startswith("worktree "):
            continue
        path = line[len("worktree "):].strip()
        if "brikie-swarm-" in Path(path).name:
            rc, _o, _e = await _git(
                ["worktree", "remove", "--force", path], repo_root)
            if rc == 0:
                removed += 1
    if removed:
        await _git(["worktree", "prune"], repo_root)
        logger.info("Pruned %d stale swarm worktree(s).", removed)
    return removed


async def provision(repo_root: Path, label: str) -> Workspace:
    """Create an isolated worktree at HEAD, or a shared workspace if we can't."""
    repo_root = Path(repo_root).resolve()
    if not await is_git_repo(repo_root):
        logger.info("Swarm workspace: %s is not a git repo — coder runs "
                    "un-isolated in the shared tree.", repo_root)
        return Workspace(repo_root, None, isolated=False)

    tmp = Path(tempfile.mkdtemp(prefix=f"brikie-swarm-{_slug(label)}-"))
    tmp.rmdir()  # `git worktree add` wants to create the dir itself
    code, _out, err = await _git(
        ["worktree", "add", "--detach", str(tmp), "HEAD"], repo_root)
    if code != 0:
        logger.warning("Swarm workspace: `git worktree add` failed (%s) — "
                       "falling back to the shared tree.", err.strip())
        return Workspace(repo_root, None, isolated=False)
    return Workspace(tmp, repo_root, isolated=True)
