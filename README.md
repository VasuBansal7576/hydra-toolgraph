# hydra-toolgraph

A context layer that learns which tools to load from agent traces, with versioned, undoable, provenance-tagged `REQUIRES` edges in HydraDB — plus session `RESOLVED` skips.

**Claim:** edges learned from traces. GRTF ([arXiv 2502.07223](https://arxiv.org/abs/2502.07223)) uses hand-built deps and lists automatic construction as future work. Graph-of-Skills ([arXiv 2604.05333](https://arxiv.org/abs/2604.05333)) says its graph is never revised from traces.

## H4 learning curve (headline)

hydra_graph on ToolLinkOS grouped-by-main, seed **7** (seeds 21/42 cut for deadline). Source: `docs/RESULTS_V3.md` / `results/v3_h4_seed7.json`. **measured.**

| Traces | c16 | tokens / complete |
| ---: | ---: | ---: |
| 0 | 0.013 | 1277 |
| 10 | 0.066 | 1218 |
| 30 | 0.116 | 1147 |
| 100 | 0.255 | 1138 |
| 300 | 0.399 | 1134 |
| 939 | 0.472 | 1148 |

Tokens fall null→full (−130); completeness rises (+0.46). Pre-reg H4 **PASS** on seed 7 (spread=0 with one seed — caveat in RESULTS).

## Agent-level table by arm (B1, seeds 7/21/42)

Main-macro completeness@16. **measured.** Pre-reg sha `6264be76…`.

| Arm | c16 | tokens | LLM turns | store reqs | p50 ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| preload_all | 1.000 | 49345 | 0 | 0 | 0 |
| flat_topk | 0.030 | 1420 | 0 | 0 | 0 |
| iterative_search | 0.047 | 1409 | 50.4 | 0 | 18 |
| kv_hop | 0.545 | 1103 | 0 | 5.67 | 1.1 |
| kv_hop_session | 0.545 | 1103 | 0 | 6.67 | 1.1 |
| kv_closure | 0.545 | 1103 | 0 | 1 | 0.1 |
| **hydra_graph** | **0.475** | **1123** | **0** | **2** | **202** |
| oracle | 0.955 | 956 | 0 | 1 | 3 |

### Hypotheses (pre-registered)

| ID | Result | One line |
| --- | --- | --- |
| H1 multi-hop | **PASS** | hydra ≫ iterative on c16; 0 vs ~50 LLM turns |
| H2 plain store | **PASS** | c16 tie within hydra seed spread; 2 store reqs; staleness kv 0.03 / hydra phantoms 0 |
| H3 sessions | **FAIL** | hydra tokens not below kv_hop; wrong-skip 25% > 2% |
| H4 learning curve | **PASS** | token drop + c16 rise on seed 7 (single-seed caveat) |

## Where plain store ties vs breaks

- **Ties (H2):** completeness within spread; Hydra wins on constant **2** store requests vs kv_hop ~5.7; kv_closure goes stale (0.03) while hydra phantoms stay 0 after sync.
- **Breaks (H3):** session skips are not yet safe enough (wrong-skip 0.25); hydra tokens/turn sit near kv_hop_session, above kv_hop.

## Leak / matched fan-in (v2 carry-forward)

Every ToolLinkOS main has exactly 3 queries with an identical gold set (`1569/1569`). A query-level split leaks siblings. v2 matched fan-in (`fused[:32]`) removed the metadata confound; v3 arms share the same fan-in via `ARM_KNOBS`.

## Prior art

- GRTF mAP@10 **0.856** = reference, different protocol (hand-built catalog deps).
- Graph-of-Skills: graph never revised from traces; we learn `REQUIRES` from co-occurrence (+ β negative feedback).

## Limits

- ToolLinkOS is synthetic; B2 is constructed; the read-only and invalidation rules are name-based.
- Deadline cuts: B2 + H4 + staleness on seed **7** only; staleness **40×20** not 200×50; H1 without gold-depth≥2 filter. See `docs/RESULTS_V3.md`.
- MSpaths (`pathCount=64`, `resultLimit=500`) can truncate vs full BFS — hydra c16 can trail kv on dense graphs even when store-request/staleness edges hold.
- Session wrong-skip remains high (H3 fail); do not claim production-ready session memory.
- `as_of` uses `REQUIRES_HIST` (+ local hist); no native HydraDB time-travel.
- No Jev / no paid LLM judge.

## Quickstart

```bash
git clone https://github.com/VasuBansal7576/hydra-toolgraph.git && cd hydra-toolgraph
pip install -r requirements.txt
docker compose up --build
# demo → http://localhost:5000
# mcp  → POST http://localhost:8080/tools/{load_tools,record_trace,record_result,undo_edit,as_of,explain}
```

Stdio MCP: `PYTHONPATH=. python3 -m toolgraph.mcp_server`  
Smoke: `PYTHONPATH=. python3 scripts/smoke_demo.py`  
HF Spaces single-container: `deploy/hf-space/README.md` (do not deploy without owner approval).

## Product

| Tool | Role |
| --- | --- |
| `load_tools(query, session_id, k)` | fusion + MSpaths over `REQUIRES`; 2 store requests with session |
| `record_trace(session_id, query, tools_used, tools_loaded)` | co-occurrence + unused-load β penalty |
| `record_result(session_id, tool, args_hash)` | Entity+RESOLVED; INVALIDATES clears |
| `undo_edit(edit_id)` | restore previous live REQUIRES + HIST undo |
| `as_of(ts)` | REQUIRES_HIST snapshot + diff vs now |
| `explain(tool)` | in/out REQUIRES + lesson |

Demo panels: context meter · bundle (why-path + skips) · edit timeline.

## Reproduce

```bash
export PYTHONPATH=.
python3 -m unittest discover -s tests -v
python3 scripts/run_v3_sealed.py --only b1   # needs HydraDB on :8443
```

Decisions: `docs/DECISIONS.md`. Sealed numbers: `docs/RESULTS_V3.md`. Drafts (do not post): `docs/LOOM_SCRIPT.md`, `docs/GTM_POST.md`.
