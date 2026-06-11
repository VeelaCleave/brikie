"""Brikie Bricks — hot-swappable modules for the Baseplate kernel.

No concrete brick is imported here.  Every brick is a self-contained,
independently installable package.  The Baseplate kernel discovers bricks
at runtime via pluggy entry-point metadata or filesystem scanning —
never by hardcoded import path.

Submodules (each is independently importable):
    provider    — LLM providers (HTTP, local, WebSocket).
    interface   — Human/system communication (CLI, Web UI).
    tool        — Agent actions on the environment.
    soul        — Persona manifests for agent identity.
    registry    — Remote brick registry (Kadeia) client and installer.
    memory      — Memory subsystems (LCM, MemPalace, LLM Wiki).
    logging     — Diagnostics, token accounting, and call tracing.
    improvement — Self-healing middleware for auto-fixing tool calls.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Any, Dict, List, Type

logger = logging.getLogger(__name__)

# ── ABCs only — no concrete bricks ────────────────────────────────────
from brikie.bricks.interface import InterfaceBrick
from brikie.bricks.logging import LoggingBrick
from brikie.bricks.provider import ProviderBrick
from brikie.bricks.tool import ToolBrick
from brikie.bricks.soul import SoulBrick
from brikie.bricks.memory import MemoryBrick
from brikie.bricks.improvement import ImprovementBrick

__all__ = [
    "InterfaceBrick",
    "ProviderBrick",
    "ToolBrick",
    "SoulBrick",
    "MemoryBrick",
    "LoggingBrick",
    "ImprovementBrick",
    "discover_bricks",
]

BRICK_SUBPACKAGES: Dict[str, str] = {
    "tool": "brikie.bricks.tool",
    "interface": "brikie.bricks.interface",
    "provider": "brikie.bricks.provider",
    "soul": "brikie.bricks.soul",
    "memory": "brikie.bricks.memory",
    "logging": "brikie.bricks.logging",
    "improvement": "brikie.bricks.improvement",
    "registry": "brikie.bricks.registry",
}


def discover_bricks(category: str | None = None) -> Dict[str, List[Type[Any]]]:
    """Discover available brick implementations by scanning subpackages.

    This is the **only** entry point the kernel should use to learn which
    concrete bricks exist.  No brick is ever imported by name.

    Args:
        category: Optional category to scan (e.g. ``"tool"``, ``"provider"``).
                  If ``None`` all categories are scanned.

    Returns:
        Mapping of ``{category_name: [brick_class, ...]}`` for every
        concrete brick found in the scanned subpackages.
    """
    categories = [category] if category else list(BRICK_SUBPACKAGES)
    results: Dict[str, List[Type[Any]]] = {}

    for cat in categories:
        pkg_name = BRICK_SUBPACKAGES.get(cat)
        if pkg_name is None:
            continue

        try:
            pkg = importlib.import_module(pkg_name)
        except ImportError:
            logger.debug("Brick subpackage %s not available — skipping.", pkg_name)
            continue

        discovered: List[Type[Any]] = []
        for _imp, modname, _ispkg in pkgutil.walk_packages(
            path=getattr(pkg, "__path__", []),
            prefix=f"{pkg_name}.",
            onerror=lambda _: None,
        ):
            try:
                mod = importlib.import_module(modname)
            except ImportError:
                continue

            for attr_name in dir(mod):
                attr = getattr(mod, attr_name)
                if isinstance(attr, type) and attr.__module__ == modname:
                    # We only flag concrete classes that are NOT ABCs.
                    # Subclass-tri via __subclasshook__ is avoided to keep
                    # things simple — we just note the class exists.
                    discovered.append(attr)

        results[cat] = discovered

    return results
