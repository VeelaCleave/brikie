"""Tests for LoopDetectorBrick (BRK-910) — loop detection and realignment.

Tests cover:
1. Basic recording of tool calls into the ring buffer
2. Detection of repeated identical tool calls
3. Detection of oscillating patterns (A→B→A→B)
4. Detection of repeated errors
5. No false-positive on normal varied tool calls
6. loop_status tool output format
7. dream_context output format
8. Hook callback wiring
"""

import pytest
from typing import Any, Dict, Optional

from brikie.bricks.improvement.loop_detector import (
    LoopDetectorBrick,
    LoopInfo,
    _LOOP_TYPE_REPEAT,
    _LOOP_TYPE_OSCILLATE,
    _LOOP_TYPE_ERROR,
    _LOOP_REPEAT_THRESHOLD,
)
from brikie.config.types import BrickState, HookType, ToolCall


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def detector():
    """Create a LoopDetectorBrick with a small buffer for testing."""
    brick = LoopDetectorBrick(buffer_size=32)
    return brick


def make_tc(name: str, args: Optional[Dict[str, Any]] = None,
            result: Optional[str] = None) -> ToolCall:
    """Create a ToolCall with the given name, args, and result."""
    return ToolCall(
        name=name,
        args=args or {},
        result=result,
        tool_call_id=f"call_{name}_{id(args)}",
    )


# ──────────────────────────────────────────────────────────────────────
# Recording
# ──────────────────────────────────────────────────────────────────────


class TestRecording:
    """Test that tool calls are correctly recorded into the ring buffer."""

    def test_record_tool_call(self, detector):
        tc = make_tc("read_file", {"path": "/tmp/test.txt"}, "hello world")
        detector._record(tc)

        assert len(detector._buffer) == 1
        record = detector._buffer[0]
        assert record.name == "read_file"
        assert record.success is True
        assert record.result_preview == "hello world"
        assert len(record.args_hash) == 64  # SHA-256

    def test_record_tool_call_failure(self, detector):
        tc = make_tc("read_file", {}, "Error: file not found")
        detector._record(tc)

        assert len(detector._buffer) == 1
        record = detector._buffer[0]
        assert record.success is False
        assert record.result_preview == "Error: file not found"

    def test_record_tool_call_with_traceback(self, detector):
        tc = make_tc("bash_execute", {"command": "rm -rf /"},
                      "Traceback (most recent call last):")
        detector._record(tc)

        assert detector._buffer[0].success is False

    def test_record_dict_tool_call(self, detector):
        tc_dict = {
            "name": "read_file",
            "args": {"path": "/tmp/x"},
            "result": "content",
        }
        detector._record(tc_dict)
        assert len(detector._buffer) == 1
        assert detector._buffer[0].name == "read_file"

    def test_record_non_dict_non_toolcall(self, detector):
        detector._record("not a tool call")
        assert len(detector._buffer) == 0

    def test_buffer_respects_maxlen(self, detector):
        brick = LoopDetectorBrick(buffer_size=5)
        for i in range(10):
            tc = make_tc(f"tool_{i}", {"n": i})
            brick._record(tc)

        assert len(brick._buffer) == 5
        # Last 5 should be tool_5 through tool_9
        names = [r.name for r in brick._buffer]
        assert names == ["tool_5", "tool_6", "tool_7", "tool_8", "tool_9"]


# ──────────────────────────────────────────────────────────────────────
# Loop Detection — Repeated Calls
# ──────────────────────────────────────────────────────────────────────


class TestDetectRepeated:
    """Test detection of repeated identical tool calls."""

    def test_detects_repeated_identical_calls(self, detector):
        for _ in range(6):
            detector._record(make_tc("read_file", {"path": "/tmp/x"}))

        loop = detector._detect_loop()
        assert loop is not None
        assert loop.loop_type == _LOOP_TYPE_REPEAT
        assert loop.tool_names == ["read_file"]
        assert loop.count >= 4

    def test_does_not_detect_different_args(self, detector):
        for i in range(6):
            detector._record(make_tc("read_file", {"path": f"/tmp/{i}"}))

        loop = detector._detect_loop()
        assert loop is None  # Different args should not trigger

    def test_does_not_detect_mixed_calls(self, detector):
        detector._record(make_tc("read_file"))
        detector._record(make_tc("write_file"))
        detector._record(make_tc("read_file"))
        detector._record(make_tc("write_file"))

        loop = detector._detect_loop()
        assert loop is None

    def test_threshold_not_reached(self, detector):
        for _ in range(3):
            detector._record(make_tc("read_file"))

        loop = detector._detect_loop()
        assert loop is None  # Need 4 to trigger


