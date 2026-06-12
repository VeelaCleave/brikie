"""Tests for the two autonomy-resilience fixes:

1. Lossless context compaction — when the prompt exceeds budget, the oldest
   turns are folded into a faithful LLM-written summary (not deleted), so a
   long tool-heavy turn keeps inference fast without losing information.
2. Provider retry — a transient timeout / 5xx is retried, not a hard fail.
"""

from __future__ import annotations

import httpx
import pytest

from brikie.bricks.provider.http_provider import HTTPProvider, ProviderConnectionError
from brikie.config.types import Message
from brikie.kernel.event_loop import (
    COMPACTION_KEEP_RECENT,
    EventLoop,
    _estimate_tokens,
)
from brikie.kernel.hooks import HookDispatcher
from brikie.kernel.registry import BrickRegistry, ProviderBrick
from brikie.kernel.state import StateManager
from brikie.config.types import BrickState


class _FakeSummarizer(ProviderBrick):
    """A provider stand-in that returns a canned summary and counts calls."""

    BRICK_NUMBER = "BRK-200"

    def __init__(self, summary: str = "SUMMARY: did the work, key fact = 42"):
        self._summary = summary
        self.calls = 0

    @property
    def name(self) -> str:
        return "fake_provider"

    @property
    def state(self) -> BrickState:
        return BrickState.ACTIVE

    @property
    def model(self) -> str:
        return "fake"

    async def init(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def get_completion(self, messages, tools):
        self.calls += 1
        return self._summary, [], {}


def _loop(budget: int, provider=None) -> EventLoop:
    registry = BrickRegistry()
    if provider is not None:
        registry.register(provider)
    loop = EventLoop(
        registry=registry, state=StateManager(), hooks=HookDispatcher()
    )
    loop._context_budget = budget
    return loop


class TestTokenHelper:
    def test_estimate(self):
        assert _estimate_tokens("a" * 400) == 100
        assert _estimate_tokens(None) == 0


class TestLosslessCompaction:
    async def test_under_budget_does_not_summarize(self):
        prov = _FakeSummarizer()
        loop = _loop(budget=100000, provider=prov)
        loop._message_history = [Message(role="user", content="hi")]
        await loop._compact_if_needed()
        assert prov.calls == 0
        assert len(loop._message_history) == 1

    async def test_over_budget_folds_into_summary(self):
        prov = _FakeSummarizer()
        loop = _loop(budget=500, provider=prov)
        # a bloated turn: 30 big tool results + recent tail
        loop._message_history = [
            Message(role="tool", content="FILE BODY " * 300, tool_call_id=f"t{i}")
            for i in range(30)
        ] + [Message(role="user", content="what next?")]

        await loop._compact_if_needed()

        assert prov.calls == 1  # summarized once
        # the oldest turns became ONE faithful summary message…
        assert loop._message_history[0].role == "system"
        assert "SUMMARY" in loop._message_history[0].content
        assert "lossless" in loop._message_history[0].content
        # …recent working set preserved verbatim…
        assert loop._message_history[-1].content == "what next?"
        # …and the history is now far smaller.
        assert len(loop._message_history) == 1 + COMPACTION_KEEP_RECENT

    async def test_recent_messages_kept_verbatim(self):
        prov = _FakeSummarizer()
        loop = _loop(budget=100, provider=prov)
        tail = [Message(role="assistant", content=f"recent {i}")
                for i in range(COMPACTION_KEEP_RECENT)]
        loop._message_history = [
            Message(role="tool", content="x" * 4000) for _ in range(15)
        ] + tail
        await loop._compact_if_needed()
        kept = [m.content for m in loop._message_history[-COMPACTION_KEEP_RECENT:]]
        assert kept == [m.content for m in tail]

    async def test_summary_failure_keeps_history_intact(self):
        class _Failing(_FakeSummarizer):
            async def get_completion(self, messages, tools):
                self.calls += 1
                raise RuntimeError("boom")

        prov = _Failing()
        loop = _loop(budget=100, provider=prov)
        original = [Message(role="tool", content="y" * 4000) for _ in range(15)]
        loop._message_history = list(original)
        await loop._compact_if_needed()
        # nothing lost — full history retained (proceed slower, never wedge)
        assert len(loop._message_history) == len(original)

    async def test_rolling_summary_refolds_prior_summary(self):
        prov = _FakeSummarizer()
        loop = _loop(budget=500, provider=prov)
        loop._message_history = [
            Message(role="tool", content="A " * 400) for _ in range(30)
        ] + [Message(role="user", content="go")]
        await loop._compact_if_needed()
        first_calls = prov.calls
        # grow again past budget; the prior summary is re-folded, not stacked
        loop._message_history += [
            Message(role="tool", content="B " * 400) for _ in range(30)
        ]
        await loop._compact_if_needed()
        assert prov.calls == first_calls + 1
        # still exactly one summary message at the front
        summaries = [m for m in loop._message_history
                     if m.role == "system" and "compacted" in (m.content or "")]
        assert len(summaries) == 1


# ──────────────────────────────────────────────────────────────────────
# Provider retry
# ──────────────────────────────────────────────────────────────────────


class _Handler:
    def __init__(self, fail_times: int, status: int | None = None):
        self.calls = 0
        self._fail_times = fail_times
        self._status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.calls <= self._fail_times:
            if self._status:
                return httpx.Response(self._status, text="busy")
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {},
        })


async def _provider_with(handler) -> HTTPProvider:
    p = HTTPProvider(model="m", base_url="http://test/v1",
                     max_retries=2, retry_backoff=0.0)
    await p.init()
    await p._client.aclose()
    p._client = httpx.AsyncClient(
        base_url="http://test/v1", transport=httpx.MockTransport(handler))
    return p


class TestProviderRetry:
    async def test_retries_timeout_then_succeeds(self):
        handler = _Handler(fail_times=2)
        p = await _provider_with(handler)
        try:
            content, _calls, _meta = await p.get_completion(
                [{"role": "user", "content": "hi"}], [])
            assert content == "ok"
            assert handler.calls == 3
        finally:
            await p.shutdown()

    async def test_retries_503_then_succeeds(self):
        handler = _Handler(fail_times=1, status=503)
        p = await _provider_with(handler)
        try:
            content, _c, _m = await p.get_completion(
                [{"role": "user", "content": "hi"}], [])
            assert content == "ok"
        finally:
            await p.shutdown()

    async def test_exhausts_retries_then_friendly_error(self):
        handler = _Handler(fail_times=99)
        p = await _provider_with(handler)
        try:
            with pytest.raises(ProviderConnectionError, match="attempts"):
                await p.get_completion([{"role": "user", "content": "hi"}], [])
            assert handler.calls == 3
        finally:
            await p.shutdown()

    async def test_404_not_retried(self):
        handler = _Handler(fail_times=99, status=404)
        p = await _provider_with(handler)
        try:
            with pytest.raises(ProviderConnectionError):
                await p.get_completion([{"role": "user", "content": "hi"}], [])
            assert handler.calls == 1
        finally:
            await p.shutdown()
