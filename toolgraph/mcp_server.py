"""MCP stdio server and optional HTTP JSON gateway for ToolgraphService."""

from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable
from urllib.parse import urlparse

from toolgraph.service import ToolgraphService, default_service

ToolHandler = Callable[..., dict]


def _tool_schema() -> list[dict]:
    return [
        {
            "name": "load_tools",
            "description": "Rank tools for a query with graph provenance edges.",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "k": {"type": "integer", "default": 16}},
                "required": ["query"],
            },
        },
        {
            "name": "record_trace",
            "description": "Learn DEPENDS_ON edges from an observed tool trace (main first).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "tools_used": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["query", "tools_used"],
            },
        },
        {
            "name": "undo_edit",
            "description": "Remove an edit and all later edits; rebuild graph state.",
            "inputSchema": {
                "type": "object",
                "properties": {"edit_id": {"type": "string"}},
                "required": ["edit_id"],
            },
        },
        {
            "name": "as_of",
            "description": "Rebuild in-memory snapshot with edits at or before date_iso.",
            "inputSchema": {
                "type": "object",
                "properties": {"date_iso": {"type": "string"}},
                "required": ["date_iso"],
            },
        },
        {
            "name": "explain",
            "description": "Show in/out edges and lesson for one tool.",
            "inputSchema": {
                "type": "object",
                "properties": {"tool": {"type": "string"}},
                "required": ["tool"],
            },
        },
    ]


class ToolDispatcher:
    def __init__(self, service: ToolgraphService) -> None:
        self.service = service
        self._handlers: dict[str, ToolHandler] = {
            "load_tools": lambda args: self.service.load_tools(str(args["query"]), int(args.get("k", 16))),
            "record_trace": lambda args: self.service.record_trace(
                str(args["query"]), list(args.get("tools_used") or [])
            ),
            "undo_edit": lambda args: self.service.undo_edit(str(args["edit_id"])),
            "as_of": lambda args: self.service.as_of(str(args["date_iso"])),
            "explain": lambda args: self.service.explain(str(args["tool"])),
        }

    def call(self, name: str, arguments: dict | None) -> dict:
        handler = self._handlers.get(name)
        if handler is None:
            raise KeyError(f"unknown tool {name!r}")
        return handler(arguments or {})


class MinimalMcpStdio:
    """JSON-RPC lines on stdin/stdout when the mcp SDK is not installed."""

    def __init__(self, dispatcher: ToolDispatcher) -> None:
        self.dispatcher = dispatcher

    def run(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            req = json.loads(line)
            resp = self._handle(req)
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()

    def _handle(self, req: dict) -> dict:
        req_id = req.get("id")
        method = req.get("method", "")
        base = {"jsonrpc": "2.0", "id": req_id}
        try:
            if method == "initialize":
                return {
                    **base,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "toolgraph", "version": "0.1.0"},
                    },
                }
            if method == "notifications/initialized":
                return {**base, "result": {}}
            if method == "tools/list":
                return {**base, "result": {"tools": _tool_schema()}}
            if method == "tools/call":
                params = req.get("params") or {}
                name = str(params.get("name"))
                arguments = params.get("arguments") or {}
                result = self.dispatcher.call(name, arguments)
                return {
                    **base,
                    "result": {"content": [{"type": "text", "text": json.dumps(result, indent=2)}], "isError": False},
                }
            return {**base, "error": {"code": -32601, "message": f"method not found: {method}"}}
        except Exception as exc:  # noqa: BLE001
            return {**base, "error": {"code": -32000, "message": str(exc)}}


def run_stdio(service: ToolgraphService) -> None:
    dispatcher = ToolDispatcher(service)
    try:
        from mcp.server.fastmcp import FastMCP  # type: ignore[import-not-found]

        mcp = FastMCP("toolgraph")

        @mcp.tool()
        def load_tools(query: str, k: int = 16) -> dict:
            return dispatcher.call("load_tools", {"query": query, "k": k})

        @mcp.tool()
        def record_trace(query: str, tools_used: list[str]) -> dict:
            return dispatcher.call("record_trace", {"query": query, "tools_used": tools_used})

        @mcp.tool()
        def undo_edit(edit_id: str) -> dict:
            return dispatcher.call("undo_edit", {"edit_id": edit_id})

        @mcp.tool()
        def as_of(date_iso: str) -> dict:
            return dispatcher.call("as_of", {"date_iso": date_iso})

        @mcp.tool()
        def explain(tool: str) -> dict:
            return dispatcher.call("explain", {"tool": tool})

        mcp.run(transport="stdio")
        return
    except ImportError:
        MinimalMcpStdio(dispatcher).run()


class _HttpHandler(BaseHTTPRequestHandler):
    dispatcher: ToolDispatcher
    log_message = lambda self, *_args, **_kwargs: None  # noqa: E731

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/health"):
            self._json(200, {"ok": True, "tools": [t["name"] for t in _tool_schema()]})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 2 or parts[0] != "tools":
            self._json(404, {"error": "use POST /tools/{name}"})
            return
        name = parts[1]
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b"{}"
        try:
            args = json.loads(body.decode() or "{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid json"})
            return
        try:
            result = self.dispatcher.call(name, args)
            self._json(200, result)
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": str(exc)})

    def _json(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)


def run_http(service: ToolgraphService, port: int) -> None:
    dispatcher = ToolDispatcher(service)

    class Handler(_HttpHandler):
        pass

    Handler.dispatcher = dispatcher
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"toolgraph http listening on :{port}", flush=True)
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Toolgraph MCP / HTTP server")
    parser.add_argument("--http", type=int, default=0, help="Also serve POST /tools/{name} on this port (0=stdio only)")
    parser.add_argument("--http-only", action="store_true", help="HTTP only, no stdio MCP")
    args = parser.parse_args()
    service = default_service()
    if args.http:
        if args.http_only:
            run_http(service, args.http)
        else:
            thread = threading.Thread(target=run_http, args=(service, args.http), daemon=True)
            thread.start()
            run_stdio(service)
    elif args.http_only:
        run_http(service, 8080)
    else:
        run_stdio(service)


if __name__ == "__main__":
    main()