"""TelegramBrick — chat with your agent from your phone.

A push-based Interface Brick: a background task long-polls the Telegram
Bot API (plain httpx, no SDK, works behind NAT) and feeds authorized
messages into an asyncio queue that ``get_input()`` consumes. Slash
commands need no special handling — the kernel intercepts ``/...``
before the model, so /help, /bricks, /focus, and /afk work from chat
exactly as they do in the terminal.

Security model — the allowlist is NOT optional:
    A bot token is discoverable, and this interface drives an agent
    with shell and file tools. Without ``allowed_user_ids`` configured
    the brick refuses **everyone**, replying with the sender's numeric
    id so the operator can copy it into the build set config:

        {"brk": "BRK-320", "config": {"allowed_user_ids": [123456789]}}

Rendering is chat-shaped: replies are chunked to Telegram's 4096-char
limit, tool calls render as compact one-liners, and model thinking /
tool results only appear with ``verbose: true``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional, Set

import httpx

from brikie.bricks.interface.base import InterfaceBrick

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "https://api.telegram.org"
_CHUNK = 4096
_POLL_TIMEOUT = 50  # Telegram long-poll hold, seconds


class TelegramBrick(InterfaceBrick):
    BRICK_NUMBER = "BRK-320"
    """Telegram Bot API interface (long-polling).

    Args:
        token: Bot token from @BotFather, literally or as an ``env:VAR``
            reference (default ``env:TELEGRAM_BOT_TOKEN``).
        allowed_user_ids: Telegram numeric user ids permitted to talk to
            the agent. Empty = refuse everyone (and tell them their id).
        verbose: Also send model thinking and tool results to the chat.
        api_url: API base override (for tests).
    """

    def __init__(
        self,
        token: str = "env:TELEGRAM_BOT_TOKEN",
        allowed_user_ids: Optional[List[int]] = None,
        verbose: bool = False,
        api_url: str = DEFAULT_API_URL,
    ) -> None:
        super().__init__()
        self._name = "telegram"
        self._token_ref = token
        self._allowed: Set[int] = set(allowed_user_ids or [])
        self._verbose = verbose
        self._api_url = api_url.rstrip("/")
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._chats: Set[int] = set()      # authorized chats seen this session
        self._warned: Set[int] = set()     # unauthorized users already told
        self._offset = 0                   # getUpdates cursor
        self._client: Optional[httpx.AsyncClient] = None
        self._poll_task: Optional[asyncio.Task] = None

    @property
    def name(self) -> str:
        return self._name

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _resolve_token(self) -> str:
        token = self._token_ref
        if token.startswith("env:"):
            token = os.environ.get(token[4:], "").strip()
        return token

    async def init(self) -> None:
        token = self._resolve_token()
        if not token:
            logger.error(
                "TelegramBrick: no bot token — set %s (get one from "
                "@BotFather). The telegram interface stays silent.",
                self._token_ref if self._token_ref.startswith("env:")
                else "the token config",
            )
            await super().init()
            return
        if not self._allowed:
            logger.warning(
                "TelegramBrick: allowed_user_ids is empty — every sender "
                "will be refused (and told their id). Add yours to the "
                "build set config."
            )
        self._client = httpx.AsyncClient(
            base_url=f"{self._api_url}/bot{token}", timeout=_POLL_TIMEOUT + 10
        )
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name="telegram-poll"
        )
        logger.info("TelegramBrick polling for updates.")
        await super().init()

    async def shutdown(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        await super().shutdown()

    # ------------------------------------------------------------------
    # Input — long-poll worker feeds the queue, get_input() drains it
    # ------------------------------------------------------------------

    async def get_input(self) -> str:
        return await self._queue.get()

    async def _poll_loop(self) -> None:
        while True:
            try:
                updates = await self._api("getUpdates", {
                    "offset": self._offset,
                    "timeout": _POLL_TIMEOUT,
                    "allowed_updates": ["message"],
                })
                await self._process_updates(updates or [])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Telegram poll failed (%s) — retrying in 5s", exc)
                await asyncio.sleep(5)

    async def _process_updates(self, updates: List[Dict[str, Any]]) -> None:
        """Queue authorized message texts; advance the update cursor."""
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self._offset = max(self._offset, update_id + 1)

            message = update.get("message") or {}
            text = (message.get("text") or "").strip()
            sender = (message.get("from") or {}).get("id")
            chat_id = (message.get("chat") or {}).get("id")
            if not text or sender is None or chat_id is None:
                continue

            if sender not in self._allowed:
                if sender not in self._warned:
                    self._warned.add(sender)
                    await self._send(
                        chat_id,
                        "🧱 This brikie instance hasn't authorized you.\n"
                        f"Your Telegram user id is {sender} — the operator "
                        "can add it to the telegram brick's "
                        "allowed_user_ids config.",
                    )
                logger.warning(
                    "Telegram: refused message from unauthorized user %s",
                    sender,
                )
                continue

            self._chats.add(chat_id)
            await self._queue.put(text)

    # ------------------------------------------------------------------
    # Output — chat-shaped rendering
    # ------------------------------------------------------------------

    async def output(self, msg: str) -> None:
        if msg:
            await self._broadcast(msg)

    async def render_user_message(self, content: str) -> None:
        # The sender's own chat already shows their message; echoing it
        # back would just duplicate every prompt.
        return

    async def render_assistant_response(self, content: str) -> None:
        if content:
            await self._broadcast(content)

    async def render_thinking(self, reasoning: str) -> None:
        if self._verbose and reasoning:
            await self._broadcast(f"💭 {reasoning[:1000]}")

    async def render_tool_calls(self, raw_calls: List[Dict[str, Any]]) -> None:
        lines = []
        for call in raw_calls:
            func = call.get("function", call)
            name = func.get("name", "?")
            args = str(func.get("arguments", ""))[:120]
            lines.append(f"● {name}({args})")
        if lines:
            await self._broadcast("\n".join(lines))

    async def render_tool_result(
        self, name: str, args: Dict[str, Any], result: str
    ) -> None:
        if self._verbose:
            await self._broadcast(f"⎿ {name}: {str(result)[:300]}")

    async def render_afk_event(self, actor: str, text: str) -> None:
        await self._broadcast(f"◆ {actor}: {text}")

    async def render_info(self, title: str, body: str) -> None:
        await self._broadcast(f"ℹ {title}\n{body}")

    async def render_error(self, msg: str) -> None:
        await self._broadcast(f"⚠ {msg}")

    async def render_startup(self, info: Dict[str, Any]) -> None:
        # No chats are known until someone messages; nothing to send yet.
        return

    # ------------------------------------------------------------------
    # Telegram API plumbing
    # ------------------------------------------------------------------

    async def _broadcast(self, text: str) -> None:
        """Send to every authorized chat seen this session."""
        for chat_id in self._chats:
            await self._send(chat_id, text)

    async def _send(self, chat_id: int, text: str) -> None:
        """sendMessage with 4096-char chunking; failures degrade to logs."""
        for start in range(0, len(text), _CHUNK):
            try:
                await self._api("sendMessage", {
                    "chat_id": chat_id,
                    "text": text[start:start + _CHUNK],
                })
            except Exception as exc:
                logger.warning("Telegram send to %s failed: %s", chat_id, exc)
                return

    async def _api(self, method: str, params: Dict[str, Any]) -> Any:
        """POST one Bot API method and return its ``result`` payload."""
        if self._client is None:
            raise RuntimeError("TelegramBrick not initialized")
        response = await self._client.post(f"/{method}", json=params)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(
                f"Telegram {method}: {data.get('description', 'unknown error')}"
            )
        return data.get("result")
