from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from brikie.bricks.improvement.base import FailureMode, FixAttempt, ImprovementBrick
from brikie.bricks.improvement.fix_strategies import (
    fuzzy_name_match,
    repair_json,
    should_retry,
    validate_args_against_schema,
)
from brikie.config.types import HookType


# ── Mock ToolCall ──────────────────────────────────────────────────────


@dataclass
class _MockToolCall:
    name: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    result: Optional[str] = None
    trace_id: str = ""


# ── FixStrategies tests ────────────────────────────────────────────────


class TestRepairJson:
    def test_repair_trailing_comma(self):
        raw = '{"a": 1, "b": 2,}'
        fixed = repair_json(raw)
        assert fixed is not None
        import json
        assert json.loads(fixed) == {"a": 1, "b": 2}

    def test_repair_single_quotes(self):
        raw = "{'a': 1, 'b': 'hello'}"
        fixed = repair_json(raw)
        assert fixed is not None
        import json
        assert json.loads(fixed) == {"a": 1, "b": "hello"}

    def test_repair_unquoted_keys(self):
        raw = "{a: 1, b: 2}"
        fixed = repair_json(raw)
        assert fixed is not None
        import json
        assert json.loads(fixed) == {"a": 1, "b": 2}

    def test_repair_missing_closing_brace(self):
        raw = '{"a": {"b": 1}'
        fixed = repair_json(raw)
        assert fixed is not None
        import json
        assert json.loads(fixed) == {"a": {"b": 1}}

    def test_repair_code_fence(self):
        raw = "```json\n{\"a\": 1}\n```"
        fixed = repair_json(raw)
        assert fixed is not None
        import json
        assert json.loads(fixed) == {"a": 1}

    def test_repair_already_valid(self):
        raw = '{"a": 1, "b": 2}'
        fixed = repair_json(raw)
        assert fixed == raw

    def test_repair_unfixable_returns_none(self):
        assert repair_json("") is None
        assert repair_json("not json at all really") is None
        assert repair_json(None) is None


class TestValidateArgsAgainstSchema:
    SCHEMA = {
        "type": "object",
        "properties": {
            "expression": {"type": "string"},
            "count": {"type": "integer", "default": 1},
        },
        "required": ["expression"],
    }

    def test_drops_unknown_keys(self):
        fixed, warnings = validate_args_against_schema(
            {"expression": "2+2", "unknown_key": "x"},
            self.SCHEMA,
        )
        assert "unknown_key" not in fixed
        assert any("unknown" in w for w in warnings)

    def test_coerces_types(self):
        fixed, _warnings = validate_args_against_schema(
            {"expression": 42, "count": "3"},
            self.SCHEMA,
        )
        assert isinstance(fixed["expression"], str)
        assert fixed["expression"] == "42"
        assert fixed["count"] == 3

    def test_fills_missing_required_with_default(self):
        fixed, warnings = validate_args_against_schema(
            {"expression": "hello"},
            self.SCHEMA,
        )
        assert fixed["expression"] == "hello"
        assert "count" in fixed
        assert fixed["count"] == 1

    def test_handles_non_dict_args(self):
        fixed, warnings = validate_args_against_schema("not a dict", self.SCHEMA)
        assert fixed == {}
        assert warnings == ["args is not a dict"]


class TestFuzzyNameMatch:
    def test_exact_match(self):
        assert fuzzy_name_match("calculator", ["calculator", "reverse_string"]) == "calculator"

    def test_typo(self):
        assert fuzzy_name_match("calculatr", ["calculator", "reverse_string"]) == "calculator"

    def test_case_insensitive(self):
        assert fuzzy_name_match("CALCULATOR", ["calculator"]) == "calculator"

    def test_no_match_beyond_distance(self):
        assert fuzzy_name_match("completely_different", ["calculator"]) is None

    def test_empty_input(self):
        assert fuzzy_name_match("", ["calculator"]) is None
        assert fuzzy_name_match("calc", []) is None


class TestShouldRetry:
    def test_timeout_is_retryable(self):
        assert should_retry("Connection timed out after 30s") is True

    def test_rate_limit_is_retryable(self):
        assert should_retry("Rate limit exceeded, try again later") is True

    def test_permission_denied_not_retryable(self):
        assert should_retry("Permission denied: /etc/shadow") is False

    def test_no_toolbrick_not_retryable(self):
        assert should_retry("No ToolBrick found for 'fake_tool'") is False

    def test_empty_not_retryable(self):
        assert should_retry("") is False
        assert should_retry(None) is False


# ── ImprovementBrick ABC ───────────────────────────────────────────────


class TestImprovementBrickABC:
    def test_improvement_brick_defaults(self):
        class _MinimalBrick(ImprovementBrick):
            async def _try_fix(self, attempt, tc):
                pass

        brick = _MinimalBrick()
        assert brick.name == "base_improvement"
        assert brick.state.value == "warm_up"

    async def test_improvement_brick_lifecycle(self):
        class _MinimalBrick(ImprovementBrick):
            async def _try_fix(self, attempt, tc):
                pass

        brick = _MinimalBrick()
        await brick.init()
        assert brick.state.value == "active"
        await brick.shutdown()
        assert brick.state.value == "warm_up"

    async def test_hook_callbacks_returns_post_tool_call(self):
        class _MinimalBrick(ImprovementBrick):
            async def _try_fix(self, attempt, tc):
                pass

        brick = _MinimalBrick()
        callbacks = await brick.get_hook_callbacks()
        assert HookType.POST_TOOL_CALL in callbacks
        assert len(callbacks[HookType.POST_TOOL_CALL]) == 1


# ── IsSuccess helper ───────────────────────────────────────────────────


class TestIsSuccess:
    def test_none_is_success(self):
        assert ImprovementBrick._is_success(None) is True

    def test_empty_string_is_success(self):
        assert ImprovementBrick._is_success("") is True

    def test_error_prefix_is_failure(self):
        assert ImprovementBrick._is_success("Error: something broke") is False

    def test_traceback_is_failure(self):
        assert ImprovementBrick._is_success("Traceback (most recent call last)") is False

    def test_no_toolbrick_is_failure(self):
        assert ImprovementBrick._is_success("No ToolBrick found for 'x'") is False

    def test_normal_result_is_success(self):
        assert ImprovementBrick._is_success("42") is True
