"""Brikie Bricks — hot-swappable modules for the Baseplate kernel.

Submodules:
    provider — LLM providers (HTTP, local, WebSocket).
    interface — Human/system communication (CLI, Web UI).
    tool — Agent actions on the environment.
"""

from brikie.bricks.interface import CLIBrick, InterfaceBrick
from brikie.bricks.provider import HTTPProvider, ProviderBrick
from brikie.bricks.tool import DummyToolBrick, ToolBrick

__all__ = [
    "CLIBrick",
    "HTTPProvider",
    "InterfaceBrick",
    "ProviderBrick",
    "ToolBrick",
    "DummyToolBrick",
]
