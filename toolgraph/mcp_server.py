"""MCP stdio server: load_tools(task, k) and record_trace(task, tools_used)."""

from __future__ import annotations

import json
import sys
from typing import Any

from toolgraph.loader import build_loader, load_tools, record_trace, undo_last

STATE = None


def _state():
    global STATE
    if STATE is None:
        STATE = build_loader()
    return STATE


def handle(request: dict[str, Any]) -> dict[str, Any]:
    method = request.get("method")
    req_id = request.get("id")
    params = request.get("params") or {}
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "hydra-toolgraph", "version": "0.2.0"},
            },
        }
    if method == "notifications/initialized":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {
                        "name": "load_tools",
                        "description": (
                            "Return the tool closure for a task: fused seeds plus "
                            "learned prerequisite edges, each with a reason path."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "task": {"type": "string"},
                                "k": {"type": "integer", "default": 16},
                            },
                            "required": ["task"],
                        },
                    },
                    {
                        "name": "record_trace",
                        "description": (
                            "Record an observed agent trace. tools_used[0] is the "
                            "main tool; the rest become DEPENDS_ON helpers via do/undo."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "task": {"type": "string"},
                                "tools_used": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                            "required": ["task", "tools_used"],
                        },
                    },
                    {
                        "name": "undo_last_edits",
                        "description": "Undo the last record_trace edit batch.",
                        "inputSchema": {"type": "object", "properties": {}},
                    },
                ]
            },
        }
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        state = _state()
        if name == "load_tools":
            result = load_tools(state, str(args.get("task") or ""), int(args.get("k") or 16))
        elif name == "record_trace":
            result = record_trace(state, str(args.get("task") or ""), list(args.get("tools_used") or []))
        elif name == "undo_last_edits":
            result = undo_last(state)
        else:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"unknown tool {name}"},
            }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                "isError": False,
            },
        }
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"unknown method {method}"},
    }


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request = json.loads(line)
        response = handle(request)
        if request.get("id") is None and request.get("method", "").startswith("notifications/"):
            continue
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
