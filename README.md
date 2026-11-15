# hydra-toolgraph

A context layer that learns which tools to load from agent traces, with versioned, undoable, provenance-tagged edges in HydraDB.

**Claim:** edges learned from traces. GRTF ([arXiv 2502.07223](https://arxiv.org/abs/2502.07223)) uses hand-built deps and lists automatic construction as future work. Graph-of-Skills ([arXiv 2604.05333](https://arxiv.org/abs/2604.05333)) says its graph is never revised from traces.

## Quickstart (< 5 commands)

```bash
git clone https://github.com/VasuBansal7576/hydra-toolgraph.git && cd hydra-toolgraph
pip install -r requirements.txt
docker compose up --build
# demo → http://localhost:5000
# mcp  → POST http://localhost:8080/tools/{load_tools,record_trace,undo_edit,as_of,explain}
```

Stdio MCP: `PYTHONPATH=. python3 -m toolgraph.mcp_server`  
Smoke (fake Hydra ok): `PYTHONPATH=. python3 scripts/smoke_demo.py`  
Data fetch: `docs/DATA.md`.

## Results (grouped-by-main, matched fan-in=32)

Headline split is by **main** (seeds 7/21/42). Measured leak: every ToolLinkOS main has exactly 3 queries with an identical gold set (`1569/1569`). A query-level split leaks siblings and inflates c16 from ~**0.53** (grouped, k4 ablation) to ~**0.91** (query-level appendix).

**Matched fan-in:** metadata also-load, one-hop+prior, CTE+prior, and Hydra+prior all seed from the same **32** fused hits (`toolgraph/arms.py` `ARM_KNOBS`). The 4-seed graph variants are labelled ablations only.

Main-macro completeness@16 (mean of 3 seeds). Source: `results/curve.json` v2.3-matched-fanin.

| Traces | Flat | Metadata | 1-hop (32) | CTE (32) | Hydra (32) | Hydra k4 | Trace-only (32) | Oracle |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 0.030 | 0.033 | 0.033 | 0.033 | 0.033 | 0.030 | 0.030 | 0.955 |
| 100 | 0.030 | 0.227 | 0.227 | 0.227 | 0.227 | 0.117 | 0.224 | 0.955 |
| 300 | 0.030 | 0.453 | 0.453 | 0.452 | 0.452 | 0.247 | 0.454 | 0.955 |
| 939 | 0.030 | **0.649** | **0.649** | **0.649** | **0.656** | 0.532 | 0.657 | 0.955 |

Full-trace mAP@10: Hydra 0.571, metadata 0.559, CTE 0.561, flat 0.208. GRTF **0.856** = reference, different protocol.

**Blunt call:** after matching fan-in, Hydra **ties** metadata and the equal-depth CTE within seed spread (Hydra 0.656 vs metadata 0.649; Δ=0.007, not beyond spread). It does **not** win a completeness contest against the vector-store also-load control. The earlier metadata lead (0.657 vs 0.532) was largely the fan-in confound (32 vs 4); the k4 ablation recovers that gap (0.532). Honest HydraDB value here: versioned edits, undo, provenance, as_of.

Composition slice: mean ~2 queries / ≤1 main; when non-empty, matched arms score 1.0 @16.

## Product

| Tool | Role |
| --- | --- |
| `load_tools(query, k)` | fusion + Hydra learned/prior walk; provenance edges per tool |
| `record_trace(query, tools_used)` | learn edges with do/undo + versioning |
| `undo_edit(edit_id)` | roll back a recorded edit |
| `as_of(date)` | replay edit log up to a timestamp |
| `explain(tool)` | in/out edges + lesson |

Demo: query → loaded tools with why-edges → record trace → undo → as_of.

## Limits

- ToolLinkOS instances are synthetic; transfer to live MCP catalogs is unproven.
- Completeness@16 does not separate Hydra from matched CTE/metadata on this dataset.
- `as_of` is edit-log replay, not native HydraDB path time-travel.
- No Jev / no paid LLM judge. I/O prior is deterministic overlap ≥ 0.6 only.

## Reproduce curve + tests

```bash
export PYTHONPATH=.
python3 -m unittest discover -s tests -v
python3 -m toolgraph.eval_curve
```

Decisions: `docs/DECISIONS.md`. Drafts (do not post): `docs/LOOM_SCRIPT.md`, `docs/GTM_POST.md`.
