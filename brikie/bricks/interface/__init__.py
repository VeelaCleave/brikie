"""Interface Bricks — human/system communication interfaces (CLI, Web UI)."""

from brikie.bricks.interface.base import InterfaceBrick
from brikie.bricks.interface.cli import CLIBrick

__all__ = ["InterfaceBrick", "CLIBrick"]
