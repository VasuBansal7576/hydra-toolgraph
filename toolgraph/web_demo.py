"""Small web demo: task → seeds, walk, loaded tools, tokens saved, curve, undo."""

from __future__ import annotations

import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from toolgraph.loader import build_loader, load_tools, record_trace, undo_last

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "web"
RESULTS = ROOT / "results"
STATE = None


def state():
    global STATE
    if STATE is None:
        STATE = build_loader()
    return STATE


HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>hydra-toolgraph demo</title>
  <style>
    :root {
      --bg: #f3efe6;
      --ink: #1c1917;
      --muted: #57534e;
      --accent: #0f766e;
      --line: #d6d3d1;
      --panel: #fffdf8;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "IBM Plex Sans", "Source Sans 3", "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at 10% 0%, #d9f3ef 0, transparent 42%),
        radial-gradient(circle at 90% 10%, #f6e7d0 0, transparent 35%),
        var(--bg);
      min-height: 100vh;
    }
    main { max-width: 960px; margin: 0 auto; padding: 2.5rem 1.25rem 4rem; }
    h1 { font-size: 2rem; letter-spacing: -0.03em; margin: 0 0 0.35rem; }
    .sub { color: var(--muted); margin-bottom: 1.5rem; max-width: 42rem; }
    form { display: flex; gap: 0.6rem; flex-wrap: wrap; margin-bottom: 1rem; }
    input[type=text] {
      flex: 1 1 280px; padding: 0.85rem 1rem; border: 1px solid var(--line);
      border-radius: 10px; font-size: 1rem; background: var(--panel);
    }
    button {
      border: 0; border-radius: 10px; padding: 0.85rem 1.1rem; cursor: pointer;
      background: var(--accent); color: white; font-weight: 600;
    }
    button.secondary { background: #44403c; }
    .grid { display: grid; grid-template-columns: 1fr; gap: 1rem; }
    @media (min-width: 860px) { .grid { grid-template-columns: 1.1fr 0.9fr; } }
    section {
      background: color-mix(in srgb, var(--panel) 92%, white);
      border: 1px solid var(--line); border-radius: 14px; padding: 1rem 1.1rem;
    }
    h2 { font-size: 0.95rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); margin: 0 0 0.7rem; }
    .meta { display: flex; gap: 1rem; flex-wrap: wrap; font-size: 0.95rem; margin-bottom: 0.8rem; }
    .meta strong { color: var(--accent); }
    ol { margin: 0; padding-left: 1.1rem; }
    li { margin: 0.35rem 0; }
    .path { color: var(--muted); font-size: 0.85rem; }
    img.curve { width: 100%; border-radius: 10px; border: 1px solid var(--line); background: white; }
    pre { white-space: pre-wrap; font-size: 0.82rem; overflow: auto; max-height: 220px; }
  </style>
</head>
<body>
<main>
  <h1>hydra-toolgraph</h1>
  <p class="sub">Type a task. The loader returns fused seeds, the learned prerequisite walk, and the token cost versus the full catalog.</p>
  <form id="f">
    <input id="task" type="text" placeholder="e.g. schedule a meeting in Tokyo timezone" required />
    <button type="submit">Load tools</button>
    <button type="button" class="secondary" id="undo">Undo last edits</button>
  </form>
  <div class="grid">
    <section>
      <h2>Loaded tools</h2>
      <div class="meta" id="meta"></div>
      <ol id="tools"></ol>
    </section>
    <section>
      <h2>Walk + learning curve</h2>
      <div id="seeds"></div>
      <p class="path" id="saved"></p>
      <img class="curve" id="curve" alt="completeness curve" src="/results/completeness_curve.png" onerror="this.style.display='none'"/>
      <pre id="raw"></pre>
    </section>
  </div>
</main>
<script>
const meta = document.getElementById('meta');
const tools = document.getElementById('tools');
const seeds = document.getElementById('seeds');
const saved = document.getElementById('saved');
const raw = document.getElementById('raw');
document.getElementById('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const task = document.getElementById('task').value;
  const res = await fetch('/api/load_tools', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({task, k: 16})
  });
  const data = await res.json();
  meta.innerHTML = `<span>seeds <strong>${(data.seeds||[]).length}</strong></span>
    <span>tools <strong>${(data.tools||[]).length}</strong></span>
    <span>latency <strong>${(data.latency_ms||0).toFixed(1)} ms</strong></span>`;
  seeds.textContent = 'Seeds: ' + (data.seeds||[]).join(', ');
  saved.textContent = `Tokens loaded ${data.tokens_loaded} / catalog ${data.catalog_tokens} (saved ${data.tokens_saved})`;
  tools.innerHTML = (data.tools||[]).map(t =>
    `<li><strong>${t.name}</strong><div class="path">${(t.reason_path||[]).join(' → ')}</div></li>`
  ).join('');
  raw.textContent = JSON.stringify(data, null, 2);
});
document.getElementById('undo').addEventListener('click', async () => {
  const res = await fetch('/api/undo', {method:'POST'});
  const data = await res.json();
  raw.textContent = JSON.stringify(data, null, 2);
});
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            self._send(200, HTML.encode(), "text/html; charset=utf-8")
            return
        if path.startswith("/results/"):
            target = RESULTS / path.removeprefix("/results/")
            if target.exists() and target.is_file() and RESULTS in target.resolve().parents:
                data = target.read_bytes()
                mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                self._send(200, data, mime)
                return
        if path == "/api/curve":
            curve = RESULTS / "curve.json"
            if curve.exists():
                self._send(200, curve.read_bytes(), "application/json")
                return
            self._send(404, b'{"error":"curve not ready"}', "application/json")
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self._send(400, b'{"error":"bad json"}', "application/json")
            return
        path = urlparse(self.path).path
        try:
            if path == "/api/load_tools":
                out = load_tools(state(), str(payload.get("task") or ""), int(payload.get("k") or 16))
            elif path == "/api/record_trace":
                out = record_trace(state(), str(payload.get("task") or ""), list(payload.get("tools_used") or []))
            elif path == "/api/undo":
                out = undo_last(state())
            else:
                self._send(404, b'{"error":"not found"}', "application/json")
                return
            body = json.dumps(out).encode()
            self._send(200, body, "application/json")
        except Exception as exc:  # surface demo errors
            self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")

    def log_message(self, fmt: str, *args) -> None:
        print(f"web {self.address_string()} {fmt % args}", flush=True)


def main() -> None:
    host = "0.0.0.0"
    port = 8080
    # Eager load so the first request is not a cold catalog write.
    state()
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"demo on http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
