# hydra-toolgraph

Tool loader for the HydraDB hackathon, Track 2: learn prerequisite edges from execution traces, then load a complete tool set for a task.

**Claim:** edges learned from traces (GRTF arXiv 2502.07223 uses hand-built deps and lists automatic construction as future work; Graph-of-Skills arXiv 2604.05333 says its graph is never revised from traces).

**Not claimed yet:** that HydraDB multi-hop beats a metadata also-load control on ToolLinkOS. Numbers below come from the fix-first re-run; see `results/curve.json` and `docs/DECISIONS.md`.

## Checkpoint status

Checkpoint 1 (sweep curve on a **leaky query-level split**) was judged fix-first by an independent review.

Checkpoint 2 = those fixes only. **No MCP server, web demo, Docker Compose, Loom, or GTM** in this checkpoint.

## Leak fact (measured)

On ToolLinkOS every main tool has exactly **3** queries with an **identical** gold set (`1569/1569`). A query-level split therefore leaks a sibling's answer set. The **headline** protocol is a **grouped-by-main** split (seeds 7/21/42). The old query split remains only as a labelled appendix in `results/curve.json`.

## What changed in fix-first

1. **Jev removed** from arms, code path, and required env. I/O prior is deterministic overlap ≥ 0.6 only.
2. **Grouped-by-main** headline split; main-macro scores with bootstrap CIs clustered by main.
3. Controls (identical seeds, fusion, top-k, fallback): flat BM25+dense; metadata-vector also-load; one-hop+prior; CTE+prior; Hydra MSpaths+prior; oracle; ablations (trace removed / prior removed).
4. **MSpaths prefix costs** (s→a→b → `{s:0,a:1,b:2}`), with a unit test.
5. **Composition** redefined: sealed mains with no learned out-edges, gold reachable via prior then learned edges.
6. Tests run **without matplotlib**. Data fetch documented in `docs/DATA.md`.

## Claims hygiene

| Endpoint | Note |
| --- | --- |
| Trace-only (`hydra_mspaths_trace_only`) | Previous leaky query-split micro: c16 **0.0349 → 0.9111**, mAP@10 **0.8484**. Do not mix with prior-enabled. |
| Prior-enabled (`hydra_mspaths_prior`) | Cold start is above flat because of the I/O prior (previous query-split c16@0 **0.0402**). |
| GRTF mAP@10 **0.856** | **Reference, different protocol.** |

## Reproduce

Python 3.12, packages in `requirements.txt`, local HydraDB on `:8443`, and ToolLinkOS JSON under `data/` (see `docs/DATA.md`).

```bash
# optional: matplotlib only needed for the PNG curve
pip install -r requirements.txt
export PYTHONPATH=.
python3 -m unittest discover -s tests -v
python3 -m toolgraph.eval_curve
```

No `JEV_API_KEY` is required or read.

## Arms (headline)

| Arm | Role |
| --- | --- |
| `bm25_dense` | Flat BM25+dense fusion |
| `metadata_vector` | Same retrieval, then append each hit's also-load list from learned+prior edges |
| `sqlite_one_hop_prior` | One-hop SQL over `DEPENDS_ON` + `SCHEMA_DEP` |
| `sqlite_cte_prior` | Recursive CTE depth=4 over the same edges |
| `hydra_mspaths_prior` | HydraDB `algo.MSpaths` over the same edges |
| `hydra_mspaths_trace_only` | Ablation: prior edges removed |
| `hydra_mspaths_prior_only` | Ablation: trace edges removed |
| `oracle` | Catalog `ORACLE_DEP` ceiling |

## Not in this repo (yet)

No MCP server, web UI, Dockerfile, Compose file, Loom script, or GTM post. Those wait for an explicit product-phase instruction after this fix-first report.
