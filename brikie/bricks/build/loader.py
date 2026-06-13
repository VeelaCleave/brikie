from __future__ import annotations

import importlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from brikie.kernel.registry import BrickRegistry

logger = logging.getLogger(__name__)

# Maps BRK numbers to their dotted module paths.
# Every brick in the system is registered here so the loader can find it
# without scanning the filesystem.
BRICK_INDEX: Dict[str, str] = {
    # ── Kernel ABCs (100-199) ─────────────────────────────────────────
    "BRK-100": "brikie.kernel.registry.ProviderBrick",
    "BRK-110": "brikie.kernel.registry.InterfaceBrick",
    "BRK-120": "brikie.kernel.registry.ToolBrick",
    "BRK-130": "brikie.bricks.memory.memory_brick.MemoryBrick",
    "BRK-140": "brikie.bricks.logging.base.LoggingBrick",
    "BRK-150": "brikie.bricks.improvement.base.ImprovementBrick",
    "BRK-160": "brikie.bricks.security.base.SecurityBrick",
    "BRK-170": "brikie.bricks.soul.base.SoulBrick",

    # ── Provider Bricks (200-299) ─────────────────────────────────────
    "BRK-200": "brikie.bricks.provider.http_provider.HTTPProvider",

    # ── Interface Bricks (300-399) ────────────────────────────────────
    "BRK-300": "brikie.bricks.interface.cli.CLIBrick",
    "BRK-310": "brikie.bricks.interface.event_bus.InternalEventBusBrick",
    "BRK-320": "brikie.bricks.interface.telegram.TelegramBrick",
    "BRK-330": "brikie.bricks.interface.discord_iface.DiscordBrick",

    # ── Tool Bricks (400-499) ─────────────────────────────────────────
    "BRK-410": "brikie.bricks.tool.file_tools.ShellToolBrick",
    "BRK-420": "brikie.bricks.tool.cloakbrowser.CloakBrowserBrick",
    "BRK-430": "brikie.bricks.tool.github_tools.GitHubBrick",
    "BRK-440": "brikie.bricks.tool.mcp_client.MCPClientBrick",
    "BRK-450": "brikie.bricks.registry.installer.RegistryInstallerBrick",
    "BRK-460": "brikie.bricks.tool.goals.goal_brick.GoalBrick",

    # ── Soul Bricks (500-599) ─────────────────────────────────────────
    "BRK-500": "brikie.bricks.soul.foreman.Foreman",
    "BRK-510": "brikie.bricks.soul.dreamer.Dreamer",
    "BRK-520": "brikie.bricks.soul.crypto_trading_agent.CryptoTradingAgent",
    "BRK-530": "brikie.bricks.soul.web_design_agent.WebDesignAgent",
    "BRK-540": "brikie.bricks.soul.mason.Mason",

    # ── Memory Bricks (600-699) ───────────────────────────────────────
    "BRK-600": "brikie.bricks.memory.lcm.lcm_brick.LcmBrick",
    "BRK-610": "brikie.bricks.memory.mempalace.mempalace_brick.MempalaceBrick",
    "BRK-620": "brikie.bricks.memory.wiki.wiki_brick.WikiBrick",

    # ── Logging Bricks (700-799) ──────────────────────────────────────
    "BRK-700": "brikie.bricks.logging.token_logger.TokenLoggerBrick",
    "BRK-710": "brikie.bricks.logging.tool_tracer.ToolTracerBrick",
    "BRK-720": "brikie.bricks.logging.diagnostics.DiagnosticsCollectorBrick",

    # ── Security Bricks (800-899) ─────────────────────────────────────
    "BRK-800": "brikie.bricks.security.firewall.CommandFirewallBrick",
    "BRK-810": "brikie.bricks.security.sandbox.SandboxSecurityBrick",

    # ── Improvement Bricks (900-999) ──────────────────────────────────
    "BRK-900": "brikie.bricks.improvement.auto_fixer.AutoFixerBrick",
    "BRK-910": "brikie.bricks.improvement.loop_detector.LoopDetectorBrick",
}


class BuildSetError(Exception):
    """Raised when a Build Set cannot be loaded."""


def _is_soul_brk(brk: str) -> bool:
    """True for the 500-block — Soul Bricks are config, not runtime bricks."""
    try:
        return 500 <= int(brk[4:]) < 600
    except (ValueError, IndexError):
        return False


@dataclass
class BuildSet:
    """Deserialized Build Set manifest.

    A Build Set declares which bricks to load (by BRK number),
    their configuration, and optional metadata.

    Souls (the BRK-500 block) are persona manifests, not runtime bricks:
    they are instantiated into ``souls`` (keyed by soul name) instead of
    being registered with the BrickRegistry.
    """
    name: str = ""
    description: str = ""
    bricks: List[Dict[str, Any]] = field(default_factory=list)
    souls: Dict[str, Any] = field(default_factory=dict)
    # (brk, error) for bricks that failed to load but were skipped so the
    # rest of the stack could boot. Populated only in resilient mode.
    quarantined: List[tuple] = field(default_factory=list)


