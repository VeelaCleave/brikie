"""LLM Wiki Brick — Persistent synthesized knowledge as Markdown codebase.

Exports:
- WikiBrick: Main brick (MemoryBrick + ToolBrick) with auto-extraction and tools
- get_wiki_tools: OpenAI function tool schemas for the agent
"""

from brikie.bricks.memory.wiki.wiki_brick import WikiBrick
from brikie.bricks.memory.wiki.wiki_tools import get_wiki_tools

__all__ = [
    "WikiBrick",
    "get_wiki_tools",
]
