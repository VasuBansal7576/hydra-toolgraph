---
title: hydra-toolgraph
sdk: docker
app_port: 7860
---

# Hugging Face Space — single-container Toolgraph

One Docker image runs **HydraDB (`graph-node`)** and the **demo UI** together.
The Space listens on **port 7860**. Auth token comes from an env secret — never commit a real token.

## Why HydraDB can be embedded

`ghcr.io/hydra-db/hydradb:latest` (v0.4.0) ships a self-contained `graph-node` binary on Ubuntu 24.04.
This Dockerfile uses that image as the base, installs Python 3.12 + app deps, and starts both processes from `entrypoint.sh`.
No nested Docker / DinD is required.

If the base image cannot be pulled (GHCR auth, offline build, license block), set `TOOLGRAPH_MODE=readonly` — the demo serves committed `results/v3_report.json` and labels itself read-only (no live graph writes).

## Exact deploy steps (do this yourself — agents must not deploy)

1. Create a new Hugging Face Space → **Docker** SDK → set **App port = 7860**.
2. Push this repository (or copy `deploy/hf-space/Dockerfile` to the Space root and keep build context = repo root).  
   Space Dockerfile path options:
   - **Recommended:** Space repo is this monorepo; set Dockerfile to `deploy/hf-space/Dockerfile` in Space settings if supported, **or**
   - Copy `deploy/hf-space/Dockerfile` + `entrypoint.sh` to the Space root and keep `COPY` paths pointing at repo root layout (`data/`, `toolgraph/`, `demo/`, `results/`).
3. In Space **Settings → Secrets**, add:
   - `HYDRA_TOKEN` = a ≥32-byte bearer token (same value HydraDB will accept).  
     Optional alias: `GRAPH_AUTH_TOKEN`.
4. Optional Space variables:
   - `TOOLGRAPH_MODE=live` (default) or `readonly`
   - `DEMO_SEED=7`, `DEMO_TAU=0.15`, `DEMO_SEED_LEARN=1`
5. Build / restart the Space. Open the Space URL — demo UI on `/`, health at `/api/health`.
6. Do **not** commit real tokens. The image default `local-development-token-32-bytes` is for local compose only.

## Local proof (same image the Space would run)

```bash
# from repo root
docker build -f deploy/hf-space/Dockerfile -t toolgraph-hf .
docker run --rm -p 7860:7860 -e HYDRA_TOKEN=local-development-token-32-bytes toolgraph-hf

# in another shell
curl -s http://127.0.0.1:7860/api/health
curl -s -X POST http://127.0.0.1:7860/api/load_tools \
  -H 'content-type: application/json' \
  -d '{"query":"book a table on OpenTable","session_id":"hf-proof","k":8}'
```

Read-only fallback proof:

```bash
docker run --rm -p 7860:7860 -e TOOLGRAPH_MODE=readonly toolgraph-hf
curl -s http://127.0.0.1:7860/api/health   # expect "read_only": true
curl -s http://127.0.0.1:7860/api/results  # committed v3_report.json
```

## Compose (multi-container) still available

For local multi-service dev, use repo-root `docker compose up --build` (hydradb :8443, mcp :8080, demo :5000). The HF Space path is intentionally **one container**.
