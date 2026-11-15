# Decision trail (v2)

One row per step. Hypotheses and gates are written **before** the sealed scores they judge. Numbers that land after a run are labelled **measured**. Interpretations are labelled **inferred**.

## Pre-registered rise test (written before any v2 sealed score)

| Field | Value |
| --- | --- |
| Metric | sealed completeness@16 |
| Arms judged for rise | `hydra_learned`, `hydra_learned_jev` |
| Null point | mean @16 at 0 learn traces |
| Full point | mean @16 after all learn traces |
| Seed spread | max(seed @16) − min(seed @16) at the full point |
| Pass rule | `(full − null) > 3 × seed_spread` |
| Fail rule | otherwise report flat / within noise |
| HydraDB vs recursive-CTE | if HydraDB ties the same-depth SQLite CTE within the seed spread, say so; honest HydraDB value is then versioned edits, undo, provenance, weighted multi-hop, as_of |
| Forbidden | tuning thresholds or arms on sealed; feeding catalog `depends_on` into any learned arm |

## Design choices locked before the run

| ID | Hypothesis / change | Why | Keep / revert rule |
| --- | --- | --- | --- |
| D0 | Hide every catalog `depends_on` edge from learned arms. Oracle catalog edges are a ceiling arm only. | v1 closure at 0 traces ≈ 0.95 because labels were loaded up front; that fails the plain-store test. | Keep unless oracle arm itself fails to approach catalog closure (implementation bug). |
| D1 | Learn `DEPENDS_ON(main → helper)` only from learn-split gold sets treated as observed traces. Each edit is a do/undo Cypher pair with count + provenance. | Matches Warp-style reversible edits and SkillGraph-style trajectory mining. | Keep if edge counts grow with traces; revert batch on canary drop. |
| D2 | Propose schema candidates from param/name PRODUCES↔REQUIRES signals; verify with Jev Noul ("does A need B first?"); calibrate threshold on **learn** only; never prune the final closure with Jev. | Graph structure should beat a co-use list on unseen tools. v1 Noul-as-closure-pruner deleted gold helpers. | Keep edges above calibrated threshold; if Jev disabled, mark `hydra_learned_jev` skipped. |
| D3 | Arms share identical fusion seeds, budgets, depth (`MAX_LEN=4`), and scoring: flat BM25+dense; SQLite recursive CTE; HydraDB learned (−Jev); HydraDB learned (+Jev edges); oracle catalog. | Fair ablation: depth must not confound storage (v1 SQLite was 1-hop). | Keep. |
| D4 | Budgets 0, 10, 30, 100, 300, all; seeds 7, 21, 42; report mean + spread; split sealed into main-seen vs main-unseen in learn. | Learning curve + transfer. | Keep. |
| D5 | Ship MCP + demo + compose only after the curve artifact is committed. | Product without a measured curve is theatre. | Keep. |

## Run log

| Step | Change | Measurement | Decision |
| --- | --- | --- | --- |
| R0 | Branch `cursor/toolgraph-v2-learn-edges-1c92` off v1; pre-register D0–D5 and rise test. | n/a (before run) | proceed |
| R1 | Implement memory-first edge upserts + bulk HydraDB sync. Per-edge MATCH/SET after a full catalog load stack-overflowed graph-node (exit 139). | measured: smoke c16@40 after 50 traces: flat 0.10, CTE=Hydra 0.225, oracle 0.85; CTE set@16 == Hydra set@16 | keep bulk sync; do/undo still logged |
| R2 | Arms: flat / sqlite_cte / hydra_learned / hydra_learned_jev / oracle; budgets 0,10,30,100,300,all. | pending full curve | — |
