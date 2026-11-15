#!/usr/bin/env bash
# Single-container entrypoint: HydraDB graph-node + demo on :7860.
set -euo pipefail

TOKEN_FILE="${GRAPH_AUTH_TOKEN_FILE:-/var/run/secrets/slatedb-graph/auth-token}"
mkdir -p "$(dirname "$TOKEN_FILE")" /data/store /app/results

# Prefer HF / env secret; never bake a real production token into the image.
if [[ -n "${HYDRA_TOKEN:-}" ]]; then
  printf '%s' "$HYDRA_TOKEN" > "$TOKEN_FILE"
elif [[ -n "${GRAPH_AUTH_TOKEN:-}" ]]; then
  printf '%s' "$GRAPH_AUTH_TOKEN" > "$TOKEN_FILE"
elif [[ ! -f "$TOKEN_FILE" ]]; then
  # Local-dev fallback only (same default as docker-compose).
  printf '%s' "local-development-token-32-bytes" > "$TOKEN_FILE"
fi
chmod 600 "$TOKEN_FILE" || true

export CLOUD_PROVIDER="${CLOUD_PROVIDER:-local}"
export LOCAL_PATH="${LOCAL_PATH:-/data/store}"
export GRAPH_NAMESPACE="${GRAPH_NAMESPACE:-default}"
export GRAPH_ID="${GRAPH_ID:-default}"
export GRAPH_ALLOW_PLAINTEXT="${GRAPH_ALLOW_PLAINTEXT:-true}"
export GRAPH_USAGE_REPORT_ENABLED="${GRAPH_USAGE_REPORT_ENABLED:-false}"
export GRAPH_AUTH_TOKEN_FILE="$TOKEN_FILE"
export HYDRA_URL="${HYDRA_URL:-http://127.0.0.1:8443}"
export HYDRA_TOKEN="$(cat "$TOKEN_FILE")"
export DEMO_PORT="${DEMO_PORT:-7860}"
export PYTHONPATH="${PYTHONPATH:-/app}"
export FASTEMBED_CACHE_PATH="${FASTEMBED_CACHE_PATH:-/app/fastembed_cache}"

MODE="${TOOLGRAPH_MODE:-live}"  # live | readonly

start_hydra() {
  echo "starting graph-node on :8443…"
  /usr/local/bin/graph-node &
  HYDRA_PID=$!
  for _ in $(seq 1 60); do
    if bash -c 'echo > /dev/tcp/127.0.0.1/8443' 2>/dev/null; then
      echo "hydradb ready"
      return 0
    fi
    sleep 1
  done
  echo "hydradb failed to become ready" >&2
  return 1
}

start_demo() {
  echo "starting demo on :${DEMO_PORT} (mode=${MODE})…"
  if [[ "$MODE" == "readonly" ]]; then
    export DEMO_READ_ONLY=1
    export DEMO_SEED_LEARN=0
  fi
  exec python3 -m demo.app
}

if [[ "$MODE" == "readonly" ]]; then
  echo "TOOLGRAPH_MODE=readonly — serving committed results; HydraDB not started."
  export DEMO_READ_ONLY=1
  start_demo
fi

if ! start_hydra; then
  echo "Falling back to read-only demo (committed results)." >&2
  export DEMO_READ_ONLY=1
  export TOOLGRAPH_MODE=readonly
  start_demo
fi

# Run demo in foreground; if it exits, stop hydra.
python3 -m demo.app &
DEMO_PID=$!
trap 'kill $DEMO_PID $HYDRA_PID 2>/dev/null || true' EXIT
wait "$DEMO_PID"
