"""brikie.co Registry — remote brick registry client and installer.

ABCs and base types only — no concrete bricks are exported here.
Import concrete bricks directly from their modules:

    from brikie.bricks.registry.installer import RegistryInstallerBrick
    from brikie.bricks.registry.registry_client import RegistryClient
"""

from brikie.bricks.registry.base import BrickManifest

__all__ = ["BrickManifest"]
