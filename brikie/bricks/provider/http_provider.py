"""OpenAI-compatible HTTP Provider Brick.

Sends chat completion requests via httpx to any OpenAI-compatible API
(OpenAI, Anthropic via adapter, local Ollama, etc.).
"""

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from brikie.bricks.provider.base import ProviderBrick

logger = logging.getLogger(__name__)


class HTTPProvider(ProviderBrick):
    """Provider Brick that communicates with an OpenAI-compatible HTTP API.

    Supports configuration via model, API key, base URL, and optional timeout.
    Handles JSON serialization, tool schemas, and response parsing.
    """

    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: str = "sk-placeholder",
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 30.0,
    ) -> None:
        super().__init__()
        self._name = "http_provider"
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def name(self) -> str:
        return self._name

    async def init(self) -> None:
        """Initialize the async HTTP client."""
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(self._timeout),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
        )
        logger.info("HTTPProvider %s initialized (%s)", self._name, self._model)
        super().init()

    async def shutdown(self) -> None:
        """Close the async HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None
        super().shutdown()

    async def get_completion(
        self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
    ) -> tuple[str, List[Dict[str, Any]]]:
        """Send a chat completion request to the OpenAI-compatible API.

        Args:
            messages: Conversation history in OpenAI format.
            tools: Tool schemas (OpenAI `tools` array).

        Returns:
            Tuple of (content, raw_tool_calls).
        """
        if self._client is None:
            raise RuntimeError(f"HTTPProvider {self._name} not initialized.")

        # Build request payload
        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": messages,
        }

        # Add tool schemas if provided
        if tools:
            payload["tools"] = tools

        # POST to /chat/completions
        response = await self._client.post(
            "/chat/completions",
            json=payload,
        )
        response.raise_for_status()

        data = response.json()
        choice = data["choices"][0]
        message = choice["message"]

        content = message.get("content", "")

        # Parse tool calls from the response
        raw_tool_calls: List[Dict[str, Any]] = []
        if "tool_calls" in message and message["tool_calls"]:
            for tc in message["tool_calls"]:
                if tc["type"] == "function":
                    raw_tool_calls.append(tc)

        return (content, raw_tool_calls)
