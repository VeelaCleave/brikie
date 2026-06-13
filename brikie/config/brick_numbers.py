"""Brick Number Registry — canonical IDs for every Brick in the system.

Each brick — ABC or concrete — has a unique BRK-NNN identifier.
Numbers are grouped by 100-block category:

    000-099  Infrastructure types (enums, dataclasses, manifests)
    100-199  Kernel ABCs
    200-299  Provider Bricks
    300-399  Interface Bricks
    400-499  Tool Bricks
    500-599  Soul / Identity Bricks
    600-699  Memory Bricks
    700-799  Logging Bricks
    800-899  Security Bricks
    900-999  Improvement Bricks
    1000+    Reserved / Third-party / Ecosystem bricks

Usage:
    from brikie.config.brick_numbers import BRICK_NUMBERS, brick_number

    # Get the number for any class:
    num = brick_number(CloakBrowserBrick)  # returns "BRK-420"

    # Check if a class is a known brick:
    is_brick = cls.__name__ in BRICK_NUMBERS
"""

from typing import Dict, Optional, Type

BRICK_NUMBERS: Dict[str, str] = {
    # ── Kernel ABCs (100-199) ─────────────────────────────────────────
    "ProviderBrick": "BRK-100",
    "InterfaceBrick": "BRK-110",
    "ToolBrick": "BRK-120",
    "MemoryBrick": "BRK-130",
    "LoggingBrick": "BRK-140",
    "ImprovementBrick": "BRK-150",
    "SecurityBrick": "BRK-160",
    "SoulBrick": "BRK-170",

    # ── Provider Bricks (200-299) ─────────────────────────────────────
    "HTTPProvider": "BRK-200",

    # ── Interface Bricks (300-399) ────────────────────────────────────
    "CLIBrick": "BRK-300",
    "InternalEventBusBrick": "BRK-310",
    "TelegramBrick": "BRK-320",
    "DiscordBrick": "BRK-330",

    # ── Tool Bricks (400-499) ─────────────────────────────────────────
    "ShellToolBrick": "BRK-410",
    "CloakBrowserBrick": "BRK-420",
    "GitHubBrick": "BRK-430",
    "MCPClientBrick": "BRK-440",
    "GoalBrick": "BRK-460",
    "RegistryInstallerBrick": "BRK-450",

    # ── Soul Bricks (500-599) ─────────────────────────────────────────
    "Foreman": "BRK-500",
    "Dreamer": "BRK-510",
    "CryptoTradingAgent": "BRK-520",
    "WebDesignAgent": "BRK-530",
    "Mason": "BRK-540",

    # ── Memory Bricks (600-699) ───────────────────────────────────────
    "LcmBrick": "BRK-600",
    "MempalaceBrick": "BRK-610",
    "WikiBrick": "BRK-620",

    # ── Logging Bricks (700-799) ──────────────────────────────────────
    "TokenLoggerBrick": "BRK-700",
    "ToolTracerBrick": "BRK-710",
    "DiagnosticsCollectorBrick": "BRK-720",

    # ── Security Bricks (800-899) ─────────────────────────────────────
    "CommandFirewallBrick": "BRK-800",
    "SandboxSecurityBrick": "BRK-810",

    # ── Improvement Bricks (900-999) ──────────────────────────────────
    "AutoFixerBrick": "BRK-900",
    "LoopDetectorBrick": "BRK-910",
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
        "kernel_abcs": {k: v for k, v in BRICK_NUMBERS.items() if 100 <= int(v[4:]) < 200},
        "providers":  {k: v for k, v in BRICK_NUMBERS.items() if 200 <= int(v[4:]) < 300},
        "interfaces": {k: v for k, v in BRICK_NUMBERS.items() if 300 <= int(v[4:]) < 400},
        "tools":      {k: v for k, v in BRICK_NUMBERS.items() if 400 <= int(v[4:]) < 500},
        "souls":      {k: v for k, v in BRICK_NUMBERS.items() if 500 <= int(v[4:]) < 600},
        "memory":     {k: v for k, v in BRICK_NUMBERS.items() if 600 <= int(v[4:]) < 700},
        "logging":    {k: v for k, v in BRICK_NUMBERS.items() if 700 <= int(v[4:]) < 800},
        "security":   {k: v for k, v in BRICK_NUMBERS.items() if 800 <= int(v[4:]) < 900},
        "improvement": {k: v for k, v in BRICK_NUMBERS.items() if 900 <= int(v[4:]) < 1000},
    }
