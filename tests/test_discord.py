"""Tests for the Discord interface brick.

The gateway is never connected — we drive the brick's message handler and
rendering directly with fakes, the way a real on_message / channel.send
would, so no network or token is needed.
"""

from __future__ import annotations

from typing import List

import pytest

from brikie.bricks.interface.discord_iface import DiscordBrick
from brikie.config.types import BrickState


class _FakeChannel:
    def __init__(self) -> None:
        self.sent: List[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


class _FakeAuthor:
    def __init__(self, uid: int) -> None:
        self.id = uid


class _FakeMessage:
    def __init__(self, uid: int, content: str, channel: _FakeChannel) -> None:
        self.author = _FakeAuthor(uid)
        self.content = content
        self.channel = channel


@pytest.fixture
def brick() -> DiscordBrick:
    return DiscordBrick(token="tok", allowed_user_ids=[111])


class TestAllowlist:
    async def test_authorized_message_queued(self, brick):
        ch = _FakeChannel()
        await brick._on_message(_FakeMessage(111, "hello", ch))
        assert await brick.get_input() == "hello"
        assert ch in brick._channels

    async def test_non_owner_refused(self, brick):
        # brick fixture has allowlist [111]; 999 is not the owner
        ch = _FakeChannel()
        await brick._on_message(_FakeMessage(999, "let me in", ch))
        assert brick._queue.empty()
        assert "already paired" in ch.sent[0]

    async def test_unauthorized_warned_once(self, brick):
        ch = _FakeChannel()
        await brick._on_message(_FakeMessage(999, "a", ch))
        await brick._on_message(_FakeMessage(999, "b", ch))
        assert len(ch.sent) == 1

    async def test_empty_text_ignored(self, brick):
        ch = _FakeChannel()
        await brick._on_message(_FakeMessage(111, "   ", ch))
        assert brick._queue.empty()

    async def test_slash_command_passes_through(self, brick):
        ch = _FakeChannel()
        await brick._on_message(_FakeMessage(111, "/afk 2", ch))
        assert await brick.get_input() == "/afk 2"


class TestRendering:
    async def test_long_reply_chunked_to_2000(self, brick):
        ch = _FakeChannel()
        brick._channels.add(ch)
        await brick.render_assistant_response("y" * 4500)
        assert len(ch.sent) == 3
        assert all(len(m) <= 2000 for m in ch.sent)

    async def test_tool_calls_render_as_code_block(self, brick):
        ch = _FakeChannel()
        brick._channels.add(ch)
        await brick.render_tool_calls([
            {"function": {"name": "bash_execute", "arguments": '{"command":"ls"}'}},
        ])
        assert ch.sent[0].startswith("```")
        assert "bash_execute" in ch.sent[0]

    async def test_thinking_hidden_unless_verbose(self, brick):
        ch = _FakeChannel()
        brick._channels.add(ch)
        await brick.render_thinking("hmm")
        assert ch.sent == []
        brick._verbose = True
        await brick.render_thinking("hmm")
        assert "💭" in ch.sent[0]

    async def test_broadcast_reaches_all_channels(self, brick):
        a, b = _FakeChannel(), _FakeChannel()
        brick._channels.update({a, b})
        await brick.render_error("boom")
        assert a.sent and b.sent


class TestLifecycle:
    async def test_missing_token_is_inert(self, monkeypatch):
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        b = DiscordBrick()
        await b.init()  # must not raise
        assert b._client is None
        assert b.state is BrickState.ACTIVE
        await b.shutdown()

    def test_env_token_reference(self, monkeypatch):
        monkeypatch.setenv("TEST_DISCORD_TOKEN", "abc.def.ghi")
        b = DiscordBrick(token="env:TEST_DISCORD_TOKEN")
        assert b._resolve_token() == "abc.def.ghi"
