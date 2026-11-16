# v4 pre-registration — close hydra↔kv c16 gap

**Timestamp (UTC):** 2026-11-16T07:05:00Z  
**Timestamp (IST):** 2026-11-16 12:35 IST  
**Branch:** `cursor/toolgraph-v2-learn-edges-1c92`  
**Parent HEAD at pre-registration:** `2aadec7e9080ec415ed55028a101ccbf0ef2e064`  
**Diagnosis artifact:** `results/v4_diagnosis_faithful.json` (faithful replay of sealed seeds 7/21/42 vs live HydraDB)

## Diagnosis (locked before this file)

On sealed B1, hydra mean c16 **0.475** trails kv **0.545** by **0.070** and loses seeds **21** and **42** (wins **7**). Per-query replay:

1. **Dominant miss = MSpaths truncation vs full memory BFS/CTE** — when kv succeeds and hydra fails, gold helpers are almost always present in `recursive_cte` top-16 but absent from `algo.MSpaths` (`pathCount=64`, `resultLimit=500`) output (35 / 40 / 51 cases on seeds 7 / 21 / 42).
2. **Secondary miss = expansion crowding** — non-gold co-occurrence neighbors fill the k=16 budget and displace fused gold tools such as `*_user_login` (17 / 24 / 26 cases).
3. **Not primarily tau or raw seed retrieval** — memory CTE alone nearly matches kv (e.g. seed 21: cte 0.610 vs kv 0.619); seeds-only c16 stays ≈0.03. Tau differs by seed (0.15 vs 0.55) but both arms share the same store.

## Pre-registered fix (ONE primary + ONE variant)

### Variant A — primary (will be evaluated first)

**Name:** `hydra_merge_memory_cte`

After the existing Hydra `load_tools` MSpaths call (same seeds / τ / fan-in as sealed v3), **merge** the local SQLite `recursive_cte` closure over those same fused seeds into the ranked bundle:

- Keep Hydra path costs / why-paths when present.
- Insert any CTE-reachable tools missing from the MSpaths result (fills truncation holes).
- Re-rank with the same `rank_costs` / `SEED_PENALTY` convention; take top-k = 16.
- Do **not** change fusion, τ calibration, PATH_COUNT, or kv arms.

**Predicted effect:** hydra c16 moves toward memory-CTE / kv on seeds where truncation dominated; store_requests stay asserted 2 when `session_id` is set.

### Variant B — backup (only if A is blocked by a bug before first fresh-seed number)

**Name:** `hydra_idf_expand_cutoff`

Apply the demo `IDF-anchor + relative expand cutoff (α=0.5)` so MSpaths `sourceValues` are only the expand-seed subset, while the full fused fan-in remains available for seed padding. Addresses crowding + MSpaths budget waste. **Not** run on the fresh split unless A cannot execute.

## Fresh evaluation protocol (also locked)

- **Fresh seeds never used in v3 sealed headlines or this diagnosis tuning:** `101`, `202`, `303`.
- Split: existing `split_by_main` grouped-main protocol; learn → calibrate τ on dev → score sealed.
- Arms: `kv_hop`, `hydra_graph` **baseline** (no merge), `hydra_graph` **variant A**.
- Metric: main-macro completeness@16 (same as B1). Report per-seed and mean.
- **No post-hoc metric changes.** No τ grid changes beyond existing `calibrate_tau` on that seed's dev split.
- Pass criterion (informative, not a new H-gate): mean hydra_A c16 ≥ mean kv c16 − 0.01 on these 3 seeds **or** hydra_A beats baseline hydra by ≥ 0.03 mean without falling further behind kv than baseline did.

## Outcome handling (locked)

- **If A wins** (meets criterion above): update README headline table with fresh-seed numbers + link to this file.
- **If A loses:** add an honest **v4** section to README / RESULTS; **leave** the v3 headline numbers unchanged.
