"""HTTP Provider Bricks — LLM API translation layers.

Supports both OpenAI and Claude API formats out of the box.
"""

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from brikie.bricks.provider.base import ProviderBrick

logger = logging.getLogger(__name__)


class HTTPProvider(ProviderBrick):
    """Provider Brick that communicates with LLM APIs over HTTP.

    Supports both OpenAI and Anthropic (Claude) API formats.
    Configuration is driven by `api_format`, `model`, `api_key`,
    `base_url`, and optional `timeout`.
    """

    FORMAT_OPENAI = "openai"
    FORMAT_CLAUDE = "claude"

    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: str = "sk-placeholder",
        base_url: str = "https://api.openai.com/v1",
        api_format: str = FORMAT_OPENAI,
        timeout: float = 30.0,
    ) -> None:
        super().__init__()
        self._name = "http_provider"
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._api_format = api_format
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def name(self) -> str:
        return self._name

    async def init(self) -> None:
        """Initialize the async HTTP client."""
        if self._api_format == self.FORMAT_CLAUDE:
            headers = {
                "Content-Type": "application/json",
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
            }
        else:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            }

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(self._timeout),
            headers=headers,
        )
        logger.info(
            "HTTPProvider %s initialized (%s / %s)",
            self._name,
            self._model,
            self._api_format,
        )
        super().init()

    async def shutdown(self) -> None:
        """Close the async HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None
        super().shutdown()

    async def get_completion(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> tuple[str, List[Dict[str, Any]]]:
        """Send a completion request and return (content, tool_calls).

        Dispatches to the correct API format handler based on `api_format`.
        """
        if self._client is None:
            raise RuntimeError(f"HTTPProvider {self._name} not initialized.")

        if self._api_format == self.FORMAT_CLAUDE:
            return await self._call_claude(messages, tools)
        return await self._call_openai(messages, tools)

    # ------------------------------------------------------------------
    # OpenAI API
    # ------------------------------------------------------------------

    async def _call_openai(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> tuple[str, List[Dict[str, Any]]]:
        """Handle OpenAI-format /chat/completions endpoint."""
        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools

        response = await self._client.post("/chat/completions", json=payload)
        response.raise_for_status()
        data = response.json()

        message = data["choices"][0]["message"]
        content = message.get("content", "") or ""

        raw_calls: List[Dict[str, Any]] = []
        for tc in message.get("tool_calls") or []:
            raw_calls.append(tc)

        return content, raw_calls

    # ------------------------------------------------------------------
    # Claude API
    # ------------------------------------------------------------------

    async def _call_claude(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> tuple[str, List[Dict[str, Any]]]:
        """Handle Anthropic-format /v1/messages endpoint."""
        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": 4096,
        }
        if tools:
            payload["tools"] = self._convert_tools_for_claude(tools)

        response = await self._client.post("/v1/messages", json=payload)
        response.raise_for_status()
        data = response.json()

        content = ""
        raw_calls: List[Dict[str, Any]] = []

        for block in data.get("content") or []:
            if block.get("type") == "text":
                content += block.get("text", "")
            elif block.get("type") == "tool_use":
                raw_calls.append(block)

        return content, raw_calls

    @staticmethod
    def _convert_tools_for_claude(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert OpenAI-style tool schemas to Claude format.

        OpenAI format:
        {
            "type": "function",
            "function": {
                "name": "calculator",
                "description": "...",
                "parameters": {"type": "object", "properties": {...}}
            }
        }

        Claude format:
        {
            "name": "calculator",
            "description": "...",
            "input_schema": {"type": "object", "properties": {...}}
        }
        """
        claude_tools: List[Dict[str, Any]] = []
        for tool in tools:
            if "function" in tool:
                func = tool["function"]
                claude_tools.append({
                    "name": func.get("name", ""),
                    "description": func.get("description", ""),
                    "input_schema": func.get("parameters", {}),
                })
            else:
                # Already in Claude format
                claude_tools.append(tool)
        return claude_tools
