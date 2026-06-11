from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List

from brikie.config.types import BrickState, HookType

logger = logging.getLogger(__name__)


class FailureMode(str, Enum):
    """Taxonomy of fixable tool-call failures."""

    NONE = "none"                     # No failure detected
    JSON_PARSE_ERROR = "json_parse"   # LLM sent malformed JSON args
    SCHEMA_MISMATCH = "schema"        # Wrong param names / types
    FUZZY_NAME = "fuzzy_name"         # Typo in tool name (Levenshtein ≤ 2)
    RUNTIME_RETRY = "runtime_retry"   # Transient runtime error, retry once


@dataclass
class FixAttempt:
    """Record of a single auto-fix attempt."""

    trace_id: str = ""
    tool_name: str = ""
    failure_mode: FailureMode = FailureMode.NONE
    original_args: Dict[str, Any] = field(default_factory=dict)
    fixed_args: Dict[str, Any] = field(default_factory=dict)
    original_error: str = ""
    success: bool = False
    attempts: int = 0


class ImprovementBrick(abc.ABC):
    """Abstract base class for Improvement Bricks.

    Improvement Bricks hook into POST_TOOL_CALL to inspect tool-call
    results and attempt automatic repairs before the LLM sees the error.

    Design invariants:
    1. MAX 2 fix attempts per tool call.
    2. NEVER modify a successful result.
    3. NEVER block the main event loop.
    4. Log every fix attempt for diagnostics.
    """

    MAX_FIX_ATTEMPTS = 2

    def __init__(self) -> None:
        self._name: str = "base_improvement"
        self._state: BrickState = BrickState.WARM_UP
        self._attempt_counts: Dict[str, int] = {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> BrickState:
        return self._state

    async def init(self) -> None:
        self._state = BrickState.ACTIVE
        logger.info("Improvement brick %s started.", self._name)

    async def shutdown(self) -> None:
        self._state = BrickState.WARM_UP
        self._attempt_counts.clear()
        logger.info("Improvement brick %s shut down.", self._name)

    async def get_hook_callbacks(self) -> Dict[HookType, List[callable]]:
        """Return the hook callbacks this brick should register.

        By default, Improvement Bricks hook POST_TOOL_CALL.
        """
        async def on_post_tool_call(data: Any) -> None:
            await self._on_post_tool_call(data)

        return {HookType.POST_TOOL_CALL: [on_post_tool_call]}

    async def _on_post_tool_call(self, data: Any) -> None:
        """Intercept POST_TOOL_CALL, inspect results, and fix failures."""
        tool_calls = self._normalize_tool_calls(data)
        for tc in tool_calls:
            if self._is_success(tc.result):
                continue

            key = self._trace_key(tc)
            if self._attempt_counts.get(key, 0) >= self.MAX_FIX_ATTEMPTS:
                logger.debug("Max fix attempts reached for %s", tc.name)
                continue

            self._attempt_counts[key] = self._attempt_counts.get(key, 0) + 1
            attempt = FixAttempt(
                trace_id=getattr(tc, "trace_id", ""),
                tool_name=tc.name,
                original_args=tc.args,
                original_error=tc.result or "",
                attempts=self._attempt_counts[key],
            )

            await self._try_fix(attempt, tc)

    @abc.abstractmethod
    async def _try_fix(self, attempt: FixAttempt, tc: Any) -> None:
        """Attempt to fix a failed tool call.

        If successful, mutate tc.result and tc.args in-place.
        """

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _is_success(result: str | None) -> bool:
        if not result:
            return True
        lowered = result.lower().strip()
        if lowered.startswith("error:") or "traceback" in lowered:
            return False
        if lowered.startswith("no toolbrick found"):
            return False
        return True

    @staticmethod
    def _trace_key(tc: Any) -> str:
        trace_id = getattr(tc, "trace_id", None)
        if trace_id:
            return trace_id
        return f"{tc.name}:{str(tc.args)[:50]}"

    @staticmethod
    def _normalize_tool_calls(data: Any) -> List[Any]:
        if isinstance(data, list):
            return data
        if hasattr(data, "data"):
            inner = data.data
            if isinstance(inner, list):
                return inner
        return []
