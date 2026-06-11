"""Logging Bricks — diagnostics, token accounting, and call tracing.

Logging Bricks are passive observers that hook into the middleware pipeline
to record internal state, LLM token usage, tool-call lifecycles, and event
bus diagnostics for post-hoc analysis by Improvement Bricks and the Dreamer.

ABCs only — no concrete bricks are exported here.
Import concrete bricks directly from their modules:

    from brikie.bricks.logging.token_logger import TokenLoggerBrick
    from brikie.bricks.logging.tool_tracer import ToolTracerBrick
    from brikie.bricks.logging.diagnostics import DiagnosticsCollectorBrick
"""

from brikie.bricks.logging.base import LoggingBrick, LogEntry, LogLevel, LogEvent

__all__ = [
    "LoggingBrick",
    "LogEntry",
    "LogLevel",
    "LogEvent",
]
