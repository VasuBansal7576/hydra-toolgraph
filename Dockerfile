FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake MiniLM into the image so runtime does not need HuggingFace egress.
ENV FASTEMBED_CACHE_PATH=/app/fastembed_cache
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/all-MiniLM-L6-v2')"

COPY data ./data
COPY toolgraph ./toolgraph
COPY demo ./demo
COPY scripts ./scripts

ENV PYTHONPATH=/app
ENV HYDRA_URL=http://hydradb:8443
ENV HYDRA_TOKEN=local-development-token-32-bytes

EXPOSE 8080 5000

CMD ["python", "-m", "toolgraph.mcp_server", "--http", "8080", "--http-only"]
