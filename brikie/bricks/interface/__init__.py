"""Interface Bricks — human/system communication interfaces (CLI, Web UI).

ABCs only — no concrete bricks are exported here.
Import concrete bricks directly from their modules:

    from brikie.bricks.interface.cli import CLIBrick
"""

from brikie.bricks.interface.base import InterfaceBrick

__all__ = ["InterfaceBrick"]
