"""Memory Bricks module — tripartite memory architecture.

Provides the MemoryBrick ABC. Concrete brick implementations (LCM, MemPalace,
LLM Wiki) live in their own submodules and are independently installable.

Import bricks directly from their subpackages:
    from brikie.bricks.memory.lcm import LcmBrick
    from brikie.bricks.memory.mempalace import MempalaceBrick
    from brikie.bricks.memory.wiki import WikiBrick
"""

from brikie.bricks.memory.memory_brick import MemoryBrick

__all__ = ["MemoryBrick"]
