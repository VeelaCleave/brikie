#!/usr/bin/env python3
"""A minimal stdio MCP server for tests and live verification.

Implements just enough of the Model Context Protocol — initialize,
tools/list, tools/call over newline-delimited JSON-RPC — to exercise
the MCPClientBrick without any external dependency. Exposes two tools:
``echo`` and ``add``.
"""

import json
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "Echo back the provided text.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "Add two numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
]


def _result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": message}}


def _handle(message):
    method = message.get("method")
    req_id = message.get("id")
    if method == "initialize":
        return _result(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "mock", "version": "0.0.1"},
        })
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return _result(req_id, {"tools": TOOLS})
    if method == "tools/call":
        params = message.get("params", {})
        name = params.get("name")
        args = params.get("arguments", {})
        if name == "echo":
            text = str(args.get("text", ""))
            return _result(req_id, {"content": [{"type": "text", "text": text}]})
        if name == "add":
            total = args.get("a", 0) + args.get("b", 0)
            return _result(req_id, {
                "content": [{"type": "text", "text": str(total)}],
            })
        return _result(req_id, {
            "content": [{"type": "text", "text": f"unknown tool: {name}"}],
            "isError": True,
        })
    if req_id is not None:
        return _error(req_id, f"unknown method: {method}")
    return None


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = _handle(message)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
