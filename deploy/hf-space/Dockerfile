# Hugging Face Spaces single-container image (sdk: docker, app_port 7860).
# Embeds HydraDB graph-node + the Toolgraph demo in one process tree via entrypoint.sh.
#
# Build context MUST be the repository root:
#   docker build -f deploy/hf-space/Dockerfile -t toolgraph-hf .
#
# If pulling ghcr.io/hydra-db/hydradb fails (auth / mirror), set
# TOOLGRAPH_MODE=readonly to serve committed results/v3_report.json only.

FROM ghcr.io/hydra-db/hydradb:latest

USER root
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    build-essential \
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
RUN chmod +x /app/entrypoint.sh \
    && mkdir -p /var/run/secrets/slatedb-graph /data/store /app/fastembed_cache

ENV CLOUD_PROVIDER=local \
    LOCAL_PATH=/data/store \
    GRAPH_NAMESPACE=default \
    GRAPH_ID=default \
    GRAPH_ALLOW_PLAINTEXT=true \
    GRAPH_USAGE_REPORT_ENABLED=false \
    GRAPH_AUTH_TOKEN_FILE=/var/run/secrets/slatedb-graph/auth-token \
    HYDRA_URL=http://127.0.0.1:8443 \
    DEMO_PORT=7860 \
    DEMO_SEED_LEARN=1 \
    DEMO_SEED=7 \
    DEMO_TAU=0.15 \
    PYTHONPATH=/app \
    FASTEMBED_CACHE_PATH=/app/fastembed_cache \
    TOOLGRAPH_MODE=live

# HF Spaces docker SDK listens on this port.
EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=5 \
  CMD curl -fsS http://127.0.0.1:7860/api/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
