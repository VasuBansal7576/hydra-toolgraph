#!/usr/bin/env bash
# Single-container entrypoint: optional HydraDB + demo.
# Non-root friendly (uid 1000). Demo binds immediately; bootstrap is async in demo.app.
set -euo pipefail

RUNTIME_ROOT="${TOOLGRAPH_RUNTIME:-/tmp/toolgraph}"
mkdir -p "$RUNTIME_ROOT/secrets" "$RUNTIME_ROOT/store" "$RUNTIME_ROOT/results" \
  "$RUNTIME_ROOT/fastembed_cache" /var/cache/slatedb/data 2>/dev/null || true
chmod 1777 "$RUNTIME_ROOT" /var/cache/slatedb /var/cache/slatedb/data 2>/dev/null || true

MODE="${TOOLGRAPH_MODE:-readonly}"  # default readonly for free-tier hosts

# Honour $PORT (Render) then DEMO_PORT then 7860 (HF Spaces).
export DEMO_PORT="${PORT:-${DEMO_PORT:-7860}}"
export PYTHONPATH="${PYTHONPATH:-/app}"
export FASTEMBED_CACHE_PATH="${FASTEMBED_CACHE_PATH:-$RUNTIME_ROOT/fastembed_cache}"
export DEMO_ALLOW_WRITES="${DEMO_ALLOW_WRITES:-0}"
export DEMO_SEED_LEARN="${DEMO_SEED_LEARN:-0}"
export DEMO_FUSION="${DEMO_FUSION:-bm25}"
export DEMO_SEED_TIMEOUT_SEC="${DEMO_SEED_TIMEOUT_SEC:-45}"

# If cgroup memory limit looks ≤768 MiB, force readonly + bm25.
mem_limit_bytes=""
if [[ -f /sys/fs/cgroup/memory.max ]]; then
  mem_limit_bytes="$(cat /sys/fs/cgroup/memory.max 2>/dev/null || true)"
elif [[ -f /sys/fs/cgroup/memory/memory.limit_in_bytes ]]; then
  mem_limit_bytes="$(cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null || true)"
fi
if [[ -n "${mem_limit_bytes}" && "${mem_limit_bytes}" != "max" ]]; then
  # bash arithmetic; ignore non-numeric
  if [[ "${mem_limit_bytes}" =~ ^[0-9]+$ ]] && (( mem_limit_bytes > 0 && mem_limit_bytes <= 768*1024*1024 )); then
    echo "[entrypoint] cgroup memory limit ${mem_limit_bytes} bytes ≤768MiB → TOOLGRAPH_MODE=readonly DEMO_FUSION=bm25"
    MODE=readonly
    export TOOLGRAPH_MODE=readonly
    export DEMO_FUSION=bm25
    export DEMO_SEED_LEARN=0
  fi
fi

start_demo() {
  echo "[entrypoint] starting demo on :${DEMO_PORT} (mode=${MODE}, uid=$(id -u), fusion=${DEMO_FUSION})"
  if [[ "$MODE" == "readonly" ]]; then
    export DEMO_READ_ONLY=1
    export DEMO_SEED_LEARN=0
    export DEMO_ALLOW_WRITES=0
    export DEMO_FUSION="${DEMO_FUSION:-bm25}"
  fi
  exec python3 -m demo.app
}

if [[ "$MODE" == "readonly" ]]; then
  echo "[entrypoint] TOOLGRAPH_MODE=readonly — committed results + BM25; HydraDB not started."
  export DEMO_READ_ONLY=1
  start_demo
fi

TOKEN_FILE="${GRAPH_AUTH_TOKEN_FILE:-$RUNTIME_ROOT/secrets/auth-token}"
mkdir -p "$(dirname "$TOKEN_FILE")" "$RUNTIME_ROOT/store"

if [[ -n "${HYDRA_TOKEN:-}" ]]; then
  printf '%s' "$HYDRA_TOKEN" > "$TOKEN_FILE"
elif [[ -n "${GRAPH_AUTH_TOKEN:-}" ]]; then
  printf '%s' "$GRAPH_AUTH_TOKEN" > "$TOKEN_FILE"
elif [[ ! -f "$TOKEN_FILE" ]]; then
  printf '%s' "local-development-token-32-bytes" > "$TOKEN_FILE"
fi
chmod 600 "$TOKEN_FILE" 2>/dev/null || true

export CLOUD_PROVIDER="${CLOUD_PROVIDER:-local}"
export LOCAL_PATH="${LOCAL_PATH:-$RUNTIME_ROOT/store}"
export GRAPH_NAMESPACE="${GRAPH_NAMESPACE:-default}"
export GRAPH_ID="${GRAPH_ID:-default}"
export GRAPH_ALLOW_PLAINTEXT="${GRAPH_ALLOW_PLAINTEXT:-true}"
export GRAPH_USAGE_REPORT_ENABLED="${GRAPH_USAGE_REPORT_ENABLED:-false}"
export GRAPH_AUTH_TOKEN_FILE="$TOKEN_FILE"
export HYDRA_URL="${HYDRA_URL:-http://127.0.0.1:8443}"
export HYDRA_TOKEN
HYDRA_TOKEN="$(cat "$TOKEN_FILE")"

HYDRA_WAIT_SEC="${HYDRA_WAIT_SEC:-20}"

start_hydra() {
  echo "[entrypoint] starting graph-node on :8443 (LOCAL_PATH=$LOCAL_PATH)…"
  /usr/local/bin/graph-node &
  HYDRA_PID=$!
  for _ in $(seq 1 "$HYDRA_WAIT_SEC"); do
    if bash -c 'echo > /dev/tcp/127.0.0.1/8443' 2>/dev/null; then
      echo "[entrypoint] hydradb ready"
      return 0
    fi
    sleep 1
  done
  echo "[entrypoint] hydradb failed to become ready in ${HYDRA_WAIT_SEC}s" >&2
  return 1
}

if ! start_hydra; then
  echo "[entrypoint] Falling back to read-only demo." >&2
  export DEMO_READ_ONLY=1
  export TOOLGRAPH_MODE=readonly
  MODE=readonly
  start_demo
fi

# Live: demo in foreground; hydra already backgrounded.
python3 -m demo.app &
DEMO_PID=$!
trap 'kill $DEMO_PID $HYDRA_PID 2>/dev/null || true' EXIT
wait "$DEMO_PID"