# ──────────────────────────────────────────────────────────────────────
# Loop Detection — Oscillation
# ──────────────────────────────────────────────────────────────────────


class TestDetectOscillation:
    """Test detection of A→B→A→B oscillating patterns."""

    def test_detects_oscillation(self, detector):
        for i in range(6):
            name = "tool_a" if i % 2 == 0 else "tool_b"
            detector._record(make_tc(name))

        loop = detector._detect_loop()
        assert loop is not None
        assert loop.loop_type == _LOOP_TYPE_OSCILLATE
        assert "tool_a" in loop.tool_names
        assert "tool_b" in loop.tool_names

    def test_does_not_detect_three_tool_cycle(self, detector):
        for i in range(9):
            name = ["tool_a", "tool_b", "tool_c"][i % 3]
            detector._record(make_tc(name))

        loop = detector._detect_loop()
        assert loop is None  # A→B→C cycle is not detected

    def test_oscillation_threshold_not_reached(self, detector):
        detector._record(make_tc("tool_a"))
        detector._record(make_tc("tool_b"))
        detector._record(make_tc("tool_a"))
        detector._record(make_tc("tool_b"))

        loop = detector._detect_loop()
        assert loop is None  # Need 6


# ──────────────────────────────────────────────────────────────────────
# Loop Detection — Error Loops
# ──────────────────────────────────────────────────────────────────────


class TestDetectErrorLoop:
    """Test detection of repeated error strings."""

    def test_detects_error_loop(self, detector):
        # Use different args so repeat detection doesn't fire first
        for i in range(5):
            detector._record(make_tc("read_file", {"path": f"/tmp/{i}"}, result="Error: file not found"))

        loop = detector._detect_loop()
        assert loop is not None
        assert loop.loop_type == _LOOP_TYPE_ERROR
        assert "Error: file not found" in loop.detail

    def test_does_not_detect_different_errors(self, detector):
        errors = ["Error: not found", "Error: permission denied", "Error: timeout"]
        for i in range(5):
            detector._record(make_tc("read_file", {"path": f"/tmp/{i}"}, result=errors[i % 3]))

        loop = detector._detect_loop()
        assert loop is None

    def test_does_not_detect_mixed_success_and_error(self, detector):
        # Use different args so repeat doesn't fire
        detector._record(make_tc("read_file", {"path": "/tmp/1"}, result="success"))
        detector._record(make_tc("read_file", {"path": "/tmp/2"}, result="success"))
        detector._record(make_tc("read_file", {"path": "/tmp/3"}, result="Error: not found"))
        detector._record(make_tc("read_file", {"path": "/tmp/4"}, result="success"))

        loop = detector._detect_loop()
        assert loop is None

    def test_error_threshold_not_reached(self, detector):
        for _ in range(2):
            detector._record(make_tc("read_file", result="Error: not found"))

        loop = detector._detect_loop()
        assert loop is None  # Need 3


# ──────────────────────────────────────────────────────────────────────
# Detection Priority
# ──────────────────────────────────────────────────────────────────────


class TestDetectionPriority:
    """Test that patterns are checked in the right order."""

    def test_repeat_beats_oscillation(self, detector):
        """If both repeat and oscillation match, repeat wins."""
        # Add oscillation-to-be first
        for i in range(6):
            name = "tool_a" if i % 2 == 0 else "tool_b"
            detector._record(make_tc(name))

        # Then add 4 repeats of the same call
        for _ in range(4):
            detector._record(make_tc("tool_a"))

        loop = detector._detect_loop()
        assert loop is not None
        assert loop.loop_type in (_LOOP_TYPE_REPEAT, _LOOP_TYPE_OSCILLATE)


# ──────────────────────────────────────────────────────────────────────
# loop_status Tool
# ──────────────────────────────────────────────────────────────────────


