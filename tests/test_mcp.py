"""Tests for the MCP client brick against a real stdio MCP server."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from brikie.bricks.tool.mcp_client import MCPClientBrick

_SERVER = str(Path(__file__).parent / "fixtures" / "mock_mcp_server.py")


def _config(name: str = "mock") -> dict:
    return {name: {"command": sys.executable, "args": [_SERVER]}}


@pytest.fixture
async def brick():
    b = MCPClientBrick(servers=_config())
    await b.init()
    yield b
    await b.shutdown()


class TestMCPClient:
    async def test_tools_discovered_and_namespaced(self, brick):
        names = [t["function"]["name"] for t in brick.tools]
        assert "mock_echo" in names
        assert "mock_add" in names

    async def test_descriptions_tag_the_server(self, brick):
        echo = next(t for t in brick.tools if t["function"]["name"] == "mock_echo")
        assert echo["function"]["description"].startswith("[MCP:mock]")
        assert echo["function"]["parameters"]["properties"]["text"]

    async def test_call_echo(self, brick):
        result = await brick.execute("mock_echo", {"text": "hello mcp"})
        assert result == "hello mcp"

    async def test_call_add_flattens_text(self, brick):
        result = await brick.execute("mock_add", {"a": 2, "b": 40})
        assert result == "42"

    async def test_tool_error_flagged(self, brick):
        # The mock returns isError for unknown tool names; route a known
        # exposed name whose underlying call the server rejects.
        brick._routes["mock_ghost"] = ("mock", "ghost")
        result = await brick.execute("mock_ghost", {})
        assert isinstance(result, dict) and "error" in result

    async def test_unknown_tool_raises_keyerror(self, brick):
        with pytest.raises(KeyError):
            await brick.execute("not_a_tool", {})


class TestResilience:
    async def test_bad_server_is_skipped_not_fatal(self):
        brick = MCPClientBrick(servers={
            "broken": {"command": "this-binary-does-not-exist-xyz", "args": []},
            "mock": _config()["mock"],
        })
        await brick.init()  # must not raise
        try:
            names = [t["function"]["name"] for t in brick.tools]
            assert "mock_echo" in names       # the good server still works
            assert not any(n.startswith("broken_") for n in names)
        finally:
            await brick.shutdown()

    async def test_no_servers_is_inert(self):
        brick = MCPClientBrick()
        await brick.init()
        assert brick.tools == []
        await brick.shutdown()

    async def test_call_after_shutdown_errors_cleanly(self, brick):
        await brick.shutdown()
        result = await brick.execute("mock_echo", {"text": "x"})
        assert isinstance(result, dict) and "error" in result
