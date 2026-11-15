# Decision trail (v2 → fix-first)

One row per step. Hypotheses and gates are written **before** the sealed scores they judge. Numbers that land after a run are labelled **measured**. Interpretations are labelled **inferred**.

## Pre-registered rise test

| Field | Value |
| --- | --- |
| Metric | sealed main-macro completeness@16 (grouped-by-main split) |
| Arms judged for rise | `hydra_mspaths_prior`, `hydra_mspaths_trace_only` |
| Null point | mean @16 at 0 learn traces |
| Full point | mean @16 after all learn traces |
| Seed spread | max(seed @16) − min(seed @16) at the full point |
| Pass rule | `(full − null) > 3 × seed_spread` |
| Fail rule | otherwise report flat / within noise |
| HydraDB vs recursive-CTE+prior | if HydraDB ties within seed spread, say so; honest HydraDB value is then versioned edits, undo, provenance, weighted multi-hop, as_of |
| HydraDB vs metadata-vector | report honestly; do not claim a graph win if also-load matches |
| Forbidden | tuning on sealed; feeding catalog `depends_on` into learned arms; mixing trace-only and prior-enabled endpoints |

## Design choices

| ID | Hypothesis / change | Why | Keep / revert rule |
| --- | --- | --- | --- |
| D0 | Hide every catalog `depends_on` edge from learned arms. Oracle is ceiling only. | v1 closure at 0 traces ≈ 0.95 because labels were loaded up front. | Keep. |
| D1 | Learn `DEPENDS_ON(main → helper)` only from learn-split gold sets as traces. Beta confidence; τ on **dev** only. | Trace learning is the claim vs GRTF/GoS. | Keep. |
| D2 | Deterministic I/O prior at overlap ≥ 0.6. **No Jev.** | Codex review: drop LLM edge judge; GoS uses ζ=0.6 deterministically. | Keep. |
| D3 | Headline split is **grouped by main** (seeds 7/21/42). Query-level split is appendix-only and labelled leaky. | Measured: every main has exactly 3 queries with identical gold (1569/1569); query split leaks siblings. | Keep. |
| D4 | Controls share seeds/fusion/top-k/fallback: flat BM25+dense; metadata also-load; one-hop+prior; CTE+prior; Hydra MSpaths+prior; oracle; ablations (trace removed / prior removed). | Fair ablation. | Keep. |
| D5 | MSpaths assigns **minimum prefix cost** per node (not path total). | Codex: s→a→b was {s:0,a:2,b:2}; CTE gives {s:0,a:1,b:2}. | Keep; guarded by unit test. |
| D6 | Composition = sealed mains with **no learned out-edges**, gold reachable via `SCHEMA_DEP` then `DEPENDS_ON`. | Old selector was definitionally empty. | Keep. |
| D7 | Product (MCP/demo/compose) deferred until after fix-first report. | Checkpoint stop rule. | Keep. |

## Claims hygiene

- Trace-only endpoint (`hydra_mspaths_trace_only`): previous query-split micro numbers were c16 **0.0349 → 0.9111**, mAP@10 **0.8484**. Do not mix with prior-enabled.
- Prior-enabled (`hydra_mspaths_prior`) starts above flat at 0 traces because of the I/O prior (previous query-split cold start c16 **0.0402**).
- GRTF mAP@10 **0.856** is **reference, different protocol** (hand-built catalog deps; not grouped-main; not our arms).

## Run log

| Step | Change | Measurement | Decision |
| --- | --- | --- | --- |
| R0–R8 | Checkpoint 1 sweep curve on leaky query split; Jev border band; composition size 0. | measured; see archived `results/curve.json` v2.1-sweep | superseded |
| R9 | Independent Codex gpt-5.1-sol review: fix-first. Leak fact confirmed. | process | proceed |
| R10 | Drop Jev entirely. Grouped-main headline. Metadata-vector + CTE/one-hop with prior. MSpaths prefix costs. Composition redefined. Tests run without matplotlib. | measured in R12 | keep |
| R11 | Checkpoint 2 = fix-first report only. Still no MCP / demo / compose / Loom / GTM. | process | keep |
| R12 | Grouped-main curve (`results/curve.json` v2.2-fix-first). Rise **passed**. Hydra **tied** CTE+prior within spread. Hydra **lost** to metadata-vector also-load (c16 0.532 vs 0.657 at full). Composition mean n≈2 queries / ≤1 main; when non-empty, graph arms score 1.0 @16. | measured | keep; confound identified: metadata fan-in=32 vs graph seeds=4 |
| R13 | Matched fan-in: `ARM_KNOBS` forces metadata / one-hop / CTE / Hydra to share fused[:32]. k4 variants labelled ablations. `tests/test_arm_parity.py` guards knob parity. Product: MCP + demo + compose. | measured v2.3-matched-fanin: Hydra 0.656 ≈ metadata/CTE 0.649 (tie within spread); k4 ablation 0.532 confirms confound; rise passed | keep; blunt: Hydra does not beat matched metadata |
| R14 | Ship MCP (stdio+HTTP), demo UI, docker compose, Loom/GTM drafts. | process | keep |

