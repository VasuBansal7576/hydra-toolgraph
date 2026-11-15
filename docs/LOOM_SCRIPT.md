# loom script (~2–3 min) — v3

factual shot list for the toolgraph demo. no audio hype; show the product.

## 0:00 — title card

- text: `hydra-toolgraph` + one line: learn `REQUIRES` from traces; load with provenance + session skips
- terminal: `docker compose up` (or HF single-container on :7860)

## 0:15 — context meter + bundle

- browser: demo with `session_id=demo-session-1`, OpenTable reserve query
- click **1. load tools**
- zoom panel 1: preload-all tokens vs bundle tokens
- zoom panel 2: ranked tools + why-path (`REQUIRES` chain) + provenance `p`

## 0:45 — session skip

- click **2. record_result (first tool)** then **3. load again**
- show skipped line: `skipped, already resolved by …`
- optional: curl MCP `record_result` / `load_tools` with same `session_id`

## 1:10 — record trace (weaken unused)

- fill **tools_used** (subset of loaded)
- click **4. record_trace** — unused loaded tools get β penalty
- panel 3 timeline shows the edit; optional **explain** on a weakened edge

## 1:35 — undo + as_of

- paste `edit_id`, **undo**
- set datetime-local, **as_of snapshot** — edges + diff vs now from `REQUIRES_HIST`

## 2:00 — honest results card

- flash `docs/RESULTS_V3.md` headline: H1/H2/H4 pass, **H3 fail** (wrong-skip 25%)
- do not claim hydra beats kv on completeness; claim store-request + staleness + learning curve

## 2:20 — close

- link: repo README + HF Space deploy notes (`deploy/hf-space/README.md`)
- remind: no paid judge; ToolLinkOS synthetic; B2 constructed

## capture notes

- local docker only; no api keys on screen
- pre-warm embeds with `scripts/smoke_demo.py` before recording
