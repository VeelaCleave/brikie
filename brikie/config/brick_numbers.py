"""Brick Number Registry — canonical IDs for every Brick in the system.

Each brick — ABC or concrete — has a unique BRK-NNN identifier.
Numbers are grouped by category for readability:

    001-009  Kernel ABCs
    010-019  Provider Bricks
    020-029  Interface Bricks
    030-039  Tool Bricks
    040-049  Soul Bricks
    050-059  Memory Bricks
    060-069  Logging Bricks
    070-079  Security Bricks
    080-089  Improvement Bricks
    090-099  Registry / Ecosystem Bricks
    100+     Reserved / Third-party

Usage:
    from brikie.config.brick_numbers import BRICK_NUMBERS, brick_number

    # Get the number for any class:
    num = brick_number(CloakBrowserBrick)  # returns "BRK-032"

    # Check if a class is a known brick:
    is_brick = cls.__name__ in BRICK_NUMBERS
"""

from typing import Dict, Optional, Type

BRICK_NUMBERS: Dict[str, str] = {
    # ── Kernel ABCs (001-009) ─────────────────────────────────────────
    "ProviderBrick": "BRK-001",
    "InterfaceBrick": "BRK-002",
    "ToolBrick": "BRK-003",
    "MemoryBrick": "BRK-004",
    "LoggingBrick": "BRK-005",
    "ImprovementBrick": "BRK-006",
    "SecurityBrick": "BRK-007",
    "SoulBrick": "BRK-008",
    # 009 reserved

    # ── Provider Bricks (010-019) ─────────────────────────────────────
    "HTTPProvider": "BRK-010",

    # ── Interface Bricks (020-029) ────────────────────────────────────
    "CLIBrick": "BRK-020",
    "InternalEventBusBrick": "BRK-021",

    # ── Tool Bricks (030-039) ─────────────────────────────────────────
    "DummyToolBrick": "BRK-030",
    "CloakBrowserBrick": "BRK-031",
    "KadeiaInstallerBrick": "BRK-032",

    # ── Soul Bricks (040-049) ─────────────────────────────────────────
    "SisyphusOrchestrator": "BRK-040",
    "Dreamer": "BRK-041",
    "CryptoTradingAgent": "BRK-042",
    "WebDesignAgent": "BRK-043",

    # ── Memory Bricks (050-059) ───────────────────────────────────────
    "LcmBrick": "BRK-050",
    "MempalaceBrick": "BRK-051",
    "WikiBrick": "BRK-052",

    # ── Logging Bricks (060-069) ──────────────────────────────────────
    "TokenLoggerBrick": "BRK-060",
    "ToolTracerBrick": "BRK-061",
    "DiagnosticsCollectorBrick": "BRK-062",

    # ── Security Bricks (070-079) ─────────────────────────────────────
    "CommandFirewallBrick": "BRK-070",
    "SandboxSecurityBrick": "BRK-071",

    # ── Improvement Bricks (080-089) ──────────────────────────────────
    "AutoFixerBrick": "BRK-080",
}


def brick_number(cls: Type) -> Optional[str]:
    """Return the BRK-NNN identifier for a brick class.

    Args:
        cls: A brick class (ABC or concrete).

    Returns:
        The BRK-NNN string, or None if the class is not a registered brick.
    """
    return BRICK_NUMBERS.get(cls.__name__)


def bricks_by_category() -> Dict[str, Dict[str, str]]:
    """Group registered bricks by category for manifest generation.

    Returns:
        Dict mapping category labels to dicts of {class_name: BRK-NNN}.
    """
    return {
        "kernel_abcs": {
            k: v for k, v in BRICK_NUMBERS.items()
            if v in {f"BRK-{i:03d}" for i in range(1, 10)}
        },
        "providers": {
            k: v for k, v in BRICK_NUMBERS.items()
            if v in {f"BRK-{i:03d}" for i in range(10, 20)}
        },
        "interfaces": {
            k: v for k, v in BRICK_NUMBERS.items()
            if v in {f"BRK-{i:03d}" for i in range(20, 30)}
        },
        "tools": {
            k: v for k, v in BRICK_NUMBERS.items()
            if v in {f"BRK-{i:03d}" for i in range(30, 40)}
        },
        "souls": {
            k: v for k, v in BRICK_NUMBERS.items()
            if v in {f"BRK-{i:03d}" for i in range(40, 50)}
        },
        "memory": {
            k: v for k, v in BRICK_NUMBERS.items()
            if v in {f"BRK-{i:03d}" for i in range(50, 60)}
        },
        "logging": {
            k: v for k, v in BRICK_NUMBERS.items()
            if v in {f"BRK-{i:03d}" for i in range(60, 70)}
        },
        "security": {
            k: v for k, v in BRICK_NUMBERS.items()
            if v in {f"BRK-{i:03d}" for i in range(70, 80)}
        },
        "improvement": {
            k: v for k, v in BRICK_NUMBERS.items()
            if v in {f"BRK-{i:03d}" for i in range(80, 89)}
        },
        "ecosystem": {
            k: v for k, v in BRICK_NUMBERS.items()
            if v in {f"BRK-{i:03d}" for i in range(90, 100)}
        },
    }
