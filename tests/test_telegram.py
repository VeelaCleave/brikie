"""Tests for the Telegram interface brick and multi-interface input."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

from brikie.bricks.interface.telegram import TelegramBrick
from brikie.config.types import BrickState
from brikie.kernel.event_loop import EventLoop
from brikie.kernel.hooks import HookDispatcher
from brikie.kernel.registry import BrickRegistry, InterfaceBrick as InterfaceABC
from brikie.kernel.state import StateManager


def _update(uid: int, sender: int, chat: int, text: str) -> Dict[str, Any]:
    return {
        "update_id": uid,
        "message": {
            "text": text,
            "from": {"id": sender},
            "chat": {"id": chat},
        },
    }


@pytest.fixture
def brick(monkeypatch) -> TelegramBrick:
    """A TelegramBrick with the API mocked out (records sent messages)."""
    b = TelegramBrick(token="tok-test", allowed_user_ids=[111])
    b.sent: List[Dict[str, Any]] = []

    async def fake_api(method: str, params: Dict[str, Any]) -> Any:
        if method == "sendMessage":
            b.sent.append(params)
        return []

    monkeypatch.setattr(b, "_api", fake_api)
    return b


class TestAllowlist:
    async def test_authorized_message_is_queued(self, brick):
        await brick._process_updates([_update(1, 111, 555, "hello")])
        assert await brick.get_input() == "hello"
        assert 555 in brick._chats

    async def test_non_owner_refused(self, brick):
        # brick fixture has allowlist [111]; 999 is not the owner
        await brick._process_updates([_update(1, 999, 666, "let me in")])
        assert brick._queue.empty()
        assert 666 not in brick._chats
        assert len(brick.sent) == 1
        assert "already paired" in brick.sent[0]["text"]

    async def test_non_owner_warned_only_once(self, brick):
        await brick._process_updates([
            _update(1, 999, 666, "hi"),
            _update(2, 999, 666, "hi again"),
        ])
        assert len(brick.sent) == 1

    async def test_empty_allowlist_claims_first_messager(self, monkeypatch):
        b = TelegramBrick(token="tok", allowed_user_ids=[])
        b.sent = []

        async def fake_api(method, params):
            b.sent.append(params)
            return []

        monkeypatch.setattr(b, "_api", fake_api)
        await b._process_updates([_update(1, 42, 42, "anyone home?")])
        # first messager is adopted as owner — message goes through
        assert await b.get_input() == "anyone home?"
        assert 42 in b._allowed


class TestUpdates:
    async def test_offset_advances_past_highest_update(self, brick):
        await brick._process_updates([
            _update(7, 111, 555, "a"),
            _update(9, 111, 555, "b"),
        ])
        assert brick._offset == 10

    async def test_non_text_updates_skipped(self, brick):
        await brick._process_updates([
            {"update_id": 3, "message": {"photo": [], "chat": {"id": 1},
                                         "from": {"id": 111}}},
        ])
        assert brick._queue.empty()
        assert brick._offset == 4

    async def test_slash_commands_pass_through_as_text(self, brick):
        await brick._process_updates([_update(1, 111, 555, "/focus registry UX")])
        assert await brick.get_input() == "/focus registry UX"


class TestRendering:
    async def test_long_replies_are_chunked(self, brick):
        brick._chats.add(555)
        await brick.render_assistant_response("x" * 9000)
        assert len(brick.sent) == 3
        assert all(len(m["text"]) <= 4096 for m in brick.sent)

    async def test_tool_calls_render_compact(self, brick):
        brick._chats.add(555)
        await brick.render_tool_calls([
            {"function": {"name": "bash_execute", "arguments": '{"command": "ls"}'}},
        ])
        assert brick.sent[0]["text"].startswith("● bash_execute(")

    async def test_thinking_hidden_unless_verbose(self, brick):
        brick._chats.add(555)
        await brick.render_thinking("deep thoughts")
        assert brick.sent == []
        brick._verbose = True
        await brick.render_thinking("deep thoughts")
        assert "💭" in brick.sent[0]["text"]

    async def test_user_message_not_echoed(self, brick):
        brick._chats.add(555)
        await brick.render_user_message("hi")
        assert brick.sent == []

    async def test_broadcast_reaches_all_authorized_chats(self, brick):
        brick._chats.update({555, 777})
        await brick.render_error("boom")
        assert {m["chat_id"] for m in brick.sent} == {555, 777}


class TestLifecycle:
    async def test_missing_token_is_inert_not_fatal(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        b = TelegramBrick()
        await b.init()  # must not raise (AGENTS rule: optional boot)
        assert b._poll_task is None
        assert b.state is BrickState.ACTIVE
        await b.shutdown()

    def test_env_token_reference(self, monkeypatch):
        monkeypatch.setenv("TEST_TG_TOKEN", "12345:abc")
        b = TelegramBrick(token="env:TEST_TG_TOKEN")
        assert b._resolve_token() == "12345:abc"


# ──────────────────────────────────────────────────────────────────────
# Multi-interface input racing (kernel)
# ──────────────────────────────────────────────────────────────────────


class _QueueInterface(InterfaceABC):
    """Interface whose input comes from a test-controlled queue."""

    def __init__(self, name: str) -> None:
        self._name = name
        self.queue: asyncio.Queue[str] = asyncio.Queue()

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> BrickState:
        return BrickState.ACTIVE

    async def init(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def get_input(self) -> str:
        return await self.queue.get()

    async def output(self, msg: str) -> None:
        pass


class TestMultiInterfaceInput:
    async def test_first_interface_to_speak_wins(self):
        registry = BrickRegistry()
        slow, fast = _QueueInterface("slow"), _QueueInterface("fast")
        registry.register(slow)
        registry.register(fast)
        loop = EventLoop(
            registry=registry, state=StateManager(), hooks=HookDispatcher()
        )

        await fast.queue.put("from fast")
        assert await loop._capture_input() == "from fast"

        # The slow interface's pending read survives the lost race and
        # is consumed on a later turn.
        await slow.queue.put("from slow")
        assert await loop._capture_input() == "from slow"

        for task in loop._input_tasks.values():
            task.cancel()

    async def test_single_interface_path_unchanged(self):
        registry = BrickRegistry()
        only = _QueueInterface("only")
        registry.register(only)
        loop = EventLoop(
            registry=registry, state=StateManager(), hooks=HookDispatcher()
        )
        await only.queue.put("hello")
        assert await loop._capture_input() == "hello"
        assert loop._input_tasks == {}
