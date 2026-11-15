# loom script (~2–3 min)

factual shot list for the toolgraph demo. no audio hype; show the product.

## 0:00 — title card

- text: `hydra-toolgraph` + one line: learn tool edges from traces, load with provenance
- terminal visible: `docker compose up` (hydradb + demo on :5000)

## 0:15 — load tools

- browser: demo query box prefilled with OpenTable reserve (Italian Bistro)
- click **load tools**
- zoom: ranked list + provenance rows — OpenTable→Resy `SCHEMA_DEP`, then Resy→weather/holiday `DEPENDS_ON` with `prov` / `p`

## 0:45 — record trace

- fill **trace tools** (main first, helpers after)
- click **record trace**
- show JSON: `edit_ids`, `edges_added`
- optional: explain one helper — in/out edges + lesson field

## 1:10 — undo

- paste last `edit_id`, click **undo**
- reload tools: ranking/provenance should reflect removed edge

## 1:35 — as_of

- pick a datetime in the past (before traces were recorded)
- click **rebuild as_of**, then **load tools** again
- note fewer replayed edits / edges in JSON (`edits_replayed`, `edges_active`)

## 2:00 — mcp / compose (optional b-roll)

- cutaway: `curl -X POST localhost:8080/tools/load_tools -d '{"query":"..."}'`
- or editor showing `toolgraph/mcp_server.py` stdio + `--http` flag

## 2:20 — close

- one line on measured eval (checkpoint 2): grouped-main split, no jev, hydra ties sqlite cte on this dataset
- link: repo readme + `results/curve.json` (do not claim hydra beats metadata control)

## capture notes

- use local docker only; no api keys on screen
- if embed download is slow on first run, pre-warm with `scripts/smoke_demo.py` before recording