class BuildLoader:
    """Loads Build Set manifests and registers bricks with the BrickRegistry.

    Usage:
        loader = BuildLoader(registry)
        build = loader.load("path/to/build.json")
        # All bricks in the set are now registered and ready for warm-up.
    """

    def __init__(self, registry: BrickRegistry) -> None:
        self._registry = registry

    def load(self, path: str | Path, resilient: bool = False) -> BuildSet:
        """Load a Build Set JSON and register all its bricks.

        Args:
            path: Path to the Build Set JSON manifest.
            resilient: When True, a brick that fails to instantiate is
                quarantined (recorded in ``build.quarantined``) and the
                rest of the stack still loads — one bad brick, e.g. a
                broken agent-authored one, can't block the whole boot.
                When False (default), any failure raises BuildSetError.

        Returns:
            The parsed BuildSet.

        Raises:
            BuildSetError: If the file can't be read, or — in strict mode
                — if any brick fails to load.
        """
        p = Path(path).expanduser().resolve()
        if not p.exists():
            raise BuildSetError(f"Build Set not found: {p}")

        try:
            data = json.loads(p.read_text())
        except json.JSONDecodeError as exc:
            raise BuildSetError(f"Invalid Build Set JSON: {exc}") from exc

        build = BuildSet(
            name=data.get("name", p.stem),
            description=data.get("description", ""),
        )

        registered = []
        failed = []

        for entry in data.get("bricks", []):
            brk = entry.get("brk", "") if isinstance(entry, dict) else entry
            config = entry if isinstance(entry, dict) else {}
            try:
                if _is_soul_brk(brk):
                    soul = self._instantiate(brk, config)
                    build.souls[soul.name] = soul
                else:
                    self._load_brick(brk, config)
                registered.append(brk)
            except BuildSetError as exc:
                failed.append((brk, str(exc)))
                logger.error("Failed to load brick %s: %s", brk, exc)

        if failed and not resilient:
            detail = "; ".join(f"{brk}: {err}" for brk, err in failed)
            raise BuildSetError(
                f"Build Set '{build.name}': {len(failed)} of "
                f"{len(registered) + len(failed)} brick(s) failed to load — {detail}"
            )

        build.quarantined = failed
        if failed:
            logger.warning(
                "Build Set '%s': quarantined %d brick(s) so the rest could "
                "boot — %s",
                build.name, len(failed),
                ", ".join(brk for brk, _ in failed),
            )
        logger.info(
            "Build Set '%s': %d bricks registered, %d soul(s) loaded.",
            build.name, len(registered) - len(build.souls), len(build.souls),
        )

        return build

    def validate_minimum_stack(self) -> None:
        """Enforce the Baseplate's minimum viable stack.

        Per the blueprint, an agent cannot reach consciousness without at
        least one Provider Brick (cognition) and one Interface Brick
        (a medium for communication).

        Raises:
            BuildSetError: If either is missing from the registry.
        """
        from brikie.kernel.registry import InterfaceBrick, ProviderBrick

        missing = []
        if not self._registry.get_all(ProviderBrick):
            missing.append("a Provider Brick (e.g. BRK-200)")
        if not self._registry.get_all(InterfaceBrick):
            missing.append("an Interface Brick (e.g. BRK-300)")
        if missing:
            raise BuildSetError(
                "Build Set does not meet the minimum stack: missing "
                + " and ".join(missing)
            )

    def _instantiate(self, brk: str, config: Dict[str, Any]) -> Any:
        """Import and instantiate a brick class by BRK number.

        Args:
            brk: The BRK-NNN identifier.
            config: Optional configuration dict passed to the constructor.

        Raises:
            BuildSetError: If the BRK is unknown or the class can't be loaded.
        """
        module_path = BRICK_INDEX.get(brk)
        if module_path is None:
            raise BuildSetError(f"Unknown brick number: {brk}")

        try:
            mod_path, _, class_name = module_path.rpartition(".")
            mod = importlib.import_module(mod_path)
            brick_cls = getattr(mod, class_name)
        except (ImportError, AttributeError) as exc:
            raise BuildSetError(
                f"Cannot import {module_path}: {exc}"
            ) from exc

        init_kwargs = config.get("config", {})
        if not isinstance(init_kwargs, dict):
            init_kwargs = {}

        # Auto-inject the BrickRegistry for bricks that need it
        try:
            import inspect
            sig = inspect.signature(brick_cls.__init__)
            if "registry" in sig.parameters and "registry" not in init_kwargs:
                init_kwargs["registry"] = self._registry
        except (ValueError, TypeError):
            pass

        return brick_cls(**init_kwargs) if init_kwargs else brick_cls()

    def _load_brick(self, brk: str, config: Dict[str, Any]) -> None:
        """Instantiate a brick and register it with the BrickRegistry."""
        brick = self._instantiate(brk, config)
        self._registry.register(brick)
        logger.debug("Registered %s", brk)
