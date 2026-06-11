from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List

from brikie.config.types import BrickState, HookType

logger = logging.getLogger(__name__)


class SecurityDecision(str, Enum):
    """Outcome of a security evaluation."""

    ALLOW = "allow"
    BLOCK = "block"
    SANDBOX = "sandbox"  # Allow but route through sandbox
    ESCALATE = "escalate"  # Needs human review


@dataclass
class BlockedCommand:
    """Record of a blocked tool call for audit logging."""

    tool_name: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    rule_matched: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    session_id: str = ""


class SecurityBrick(abc.ABC):
    """Abstract base class for Security Bricks.

    Security Bricks intercept PRE_TOOL to inspect tool calls before
    execution and decide: ALLOW, BLOCK (with error), SANDBOX (route
    through isolation), or ESCALATE (pause for human review).

    Design invariants:
    1. NEVER allow a blocked command — always return BLOCK.
    2. NEVER modify tool call args in-place (only reject or pass through).
    3. Log every BLOCK decision for audit trails.
    4. Allow rules take precedence over block rules.
    """

    def __init__(self) -> None:
        self._name: str = "base_security"
        self._state: BrickState = BrickState.WARM_UP
        self._blocked_log: List[BlockedCommand] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> BrickState:
        return self._state

    @property
    def blocked_log(self) -> List[BlockedCommand]:
        return list(self._blocked_log)

    async def init(self) -> None:
        self._state = BrickState.ACTIVE
        logger.info("Security brick %s started.", self._name)

    async def shutdown(self) -> None:
        self._state = BrickState.WARM_UP
        logger.info("Security brick %s shut down.", self._name)

    @abc.abstractmethod
    async def evaluate(
        self,
        tool_name: str,
        args: Dict[str, Any],
        session_id: str = "",
    ) -> SecurityDecision:
        """Evaluate whether a tool call is allowed.

        Args:
            tool_name: The canonical tool name being invoked.
            args: The arguments being passed to the tool.
            session_id: Optional session identifier for audit logging.

        Returns:
            SecurityDecision: ALLOW, BLOCK, SANDBOX, or ESCALATE.
        """

    def get_hook_callbacks(self) -> Dict[HookType, List[callable]]:
        """Return PRE_TOOL hook callbacks."""
        async def on_pre_tool(data: Any) -> None:
            await self._on_pre_tool(data)

        return {HookType.PRE_TOOL: [on_pre_tool]}

    async def _on_pre_tool(self, data: Any) -> None:
        """Intercept PRE_TOOL and evaluate each tool call."""
        tool_calls = _normalize_tool_calls(data)
        for tc in tool_calls:
            decision = await self.evaluate(
                tool_name=tc.name if hasattr(tc, "name") else "",
                args=tc.args if hasattr(tc, "args") else {},
            )
            if decision == SecurityDecision.BLOCK:
                self._log_blocked(tc)
                tc.result = self._blocked_error(tc)

    def _log_blocked(self, tc: Any) -> None:
        """Record a blocked command for audit."""
        entry = BlockedCommand(
            tool_name=tc.name if hasattr(tc, "name") else "",
            args=tc.args if hasattr(tc, "args") else {},
            reason=self._block_reason(),
            rule_matched=self._rule_matched(),
        )
        self._blocked_log.append(entry)
        logger.warning(
            "SECURITY BLOCK: %s blocked — %s",
            entry.tool_name, entry.reason,
        )

    @staticmethod
    def _blocked_error(tc: Any) -> str:
        name = tc.name if hasattr(tc, "name") else "unknown"
        return (
            f"Error: Security policy blocked '{name}'. "
            "If you believe this was unexpected, rephrase your request."
        )

    def _block_reason(self) -> str:
        """Override to return a human-readable block reason."""
        return "Blocked by security policy"

    def _rule_matched(self) -> str:
        """Override to return the specific rule that matched."""
        return "default"


def _normalize_tool_calls(data: Any) -> List[Any]:
    """Normalize hook data to a list of tool-call-like objects."""
    if isinstance(data, list):
        return data
    if hasattr(data, "data"):
        inner = data.data
        if isinstance(inner, list):
            return inner
    return []
