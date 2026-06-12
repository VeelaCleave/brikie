"""Tests for the two autonomy-resilience fixes:

1. Context compaction — a long, tool-heavy turn can't grow the prompt past
   the budget (the bloat that timed out brikie's 9-minute turn).
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
    _truncate,
)
from brikie.kernel.hooks import HookDispatcher
from brikie.kernel.registry import BrickRegistry
from brikie.kernel.state import StateManager


def _loop(budget: int) -> EventLoop:
    loop = EventLoop(
        registry=BrickRegistry(), state=StateManager(), hooks=HookDispatcher()
    )
    loop._context_budget = budget
    return loop


class TestTokenHelpers:
    def test_estimate(self):
        assert _estimate_tokens("a" * 400) == 100
        assert _estimate_tokens(None) == 0

    def test_truncate_marks_drop(self):
        out = _truncate("x" * 1000, 100)
        assert out.startswith("x" * 100)
        assert "elided" in out and len(out) < 200


class TestCompaction:
    def test_under_budget_is_untouched(self):
        loop = _loop(budget=100000)
        history = [{"role": "user", "content": "hi"},
                   {"role": "assistant", "content": "hello"}]
        assert loop._compact_to_budget(list(history), 0) == history

    def test_old_tool_results_elided_when_over_budget(self):
        loop = _loop(budget=200)
        # many bulky tool results (the read_file bloat) + a recent tail
        history = [
            {"role": "tool", "content": "FILE BODY " * 200, "tool_call_id": f"t{i}"}
            for i in range(10)
        ]
        history += [{"role": "user", "content": "what next?"}]
        compacted = loop._compact_to_budget(list(history), 0)
        # the oldest tool bodies are elided…
        assert any("elided" in m["content"] for m in compacted[:2])
        # …and the recent working set is untouched
        assert compacted[-1]["content"] == "what next?"

    def test_recent_messages_preserved(self):
        loop = _loop(budget=50)
        history = [
            {"role": "assistant", "content": "x" * 4000} for _ in range(20)
        ]
        compacted = loop._compact_to_budget(list(history), 0)
        kept = history[-COMPACTION_KEEP_RECENT:]
        # the last COMPACTION_KEEP_RECENT keep their full content
        assert [m["content"] for m in compacted[-COMPACTION_KEEP_RECENT:]] == \
               [m["content"] for m in kept]

    async def test_build_messages_stays_within_budget(self):
        loop = _loop(budget=500)
        # simulate a bloated turn: 40 big tool results
        loop._message_history = [
            Message(role="tool", content="DATA " * 300, tool_call_id=f"t{i}")
            for i in range(40)
        ]
        msgs = await loop._build_provider_messages()
        total = sum(_estimate_tokens(m.get("content")) for m in msgs)
        # comfortably bounded vs the ~24k tokens of raw history
        assert total < 4000


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
        handler = _Handler(fail_times=2)  # 2 timeouts, then ok
        p = await _provider_with(handler)
        try:
            content, _calls, _meta = await p.get_completion(
                [{"role": "user", "content": "hi"}], [])
            assert content == "ok"
            assert handler.calls == 3  # 2 failures + 1 success
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
        handler = _Handler(fail_times=99)  # always times out
        p = await _provider_with(handler)
        try:
            with pytest.raises(ProviderConnectionError, match="attempts"):
                await p.get_completion([{"role": "user", "content": "hi"}], [])
            assert handler.calls == 3  # initial + 2 retries
        finally:
            await p.shutdown()

    async def test_404_not_retried(self):
        handler = _Handler(fail_times=99, status=404)
        p = await _provider_with(handler)
        try:
            with pytest.raises(ProviderConnectionError):
                await p.get_completion([{"role": "user", "content": "hi"}], [])
            assert handler.calls == 1  # not retried
        finally:
            await p.shutdown()
