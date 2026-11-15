---
title: hydra-toolgraph
sdk: docker
app_port: 7860
---

# Hugging Face Space — single-container Toolgraph

One Docker image can run **HydraDB (`graph-node`)** and the **demo UI** together.
Listens on **`$PORT` if set (Render), else 7860** (HF Spaces). Auth token from an env secret — never commit a real token.

## Non-root / free-tier hosts

HF Spaces and Render-style hosts often run the container as **uid 1000**. This image:

- Keeps all writable state under **`/tmp/toolgraph`** (token, store, caches) with mode **1777**
- Writes the auth token **only in live mode**, after the readonly short-circuit
- Honours **`$PORT`** then `DEMO_PORT` then `7860`
- Defaults **`TOOLGRAPH_MODE=readonly`**, **`DEMO_FUSION=bm25`**, **`DEMO_SEED_LEARN=0`**, **`DEMO_ALLOW_WRITES=0`**
- **HTTP binds immediately**; `/api/health` reports `phase` (`starting` → `seeding` → `ready` / `readonly_fallback`) without blocking on MiniLM/Hydra
- If cgroup memory ≤768 MiB, entrypoint forces readonly + BM25

### Memory (512 MB free / 0.1 CPU)

| Mode | Expected | Notes |
| --- | --- | --- |
| `readonly` + `DEMO_FUSION=bm25` (default) | fits ~512 MB | no MiniLM load; committed RESULTS + fusion ranking |
| `live` + dense MiniLM | often OOM / hang on free tier | set only on ≥1–2 GB instances |

Verify locally:

```bash
docker run --rm --user 1000 --memory 512m --cpus 0.1 -p 7860:7860 toolgraph-hf
# within ~5s of port open:
curl -s http://127.0.0.1:7860/api/health   # phase seeding|ready|readonly_fallback
# after ready:
curl -s -X POST http://127.0.0.1:7860/api/load_tools -H 'content-type: application/json' \
  -d '{"query":"book a table","k":4}'
```

## Why HydraDB can be embedded

`ghcr.io/hydra-db/hydradb:latest` (v0.4.0) ships `graph-node` on Ubuntu 24.04. This Dockerfile uses that image as the base, installs Python + app deps, and starts both from `entrypoint.sh`. No DinD.

If the base image cannot be pulled, or RAM is tight, set `TOOLGRAPH_MODE=readonly`.

## Exact deploy steps (do this yourself — agents must not deploy)

1. Create a Hugging Face Space → **Docker** SDK → **App port = 7860** (or set Render `PORT`).
2. Build from repo root with `deploy/hf-space/Dockerfile`.
3. Secrets: `HYDRA_TOKEN` (≥32 bytes) for live mode. Optional: `GRAPH_AUTH_TOKEN`.
4. Variables: `TOOLGRAPH_MODE=live|readonly`, `DEMO_SEED=7`, `DEMO_ALLOW_WRITES=0` (default).
5. Do **not** commit real tokens.

## Local proof (including non-root)

```bash
docker build -f deploy/hf-space/Dockerfile -t toolgraph-hf .

# live as uid 1000
docker run --rm --user 1000 -p 7860:7860 \
  -e HYDRA_TOKEN=local-development-token-32-bytes \
  -e DEMO_SEED_LEARN=0 toolgraph-hf
curl -s http://127.0.0.1:7860/api/health

# readonly as uid 1000 (512 MB–friendly)
docker run --rm --user 1000 -p 7860:7860 -e TOOLGRAPH_MODE=readonly toolgraph-hf
curl -s http://127.0.0.1:7860/api/health   # read_only: true
```

## Compose (multi-container) still available

Repo-root `docker compose up --build` (hydradb :8443, mcp :8080, demo :5000). HF/Render path is intentionally **one container**.
