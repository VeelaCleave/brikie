"""Chat-platform config helpers shared by onboarding and ``brikie config``.

Deliberately small: persist a bot token to ``~/.brikie/.env`` (which
brikie auto-loads at startup) and add the matching interface brick to a
Build Set. The setup UX lives in ``onboard.py`` — the Hermes/OpenClaw
model, where you simply paste a token during onboarding and the bot
adopts the first person who messages it as its owner.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

ENV_FILE = Path.home() / ".brikie" / ".env"

# platform key -> setup metadata
CHAT_PLATFORMS = {
    "telegram": {
        "label": "Telegram",
        "brk": "BRK-320",
        "token_env": "TELEGRAM_BOT_TOKEN",
        "make_bot": "make a bot with @BotFather (/newbot) and paste its token",
        "post_step": None,
    },
    "discord": {
        "label": "Discord",
        "brk": "BRK-330",
        "token_env": "DISCORD_BOT_TOKEN",
        "make_bot": (
            "make an app at https://discord.com/developers, add a Bot, "
            "and paste its token"
        ),
        "post_step": (
            "enable Bot → Privileged Gateway Intents → Message Content "
            "Intent in the Developer Portal, and invite the bot to a server"
        ),
    },
}

logger = logging.getLogger(__name__)


def load_env_file(path: Path = ENV_FILE) -> None:
    """Load ``KEY=value`` lines from *path* into ``os.environ``.

    An existing environment variable wins, so an explicit export always
    overrides the stored value. A missing file is a no-op.
    """
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def save_env_var(key: str, value: str, path: Path = ENV_FILE) -> None:
    """Upsert ``key=value`` in *path* (created 600 if absent)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    found = False
    if path.is_file():
        for raw in path.read_text().splitlines():
            if raw.strip().startswith(f"{key}="):
                lines.append(f"{key}={value}")
                found = True
            else:
                lines.append(raw)
    if not found:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)


def add_interface_to_set(sets_dir: Path, set_name: str, brk: str) -> Path:
    """Add an interface brick to ``{set_name}.json`` (idempotent).

    No allowlist is written — the brick adopts the first person who
    messages it as its owner (see the interface bricks' claim mode).
    """
    path = sets_dir / f"{set_name}.json"
    if path.is_file():
        data = json.loads(path.read_text())
    else:
        data = {"name": set_name, "description": "", "bricks": []}

    bricks = data.setdefault("bricks", [])
    if not any(isinstance(b, dict) and b.get("brk") == brk for b in bricks):
        bricks.append({"brk": brk})
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path
