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
| R12 | Grouped-main curve (`results/curve.json` v2.2-fix-first). Rise **passed**. Hydra **tied** CTE+prior within spread. Hydra **lost** to metadata-vector also-load (c16 0.532 vs 0.657 at full). Composition mean n≈2 queries / ≤1 main; when non-empty, graph arms score 1.0 @16. | measured | keep; be blunt about metadata win |
