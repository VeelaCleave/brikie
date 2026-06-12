"""Tests for the background gateway: headless set derivation, nohup
service management, and the gateway command dispatch.

systemd paths are not exercised here (CI has no user bus); the nohup
fallback covers the same install/stop/status/logs surface.
"""

from __future__ import annotations

import json

import pytest

from brikie import gateway


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Redirect all gateway state into a temp dir and force the nohup path."""
    monkeypatch.setattr(gateway, "BRIKIE_DIR", tmp_path)
    monkeypatch.setattr(gateway, "PID_FILE", tmp_path / "gw.pid")
    monkeypatch.setattr(gateway, "LOG_FILE", tmp_path / "gw.log")
    monkeypatch.setattr(gateway, "UNIT_PATH", tmp_path / "unit.service")
    monkeypatch.setattr(gateway, "has_systemd", lambda: False)


class TestGatewaySet:
    def test_drops_cli_keeps_chat(self, tmp_path):
        (tmp_path / "default.json").write_text(json.dumps({
            "name": "default",
            "bricks": [{"brk": "BRK-300"}, {"brk": "BRK-200"}, {"brk": "BRK-410"}],
        }))
        name = gateway.make_gateway_set(tmp_path, "default", "BRK-330")
        assert name == "default-gateway"
        data = json.loads((tmp_path / "default-gateway.json").read_text())
        brks = [b["brk"] for b in data["bricks"]]
        assert "BRK-300" not in brks        # no terminal in a service
        assert "BRK-330" in brks            # chat interface added
        assert "BRK-200" in brks            # provider kept

    def test_missing_source_still_yields_chat_set(self, tmp_path):
        name = gateway.make_gateway_set(tmp_path, "nope", "BRK-320")
        data = json.loads((tmp_path / f"{name}.json").read_text())
        assert [b["brk"] for b in data["bricks"]] == ["BRK-320"]


class TestNohupService:
    def test_install_start_status_stop(self, monkeypatch, tmp_path):
        started = {}

        class FakeProc:
            pid = 4242

        def fake_popen(cmd, **kw):
            started["cmd"] = cmd
            return FakeProc()

        monkeypatch.setattr(gateway.subprocess, "Popen", fake_popen)
        ok, msg = gateway.install_service("default-gateway")
        assert ok and "4242" in msg
        assert "--set" in started["cmd"]
        assert (tmp_path / "gw.pid").read_text().strip() == "4242"

        # status reflects the live pid (os.kill(pid, 0) succeeds for ours)
        monkeypatch.setattr(gateway.os, "kill", lambda pid, sig: None)
        assert "running" in gateway.service_status()

        killed = {}
        monkeypatch.setattr(gateway.os, "kill",
                            lambda pid, sig: killed.update(pid=pid, sig=sig))
        ok, _ = gateway.stop_service()
        assert ok and killed["pid"] == 4242
        assert not (tmp_path / "gw.pid").exists()

    def test_status_when_stopped(self):
        assert "stopped" in gateway.service_status()

    def test_logs_empty_initially(self):
        assert "no logs" in gateway.service_logs()


class TestCommandDispatch:
    def test_status_action(self, capsys):
        assert gateway.run_gateway_command("status") == 0
        assert "gateway" in capsys.readouterr().out

    def test_unknown_action(self, capsys):
        assert gateway.run_gateway_command("frobnicate") == 1
        assert "unknown" in capsys.readouterr().out

    def test_stop_when_not_running(self, capsys):
        assert gateway.run_gateway_command("stop") == 0
