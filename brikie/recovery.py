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
from pathlib import Path

LAST_GOOD_FILE = Path.home() / ".brikie" / "last-good-set"

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
