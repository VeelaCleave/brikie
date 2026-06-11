"""Kadeia Registry — remote brick registry client and installer.

ABCs and base types only — no concrete bricks are exported here.
Import concrete bricks directly from their modules:

    from brikie.bricks.registry.kadeia_installer import KadeiaInstallerBrick
    from brikie.bricks.registry.kadeia_registry import KadeiaRegistry
"""

from brikie.bricks.registry.base import BrickManifest

__all__ = ["BrickManifest"]
