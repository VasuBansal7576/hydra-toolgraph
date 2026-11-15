---
title: hydra-toolgraph
sdk: docker
app_port: 7860
---

# Hugging Face Space — single-container Toolgraph

One Docker image runs **HydraDB (`graph-node`)** and the **demo UI** together.
Listens on **`$PORT` if set (Render), else 7860** (HF Spaces). Auth token from an env secret — never commit a real token.

## Non-root / free-tier hosts

HF Spaces and Render-style hosts often run the container as **uid 1000**. This image:

- Keeps all writable state under **`/tmp/toolgraph`** (token, store, caches) with mode **1777**
- Honours **`$PORT`** then `DEMO_PORT` then `7860`
- Defaults **`TOOLGRAPH_MODE=live`**, **`DEMO_FUSION=bm25`**, **`DEMO_LOAD_PRECOMPUTED_EDGES=1`**, **`DEMO_ALLOW_WRITES=0`**
- Seeds the graph from `results/demo_requires_edges.json` (~2.7k REQUIRES); falls back to learn-split ingest if that file is missing
- **HTTP binds immediately**; `/api/health` reports `phase` (`starting` → `seeding` → `ready` / `readonly_fallback`)
- If cgroup memory ≤768 MiB, entrypoint forces **BM25** (not readonly) — skips MiniLM
- Readonly only when HydraDB is unreachable, seeding exceeds `DEMO_SEED_TIMEOUT_SEC` (default 180s), or `TOOLGRAPH_MODE=readonly`

### Memory (512 MB free / 0.1 CPU)

| Mode | Expected | Notes |
| --- | --- | --- |
| `live` + `DEMO_FUSION=bm25` (default) | fits ~512 MB | graph-node + precomputed edges; no MiniLM |
| `live` + dense MiniLM | often OOM / restart on free tier | set only on ≥1–2 GB instances |
| `readonly` + BM25 | emergency fallback | no graph-node; committed RESULTS only |

Verify locally:

```bash
docker run --rm --user 1000:1000 --memory 512m --cpus 0.1 -p 7860:7860 toolgraph-hf
# within a few seconds of port open:
curl -s http://127.0.0.1:7860/api/health   # phase seeding|ready|readonly_fallback
# after phase=ready, edges > 0:
curl -s -X POST http://127.0.0.1:7860/api/load_tools -H 'content-type: application/json' \
  -d '{"query":"book a table on OpenTable","k":8}'
```

## Why HydraDB can be embedded

`ghcr.io/hydra-db/hydradb:latest` (v0.4.0) ships `graph-node` on Ubuntu 24.04. This Dockerfile uses that image as the base, installs Python + app deps, and starts both from `entrypoint.sh`. No DinD.

If the base image cannot be pulled, set `TOOLGRAPH_MODE=readonly`.

## Exact deploy steps (do this yourself — agents must not deploy)

1. Create a Hugging Face Space → **Docker** SDK → **App port = 7860** (or set Render `PORT`).
2. Build from repo root with `deploy/hf-space/Dockerfile`.
3. Secrets: `HYDRA_TOKEN` (≥32 bytes) for live mode. Optional: `GRAPH_AUTH_TOKEN`.
4. Variables: `TOOLGRAPH_MODE=live|readonly`, `DEMO_SEED=7`, `DEMO_ALLOW_WRITES=0` (default).
5. Do **not** commit real tokens.

## Local proof (including non-root)

```bash
docker build -f deploy/hf-space/Dockerfile -t toolgraph-hf .

# live as uid 1000 on a free-tier shaped box
docker run --rm --user 1000:1000 --memory 512m --cpus 0.1 -p 7860:7860 \
  -e HYDRA_TOKEN=local-development-token-32-bytes \
  toolgraph-hf
curl -s http://127.0.0.1:7860/api/health   # edges > 0 when ready

# explicit readonly
docker run --rm --user 1000 -p 7860:7860 -e TOOLGRAPH_MODE=readonly toolgraph-hf
curl -s http://127.0.0.1:7860/api/health   # read_only: true
```

## Compose (multi-container) still available

Repo-root `docker compose up --build` (hydradb :8443, mcp :8080, demo :5000). HF/Render path is intentionally **one container**.
