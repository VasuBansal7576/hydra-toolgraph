# hydra-toolgraph

A context layer that learns which tools to load from agent traces, with versioned, undoable, provenance-tagged edges in HydraDB.

**Claim:** edges learned from traces. GRTF ([arXiv 2502.07223](https://arxiv.org/abs/2502.07223)) uses hand-built deps and lists automatic construction as future work. Graph-of-Skills ([arXiv 2604.05333](https://arxiv.org/abs/2604.05333)) says its graph is never revised from traces.

## Quickstart (< 5 commands)

```bash
git clone https://github.com/VasuBansal7576/hydra-toolgraph.git && cd hydra-toolgraph
# data/ ships with the repo; otherwise see docs/DATA.md
pip install -r requirements.txt
docker compose up --build
# demo → http://localhost:5000
# mcp  → POST http://localhost:8080/tools/{load_tools,record_trace,undo_edit,as_of,explain}
```

Stdio MCP (no Docker for the server process; still needs HydraDB on `:8443`):

```bash
export PYTHONPATH=.
python3 -m toolgraph.mcp_server
```

Smoke without Hydra (fake client): `PYTHONPATH=. python3 scripts/smoke_demo.py`

## Honest results (grouped-by-main, matched fan-in=32)

Headline protocol: split by **main** (seeds 7/21/42). Every ToolLinkOS main has exactly 3 queries with an identical gold set (`1569/1569`); a query-level split leaks siblings and inflates c16 from ~**0.53** (grouped) to ~**0.91** (query-level). That leak finding is a contribution.

**Matched fan-in:** metadata also-load, one-hop+prior, CTE+prior, and Hydra+prior all seed from the same **32** fused hits (`ARM_KNOBS` in `toolgraph/arms.py`). The old 4-seed graph variants are labelled ablations.

| Traces | Flat | Metadata | 1-hop+prior (32) | CTE+prior (32) | Hydra+prior (32) | Hydra k4 ablation | Oracle |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| *(filled from `results/curve.json` v2.3-matched-fanin after rerun)* | | | | | | | |

See `results/curve.json` gate notes for the blunt call on whether Hydra beats metadata after matching fan-in. GRTF mAP@10 **0.856** is **reference, different protocol**.

## Product surface

| Tool | Role |
| --- | --- |
| `load_tools(query, k)` | BM25+dense fusion + Hydra learned/prior walk; returns tools with provenance edges |
| `record_trace(query, tools_used)` | Learn edges with do/undo Cypher pairs + versioning |
| `undo_edit(edit_id)` | Roll back a recorded edit |
| `as_of(date)` | Replay the edit log up to a timestamp |
| `explain(tool)` | In/out edges + lesson for one tool |

Demo UI: query → loaded tools with why-edges → record trace → undo → as_of.

## Limits

- ToolLinkOS gold sets are synthetic (gpt-4o generated instances); transfer to live MCP catalogs is unproven.
- On the previous unmatched-fan-in run, metadata also-load beat Hydra; matched-fan-in results are in `results/curve.json`.
- HydraDB advantage on this dataset is versioning / undo / provenance / as_of, not a large completeness gap vs an equal-depth SQLite CTE once fan-in matches.
- No Jev / no paid LLM judge. I/O prior is deterministic overlap ≥ 0.6 only.
- `as_of` is edit-log replay, not a native HydraDB time-travel API for path topology.

## Reproduce the curve

```bash
export PYTHONPATH=.
python3 -m unittest discover -s tests -v
python3 -m toolgraph.eval_curve
```

Data: `docs/DATA.md`. Decisions: `docs/DECISIONS.md`. Loom / GTM drafts (do not post): `docs/LOOM_SCRIPT.md`, `docs/GTM_POST.md`.
