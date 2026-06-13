from __future__ import annotations

from brikie.bricks.improvement.auto_fixer import AutoFixerBrick
from brikie.bricks.interface.cli import CLIBrick
from brikie.bricks.interface.event_bus import InternalEventBusBrick
from brikie.bricks.interface.telegram import TelegramBrick
from brikie.bricks.interface.discord_iface import DiscordBrick
from brikie.bricks.logging.diagnostics import DiagnosticsCollectorBrick
from brikie.bricks.logging.token_logger import TokenLoggerBrick
from brikie.bricks.logging.tool_tracer import ToolTracerBrick
from brikie.bricks.memory.lcm.lcm_brick import LcmBrick
from brikie.bricks.memory.mempalace.mempalace_brick import MempalaceBrick
from brikie.bricks.memory.wiki.wiki_brick import WikiBrick
from brikie.bricks.provider.http_provider import HTTPProvider
from brikie.bricks.registry.installer import RegistryInstallerBrick
from brikie.bricks.security.firewall import CommandFirewallBrick
from brikie.bricks.security.sandbox import SandboxSecurityBrick
from brikie.bricks.security.watchdog import WatchdogSecurityBrick
from brikie.bricks.soul.crypto_trading_agent import CryptoTradingAgent
from brikie.bricks.soul.dreamer import Dreamer
from brikie.bricks.soul.foreman import Foreman
from brikie.bricks.soul.mason import Mason
from brikie.bricks.soul.web_design_agent import WebDesignAgent
from brikie.bricks.tool.cloakbrowser import CloakBrowserBrick
from brikie.bricks.tool.file_tools import ShellToolBrick
from brikie.bricks.tool.github_tools import GitHubBrick
from brikie.bricks.tool.mcp_client import MCPClientBrick
from brikie.bricks.tool.goals.goal_brick import GoalBrick
from brikie.config.brick_numbers import BRICK_NUMBERS, brick_number, bricks_by_category


# Every concrete brick must have a BRICK_NUMBER class attribute.
# The BRICK_NUMBERS dict must be the single source of truth.
_CONCRETE_BRICKS = [
    (HTTPProvider, "BRK-200"),
    (CLIBrick, "BRK-300"),
    (InternalEventBusBrick, "BRK-310"),
    (TelegramBrick, "BRK-320"),
    (DiscordBrick, "BRK-330"),
    (ShellToolBrick, "BRK-410"),
    (CloakBrowserBrick, "BRK-420"),
    (GitHubBrick, "BRK-430"),
    (MCPClientBrick, "BRK-440"),
    (GoalBrick, "BRK-460"),
    (RegistryInstallerBrick, "BRK-450"),
    (Foreman, "BRK-500"),
    (Dreamer, "BRK-510"),
    (CryptoTradingAgent, "BRK-520"),
    (WebDesignAgent, "BRK-530"),
    (Mason, "BRK-540"),
    (LcmBrick, "BRK-600"),
    (MempalaceBrick, "BRK-610"),
    (WikiBrick, "BRK-620"),
    (TokenLoggerBrick, "BRK-700"),
    (ToolTracerBrick, "BRK-710"),
    (DiagnosticsCollectorBrick, "BRK-720"),
    (CommandFirewallBrick, "BRK-800"),
    (SandboxSecurityBrick, "BRK-810"),
    (WatchdogSecurityBrick, "BRK-820"),
    (AutoFixerBrick, "BRK-900"),
]


class TestBrickNumbers:
    def test_all_concrete_bricks_have_brk_number(self):
        """Every concrete brick must define BRICK_NUMBER matching the registry."""
        for cls, expected in _CONCRETE_BRICKS:
            assert hasattr(cls, "BRICK_NUMBER"), f"{cls.__name__} missing BRICK_NUMBER"
            assert cls.BRICK_NUMBER == expected, (
                f"{cls.__name__}.BRICK_NUMBER is {cls.BRICK_NUMBER}, expected {expected}"
            )

    def test_brick_number_function(self):
        """brick_number() lookup works for all concrete bricks."""
        for cls, expected in _CONCRETE_BRICKS:
            assert brick_number(cls) == expected

    def test_brick_number_none_for_unknown(self):
        """brick_number() returns None for non-brick classes."""
        class NotABrick:
            pass
        assert brick_number(NotABrick) is None

    def test_registry_count(self):
        """BRICK_NUMBERS dict covers all concrete bricks + ABCs."""
        assert len(BRICK_NUMBERS) == 35

    def test_all_numbers_have_brk_prefix(self):
        """Every entry should start with BRK-."""
        for name, num in BRICK_NUMBERS.items():
            assert num.startswith("BRK-"), f"{name} has invalid number: {num}"

    def test_all_numbers_are_unique(self):
        """No duplicate numbers."""
        numbers = list(BRICK_NUMBERS.values())
        assert len(numbers) == len(set(numbers)), "Duplicate BRK numbers found"

    def test_bricks_by_category_returns_all(self):
        """bricks_by_category() should include every registered brick."""
        categories = bricks_by_category()
        all_bricks = {}
        for cat, bricks in categories.items():
            all_bricks.update(bricks)
        assert set(all_bricks.keys()) == set(BRICK_NUMBERS.keys())
