"""Shared types, enums, and Pydantic models for the Brikie Baseplate."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class BrickState(Enum):
    """Lifecycle states for a Brick module."""
    WARM_UP = "warm_up"
    ACTIVE = "active"


class HookType(Enum):
    """Middleware hook stages in the Baseplate event loop.

    Execution order is linear: PRE_PARSE → PRE_LLM → POST_LLM → PRE_TOOL → POST_TOOL → POST_TOOL_CALL
    """
    PRE_PARSE = "pre_parse"
    PRE_LLM = "pre_llm"
    POST_LLM = "post_llm"
    PRE_TOOL = "pre_tool"
    POST_TOOL = "post_tool"
    POST_TOOL_CALL = "post_tool_call"


@dataclass
class Message:
    """Standardized message object flowing through the Baseplate pipeline.

    Attributes:
        role: Origin of the message (e.g., 'user', 'assistant', 'tool', 'system').
        content: The text payload.
        tool_call_id: Optional identifier linking this message to a specific tool call.
    """
    role: str
    content: str
    tool_call_id: Optional[str] = field(default=None)


@dataclass
class ToolCall:
    """Represents a single tool invocation or result.

    Attributes:
        name: The canonical tool name (e.g., 'calculator', 'cloak_browser').
        args: Key-value arguments passed to the tool.
        result: The return value after execution (set after the tool runs).
        trace_id: Optional UUID for correlating tool calls across middleware hooks.
    """
    name: str
    args: Dict[str, Any]
    result: Optional[str] = field(default=None)
    trace_id: Optional[str] = field(default=None)


@dataclass
class HookEvent:
    """Event dispatched through the middleware hook pipeline.

    Attributes:
        hook_type: Which stage of the lifecycle this event belongs to.
        data: Arbitrary payload carried by the event.
        brick_name: Source or target Brick that generated/receives the event.
    """
    hook_type: HookType
    data: Any
    brick_name: str
