"""DiscordBrick — chat with your agent from Discord.

A push-based Interface Brick built on discord.py's gateway websocket.
Authorized messages feed an asyncio queue that ``get_input()`` drains;
slash commands flow through untouched because the kernel intercepts
``/...`` before the model, so /help, /bricks, /focus, and /afk work from
a Discord channel exactly as they do in the terminal.

discord.py is an **optional dependency** — install it with
``pip install brikie[discord]``. Without it the brick logs a clear
message and stays inert; boot continues.

Security model — the allowlist is NOT optional:
    A bot token drives an agent with shell and file tools. Without
    ``allowed_user_ids`` configured the brick refuses everyone, replying
    with the sender's numeric id so the operator can copy it into config:

        {"brk": "BRK-330", "config": {"allowed_user_ids": [123456789012345678]}}

Rendering is chat-shaped: replies are chunked to Discord's 2000-char
limit, tool calls render as compact one-liners, and model thinking /
tool results only appear with ``verbose: true``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional, Set

from brikie.bricks.interface.base import InterfaceBrick

logger = logging.getLogger(__name__)

_CHUNK = 2000  # Discord's hard message length limit

try:  # optional dependency
    import discord
    _HAS_DISCORD = True
except ImportError:  # pragma: no cover - exercised via the inert path
    discord = None  # type: ignore
    _HAS_DISCORD = False


class DiscordBrick(InterfaceBrick):
    BRICK_NUMBER = "BRK-330"
    """Discord gateway interface (discord.py).

    Args:
        token: Bot token, literally or as an ``env:VAR`` reference
            (default ``env:DISCORD_BOT_TOKEN``).
        allowed_user_ids: Discord numeric user ids permitted to talk to
            the agent. Empty = refuse everyone (and tell them their id).
        verbose: Also send model thinking and tool results to the channel.
    """

    def __init__(
        self,
        token: str = "env:DISCORD_BOT_TOKEN",
        allowed_user_ids: Optional[List[int]] = None,
        verbose: bool = False,
    ) -> None:
        super().__init__()
        self._name = "discord"
        self._token_ref = token
        self._allowed: Set[int] = set(allowed_user_ids or [])
        self._verbose = verbose
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._channels: Set[Any] = set()   # authorized channels seen this session
        self._warned: Set[int] = set()
        self._client: Optional[Any] = None
        self._run_task: Optional[asyncio.Task] = None

    @property
    def name(self) -> str:
        return self._name

    def _resolve_token(self) -> str:
        token = self._token_ref
        if token.startswith("env:"):
            token = os.environ.get(token[4:], "").strip()
        return token

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def init(self) -> None:
        if not _HAS_DISCORD:
            logger.error(
                "DiscordBrick: discord.py is not installed — run "
                "`pip install brikie[discord]`. The Discord interface "
                "stays silent."
            )
            await super().init()
            return
        token = self._resolve_token()
        if not token:
            logger.error(
                "DiscordBrick: no bot token — set %s. Interface stays silent.",
                self._token_ref if self._token_ref.startswith("env:")
                else "the token config",
            )
            await super().init()
            return
        if not self._allowed:
            logger.warning(
                "DiscordBrick: allowed_user_ids is empty — every sender "
                "will be refused (and told their id). Add yours to the "
                "build set config."
            )

        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)

        @self._client.event
        async def on_message(message: Any) -> None:  # noqa: ANN401
            await self._on_message(message)

        @self._client.event
        async def on_ready() -> None:
            user = getattr(self._client, "user", None)
            logger.info("DiscordBrick connected as %s — message the bot to begin.", user)

        self._run_task = asyncio.create_task(
            self._run_gateway(token), name="discord-gateway"
        )
        logger.info("DiscordBrick connecting to the gateway…")
        await super().init()

    async def _run_gateway(self, token: str) -> None:
        """Run the gateway, surfacing the failures users actually hit.

        The most common is the message_content privileged intent not
        being enabled — without it the bot connects but never sees text.
        """
        try:
            await self._client.start(token)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            name = type(exc).__name__
            if "PrivilegedIntents" in name:
                logger.error(
                    "DiscordBrick: the bot connected but the MESSAGE "
                    "CONTENT intent is disabled. Enable it at "
                    "https://discord.com/developers → your app → Bot → "
                    "Privileged Gateway Intents → Message Content Intent, "
                    "then restart. (Without it the bot can't read messages.)"
                )
            elif "LoginFailure" in name or "Improper token" in str(exc):
                logger.error(
                    "DiscordBrick: the bot token was rejected — check "
                    "DISCORD_BOT_TOKEN."
                )
            else:
                logger.error("DiscordBrick gateway stopped: %s: %s", name, exc)

    async def shutdown(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception as exc:
                logger.warning("Discord client close failed: %s", exc)
        if self._run_task is not None:
            self._run_task.cancel()
            try:
                await self._run_task
            except (asyncio.CancelledError, Exception):
                pass
            self._run_task = None
        self._client = None
        await super().shutdown()

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------

    async def get_input(self) -> str:
        return await self._queue.get()

    async def _on_message(self, message: Any) -> None:  # noqa: ANN401
        """Queue authorized message content; ignore the bot's own messages."""
        if self._client is not None and message.author == self._client.user:
            return
        text = (message.content or "").strip()
        if not text:
            return
        sender = message.author.id

        if sender not in self._allowed:
            if sender not in self._warned:
                self._warned.add(sender)
                await self._reply(
                    message.channel,
                    "🧱 This brikie instance hasn't authorized you.\n"
                    f"Your Discord user id is `{sender}` — the operator can "
                    "add it to the discord brick's allowed_user_ids config.",
                )
            logger.warning("Discord: refused message from unauthorized user %s", sender)
            return

        self._channels.add(message.channel)
        await self._queue.put(text)

    # ------------------------------------------------------------------
    # Output — chat-shaped rendering
    # ------------------------------------------------------------------

    async def output(self, msg: str) -> None:
        if msg:
            await self._broadcast(msg)

    async def render_user_message(self, content: str) -> None:
        return  # the user's own message already shows in their channel

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
            await self._broadcast("```\n" + "\n".join(lines) + "\n```")

    async def render_tool_result(
        self, name: str, args: Dict[str, Any], result: str
    ) -> None:
        if self._verbose:
            await self._broadcast(f"⎿ {name}: {str(result)[:300]}")

    async def render_afk_event(self, actor: str, text: str) -> None:
        await self._broadcast(f"◆ **{actor}**: {text}")

    async def render_info(self, title: str, body: str) -> None:
        await self._broadcast(f"ℹ **{title}**\n{body}")

    async def render_error(self, msg: str) -> None:
        await self._broadcast(f"⚠ {msg}")

    async def render_startup(self, info: Dict[str, Any]) -> None:
        return  # no channel known until someone messages

    # ------------------------------------------------------------------
    # Discord plumbing
    # ------------------------------------------------------------------

    async def _broadcast(self, text: str) -> None:
        for channel in self._channels:
            await self._reply(channel, text)

    async def _reply(self, channel: Any, text: str) -> None:  # noqa: ANN401
        """Send to a channel with 2000-char chunking; failures degrade to logs."""
        for start in range(0, len(text), _CHUNK):
            try:
                await channel.send(text[start:start + _CHUNK])
            except Exception as exc:
                logger.warning("Discord send failed: %s", exc)
                return
