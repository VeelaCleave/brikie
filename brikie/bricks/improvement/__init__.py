"""Improvement Bricks — self-healing middleware for the Baseplate.

Improvement Bricks intercept hook stages to automatically detect, fix,
and retry failed operations without consuming LLM round-trips.

ABCs only — no concrete bricks are exported here.
Import concrete bricks directly from their modules:

    from brikie.bricks.improvement.auto_fixer import AutoFixerBrick
"""

from brikie.bricks.improvement.base import ImprovementBrick, FixAttempt, FailureMode

__all__ = [
    "ImprovementBrick",
    "FixAttempt",
    "FailureMode",
]
