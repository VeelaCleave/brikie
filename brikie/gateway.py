"""Run brikie as a background chat gateway — no terminal to babysit.

Select a chat platform, give it a token, and brikie runs as a service:
on Linux a ``systemd --user`` unit that starts on login and restarts on
failure; everywhere else a detached ``nohup`` process with a PID file.
Either way you close the terminal and the bot stays online.

Public surface (also exposed as ``brikie gateway <action>``):
    install / start   set up + start the service
    stop              stop it
    status            is it running?
    logs              recent output
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SERVICE_NAME = "brikie-gateway"
BRIKIE_DIR = Path.home() / ".brikie"
UNIT_PATH = Path.home() / ".config" / "systemd" / "user" / f"{SERVICE_NAME}.service"
PID_FILE = BRIKIE_DIR / f"{SERVICE_NAME}.pid"
LOG_FILE = BRIKIE_DIR / f"{SERVICE_NAME}.log"


def has_systemd() -> bool:
    """True when a usable ``systemctl --user`` is present."""
    if not shutil.which("systemctl"):
        return False
    try:
        subprocess.run(
            ["systemctl", "--user", "show-environment"],
            capture_output=True, timeout=5, check=False,
        )
        return True
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────
# Headless gateway Build Set — the chat stack with NO terminal interface
# ──────────────────────────────────────────────────────────────────────


def make_gateway_set(sets_dir: Path, source_set: str, platform_brk: str) -> str:
    """Derive a headless gateway set from *source_set*.

    Copies the source set, drops the CLI interface (BRK-300 — a service
    has no terminal), and ensures the chat interface is present. Returns
    the gateway set's name.
    """
    src = sets_dir / f"{source_set}.json"
    data = json.loads(src.read_text()) if src.is_file() else {"bricks": []}

    bricks = [
        b for b in data.get("bricks", [])
        if (b.get("brk") if isinstance(b, dict) else b) != "BRK-300"
    ]
    if not any(
        isinstance(b, dict) and b.get("brk") == platform_brk for b in bricks
    ):
        bricks.append({"brk": platform_brk})

    name = f"{source_set}-gateway"
    out = {
        "name": name,
        "description": "Headless chat gateway (no terminal interface).",
        "bricks": bricks,
    }
    (sets_dir / f"{name}.json").write_text(json.dumps(out, indent=2) + "\n")
    return name


# ──────────────────────────────────────────────────────────────────────
# Service management
# ──────────────────────────────────────────────────────────────────────


def install_service(set_name: str) -> tuple[bool, str]:
    """Install and start the gateway service. Returns (ok, message)."""
    BRIKIE_DIR.mkdir(parents=True, exist_ok=True)
    if has_systemd():
        return _install_systemd(set_name)
    return _install_nohup(set_name)


def _install_systemd(set_name: str) -> tuple[bool, str]:
    UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    unit = f"""[Unit]
Description=brikie chat gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={BRIKIE_DIR}
EnvironmentFile=-{BRIKIE_DIR / ".env"}
ExecStart={sys.executable} -m brikie --set {set_name} --log info
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""
    UNIT_PATH.write_text(unit)
    # Survive logout/reboot when permitted (best-effort).
    import getpass
    try:
        user = getpass.getuser()
    except Exception:
        user = os.environ.get("USER", "")
    if user:
        subprocess.run(["loginctl", "enable-linger", user],
                       capture_output=True, check=False)
    subprocess.run(["systemctl", "--user", "daemon-reload"],
                   capture_output=True, check=False)
    result = subprocess.run(
        ["systemctl", "--user", "enable", "--now", SERVICE_NAME],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return False, result.stderr.strip() or "systemctl enable failed"
    return True, (
        "service started (systemd). Manage it with:\n"
        "  brikie gateway status   ·   brikie gateway logs   ·   "
        "brikie gateway stop"
    )


def _install_nohup(set_name: str) -> tuple[bool, str]:
    if _nohup_running():
        stop_service()
    env = dict(os.environ)
    env_file = BRIKIE_DIR / ".env"
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    with open(LOG_FILE, "ab") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "brikie", "--set", set_name],
            stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            cwd=str(BRIKIE_DIR), env=env, start_new_session=True,
        )
    PID_FILE.write_text(str(proc.pid))
    return True, (
        f"service started (pid {proc.pid}). Manage it with:\n"
        f"  brikie gateway status   ·   brikie gateway logs   ·   "
        f"brikie gateway stop"
    )


def stop_service() -> tuple[bool, str]:
    if has_systemd() and UNIT_PATH.is_file():
        subprocess.run(["systemctl", "--user", "disable", "--now", SERVICE_NAME],
                       capture_output=True, check=False)
        return True, "gateway stopped."
    return _stop_nohup()


def _stop_nohup() -> tuple[bool, str]:
    if not _nohup_running():
        return True, "gateway was not running."
    pid = int(PID_FILE.read_text().strip())
    try:
        os.kill(pid, 15)
    except ProcessLookupError:
        pass
    PID_FILE.unlink(missing_ok=True)
    return True, "gateway stopped."


def _nohup_running() -> bool:
    if not PID_FILE.is_file():
        return False
    try:
        os.kill(int(PID_FILE.read_text().strip()), 0)
        return True
    except (ProcessLookupError, ValueError):
        return False


def service_status() -> str:
    if has_systemd() and UNIT_PATH.is_file():
        result = subprocess.run(
            ["systemctl", "--user", "is-active", SERVICE_NAME],
            capture_output=True, text=True, check=False,
        )
        state = result.stdout.strip() or "unknown"
        return f"gateway (systemd): {state}"
    return f"gateway (nohup): {'running' if _nohup_running() else 'stopped'}"


def service_logs(lines: int = 40) -> str:
    if has_systemd() and UNIT_PATH.is_file():
        result = subprocess.run(
            ["journalctl", "--user", "-u", SERVICE_NAME, "-n", str(lines),
             "--no-pager"],
            capture_output=True, text=True, check=False,
        )
        return result.stdout or result.stderr or "(no logs yet)"
    if LOG_FILE.is_file():
        return "\n".join(LOG_FILE.read_text().splitlines()[-lines:])
    return "(no logs yet)"


def run_gateway_command(action: str) -> int:
    """``brikie gateway <action>`` dispatch. Returns an exit code."""
    action = (action or "status").lower()
    if action in ("status",):
        print(service_status())
    elif action in ("logs", "log"):
        print(service_logs())
    elif action in ("stop",):
        ok, msg = stop_service()
        print(msg)
        return 0 if ok else 1
    elif action in ("start", "restart", "install"):
        # Reuse the most recently installed unit's set if present.
        set_name = _installed_set_name() or "default-gateway"
        ok, msg = install_service(set_name)
        print(msg)
        return 0 if ok else 1
    else:
        print(f"unknown gateway action '{action}' — "
              "use status, logs, stop, or restart")
        return 1
    return 0


def _installed_set_name() -> str | None:
    """Recover the --set name from the installed systemd unit, if any."""
    if not UNIT_PATH.is_file():
        return None
    for line in UNIT_PATH.read_text().splitlines():
        if line.startswith("ExecStart=") and "--set" in line:
            parts = line.split()
            if "--set" in parts:
                return parts[parts.index("--set") + 1]
    return None
