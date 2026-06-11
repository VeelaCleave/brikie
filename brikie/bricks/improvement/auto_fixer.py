from __future__ import annotations

import json
import logging
from copy import deepcopy
from typing import Any, Dict, List, Optional

from brikie.bricks.improvement.base import FailureMode, FixAttempt, ImprovementBrick
from brikie.bricks.improvement.fix_strategies import (
    fuzzy_name_match,
    repair_json,
    should_retry,
    validate_args_against_schema,
)
from brikie.kernel.registry import BrickRegistry, ToolBrick

logger = logging.getLogger(__name__)


class AutoFixerBrick(ImprovementBrick):
    BRICK_NUMBER = "BRK-900"
    """Auto Tool-Call Fixing Improvement Brick.

    Hooks POST_TOOL_CALL and attempts to repair failed tool calls
    using a cascade of fix strategies:

    1. FUZZY_NAME — mispelled tool name → correct name & re-execute
    2. JSON_PARSE — malformed arguments JSON → repair & re-execute
    3. SCHEMA — wrong param names/types → coerce & re-execute
    4. RUNTIME_RETRY — transient errors → retry once

    Strategy cascade: try 1 strategy per attempt, max 2 attempts.
    """

    def __init__(self, registry: BrickRegistry) -> None:
        super().__init__()
        self._name = "auto_fixer"
        self._registry = registry
        self._registered_names: List[str] = []
        self._schemas_by_tool: Dict[str, Dict[str, Any]] = {}

    async def init(self) -> None:
        """Index registered tool schemas for validation."""
        self._index_tools()
        await super().init()

    def _index_tools(self) -> None:
        """Build a lookup of all registered tool names and their schemas."""
        names: List[str] = []
        schemas: Dict[str, Dict[str, Any]] = {}

        tools = self._registry.get_all(ToolBrick)
        for brick in tools:
            if hasattr(brick, "tools") and brick.tools is not None:
                for schema in brick.tools:
                    func = schema.get("function", {})
                    name = func.get("name", "")
                    if name:
                        names.append(name)
                        params = func.get("parameters", {})
                        schemas[name] = params

        self._registered_names = names
        self._schemas_by_tool = schemas
        logger.debug("AutoFixer indexed %d tool schemas.", len(names))

    async def _try_fix(self, attempt: FixAttempt, tc: Any) -> None:
        """Attempt to fix a single failed tool call.

        Strategy cascade:
        Attempt 1: classify and apply one fix strategy
        Attempt 2: escalate to a different strategy
        """
        result_text = attempt.original_error or ""

        # --- Attempt 1: classify error ---
        mode, fixed = self._classify_and_fix(
            tc.name, tc.args, result_text,
        )
        attempt.failure_mode = mode

        if mode != FailureMode.NONE and fixed:
            attempt.fixed_args = fixed
            await self._re_execute(tc, attempt)
            if self._is_success(tc.result):
                attempt.success = True
                logger.info(
                    "AutoFix: %s fixed %s (%s) in attempt %d",
                    tc.name, mode.value, attempt.trace_id, attempt.attempts,
                )
                return

        # --- Attempt 2: escalate to different strategy ---
        mode2, fixed2 = self._classify_and_fix(
            tc.name,
            attempt.fixed_args if attempt.fixed_args else tc.args,
            tc.result or attempt.original_error,
            skip_modes={mode},
        )

        if mode2 != FailureMode.NONE and fixed2:
            att = deepcopy(attempt)
            att.failure_mode = mode2
            att.fixed_args = fixed2
            att.attempts += 1
            await self._re_execute(tc, att)
            if self._is_success(tc.result):
                attempt.success = True
                attempt.fixed_args = fixed2
                logger.info(
                    "AutoFix: %s fixed %s (%s) in attempt %d",
                    tc.name, mode2.value, att.trace_id, att.attempts,
                )

    def _classify_and_fix(
        self,
        name: str,
        args: Dict[str, Any],
        error_text: str,
        skip_modes: set[FailureMode] | None = None,
    ) -> tuple[FailureMode, Optional[Dict[str, Any]]]:
        """Classify the failure mode and attempt a fix.

        Returns:
            Tuple of (detected failure mode, fixed_args or None).
        """
        skip = skip_modes or set()

        # 1. Fuzzy name match
        if FailureMode.FUZZY_NAME not in skip:
            matched = fuzzy_name_match(name, self._registered_names)
            if matched is not None and matched != name:
                return FailureMode.FUZZY_NAME, args

        # 2. JSON parse error — args might be a raw string
        args_raw = error_text or str(args)
        if FailureMode.JSON_PARSE_ERROR not in skip:
            fixed_json = repair_json(args_raw)
            if fixed_json is not None:
                try:
                    parsed = json.loads(fixed_json)
                    if isinstance(parsed, dict):
                        return FailureMode.JSON_PARSE_ERROR, parsed
                except (json.JSONDecodeError, ValueError):
                    pass

        # 3. Schema mismatch — validate against known schema
        if FailureMode.SCHEMA_MISMATCH not in skip:
            schema = self._schemas_by_tool.get(name)
            if schema and isinstance(args, dict):
                fixed, _warnings = validate_args_against_schema(args, schema)
                if fixed != args:
                    return FailureMode.SCHEMA_MISMATCH, fixed

        # 4. Runtime retry
        if FailureMode.RUNTIME_RETRY not in skip:
            if should_retry(error_text):
                return FailureMode.RUNTIME_RETRY, args

        return FailureMode.NONE, None

    async def _re_execute(self, tc: Any, attempt: FixAttempt) -> None:
        """Re-execute the tool call with fixed arguments.

        Finds the correct ToolBrick and calls execute(). Mutates
        tc.result and tc.args in-place.
        """
        tools = self._registry.get_all(ToolBrick)

        target_args = attempt.fixed_args

        for brick in tools:
            if hasattr(brick, "tools") and brick.tools is not None:
                names = [
                    s.get("function", {}).get("name", "")
                    for s in brick.tools
                ]
                if tc.name in names:
                    try:
                        tc.args = target_args
                        result = await brick.execute(tc.name, target_args)
                        tc.result = str(result) if result is not None else ""
                        return
                    except Exception as exc:
                        tc.result = f"Error: {exc}"
                        return

        # Fallback: try all bricks
        for brick in tools:
            try:
                tc.args = target_args
                result = await brick.execute(tc.name, target_args)
                tc.result = str(result) if result is not None else ""
                return
            except (KeyError, ValueError):
                continue

        tc.result = f"Error: AutoFix could not find ToolBrick for '{tc.name}'"
