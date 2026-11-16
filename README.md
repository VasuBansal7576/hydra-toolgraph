# ToolGraph

**Finding an action is not the same as finding everything it needs.**

ToolGraph is an inspectable tool-context layer for agent builders: retrieve candidate tools, add observed tool associations, and inspect the evidence behind the bundle. Record observed traces, compare the next retrieval, and undo a learned edit. The demo retrieves definitions; it does not execute tools or establish task success.

[Inspect the recorded ToolSandbox comparison](https://hydra-toolgraph.onrender.com/#evidence) · [Open the dependency workbench](https://hydra-toolgraph.onrender.com/) · [Evaluation and limitations](docs/RESULTS_V3.md)

### Try the evidence loop

1. Enter a specific task and inspect its returned tools and dependency paths.
2. Pin that response as the before snapshot.
3. On a write-enabled local instance, record exact tool names from an actual observed trace. Public write-disabled deployments keep this control unavailable.
4. Retrieve the same task again and inspect the additions, removals and evidence. Then try a related task separately. A changed query is not a controlled before/after comparison.
5. Undo the returned edit ID and inspect retrieval again. An unchanged bundle is a valid result, not a hidden failure.

**Current boundary:** this is a retrieval and learning prototype, not a validated execution planner. The current runtime combines Hydra paths with local learned-edge closure and labels that mode explicitly. The sealed v3 results below describe the earlier evaluated policy, not a new benchmark for this hybrid path. Session skipping is disabled in the runtime: tool-name equality does not prove that a prior result satisfies a new request. Session IDs are retained for trace provenance. The historical evaluation of session skipping below is unchanged.

## Executed transfer with real upstream tools

We froze a new recipient task against **15 real ToolSandbox tools**, then executed the returned bundles in the actual upstream sandbox. At a three-tool budget, BM25 omitted contact lookup and could not send. After one observed repair episode, the learned bundle included lookup, cellular repair and send, and the scripted execution succeeded with cellular initially off or on. No real SMS was sent.

**The equally informed local-closure control tied Hydra.** This shows a benefit from recorded experience in one authored continuation, not a graph-database quality advantage or autonomous-agent benchmark. The already-enabled control still recorded a redundant setter error, and budget 5 used more tokens than BM25.

[All 12 outcomes and limits](examples/toolsandbox/TRANSFER_RESULTS.md) · [Frozen protocol](examples/toolsandbox/TRANSFER_PROTOCOL.md) · [Compact phone-free receipt](examples/toolsandbox/transfer_results.json) · [Reproduce with upstream tools](examples/toolsandbox/README.md)

## New: live-state execution guards

The original proof exposed a redundant repair when cellular was already enabled. A new, explicitly authored `execute_guarded` helper now checks current state, repairs only when needed, and rechecks before acting. In actual upstream execution across three changing-recipient tasks, it removed that error: **1 to 0 tool errors**, while both arms still sent all three sandbox messages correctly. The cost was **9 to 13 tool calls**, with the same four tools and 587 schema tokens loaded by each arm.

These are developer-supplied preconditions, not rules learned by Hydra. The original frozen retrieval experiment remains unchanged. [Protocol, implementation behavior and reproduction](examples/toolsandbox/GUARDS.md) · [Verified phone-free summary](examples/toolsandbox/guard_results.json)

## Historical synthetic evaluation

A context layer that learns which tools to load from agent traces, with versioned, undoable, provenance-tagged `REQUIRES` edges in HydraDB; the earlier experiment also included session `RESOLVED` skips.

**Headline:** H3 sessions **FAIL** (wrong-skip 25%; tokens not below kv_hop). H1/H4 pass with disclosed cuts; H2 passes **only under a post-hoc modified staleness metric** (see below).

**Claim:** edges learned from traces. GRTF ([arXiv 2502.07223](https://arxiv.org/abs/2502.07223)) uses hand-built deps and lists automatic construction as future work. Graph-of-Skills ([arXiv 2604.05333](https://arxiv.org/abs/2604.05333)) says its graph is never revised from traces.

## H4 learning curve

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
| **hydra_graph** | **0.475** | **1123** | **0** | **2†** | **202‡** |
| oracle | 0.955 | 956 | 0 | 1 | 3 |

† hydra `store_requests=2` is **asserted by design** when `session_id` is set (`eval_v3.py` hard-codes 2; guarded by unit test) — **not** counted via `RecordingHydraClient` in the sealed B1 loop.
‡ latency column is the **mean of per-seed p50s** (seed7 446.5 / seed21 89.7 / seed42 70.3 → 202.2), not a pooled p50 over all tasks.

hydra trails kv by **0.070** c16 (inside hydra seed spread 0.097; **loses 2/3 seeds** — wins seed 7, loses 21 & 42).

### Hypotheses (pre-registered)

| ID | Result | One line |
| --- | --- | --- |
| H1 multi-hop | **PASS** | hydra ≫ iterative on c16; 0 vs ~50 LLM turns. **Gold-depth≥2 filter not applied** (all sealed mains). |
| H2 plain store | **PASS only under a post-hoc modified staleness metric** | c16 trails kv by 0.070 (inside spread); store reqs asserted=2; phantoms-only hydra staleness=0 (pre-reg set≠ metric measured hydra **1.0** on same cut) |
| **H3 sessions** | **FAIL** | hydra tokens not below kv_hop; wrong-skip **25%** > 2% |
| H4 learning curve | **PASS** | token drop + c16 rise on seed 7 (single-seed caveat) |

## Where plain store holds vs breaks

- **H2 (qualified):** completeness gap 0.070 sits inside hydra seed spread but hydra **loses 2/3 seeds**. Constant-2 store requests are by design. Staleness: kv_closure wrong 0.03; hydra phantoms 0 — but the pre-registered set-inequality metric gave hydra wrong-bundle **1.0** (MSpaths truncation ≠ truth). See `docs/DECISIONS.md` deviation row + `docs/RESULTS_V3.md`.
- **H3 FAIL:** session skips are not safe (wrong-skip 0.25); hydra tokens/turn sit near kv_hop_session, above kv_hop.

## Leak / matched fan-in (v2 carry-forward)

Every ToolLinkOS main has exactly 3 queries with an identical gold set (`1569/1569`). A query-level split leaks siblings. v2 matched fan-in (`fused[:32]`) removed the metadata confound; v3 arms share the same fan-in via `ARM_KNOBS`.

## Prior art

- GRTF mAP@10 **0.856** = reference, different protocol (hand-built catalog deps).
- Graph-of-Skills: graph never revised from traces; we learn `REQUIRES` from co-occurrence (+ β negative feedback).
- AutoTool / Anthropic Tool Search Tool / MCP-Zero: retrieval- or search-oriented tool selection without versioned, undoable, provenance-tagged graph edges learned from traces (different claim surface).

## Limits

- ToolLinkOS is synthetic; B2 is constructed; the read-only and invalidation rules are name-based.
- Deadline cuts: B2 + H4 + staleness on seed **7** only; staleness **40×20** not 200×50; H1 **without** gold-depth≥2 filter. See `docs/RESULTS_V3.md`.
- Staleness hydra metric was changed post-hoc to phantoms-only in the results commit; H2 pass is qualified.
- MSpaths (`pathCount=64`, `resultLimit=500`) can truncate vs full BFS.
- Session wrong-skip remains high (H3 fail).
- `as_of` uses `REQUIRES_HIST` (+ local hist); no native HydraDB time-travel.
- Public demo writes (`record_trace` / `undo_edit` / `record_result`) require `DEMO_ALLOW_WRITES=1`.
- No Jev / no paid LLM judge.
- Hosted demo default is **live + BM25** (no MiniLM) with precomputed edges; dense MiniLM needs ≥1–2 GB. Readonly is fallback when Hydra is down or seeding times out — see `deploy/hf-space/README.md`.
- Demo `load_tools` applies an **IDF-anchor + relative expand cutoff** so generic verbs like `book` do not seed Uber-style pollution; sealed eval fan-in is unchanged (headline c16 numbers above are untouched). See `results/demo_seed_policy.json`.

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
| `load_tools(query, session_id, k)` | fusion + MSpaths over `REQUIRES`; design: 2 store requests with session |
| `record_trace(...)` | co-occurrence + unused-load β (gated by `DEMO_ALLOW_WRITES`) |
| `record_result(...)` | Entity+RESOLVED; INVALIDATES clears (gated) |
| `undo_edit(edit_id)` | restore previous live REQUIRES + HIST undo (gated) |
| `as_of(ts)` | REQUIRES_HIST snapshot + diff vs now |
| `explain(tool)` | in/out REQUIRES + lesson |

Demo panels: task input, dependency evidence, pinned before/after tool-set comparison, explicit observed-trace recording, undo, and the raw API audit trail. Session results are not fabricated by clicking the first tool.
Read-only mode labels the bundle as a **no-graph retrieval fallback** and does not claim store requests. Context-meter tokens use the same JSON-schema cl100k definition as B1 (~49k preload), not tool `base_text`.

## Reproduce

```bash
export PYTHONPATH=.
python3 -m unittest discover -s tests -v
python3 scripts/run_v3_sealed.py --only b1   # needs HydraDB on :8443
```

Decisions: `docs/DECISIONS.md`. Sealed numbers: `docs/RESULTS_V3.md`. Drafts (do not post): `docs/LOOM_SCRIPT.md`, `docs/GTM_POST.md`.
