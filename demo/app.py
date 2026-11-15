"""Demo UI + JSON API. HTTP binds immediately; heavy init runs in a background thread.

/api/health always answers (reports phase). /api/load_tools waits until ready or
returns 503 while seeding. Default path is live + BM25 (graph seeded from
precomputed edges). Readonly only when explicitly requested, Hydra is down, or
seeding exceeds DEMO_SEED_TIMEOUT_SEC.
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
PORT = int(os.environ.get("PORT") or os.environ.get("DEMO_PORT") or "5000")
SEED_TIMEOUT_SEC = float(os.environ.get("DEMO_SEED_TIMEOUT_SEC", "180"))

_boot: dict = {
    "phase": "starting",  # starting | seeding | ready | readonly_fallback | error
    "detail": "binding port",
    "started_at": time.time(),
    "ready_at": None,
    "error": None,
    "fusion": None,
    "fallback_reason": None,
}
_service = None
_committed: dict | None = None
_boot_lock = threading.Lock()


def _log(msg: str) -> None:
    print(f"[demo] {msg}", flush=True)


def _set_phase(phase: str, detail: str = "", **extra: object) -> None:
    with _boot_lock:
        _boot["phase"] = phase
        _boot["detail"] = detail
        for key, value in extra.items():
            _boot[key] = value
    _log(f"phase={phase} {detail}".strip())


def _load_committed() -> dict:
    path = ROOT.parent / "results" / "v3_report.json"
    if path.exists():
        return json.loads(path.read_text())
    return {"label": "missing", "note": "no committed results/v3_report.json"}


def _mem_available_mb() -> float | None:
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return float(line.split()[1]) / 1024.0
    except OSError:
        return None
    return None


def _env_ro() -> bool:
    return os.environ.get("DEMO_READ_ONLY", "").strip().lower() in {"1", "true", "yes", "on"}


def _explicit_readonly() -> bool:
    """Readonly only when the operator asked for it — not when RAM is tight."""
    if os.environ.get("TOOLGRAPH_MODE", "").strip().lower() == "readonly":
        return True
    return _env_ro()


def _force_bm25_on_small_box() -> None:
    """≤768 MiB hosts skip MiniLM; live graph + BM25 still runs."""
    mem = _mem_available_mb()
    if mem is not None and mem < 700:
        os.environ["DEMO_FUSION"] = "bm25"
        _log(f"small box MemAvailable={mem:.0f}MiB → DEMO_FUSION=bm25")


def _build_service(*, read_only: bool, fusion: str) -> object:
    os.environ["DEMO_FUSION"] = fusion
    if read_only:
        os.environ["DEMO_READ_ONLY"] = "1"
        os.environ["DEMO_ALLOW_WRITES"] = "0"
        os.environ["DEMO_SEED_LEARN"] = "0"
        os.environ["DEMO_LOAD_PRECOMPUTED_EDGES"] = "0"
    else:
        os.environ.setdefault("DEMO_LOAD_PRECOMPUTED_EDGES", "1")
        os.environ.setdefault("DEMO_SEED_LEARN", "1")
        os.environ.setdefault("DEMO_ALLOW_WRITES", "0")
    # Late import so the HTTP server can bind before loading numpy/hydra.
    from toolgraph.service import default_service

    return default_service()


def _bootstrap() -> None:
    global _service, _committed
    deadline = time.time() + SEED_TIMEOUT_SEC
    _committed = _load_committed()
    _force_bm25_on_small_box()
    _set_phase(
        "seeding",
        f"bootstrap start explicit_ro={_explicit_readonly()} "
        f"mem_avail_mb={_mem_available_mb()} timeout={SEED_TIMEOUT_SEC}",
    )

    def timed_out() -> bool:
        return time.time() > deadline

    try:
        if _explicit_readonly():
            _set_phase("seeding", "building BM25-only readonly service (explicit)")
            svc = _build_service(read_only=True, fusion="bm25")
            with _boot_lock:
                _service = svc
            _set_phase("ready", "readonly BM25 ready", fusion="bm25", ready_at=time.time())
            return

        if timed_out():
            raise TimeoutError("seed timeout before live build")
        fusion = os.environ.get("DEMO_FUSION", "bm25").strip().lower() or "bm25"
        _set_phase(
            "seeding",
            f"building live service fusion={fusion} "
            f"(precomputed={os.environ.get('DEMO_LOAD_PRECOMPUTED_EDGES', '1')})",
        )
        svc = _build_service(read_only=False, fusion=fusion)
        if timed_out():
            raise TimeoutError("seed timeout after live build")
        with _boot_lock:
            _service = svc
        edges = getattr(svc, "status", lambda: {})().get("edges")
        _set_phase(
            "ready",
            f"live ready fusion={fusion} edges={edges}",
            fusion=fusion,
            ready_at=time.time(),
        )
    except Exception as exc:  # noqa: BLE001 — must never kill the server
        _log(f"bootstrap error: {exc}")
        traceback.print_exc()
        try:
            _set_phase(
                "seeding",
                f"fallback to readonly after error/timeout: {exc}",
                fallback_reason=str(exc),
            )
            os.environ["DEMO_READ_ONLY"] = "1"
            os.environ["DEMO_ALLOW_WRITES"] = "0"
            os.environ["DEMO_SEED_LEARN"] = "0"
            os.environ["DEMO_LOAD_PRECOMPUTED_EDGES"] = "0"
            os.environ["DEMO_FUSION"] = "bm25"
            svc = _build_service(read_only=True, fusion="bm25")
            with _boot_lock:
                _service = svc
            _set_phase(
                "readonly_fallback",
                f"serving committed results + BM25 fallback ({exc})",
                fusion="bm25",
                fallback_reason=str(exc),
                ready_at=time.time(),
                error=str(exc),
            )
        except Exception as exc2:  # noqa: BLE001
            _set_phase("error", f"bootstrap failed: {exc2}", error=str(exc2))
            traceback.print_exc()


def get_service():
    with _boot_lock:
        return _service


def health_payload() -> dict:
    with _boot_lock:
        boot = dict(_boot)
        svc = _service
    elapsed = time.time() - float(boot.get("started_at") or time.time())
    payload = {
        "ok": True,
        "phase": boot.get("phase"),
        "detail": boot.get("detail"),
        "elapsed_sec": round(elapsed, 2),
        "ready": boot.get("phase") in {"ready", "readonly_fallback"},
        "fusion": boot.get("fusion"),
        "fallback_reason": boot.get("fallback_reason"),
        "error": boot.get("error"),
        "seed_timeout_sec": SEED_TIMEOUT_SEC,
        "port": PORT,
    }
    if svc is not None and hasattr(svc, "status"):
        try:
            payload.update({f"service_{k}" if k == "ok" else k: v for k, v in svc.status().items()})
            payload["ok"] = True
        except Exception as exc:  # noqa: BLE001
            payload["service_status_error"] = str(exc)
    return payload


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
        if svc is None:
            self._json(
                503,
                {
                    "error": "service not ready",
                    "phase": _boot.get("phase"),
                    "detail": _boot.get("detail"),
                },
            )
            return

        path = parsed.path.rstrip("/")
        if path == "/api/load_tools":
            session = payload.get("session_id")
            self._json(
                200,
                svc.load_tools(
                    str(payload.get("query", "")),
                    session_id=(str(session) if session else None),
                    k=int(payload.get("k", 16)),
                ),
            )
        elif path == "/api/record_trace":
            session = payload.get("session_id")
            loaded = payload.get("tools_loaded")
            self._json(
                200,
                svc.record_trace(
                    str(payload.get("query", "")),
                    list(payload.get("tools_used") or []),
                    tools_loaded=(list(loaded) if loaded is not None else None),
                    session_id=(str(session) if session else None),
                ),
            )
        elif path == "/api/record_result":
            self._json(
                200,
                svc.record_result(
                    str(payload.get("session_id", "")),
                    str(payload.get("tool", "")),
                    args_hash=(str(payload["args_hash"]) if payload.get("args_hash") else None),
                ),
            )
        elif path == "/api/undo_edit":
            self._json(200, svc.undo_edit(str(payload.get("edit_id", ""))))
        elif path == "/api/as_of":
            ts = payload.get("ts")
            self._json(
                200,
                svc.as_of(
                    date_iso=(str(payload["date_iso"]) if payload.get("date_iso") else None),
                    ts=(float(ts) if ts is not None else None),
                ),
            )
        elif path == "/api/explain":
            self._json(200, svc.explain(str(payload.get("tool", ""))))
        else:
            self._json(404, {"error": "unknown route"})

    def _api_get(self, path: str) -> None:
        # Health never blocks on service construction.
        if path == "/api/health":
            self._json(200, health_payload())
            return
        if path == "/api/results":
            committed = _committed if _committed is not None else _load_committed()
            self._json(200, committed)
            return
        svc = get_service()
        if svc is None:
            self._json(
                503,
                {
                    "error": "service not ready",
                    "phase": _boot.get("phase"),
                    "detail": _boot.get("detail"),
                },
            )
            return
        if path == "/api/timeline":
            self._json(200, svc.timeline())
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
    _boot["started_at"] = time.time()
    _set_phase("starting", f"binding 0.0.0.0:{PORT}")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), DemoHandler)
    # Background bootstrap so /api/health answers as soon as the port is open.
    thread = threading.Thread(target=_bootstrap, name="demo-bootstrap", daemon=True)
    thread.start()
    _log(f"listening on http://0.0.0.0:{PORT} (bootstrap in background)")
    server.serve_forever()


if __name__ == "__main__":
    main()
