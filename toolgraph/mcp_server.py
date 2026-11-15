"""MCP stdio server and optional HTTP JSON gateway for ToolgraphService (v3)."""

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
            "description": (
                "Rank tools for a query via fusion + Hydra MSpaths over REQUIRES. "
                "With session_id: exactly 2 store requests (RESOLVED + MSpaths)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "session_id": {"type": "string"},
                    "k": {"type": "integer", "default": 16},
                },
                "required": ["query"],
            },
        },
        {
            "name": "record_trace",
            "description": (
                "Learn REQUIRES edges from an observed tool trace via co-occurrence; "
                "unused loaded tools get negative beta feedback."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "query": {"type": "string"},
                    "tools_used": {"type": "array", "items": {"type": "string"}},
                    "tools_loaded": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["query", "tools_used"],
            },
        },
        {
            "name": "record_result",
            "description": (
                "If the tool is read-only, write Entity+RESOLVED for the session. "
                "INVALIDATES clears RESOLVED for producers this tool invalidates."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "tool": {"type": "string"},
                    "args_hash": {"type": "string"},
                },
                "required": ["session_id", "tool"],
            },
        },
        {
            "name": "undo_edit",
            "description": "Restore previous live REQUIRES version for edit_id (HIST op=undo).",
            "inputSchema": {
                "type": "object",
                "properties": {"edit_id": {"type": "string"}},
                "required": ["edit_id"],
            },
        },
        {
            "name": "as_of",
            "description": "Query REQUIRES_HIST at ts (or date_iso); return edges + diff vs now.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "date_iso": {"type": "string"},
                    "ts": {"type": "number"},
                },
            },
        },
        {
            "name": "explain",
            "description": "Show in/out REQUIRES edges and lesson for one tool.",
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
            "load_tools": lambda args: self.service.load_tools(
                str(args["query"]),
                session_id=(str(args["session_id"]) if args.get("session_id") else None),
                k=int(args.get("k", 16)),
            ),
            "record_trace": lambda args: self.service.record_trace(
                str(args.get("query") or ""),
                list(args.get("tools_used") or []),
                tools_loaded=list(args["tools_loaded"]) if args.get("tools_loaded") is not None else None,
                session_id=(str(args["session_id"]) if args.get("session_id") else None),
            ),
            "record_result": lambda args: self.service.record_result(
                str(args["session_id"]),
                str(args["tool"]),
                args_hash=(str(args["args_hash"]) if args.get("args_hash") else None),
            ),
            "undo_edit": lambda args: self.service.undo_edit(str(args["edit_id"])),
            "as_of": lambda args: self.service.as_of(
                date_iso=(str(args["date_iso"]) if args.get("date_iso") else None),
                ts=(float(args["ts"]) if args.get("ts") is not None else None),
            ),
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
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            response = self._handle(message)
            if response is not None:
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()

    def _handle(self, message: dict) -> dict | None:
        method = message.get("method")
        msg_id = message.get("id")
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "toolgraph", "version": "3.0.0"},
                },
            }
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": _tool_schema()}}
        if method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}
            try:
                result = self.dispatcher.call(str(name), arguments)
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(result)}],
                        "structuredContent": result,
                        "isError": False,
                    },
                }
            except Exception as exc:  # noqa: BLE001 — surface to MCP client
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": str(exc)}],
                        "isError": True,
                    },
                }
        if msg_id is None:
            return None
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }


def run_sdk_stdio(dispatcher: ToolDispatcher) -> bool:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        return False

    mcp = FastMCP("toolgraph")

    @mcp.tool()
    def load_tools(query: str, session_id: str | None = None, k: int = 16) -> dict:
        return dispatcher.call("load_tools", {"query": query, "session_id": session_id, "k": k})

    @mcp.tool()
    def record_trace(
        query: str,
        tools_used: list[str],
        tools_loaded: list[str] | None = None,
        session_id: str | None = None,
    ) -> dict:
        return dispatcher.call(
            "record_trace",
            {
                "query": query,
                "tools_used": tools_used,
                "tools_loaded": tools_loaded,
                "session_id": session_id,
            },
        )

    @mcp.tool()
    def record_result(session_id: str, tool: str, args_hash: str | None = None) -> dict:
        return dispatcher.call(
            "record_result",
            {"session_id": session_id, "tool": tool, "args_hash": args_hash},
        )

    @mcp.tool()
    def undo_edit(edit_id: str) -> dict:
        return dispatcher.call("undo_edit", {"edit_id": edit_id})

    @mcp.tool()
    def as_of(date_iso: str | None = None, ts: float | None = None) -> dict:
        return dispatcher.call("as_of", {"date_iso": date_iso, "ts": ts})

    @mcp.tool()
    def explain(tool: str) -> dict:
        return dispatcher.call("explain", {"tool": tool})

    mcp.run()
    return True


def run_http(service: ToolgraphService, port: int) -> None:
    dispatcher = ToolDispatcher(service)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path in ("/health", "/"):
                self._json(200, service.status())
                return
            if parsed.path == "/tools":
                self._json(200, {"tools": _tool_schema()})
                return
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/tools/"):
                self._json(404, {"error": "not found"})
                return
            name = parsed.path[len("/tools/") :].strip("/")
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(body.decode() or "{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid json"})
                return
            try:
                result = dispatcher.call(name, payload)
            except KeyError:
                self._json(404, {"error": f"unknown tool {name}"})
                return
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": str(exc)})
                return
            self._json(200, result)

        def _json(self, code: int, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)

    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"mcp http://0.0.0.0:{port}", flush=True)
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", type=int, default=0, help="also serve HTTP JSON on this port")
    parser.add_argument("--http-only", action="store_true")
    args = parser.parse_args()
    service = default_service()
    if args.http and args.http_only:
        run_http(service, args.http)
        return
    if args.http:
        thread = threading.Thread(target=run_http, args=(service, args.http), daemon=True)
        thread.start()
    dispatcher = ToolDispatcher(service)
    if not run_sdk_stdio(dispatcher):
        MinimalMcpStdio(dispatcher).run()


if __name__ == "__main__":
    main()