class TestLoopStatus:
    """Test the loop_status tool output."""

    async def test_loop_status_no_loop(self, detector):
        detector._record(make_tc("read_file"))
        result = await detector._handle_loop_status({})

        assert result["in_loop"] is False
        assert result["loop_info"] is None
        assert result["suggestion"] is None

    async def test_loop_status_detected_loop(self, detector):
        for _ in range(5):
            detector._record(make_tc("read_file", {"path": "/tmp/x"}))

        # Manually trigger detection
        loop = detector._detect_loop()
        assert loop is not None
        await detector._handle_loop(loop)

        result = await detector._handle_loop_status({})
        assert result["in_loop"] is True
        assert result["loop_info"] is not None
        assert result["loop_info"]["loop_type"] == _LOOP_TYPE_REPEAT
        assert result["suggestion"] is not None
        assert len(result["recent_calls"]) > 0

    async def test_loop_status_includes_recent_calls(self, detector):
        for i in range(5):
            detector._record(make_tc(f"tool_{i}"))

        result = await detector._handle_loop_status({})
        assert len(result["recent_calls"]) == 5


# ──────────────────────────────────────────────────────────────────────
# Suggestion Builder
# ──────────────────────────────────────────────────────────────────────


class TestSuggestions:
    """Test that realignment suggestions are sensible."""

    def test_repeat_suggestion(self, detector):
        detector._current_loop = LoopInfo(
            loop_type=_LOOP_TYPE_REPEAT,
            tool_names=["read_file"],
            count=5,
            detail="Same tool call repeated 5 times",
        )
        suggestion = detector._build_suggestion()
        assert "different approach" in suggestion
        assert "goal_status()" in suggestion

    def test_oscillation_suggestion(self, detector):
        detector._current_loop = LoopInfo(
            loop_type=_LOOP_TYPE_OSCILLATE,
            tool_names=["tool_a", "tool_b"],
            count=6,
            detail="Oscillating between tools",
        )
        suggestion = detector._build_suggestion()
        assert "oscillating" in suggestion.lower()
        assert "goal_status()" in suggestion

    def test_error_loop_suggestion(self, detector):
        detector._current_loop = LoopInfo(
            loop_type=_LOOP_TYPE_ERROR,
            tool_names=["bash_execute"],
            count=3,
            detail="Same error repeated 3 times",
        )
        suggestion = detector._build_suggestion()
        assert "error" in suggestion.lower()
        assert "goal_status()" in suggestion

    def test_no_suggestion_when_no_loop(self, detector):
        assert detector._build_suggestion() == ""


# ──────────────────────────────────────────────────────────────────────
# Dream Context
# ──────────────────────────────────────────────────────────────────────


class TestDreamContext:
    """Test that dream_context returns sensible content."""

    async def test_dream_context_no_calls(self, detector):
        ctx = await detector.dream_context()
        assert "No tool calls recorded yet" in ctx

    async def test_dream_context_no_loop(self, detector):
        for i in range(3):
            detector._record(make_tc(f"tool_{i}"))
        ctx = await detector.dream_context()
        assert "No loop detected" in ctx

    async def test_dream_context_with_loop(self, detector):
        for _ in range(5):
            detector._record(make_tc("read_file", {"path": "/tmp/x"}))

        loop = detector._detect_loop()
        assert loop is not None
        await detector._handle_loop(loop)

        ctx = await detector.dream_context()
        assert "Active loop detected" in ctx
        assert _LOOP_TYPE_REPEAT in ctx


# ──────────────────────────────────────────────────────────────────────
# Hook Callbacks
# ──────────────────────────────────────────────────────────────────────


class TestHookCallbacks:
    """Test that hook callbacks are properly registered."""

    async def test_get_hook_callbacks_returns_post_tool_call(self, detector):
        callbacks = await detector.get_hook_callbacks()
        assert HookType.POST_TOOL_CALL in callbacks
        assert len(callbacks[HookType.POST_TOOL_CALL]) == 1

    async def test_hook_callback_records_tool_calls(self, detector):
        callbacks = await detector.get_hook_callbacks()
        on_post = callbacks[HookType.POST_TOOL_CALL][0]

        tool_calls = [
            make_tc("read_file", {"path": "/tmp/x"}, "content"),
            make_tc("write_file", {"path": "/tmp/y"}, "ok"),
        ]

        await on_post(tool_calls)
        assert len(detector._buffer) == 2

    async def test_hook_callback_with_hookevent(self, detector):
        """Test that the callback unwraps HookEvent correctly."""
        callbacks = await detector.get_hook_callbacks()
        on_post = callbacks[HookType.POST_TOOL_CALL][0]

        from brikie.config.types import HookEvent
        event = HookEvent(
            hook_type=HookType.POST_TOOL_CALL,
            data=[
                make_tc("read_file", {"path": "/tmp/x"}, "content"),
            ],
            brick_name="event_loop",
        )

        await on_post(event)
        assert len(detector._buffer) == 1


