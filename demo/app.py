"""Minimal demo UI: static page + JSON API backed by ToolgraphService."""

from __future__ import annotations

import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from toolgraph.service import ToolgraphService, default_service

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
PORT = int(os.environ.get("DEMO_PORT", "5000"))

_service: ToolgraphService | None = None


def get_service() -> ToolgraphService:
    global _service
    if _service is None:
        _service = default_service()
    return _service


class DemoHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._api_get(parsed.path)
            return
        if parsed.path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(body.decode() or "{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid json"})
            return
        svc = get_service()
        path = parsed.path.rstrip("/")
        if path == "/api/load_tools":
            self._json(200, svc.load_tools(str(payload.get("query", "")), int(payload.get("k", 16))))
        elif path == "/api/record_trace":
            self._json(
                200,
                svc.record_trace(str(payload.get("query", "")), list(payload.get("tools_used") or [])),
            )
        elif path == "/api/undo_edit":
            self._json(200, svc.undo_edit(str(payload.get("edit_id", ""))))
        elif path == "/api/as_of":
            self._json(200, svc.as_of(str(payload.get("date_iso", ""))))
        elif path == "/api/explain":
            self._json(200, svc.explain(str(payload.get("tool", ""))))
        else:
            self._json(404, {"error": "unknown route"})

    def _api_get(self, path: str) -> None:
        if path == "/api/health":
            self._json(200, {"ok": True})
            return
        self._json(404, {"error": "not found"})

    def _json(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), DemoHandler)
    print(f"demo http://0.0.0.0:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()