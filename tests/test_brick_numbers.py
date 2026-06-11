from __future__ import annotations

from brikie.bricks.improvement.auto_fixer import AutoFixerBrick
from brikie.bricks.interface.cli import CLIBrick
from brikie.bricks.interface.event_bus import InternalEventBusBrick
from brikie.bricks.logging.diagnostics import DiagnosticsCollectorBrick
from brikie.bricks.logging.token_logger import TokenLoggerBrick
from brikie.bricks.logging.tool_tracer import ToolTracerBrick
from brikie.bricks.memory.lcm.lcm_brick import LcmBrick
from brikie.bricks.memory.mempalace.mempalace_brick import MempalaceBrick
from brikie.bricks.memory.wiki.wiki_brick import WikiBrick
from brikie.bricks.provider.http_provider import HTTPProvider
from brikie.bricks.registry.kadeia_installer import KadeiaInstallerBrick
from brikie.bricks.security.firewall import CommandFirewallBrick
from brikie.bricks.security.sandbox import SandboxSecurityBrick
from brikie.bricks.soul.crypto_trading_agent import CryptoTradingAgent
from brikie.bricks.soul.dreamer import Dreamer
from brikie.bricks.soul.sisyphus_orchestrator import SisyphusOrchestrator
from brikie.bricks.soul.web_design_agent import WebDesignAgent
from brikie.bricks.tool.cloakbrowser import CloakBrowserBrick
from brikie.bricks.tool.dummy import DummyToolBrick
from brikie.config.brick_numbers import BRICK_NUMBERS, brick_number, bricks_by_category


# Every concrete brick must have a BRICK_NUMBER class attribute.
# The BRICK_NUMBERS dict must be the single source of truth.
_CONCRETE_BRICKS = [
    (HTTPProvider, "BRK-010"),
    (CLIBrick, "BRK-020"),
    (InternalEventBusBrick, "BRK-021"),
    (DummyToolBrick, "BRK-030"),
    (CloakBrowserBrick, "BRK-031"),
    (KadeiaInstallerBrick, "BRK-032"),
    (SisyphusOrchestrator, "BRK-040"),
    (Dreamer, "BRK-041"),
    (CryptoTradingAgent, "BRK-042"),
    (WebDesignAgent, "BRK-043"),
    (LcmBrick, "BRK-050"),
    (MempalaceBrick, "BRK-051"),
    (WikiBrick, "BRK-052"),
    (TokenLoggerBrick, "BRK-060"),
    (ToolTracerBrick, "BRK-061"),
    (DiagnosticsCollectorBrick, "BRK-062"),
    (CommandFirewallBrick, "BRK-070"),
    (SandboxSecurityBrick, "BRK-071"),
    (AutoFixerBrick, "BRK-080"),
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
        # 19 concrete + 8 ABCs = 27 total
        assert len(BRICK_NUMBERS) == 27

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