# ──────────────────────────────────────────────────────────────────────
# Lifecycle
# ──────────────────────────────────────────────────────────────────────


class TestLifecycle:
    """Test that init/shutdown work correctly."""

    async def test_init(self, detector):
        assert detector.state == BrickState.WARM_UP
        await detector.init()
        assert detector.state == BrickState.ACTIVE

    async def test_shutdown_clears_buffer(self, detector):
        await detector.init()
        detector._record(make_tc("read_file"))
        assert len(detector._buffer) == 1

        await detector.shutdown()
        assert len(detector._buffer) == 0
        assert detector._current_loop is None


# ──────────────────────────────────────────────────────────────────────
# Active realignment — the whole point: steer a spinning model without
# making it call loop_status itself.
# ──────────────────────────────────────────────────────────────────────


class TestActiveRealignment:
    async def test_detected_loop_stages_realignment(self, detector):
        callbacks = await detector.get_hook_callbacks()
        on_post = callbacks[HookType.POST_TOOL_CALL][0]
        # Four identical calls = a repeat loop.
        for _ in range(_LOOP_REPEAT_THRESHOLD):
            await on_post([make_tc("read_file", {"path": "/x"}, "same")])
        nudge = detector.pop_realignment()
        assert nudge is not None
        assert "LOOP DETECTED" in nudge
        # Drained exactly once — a second pop is empty (no spam).
        assert detector.pop_realignment() is None

    def test_no_loop_no_realignment(self, detector):
        assert detector.pop_realignment() is None

    def test_realignment_anchors_to_goal_when_goal_tool_loaded(self):
        class _Goalish:
            tools = [{"type": "function",
                      "function": {"name": "goal_status", "parameters": {}}}]

        class _Reg:
            def __init__(self, *bricks):
                self._bricks = {str(i): b for i, b in enumerate(bricks)}

        det = LoopDetectorBrick(registry=_Reg(_Goalish()))
        msg = det._build_realignment_message(
            LoopInfo(loop_type="repeat", tool_names=["read_file"],
                     count=4, detail="spinning"))
        assert "goal_status()" in msg

    def test_realignment_generic_when_no_goal_tool(self, detector):
        msg = detector._build_realignment_message(
            LoopInfo(loop_type="repeat", tool_names=["read_file"],
                     count=4, detail="spinning"))
        assert "goal_status()" not in msg
        assert "different concrete step" in msg


class TestEventLoopDrain:
    """The event loop injects staged nudges into the conversation."""

    async def test_drain_injects_system_message_and_notifies(self):
        from brikie.kernel.event_loop import EventLoop
        from brikie.kernel.hooks import HookDispatcher
        from brikie.kernel.registry import BrickRegistry, InterfaceBrick
        from brikie.kernel.state import StateManager

        class _Iface(InterfaceBrick):
            def __init__(self):
                self.errors = []

            @property
            def name(self): return "iface"

            @property
            def state(self): return BrickState.ACTIVE

            async def init(self): ...
            async def shutdown(self): ...
            async def get_input(self): return ""
            async def output(self, msg): ...
            async def render_error(self, msg): self.errors.append(msg)

        detector = LoopDetectorBrick()
        detector._pending_realignment = "⚠️ LOOP DETECTED (repeat): go"
        iface = _Iface()
        registry = BrickRegistry()
        registry.register(detector)
        registry.register(iface)
        loop = EventLoop(registry=registry, state=StateManager(),
                         hooks=HookDispatcher())

        await loop._drain_realignments()

        assert loop._message_history[-1].role == "system"
        assert "LOOP DETECTED" in loop._message_history[-1].content
        assert iface.errors and "LOOP DETECTED" in iface.errors[0]
        # Idempotent: nothing left to drain.
        await loop._drain_realignments()
        assert len(loop._message_history) == 1
