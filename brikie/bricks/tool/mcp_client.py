"""MCPClientBrick — connect to Model Context Protocol servers.

MCP is the de-facto standard for agent tool integrations: a user's
existing MCP servers (GitHub, Postgres, Notion, filesystem, …) become
brikie tools the moment this brick is seated. The brick speaks MCP's
stdio transport directly — newline-delimited JSON-RPC 2.0 over a
subprocess's stdin/stdout — so there is no SDK dependency.

Configure servers in the build set:

    {"brk": "BRK-440", "config": {"servers": {
        "git":   {"command": "uvx", "args": ["mcp-server-git"]},
        "fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}
    }}}

Each server's tools are exposed namespaced as ``<server>_<tool>`` so two
servers can't collide. A server that fails to start degrades to "no
tools from that server" — it never takes the brick (or the agent) down.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

from brikie.bricks.tool.base import ToolBrick

logger = logging.getLogger(__name__)

_PROTOCOL_VERSION = "2024-11-05"
_INIT_TIMEOUT = 30.0
_CALL_TIMEOUT = 120.0


class MCPServerError(Exception):
    """Raised when an MCP server handshake or call fails."""


class _StdioMCPServer:
    """One MCP server subprocess, spoken to over newline-delimited JSON-RPC."""

    def __init__(
        self,
        name: str,
        command: str,
        args: List[str],
        env: Optional[Dict[str, str]] = None,
    ) -> None:
        self.name = name
        self._command = command
        self._args = args
        self._env = env
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._next_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self.tools: List[Dict[str, Any]] = []  # raw MCP tool descriptors

    async def start(self) -> None:
        """Spawn the server and perform the MCP initialize handshake."""
        full_env = {**os.environ, **(self._env or {})}
        self._proc = await asyncio.create_subprocess_exec(
            self._command, *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=full_env,
        )
        self._reader_task = asyncio.create_task(
            self._read_loop(), name=f"mcp-read:{self.name}"
        )

        await asyncio.wait_for(self._initialize(), timeout=_INIT_TIMEOUT)
        result = await asyncio.wait_for(
            self._request("tools/list", {}), timeout=_INIT_TIMEOUT
        )
        self.tools = result.get("tools", []) if isinstance(result, dict) else []
        logger.info(
            "MCP server '%s' ready — %d tool(s)", self.name, len(self.tools)
        )

    async def _initialize(self) -> None:
        await self._request("initialize", {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "brikie", "version": "0.1.0"},
        })
        # MCP requires the client to confirm before normal operation.
        await self._notify("notifications/initialized", {})

    async def call_tool(self, tool: str, arguments: Dict[str, Any]) -> Any:
        """Invoke one tool; return its flattened text/content result."""
        result = await asyncio.wait_for(
            self._request("tools/call", {"name": tool, "arguments": arguments}),
            timeout=_CALL_TIMEOUT,
        )
        return self._flatten_content(result)

    @staticmethod
    def _flatten_content(result: Any) -> Any:
        """Reduce an MCP tool result to text the model can read."""
        if not isinstance(result, dict):
            return result
        blocks = result.get("content")
        if not isinstance(blocks, list):
            return result
        texts = []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
            else:
                texts.append(json.dumps(block))
        joined = "\n".join(texts)
        if result.get("isError"):
            return {"error": joined}
        return joined

    # ------------------------------------------------------------------
    # JSON-RPC transport
    # ------------------------------------------------------------------

    async def _request(self, method: str, params: Dict[str, Any]) -> Any:
        if self._proc is None or self._proc.stdin is None:
            raise MCPServerError(f"server '{self.name}' is not running")
        self._next_id += 1
        req_id = self._next_id
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future
        await self._write({
            "jsonrpc": "2.0", "id": req_id, "method": method, "params": params,
        })
        response = await future
        if "error" in response:
            err = response["error"]
            raise MCPServerError(
                f"{self.name}.{method}: {err.get('message', err)}"
            )
        return response.get("result")

    async def _notify(self, method: str, params: Dict[str, Any]) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _write(self, message: Dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        data = (json.dumps(message) + "\n").encode("utf-8")
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

    async def _read_loop(self) -> None:
        """Dispatch incoming JSON-RPC responses to their waiting futures."""
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break  # server closed stdout
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("MCP %s: non-JSON line ignored: %r", self.name, line[:120])
                continue
            msg_id = message.get("id")
            if msg_id in self._pending:
                self._pending.pop(msg_id).set_result(message)
            # Server-initiated requests/notifications are ignored: brikie
            # exposes no capabilities back to the server in this version.

        # stdout closed — fail any in-flight requests rather than hang.
        for future in self._pending.values():
            if not future.done():
                future.set_exception(MCPServerError(f"server '{self.name}' exited"))
        self._pending.clear()

    async def stop(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
        self._proc = None


class MCPClientBrick(ToolBrick):
    BRICK_NUMBER = "BRK-440"
    """Tool Brick that proxies tools from configured MCP servers.

    Args:
        servers: Mapping of server name → {command, args, env}. Each is a
            stdio MCP server brikie launches and proxies tools from.
    """

    def __init__(self, servers: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        super().__init__()
        self._name = "mcp_client"
        self._server_configs = servers or {}
        self._servers: Dict[str, _StdioMCPServer] = {}
        # exposed tool name -> (server name, original MCP tool name)
        self._routes: Dict[str, tuple[str, str]] = {}
        self.tools: List[Dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    async def init(self) -> None:
        """Start every configured server; collect and namespace their tools.

        A server that fails to start is logged and skipped — its absence
        never blocks boot or the other servers (optional-capability rule).
        """
        for name, cfg in self._server_configs.items():
            command = cfg.get("command")
            if not command:
                logger.warning("MCP server '%s' has no command — skipped", name)
                continue
            server = _StdioMCPServer(
                name=name,
                command=command,
                args=list(cfg.get("args") or []),
                env=cfg.get("env"),
            )
            try:
                await server.start()
            except Exception as exc:
                logger.warning("MCP server '%s' failed to start: %s", name, exc)
                await server.stop()
                continue
            self._servers[name] = server
            self._register_server_tools(server)

        logger.info(
            "MCPClientBrick ready — %d tool(s) from %d server(s)",
            len(self.tools), len(self._servers),
        )
        await super().init()

    def _register_server_tools(self, server: _StdioMCPServer) -> None:
        """Expose one server's MCP tools as namespaced brikie tool schemas."""
        for tool in server.tools:
            original = tool.get("name", "")
            if not original:
                continue
            exposed = f"{server.name}_{original}"
            self._routes[exposed] = (server.name, original)
            self.tools.append({
                "type": "function",
                "function": {
                    "name": exposed,
                    "description": (
                        f"[MCP:{server.name}] {tool.get('description', '')}"
                    ),
                    "parameters": tool.get("inputSchema")
                    or {"type": "object", "properties": {}},
                },
            })

    async def execute(self, name: str, args: Dict[str, Any]) -> Any:
        """Route a namespaced tool call to its MCP server.

        Raises:
            KeyError: When no MCP server owns this tool (lets another
                brick claim the call).
        """
        route = self._routes.get(name)
        if route is None:
            raise KeyError(f"Unknown MCP tool: {name}")
        server_name, tool_name = route
        server = self._servers.get(server_name)
        if server is None:
            return {"error": f"MCP server '{server_name}' is not running"}
        try:
            return await server.call_tool(tool_name, args)
        except MCPServerError as exc:
            return {"error": str(exc)}
        except asyncio.TimeoutError:
            return {"error": f"MCP tool '{name}' timed out"}

    async def shutdown(self) -> None:
        for server in self._servers.values():
            await server.stop()
        self._servers.clear()
        await super().shutdown()
