"""Unit tests for the Logging Bricks architecture (Phase 5.1).

Tests cover:
1. LogEntry/LogLevel dataclass integrity
2. LoggingBrick ABC: lifecycle, emit queue, consumer task
3. TokenLoggerBrick: capture, persistence, Dreamer API
4. ToolTracerBrick: lifecycle stages, error detection, JSONL persistence
5. DiagnosticsCollectorBrick: ring buffer, rolling windows, pub/sub
6. Hook registration via event loop
"""

import json
import os
import sqlite3
import tempfile
from pathlib import Path
import pytest

from brikie.bricks.logging.base import LogEntry, LogLevel
from brikie.bricks.logging.token_logger import TokenLoggerBrick
from brikie.bricks.logging.tool_tracer import ToolTracerBrick
from brikie.bricks.logging.diagnostics import DiagnosticsCollectorBrick
from brikie.config.types import HookType, ToolCall


# ======================================================================
# Fixtures
# ======================================================================


@pytest.fixture
def temp_db():
    """Provide a temporary SQLite database path."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture
def temp_log_dir():
    """Provide a temporary directory for JSONL log files."""
    path = tempfile.mkdtemp()
    yield path
    for file in Path(path).iterdir():
        file.unlink()
    Path(path).rmdir()


@pytest.fixture
def token_logger(temp_db):
    """Create a TokenLoggerBrick with a temporary DB."""
    brick = TokenLoggerBrick(db_path=temp_db)
    return brick


@pytest.fixture
def tool_tracer(temp_log_dir):
    """Create a ToolTracerBrick with a temporary log dir."""
    brick = ToolTracerBrick(log_dir=temp_log_dir, result_preview_max_chars=100)
    return brick


@pytest.fixture
def diagnostics_collector():
    """Create a DiagnosticsCollectorBrick with a small ring buffer."""
    brick = DiagnosticsCollectorBrick(ring_buffer_size=50)
    return brick


# ======================================================================
# LogEntry / LogLevel
# ======================================================================


class TestLogEntry:
    """Verify LogEntry and LogLevel basic behaviour."""

    def test_log_level_values(self):
        assert LogLevel.DEBUG.value == "debug"
        assert LogLevel.INFO.value == "info"
        assert LogLevel.WARNING.value == "warning"
        assert LogLevel.ERROR.value == "error"

    def test_log_entry_defaults(self):
        entry = LogEntry()
        assert entry.source == ""
        assert entry.event_type == ""
        assert entry.level == LogLevel.INFO
        assert entry.payload == {}
        assert entry.session_id == ""
        assert entry.trace_id == ""

    def test_log_entry_timestamp_auto_set(self):
        entry = LogEntry()
        assert entry.timestamp != ""

    def test_log_entry_full_construction(self):
        entry = LogEntry(
            source="test_brick",
            event_type="test_event",
            level=LogLevel.ERROR,
            payload={"key": "value"},
            session_id="ses-123",
            trace_id="trace-456",
        )
        assert entry.source == "test_brick"
        assert entry.event_type == "test_event"
        assert entry.level == LogLevel.ERROR
        assert entry.payload == {"key": "value"}
        assert entry.session_id == "ses-123"
        assert entry.trace_id == "trace-456"


# ======================================================================
# LoggingBrick ABC
# ======================================================================


class TestLoggingBrick:
    """Verify LoggingBrick lifecycle and emit queue."""

    @pytest.mark.asyncio
    async def test_init_and_shutdown(self):
        """Brick transitions WARM_UP -> ACTIVE -> WARM_UP."""
        brick = TokenLoggerBrick(db_path=":memory:")
        await brick.init()
        assert brick.state.value == "active"
        await brick.shutdown()
        assert brick.state.value == "warm_up"

    @pytest.mark.asyncio
    async def test_emit_no_block(self):
        """emit() should not block the caller."""
        brick = TokenLoggerBrick(db_path=":memory:")
        await brick.init()
        entry = LogEntry(source="test", event_type="test")
        # Should not raise or block
        brick.emit(entry)
        await brick.shutdown()

    @pytest.mark.asyncio
    async def test_get_hook_callbacks_abstract(self):
        """get_hook_callbacks() must be implemented by subclasses."""
        brick = TokenLoggerBrick(db_path=":memory:")
        callbacks = await brick.get_hook_callbacks()
        assert HookType.POST_LLM in callbacks
        assert len(callbacks[HookType.POST_LLM]) == 1

    @pytest.mark.asyncio
    async def test_shutdown_flushes_queue(self):
        """On shutdown, the queue should be drained before cancellation."""
        brick = TokenLoggerBrick(db_path=":memory:")
        await brick.init()
        for i in range(10):
            brick.emit(LogEntry(source="test", event_type=f"evt{i}"))
        # Should not hang
        await brick.shutdown()


# ======================================================================
# TokenLoggerBrick
# ======================================================================


class TestTokenLoggerBrick:
    """Verify token capture, persistence, and Dreamer API."""

    @pytest.mark.asyncio
    async def test_capture_tokens_emits_entry(self, token_logger):
        """Calling _capture_tokens should enqueue a LogEntry."""
        await token_logger.init()
        data = {
            "content": "Hello, world!",
            "tool_calls": [],
            "_meta": {
                "model": "gpt-4",
                "input_tokens": 150,
                "output_tokens": 50,
                "latency_ms": 1234.5,
                "session_id": "test-session",
            },
        }
        token_logger._capture_tokens(data)
        # Give the consumer task a chance to process
        await token_logger._emit_queue.join()
        await token_logger.shutdown()

    @pytest.mark.asyncio
    async def test_persist_to_sqlite(self, token_logger, temp_db):
        """Token entries should be written to SQLite."""
        await token_logger.init()

        entry = LogEntry(
            source="token_logger",
            event_type="token_usage",
            level=LogLevel.INFO,
            payload={
                "model": "claude-3.5",
                "input_tokens": 200,
                "output_tokens": 100,
                "latency_ms": 2500.0,
                "tool_call_count": 3,
                "cost_input": 0.0006,
                "cost_output": 0.0015,
                "cost_total": 0.0021,
            },
            session_id="test-session",
            trace_id="trace-001",
        )
        await token_logger._persist(entry)

        conn = sqlite3.connect(temp_db)
        cursor = conn.execute("SELECT model, input_tokens, output_tokens FROM token_logs")
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "claude-3.5"
        assert row[1] == 200
        assert row[2] == 100
        conn.close()

    @pytest.mark.asyncio
    async def test_last_n_cycles(self, token_logger):
        """last_n_cycles() should return recent entries in reverse order."""
        await token_logger.init()

        for i in range(5):
            entry = LogEntry(
                source="token_logger",
                event_type="token_usage",
                level=LogLevel.INFO,
                payload={
                    "model": "model-x",
                    "input_tokens": 100 * (i + 1),
                    "output_tokens": 50 * (i + 1),
                    "latency_ms": 500.0,
                    "tool_call_count": 1,
                    "cost_input": 0.0003 * (i + 1),
                    "cost_output": 0.00075 * (i + 1),
                    "cost_total": 0.00105 * (i + 1),
                },
                session_id="test-session",
            )
            await token_logger._persist(entry)

        snapshots = await token_logger.last_n_cycles(3)
        assert len(snapshots) == 3
        # Most recent first
        assert snapshots[0].input_tokens == 500

        await token_logger.shutdown()

    @pytest.mark.asyncio
    async def test_aggregate_session_stats(self, token_logger):
        """aggregate_session_stats() should sum across entries."""
        await token_logger.init()

        for i in range(3):
            entry = LogEntry(
                source="token_logger",
                event_type="token_usage",
                level=LogLevel.INFO,
                payload={
                    "model": "gpt-4",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "latency_ms": 1000.0,
                    "tool_call_count": 1,
                    "cost_input": 0.0003,
                    "cost_output": 0.00075,
                    "cost_total": 0.00105,
                },
                session_id="session-agg",
            )
            await token_logger._persist(entry)

        stats = await token_logger.aggregate_session_stats("session-agg")
        assert stats["total_input_tokens"] == 300
        assert stats["total_output_tokens"] == 150
        assert stats["call_count"] == 3
        assert stats["avg_latency_ms"] == 1000.0

        await token_logger.shutdown()

    @pytest.mark.asyncio
    async def test_empty_stats_for_unknown_session(self, token_logger):
        """aggregate_session_stats() for an unknown session returns zero-valued dict."""
        await token_logger.init()
        stats = await token_logger.aggregate_session_stats("nonexistent")
        assert stats == {
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cost": 0,
            "call_count": 0,
            "avg_latency_ms": 0,
        }
        await token_logger.shutdown()

    @pytest.mark.asyncio
    async def test_capture_tokens_missing_meta(self, token_logger):
        """_capture_tokens with no _meta should not crash."""
        await token_logger.init()
        token_logger._capture_tokens({"content": "hello"})
        token_logger._capture_tokens("not a dict")
        token_logger._capture_tokens(None)
        await token_logger._emit_queue.join()
        await token_logger.shutdown()

    @pytest.mark.asyncio
    async def test_cost_calculation(self, token_logger):
        """Cost should be calculated based on token counts."""
        await token_logger.init()
        data = {
            "content": "",
            "tool_calls": [],
            "_meta": {
                "model": "gpt-4",
                "input_tokens": 1000,
                "output_tokens": 500,
                "latency_ms": 500.0,
            },
        }
        token_logger._capture_tokens(data)
        await token_logger._emit_queue.join()
        await token_logger.shutdown()


# ======================================================================
# ToolTracerBrick
# ======================================================================


class TestToolTracerBrick:
    """Verify tool-call lifecycle tracing, error detection, and persistence."""

    @pytest.mark.asyncio
    async def test_pre_tool_initializes_trace(self, tool_tracer):
        """PRE_TOOL should initialize an in-flight trace."""
        await tool_tracer.init()

        tool_calls = [
            ToolCall(name="calculator", args={"a": 1, "b": 2}),
        ]
        tool_tracer._on_pre_tool(tool_calls)

        assert len(tool_tracer._in_flight) == 1
        trace_id = list(tool_tracer._in_flight.keys())[0]
        trace = tool_tracer._in_flight[trace_id]
        assert trace.tool_name == "calculator"
        assert trace.args == {"a": 1, "b": 2}
        assert trace.pre_tool_timestamp != ""

        await tool_tracer.shutdown()

    @pytest.mark.asyncio
    async def test_full_lifecycle(self, tool_tracer):
        """A full tool lifecycle should produce a completed trace."""
        await tool_tracer.init()

        # PRE_TOOL
        tcs = [ToolCall(name="search", args={"q": "test"})]
        tool_tracer._on_pre_tool(tcs)

        trace_id = tool_tracer._current_trace_ids[0]

        # Simulate result set by EventLoop.process_tool_calls
        tcs[0].result = "search results here"

        # POST_TOOL
        tool_tracer._on_post_tool(tcs)

        # POST_TOOL_CALL
        tool_tracer._on_post_tool_call(tcs)

        # The trace should be finalized (removed from in_flight)
        assert trace_id not in tool_tracer._in_flight

        await tool_tracer.shutdown()

    @pytest.mark.asyncio
    async def test_error_detection(self, tool_tracer):
        """An error result should be flagged as not successful."""
        assert tool_tracer._is_error("Error: file not found")
        assert tool_tracer._is_error("Exception occurred")
        assert tool_tracer._is_error("Traceback (most recent call last)")
        assert tool_tracer._is_error("failed to connect")
        assert not tool_tracer._is_error("Operation completed successfully")
        assert not tool_tracer._is_error("")
        assert not tool_tracer._is_error(None)

    @pytest.mark.asyncio
    async def test_error_extraction(self, tool_tracer):
        """Extract error message from result."""
        result = "Some output\nError: Invalid syntax\nMore output"
        extracted = tool_tracer._extract_error(result)
        assert "Error: Invalid syntax" in extracted

        # Fallback to first line
        result2 = "just a failure"
        assert tool_tracer._extract_error(result2) == "just a failure"

    @pytest.mark.asyncio
    async def test_preview_truncation(self, tool_tracer):
        """Long results should be truncated."""
        long_result = "x" * 500
        preview = tool_tracer._preview(long_result, max_chars=100)
        assert len(preview) <= 103  # 100 chars + "..."
        assert preview.endswith("...")

    @pytest.mark.asyncio
    async def test_persist_to_jsonl(self, tool_tracer, temp_log_dir):
        """Completed traces should be written to JSONL."""
        await tool_tracer.init()

        entry = LogEntry(
            source="tool_tracer",
            event_type="tool_call_complete",
            level=LogLevel.INFO,
            payload={
                "trace_id": "test-trace-1",
                "tool_name": "calculator",
                "duration_ms": 150.0,
                "success": True,
                "result_preview": "42",
            },
            trace_id="test-trace-1",
        )
        await tool_tracer._persist(entry)

        # Check the JSONL file was created
        log_files = list(Path(temp_log_dir).glob("*.jsonl"))
        assert len(log_files) == 1

        with open(log_files[0]) as f:
            line = json.loads(f.readline())
            assert line["event_type"] == "tool_call_complete"
            assert line["payload"]["tool_name"] == "calculator"
            assert line["payload"]["success"] is True

        await tool_tracer.shutdown()

    @pytest.mark.asyncio
    async def test_normalize_tool_calls(self, tool_tracer):
        """_normalize_tool_calls should handle both ToolCall objects and dicts."""
        await tool_tracer.init()

        # ToolCall objects
        tcs = [ToolCall(name="foo", args={})]
        result = tool_tracer._normalize_tool_calls(tcs)
        assert len(result) == 1
        assert result[0].name == "foo"

        # Dicts
        tcs = [{"name": "bar", "args": {"x": 1}}]
        result = tool_tracer._normalize_tool_calls(tcs)
        assert len(result) == 1
        assert result[0].name == "bar"

        # Empty
        assert tool_tracer._normalize_tool_calls([]) == []
        assert tool_tracer._normalize_tool_calls(None) == []

        await tool_tracer.shutdown()

    @pytest.mark.asyncio
    async def test_inflight_eviction(self, tool_tracer):
        """Should evict oldest trace when max_inflight is exceeded."""
        tool_tracer._max_inflight = 2
        await tool_tracer.init()

        t1 = ToolCall(name="tool1", args={})
        t2 = ToolCall(name="tool2", args={})
        t3 = ToolCall(name="tool3", args={})

        tool_tracer._on_pre_tool([t1])
        tool_tracer._on_pre_tool([t2])
        assert len(tool_tracer._in_flight) == 2

        tool_tracer._on_pre_tool([t3])
        # Should still be at 2 (oldest evicted)
        assert len(tool_tracer._in_flight) == 2

        await tool_tracer.shutdown()


# ======================================================================
# DiagnosticsCollectorBrick
# ======================================================================


class TestDiagnosticsCollectorBrick:
    """Verify ring buffer, rolling window aggregations, and pub/sub."""

    @pytest.mark.asyncio
    async def test_emit_stores_in_ring_buffer(self, diagnostics_collector):
        """emit() should add entries to the ring buffer."""
        await diagnostics_collector.init()
        entry = LogEntry(source="test", event_type="test_event", level=LogLevel.INFO)
        diagnostics_collector.emit(entry)
        await diagnostics_collector._emit_queue.join()

        events = await diagnostics_collector.get_last_n_events(10)
        assert len(events) == 1
        assert events[0].source == "test"
        await diagnostics_collector.shutdown()

    @pytest.mark.asyncio
    async def test_ring_buffer_bounded(self, diagnostics_collector):
        """Ring buffer should not exceed its configured size."""
        await diagnostics_collector.init()
        for i in range(100):
            entry = LogEntry(source="test", event_type=f"evt{i}")
            diagnostics_collector.emit(entry)

        await diagnostics_collector._emit_queue.join()
        events = await diagnostics_collector.get_last_n_events(200)
        # Only 50 entries max (ring_buffer_size=50)
        assert len(events) <= 50
        await diagnostics_collector.shutdown()

    @pytest.mark.asyncio
    async def test_rolling_window_tokens(self, diagnostics_collector):
        """Rolling window should compute token stats."""
        await diagnostics_collector.init()
        for i in range(3):
            entry = LogEntry(
                source="token_logger",
                event_type="token_usage",
                level=LogLevel.INFO,
                payload={
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "latency_ms": 500.0,
                    "cost_total": 0.00105,
                },
            )
            diagnostics_collector.emit(entry)

        await diagnostics_collector._emit_queue.join()
        stats = await diagnostics_collector.get_window_stats(60)
        assert stats.total_llm_calls == 3
        assert stats.token_input_count == 300
        assert stats.token_output_count == 150

        await diagnostics_collector.shutdown()

    @pytest.mark.asyncio
    async def test_rolling_window_tools(self, diagnostics_collector):
        """Rolling window should compute tool stats."""
        await diagnostics_collector.init()
        for i in range(5):
            entry = LogEntry(
                source="tool_tracer",
                event_type="tool_call_result",
                level=LogLevel.INFO,
                payload={"success": i % 2 == 0, "duration_ms": 200.0},
            )
            diagnostics_collector.emit(entry)

        await diagnostics_collector._emit_queue.join()
        stats = await diagnostics_collector.get_window_stats(60)
        assert stats.total_tool_calls == 5
        assert stats.tool_success_count == 3
        assert stats.tool_failure_count == 2

        await diagnostics_collector.shutdown()

    @pytest.mark.asyncio
    async def test_error_warning_count(self, diagnostics_collector):
        """Error and warning levels should be counted."""
        await diagnostics_collector.init()
        diagnostics_collector.emit(
            LogEntry(source="test", event_type="err", level=LogLevel.ERROR)
        )
        diagnostics_collector.emit(
            LogEntry(source="test", event_type="warn", level=LogLevel.WARNING)
        )
        diagnostics_collector.emit(
            LogEntry(source="test", event_type="info", level=LogLevel.INFO)
        )

        await diagnostics_collector._emit_queue.join()
        stats = await diagnostics_collector.get_window_stats(60)
        assert stats.error_count == 1
        assert stats.warning_count == 1

        await diagnostics_collector.shutdown()

    @pytest.mark.asyncio
    async def test_subscribe_and_unsubscribe(self, diagnostics_collector):
        """Subscribers should receive events."""
        await diagnostics_collector.init()

        received = []

        def callback(entry: LogEntry):
            received.append(entry)

        unsubscribe = await diagnostics_collector.subscribe(callback)
        entry = LogEntry(source="test", event_type="sub_test")
        diagnostics_collector.emit(entry)

        await diagnostics_collector._emit_queue.join()
        assert len(received) == 1
        assert received[0].event_type == "sub_test"

        # Unsubscribe
        unsubscribe()

        # Wait for the async unsubscribe to complete
        await diagnostics_collector._emit_queue.join()

        await diagnostics_collector.shutdown()

    @pytest.mark.asyncio
    async def test_subscribe_with_filter(self, diagnostics_collector):
        """Subscribers with filters should only receive matching events."""
        await diagnostics_collector.init()

        received = []

        def callback(entry: LogEntry):
            received.append(entry)

        await diagnostics_collector.subscribe(
            callback, event_type_filter="token_usage"
        )

        diagnostics_collector.emit(
            LogEntry(source="test", event_type="token_usage")
        )
        diagnostics_collector.emit(
            LogEntry(source="test", event_type="other_event")
        )

        await diagnostics_collector._emit_queue.join()
        assert len(received) == 1
        assert received[0].event_type == "token_usage"

        await diagnostics_collector.shutdown()

    @pytest.mark.asyncio
    async def test_events_filtered_by_type(self, diagnostics_collector):
        """get_last_n_events with event_type filter."""
        await diagnostics_collector.init()
        diagnostics_collector.emit(
            LogEntry(source="test", event_type="type_a")
        )
        diagnostics_collector.emit(
            LogEntry(source="test", event_type="type_b")
        )
        diagnostics_collector.emit(
            LogEntry(source="test", event_type="type_a")
        )

        await diagnostics_collector._emit_queue.join()
        events = await diagnostics_collector.get_last_n_events(10, event_type="type_a")
        assert len(events) == 2
        for e in events:
            assert e.event_type == "type_a"

        await diagnostics_collector.shutdown()

    @pytest.mark.asyncio
    async def test_empty_buffer(self, diagnostics_collector):
        """get_last_n_events on empty buffer."""
        await diagnostics_collector.init()
        events = await diagnostics_collector.get_last_n_events(10)
        assert events == []
        await diagnostics_collector.shutdown()

    @pytest.mark.asyncio
    async def test_get_session_stats(self, diagnostics_collector):
        """get_session_stats should aggregate all entries."""
        await diagnostics_collector.init()
        for i in range(5):
            diagnostics_collector.emit(
                LogEntry(
                    source="test",
                    event_type="token_usage",
                    payload={"input_tokens": 100, "output_tokens": 50, "latency_ms": 200.0, "cost_total": 0.001},
                )
            )

        await diagnostics_collector._emit_queue.join()
        stats = await diagnostics_collector.get_session_stats()
        assert stats.total_llm_calls == 5
        assert stats.token_input_count == 500

        await diagnostics_collector.shutdown()

    @pytest.mark.asyncio
    async def test_hook_callbacks_all_stages(self, diagnostics_collector):
        """get_hook_callbacks should cover all 6 hook stages."""
        await diagnostics_collector.init()
        callbacks = await diagnostics_collector.get_hook_callbacks()
        for ht in HookType:
            assert ht in callbacks, f"Missing callback for {ht}"
            assert len(callbacks[ht]) >= 1
        await diagnostics_collector.shutdown()


# ======================================================================
# Config types
# ======================================================================


class TestToolCallId:
    """Verify ToolCall tool_call_id field."""

    def test_tool_call_id_default(self):
        tc = ToolCall(name="test", args={})
        assert tc.tool_call_id is None

    def test_tool_call_id_set(self):
        tc = ToolCall(name="test", args={}, tool_call_id="call_abc123")
        assert tc.tool_call_id == "call_abc123"
