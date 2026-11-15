# Hugging Face Spaces / Render-style single-container image.
# sdk: docker, app_port 7860. Honour $PORT at runtime (Render).
#
# Build context = repository root:
#   docker build -f deploy/hf-space/Dockerfile -t toolgraph-hf .
#
# Runtime is non-root friendly: writable state under /tmp/toolgraph (1777).
# For ≤512 MB free instances prefer TOOLGRAPH_MODE=readonly (no graph-node).

FROM ghcr.io/hydra-db/hydradb:latest

USER root
ENV DEBIAN_FRONTEND=noninteractive

# No build-essential — keep lean; wheels cover linux amd64.
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt \
    && python3 -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/all-MiniLM-L6-v2')"

COPY data /app/data
COPY toolgraph /app/toolgraph
COPY demo /app/demo
COPY scripts /app/scripts
COPY results /app/results
COPY deploy/hf-space/entrypoint.sh /app/entrypoint.sh

# World-writable runtime dirs so uid 1000 (HF/Render) can write token + store.
# graph-node also mkdir's /var/cache/slatedb/data (not fully redirected by LOCAL_PATH).
RUN chmod +x /app/entrypoint.sh \
    && mkdir -p /tmp/toolgraph/secrets /tmp/toolgraph/store /tmp/toolgraph/results \
       /tmp/toolgraph/fastembed_cache \
       /var/cache/slatedb/data \
    && chmod -R 1777 /tmp/toolgraph /var/cache/slatedb \
    && chmod -R a+rX /app

# Defaults tuned for Render/HF free tiers (512 MB). Override TOOLGRAPH_MODE=live for full Hydra.
ENV CLOUD_PROVIDER=local \
    TOOLGRAPH_RUNTIME=/tmp/toolgraph \
    LOCAL_PATH=/tmp/toolgraph/store \
    GRAPH_NAMESPACE=default \
    GRAPH_ID=default \
    GRAPH_ALLOW_PLAINTEXT=true \
    GRAPH_USAGE_REPORT_ENABLED=false \
    GRAPH_AUTH_TOKEN_FILE=/tmp/toolgraph/secrets/auth-token \
    HYDRA_URL=http://127.0.0.1:8443 \
    DEMO_PORT=7860 \
    DEMO_SEED_LEARN=0 \
    DEMO_SEED=7 \
    DEMO_TAU=0.15 \
    DEMO_ALLOW_WRITES=0 \
    DEMO_FUSION=bm25 \
    DEMO_SEED_TIMEOUT_SEC=45 \
    PYTHONPATH=/app \
    FASTEMBED_CACHE_PATH=/tmp/toolgraph/fastembed_cache \
    TOOLGRAPH_MODE=readonly

# Do not force USER root at runtime — hosts often remount as uid 1000.
EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=5 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-${DEMO_PORT:-7860}}/api/health" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