## HydraDB constraints (re-checked)

Measured against `ghcr.io/hydra-db/hydradb:latest` **v0.4.0** (labelled).

| Constraint | Status |
| --- | --- |
| Writes require `UNWIND $rows` batch form | **measured**: plain `MERGE…SET` / `MATCH…CREATE` rejected |
| Vertex upsert | `UNWIND $rows AS row MERGE (a {id: row.id}) SET a:Label, …` |
| Rel create then SET | two statements: `UNWIND…CREATE` then `UNWIND…MATCH…SET` |
| One relationship type per `MATCH`; one statement per request | **measured** |
| `min` / `max` aggregates | still **rejected** |
| `CONTAINS` | still **rejected** |
| `IN` and `IS NULL` | appear **accepted** in v0.4.0 (unlike older `cypher-compat.md` claim); still prefer open-hist sentinel `valid_to = 9e15` over `IS NULL` |
| `algo.MSpaths` | inlined `sourceValues` string lists; `costProp` / `maxLen` in edges; pairs with no path silently absent |
| Native time travel | **none** — `REQUIRES_HIST` + client `as_of` |
| `MERGE` without `UNWIND` | **rejected** |

## v3 pre-registered hypotheses (H1–H4)

Written **before** any sealed B1/B2/staleness/H4 run. Spread = max − min across
seeds (7/21/42) at the point being judged. No tuning on sealed.

| ID | Claim | Pass rule |
| --- | --- | --- |
| **H1 multi-hop** | On sealed mains with gold depth ≥ 2: `hydra_graph` completeness@16 ≥ `iterative_search` − spread, and its LLM turns are lower by more than 3 × spread. Also report against `flat_topk`. | Pass if both completeness and LLM-turn inequalities hold; else fail and report measured gaps. |
| **H2 plain store, honest** | Expect completeness ties between `hydra_graph`, `kv_hop`, and `kv_closure`; report the tie. The graph passes only on store requests (constant 2 vs `kv_hop` hops + 1) and staleness (`kv_closure` wrong-bundle rate > 0 while `hydra_graph` = 0). | Pass if completeness ties within spread **and** store-request edge holds **and** staleness edge holds; else fail the failing clause. |
| **H3 sessions** | `hydra_graph` tokens per turn are lower than `flat_topk` and `kv_hop` by more than 3 × spread, with wrong-skip rate ≤ 2%. Report `kv_hop_session` honestly; if it ties on tokens, the graph's remaining edge is store requests. | Pass if token inequalities hold and wrong-skip ≤ 2%; else fail. |
| **H4 learning curve** | Over trace budgets 0, 10, 30, 100, 300, all: `hydra_graph` tokens per complete task fall by more than 3 × spread from null to full while completeness@16 does not drop by more than spread. Keep the existing completeness rise test `(full − null) > 3 × seed_spread`. | Pass if token drop and non-drop completeness both hold; completeness rise reported separately. |

### Arms under test (retrieval-seeded share `fused[:32]`)

`preload_all`, `flat_topk`, `iterative_search`, `kv_hop`, `kv_hop_session`, `kv_closure`, `hydra_graph`, `oracle`.

### Metrics (per task, then main-macro mean ± seed spread)

completeness@16; tool-schema tokens (tiktoken cl100k); LLM turns on tool discovery; store requests; load_tools p50/p95 against real HydraDB; B2 only: tokens saved by skipping, wrong-skip rate.

### Staleness protocol (pre-registered)

After learning on all learn traces, apply 200 random edits from the edit log (retire, undo, reweight). After each edit, run 50 sealed queries and count bundles that differ from the bundle computed from the true current edges. Compare `kv_closure` (materialised closure rebuilt every 50 edits) against `hydra_graph`.

### Forbidden

Tuning on sealed; feeding catalog `depends_on` into learned arms (benchmark constructor and iterative_search simulator only, documented); claiming completeness separates graph from KV on ToolLinkOS.
