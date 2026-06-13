"""Boot recovery — never let one bad brick wedge the system.

As agents author and seat their own bricks, a broken one must not be a
dead end. Three layers, from softest to hardest:

1. **Quarantine** (in the loader + warm-up): a brick that fails to load
   or initialize is dropped and logged; the rest of the stack boots.
2. **Last-known-good**: every Build Set that boots with a viable minimum
   stack is recorded here. If the requested set can't reach a minimum
   stack, brikie falls back to the last one that did.
3. **Honest failure**: if there is no good fallback, brikie says exactly
   what's wrong and how to fix it (``brikie config``) instead of a
   traceback.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

LAST_GOOD_FILE = Path.home() / ".brikie" / "last-good-set"
BREAKDOWN_DIR = Path.home() / ".brikie" / "breakdowns"

logger = logging.getLogger(__name__)


def record_good_set(set_path: str) -> None:
    """Remember *set_path* as a Build Set that booted cleanly."""
    try:
        LAST_GOOD_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_GOOD_FILE.write_text(str(Path(set_path).resolve()))
    except Exception:
        logger.debug("Could not record last-known-good set", exc_info=True)


def last_good_set() -> str | None:
    """The most recent Build Set that booted, if one is on record."""
    try:
        if LAST_GOOD_FILE.is_file():
            path = LAST_GOOD_FILE.read_text().strip()
            if path and Path(path).is_file() and Path(path).resolve() != _self():
                return path
    except Exception:
        logger.debug("Could not read last-known-good set", exc_info=True)
    return None


def _self() -> Path:  # pragma: no cover - guard helper
    return LAST_GOOD_FILE.resolve()


def summarize_quarantine(quarantined: list) -> str:
    """A one-line, user-facing summary of skipped bricks."""
    if not quarantined:
        return ""
    items = ", ".join(f"{name} ({err})" for name, err in quarantined)
    return f"Skipped {len(quarantined)} brick(s) that failed to load: {items}"


def write_context_dump(ctx: Dict[str, Any]) -> Optional[Path]:
    """Write a resumable breakdown report and return its path (or None).

    When a turn crashes with an unexpected error, the agent's state isn't
    lost in a traceback — this writes a human- and supervisor-readable
    markdown snapshot (the error, the active goal, recent conversation,
    loaded bricks, how to resume) to ``~/.brikie/breakdowns/`` and refreshes
    a ``latest.md`` pointer. Best-effort: failure to write is swallowed —
    a broken dump must never compound the breakdown it's reporting.
    """
    try:
        BREAKDOWN_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = BREAKDOWN_DIR / f"breakdown-{ts}.md"
        body = _render_dump(ctx, ts)
        path.write_text(body)
        (BREAKDOWN_DIR / "latest.md").write_text(body)
        return path
    except Exception:
        logger.debug("Could not write context dump", exc_info=True)
        return None


def _render_dump(ctx: Dict[str, Any], ts: str) -> str:
    """Render the breakdown context dict into a markdown report."""
    def section(title: str, body: str) -> str:
        return f"## {title}\n{body}\n"

    goal = ctx.get("active_goal") or "_(no active goal set)_"
    quarantined: List = ctx.get("quarantined") or []
    quarantine_line = (
        ", ".join(f"{n} ({e})" for n, e in quarantined)
        if quarantined else "none"
    )
    messages: List[str] = ctx.get("recent_messages") or []
    convo = "\n".join(messages) if messages else "_(no conversation captured)_"

    parts = [
        f"# Brikie breakdown report — {ts}",
        "",
        "Brikie hit an unexpected error during a turn. Conversation state is "
        "preserved by the memory bricks; resume with `brikie --continue`.",
        "",
        section(
            "What broke",
            f"- **{ctx.get('error_type', 'Error')}**: "
            f"{ctx.get('error_message', '(no message)')}\n"
            f"- Consecutive breakdowns: {ctx.get('consecutive', 1)}",
        ),
        section("Active goal", goal),
        section(
            "Session",
            f"- session_id: `{ctx.get('session_id', 'default')}`\n"
            f"- model: `{ctx.get('model', '—')}`\n"
            f"- bricks: {', '.join(ctx.get('bricks', [])) or 'none'}\n"
            f"- quarantined at boot: {quarantine_line}\n"
            f"- tokens: in={ctx.get('tokens_in', 0)} "
            f"out={ctx.get('tokens_out', 0)}; "
            f"history={ctx.get('history_len', 0)} messages",
        ),
        section(f"Recent conversation (last {len(messages)})", convo),
        section(
            "Traceback",
            f"```\n{ctx.get('traceback', '(none)').rstrip()}\n```",
        ),
        section(
            "How to resume",
            "1. Read the error above — if a brick is implicated, fix or "
            "remove it.\n"
            "2. `brikie --continue` reloads this conversation from memory.\n"
            "3. The active goal (if any) is still tracked in the GoalBrick "
            "store and surfaces on resume.",
        ),
    ]
    return "\n".join(parts).rstrip() + "\n"


def write_minimal_set(path: Path, provider_config: dict | None = None) -> Path:
    """Write a bare CLI + provider safe-mode Build Set to *path*.

    Used as the floor when there is no last-known-good and the requested
    set can't reach a minimum stack. The provider config is reused from a
    known source when available, else left as a placeholder the user
    completes with ``brikie config``.
    """
    bricks: list = [{"brk": "BRK-300"}]
    bricks.append({"brk": "BRK-200", "config": provider_config or {
        "model": "", "base_url": "http://localhost:11434/v1",
        "api_format": "openai", "api_key": "not-needed",
    }})
    path.write_text(json.dumps({
        "name": "safe-mode",
        "description": "Minimal recovery stack (CLI + provider only).",
        "bricks": bricks,
    }, indent=2) + "\n")
    return path
