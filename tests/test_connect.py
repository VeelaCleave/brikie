"""Tests for chat config helpers (~/.brikie/.env, set wiring) and the
first-messager 'claim' authorization in the chat interface bricks."""

from __future__ import annotations

import json

from brikie import connect
from brikie.bricks.interface.telegram import TelegramBrick
from brikie.bricks.interface.discord_iface import DiscordBrick


class TestEnvFile:
    def test_save_then_load(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        connect.save_env_var("DISCORD_BOT_TOKEN", "abc.def", path=env)
        assert oct(env.stat().st_mode)[-3:] == "600"
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        connect.load_env_file(env)
        import os
        assert os.environ["DISCORD_BOT_TOKEN"] == "abc.def"

    def test_upsert_replaces_not_duplicates(self, tmp_path):
        env = tmp_path / ".env"
        connect.save_env_var("K", "1", path=env)
        connect.save_env_var("K", "2", path=env)
        assert env.read_text().count("K=") == 1
        assert "K=2" in env.read_text()

    def test_existing_env_var_wins(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        connect.save_env_var("K", "from-file", path=env)
        monkeypatch.setenv("K", "from-shell")
        connect.load_env_file(env)
        import os
        assert os.environ["K"] == "from-shell"

    def test_missing_file_is_noop(self, tmp_path):
        connect.load_env_file(tmp_path / "nope.env")  # must not raise


class TestSetWiring:
    def test_creates_set_with_interface_no_allowlist(self, tmp_path):
        connect.add_interface_to_set(tmp_path, "default", "BRK-330")
        data = json.loads((tmp_path / "default.json").read_text())
        entry = next(b for b in data["bricks"] if b["brk"] == "BRK-330")
        # No allowlist — the bot claims its first messager.
        assert "config" not in entry

    def test_idempotent(self, tmp_path):
        connect.add_interface_to_set(tmp_path, "default", "BRK-320")
        connect.add_interface_to_set(tmp_path, "default", "BRK-320")
        data = json.loads((tmp_path / "default.json").read_text())
        assert [b["brk"] for b in data["bricks"]].count("BRK-320") == 1

    def test_adds_to_existing_set(self, tmp_path):
        (tmp_path / "default.json").write_text(json.dumps({
            "name": "default", "bricks": [{"brk": "BRK-300"}],
        }))
        connect.add_interface_to_set(tmp_path, "default", "BRK-320")
        data = json.loads((tmp_path / "default.json").read_text())
        assert {b["brk"] for b in data["bricks"]} == {"BRK-300", "BRK-320"}


def _tg_update(uid, chat, text):
    return {"update_id": 1, "message": {
        "from": {"id": uid}, "chat": {"id": chat}, "text": text}}


class TestTelegramClaim:
    async def test_first_messager_claims_then_others_refused(self):
        b = TelegramBrick(token="t", allowed_user_ids=[])
        b.sent = []

        async def fake_api(method, params):
            b.sent.append(params)
            return []

        b._api = fake_api  # type: ignore
        await b._process_updates([_tg_update(111, 111, "hi")])
        assert await b.get_input() == "hi"
        assert 111 in b._allowed
        # a different user is now refused
        await b._process_updates([_tg_update(222, 222, "hey")])
        assert b._queue.empty()

    async def test_explicit_allowlist_still_strict(self):
        b = TelegramBrick(token="t", allowed_user_ids=[111])

        async def fake_api(method, params):
            return []

        b._api = fake_api  # type: ignore
        await b._process_updates([_tg_update(999, 999, "let me in")])
        assert b._queue.empty()
        assert 999 not in b._allowed


class _Ch:
    def __init__(self):
        self.sent = []

    async def send(self, t):
        self.sent.append(t)


class _Msg:
    def __init__(self, uid, content, ch):
        self.author = type("A", (), {"id": uid})()
        self.content = content
        self.channel = ch


class TestDiscordClaim:
    async def test_first_messager_claims(self):
        b = DiscordBrick(token="t", allowed_user_ids=[])
        ch = _Ch()
        await b._on_message(_Msg(111, "hi", ch))
        assert await b.get_input() == "hi"
        assert 111 in b._allowed

    async def test_explicit_allowlist_strict(self):
        b = DiscordBrick(token="t", allowed_user_ids=[111])
        ch = _Ch()
        await b._on_message(_Msg(999, "nope", ch))
        assert b._queue.empty()
