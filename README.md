# hydra-toolgraph

Tool loader for the HydraDB hackathon, Track 2. It retrieves a small set of tools for a task and walks prerequisite edges so a dependency that lexical search never names can still be loaded.

The sealed learning curve did **not** rise. MCP, the web UI, Docker Compose, the Loom script, and the GTM draft were not built. The numbers below are from one committed run.

## Gate

**Failed the rise test. Passed the flat-baseline test.**

| Check | Result | Label |
| --- | --- | --- |
| HydraDB closure beats BM25+dense on sealed completeness | Yes, at every budget | measured |
| HydraDB closure beats SQLite one-hop | Yes, at every budget | measured |
| Mean sealed completeness finishes higher after more learn traces (+0.01 on @10 or @16) | No | measured |

Source: `results/curve.json` field `gate`, and `results/completeness_curve.png`.

Closure is already far above the flat and one-hop arms at zero learn traces. Extra traces did not move the mean sealed score up at the end of the run. @10 peaked in the middle and ended almost where it started. @16 drifted down.

## Measured curve

ToolLinkOS: 573 tools, 1,569 queries. Split 60% learn / 20% dev / 20% sealed, seeds 7, 21, and 42. Sealed size is 315 queries per seed. The improver reads learn traces only and a 40-query dev canary. It does not receive sealed ids.

X-axis is learn traces whose edits were kept. Targets were 0, 10, 30, and 90. Canary rollbacks pulled the mean kept counts down to 0, 6.7, 26.7, and 73.3. Values are the mean of the three seeds.

### completeness@10

| Traces kept (mean) | BM25+dense | SQLite one-hop | HydraDB MSpaths | Closure + Jev |
| --- | ---: | ---: | ---: | ---: |
| 0 | 0.031 | 0.115 | 0.662 | 0.063 |
| 6.7 | 0.031 | 0.115 | 0.662 | 0.063 |
| 26.7 | 0.031 | 0.115 | 0.687 | 0.063 |
| 73.3 | 0.031 | 0.115 | 0.670 | 0.063 |

### completeness@16

| Traces kept (mean) | BM25+dense | SQLite one-hop | HydraDB MSpaths | Closure + Jev |
| --- | ---: | ---: | ---: | ---: |
| 0 | 0.035 | 0.115 | 0.949 | 0.063 |
| 6.7 | 0.035 | 0.115 | 0.949 | 0.063 |
| 26.7 | 0.035 | 0.115 | 0.947 | 0.063 |
| 73.3 | 0.035 | 0.115 | 0.924 | 0.063 |

Per-seed HydraDB @10 at the last point: seed 7 went 0.670 → 0.746, seed 21 went 0.676 → 0.610, seed 42 went 0.641 → 0.654. The mean hides that split. Seed 21's sealed score fell after the canary had accepted earlier batches.

Completeness@k is 1 when every labelled gold tool is inside the first k names returned.

## Tokens and latency

Full catalog, name + description + parameters, cl100k_base: **26,951 tokens** (measured).

Mean tokens in the top 16 names, same tokenizer, at zero traces:

| Arm | Tokens @16 | Share of catalog |
| --- | ---: | ---: |
| BM25+dense | 798 | 3.0% |
| SQLite one-hop | 349 | 1.3% |
| HydraDB MSpaths | 415 | 1.5% |
| Closure + Jev | 88 | 0.3% |

Jev's token count is low because Noul drops most of the closure. That set is also incomplete (see the table above).

p95 in `results/curve.json` is the stage **after** the fused pool exists. It does not include embedding.

| Arm | p95 at 0 traces | What the timer covers |
| --- | ---: | --- |
| BM25+dense | 0.001 ms | Copying the precomputed fused list |
| SQLite one-hop | 0.029 ms | One SQL hop |
| HydraDB MSpaths | 8.3 ms | `algo.MSpaths` over Bolt/HTTP |
| Closure + Jev, cold | 237 ms | Choice + Noul + the same closure |
| BM25 online, index already built | 0.50 ms | Separate measurement, all 1,569 queries, top 64. `results/bm25_online_latency.json` |

Dense query encoding was not timed per query. Encoding the whole catalog and the query set is index build, done once before the curve.

At the last point, MSpaths p95 was 11.3 ms and top-16 tokens were 493. Lessons added after misses account for the token increase (measured: the same field grows across budgets in `curve.json`).

## What each arm does

- **BM25+dense.** BM25 and `sentence-transformers/all-MiniLM-L6-v2` (fastembed, local ONNX) fused with reciprocal rank fusion, k=60. No graph. Pool of 32.
- **SQLite one-hop.** The same `DEPENDS_ON` and `FOLLOWED_BY` rows as HydraDB, read with SQL from the seed tools only. This is the plain-store ablation.
- **HydraDB MSpaths.** Seeds are the top 4 fused tools. `algo.MSpaths` walks `DEPENDS_ON` and `FOLLOWED_BY` outgoing, `maxLen` 4, `costProp` `cost`. Seed i is charged `i * 2` plus the path cost.
- **Closure + Jev.** Jev Choice picks the seeds from the fused pool. Jev Noul drops a candidate below a threshold. The threshold is chosen on the dev split at zero traces from {0.3, 0.5, 0.7, 0.9} and then frozen. Every seed selected **0.3**. Dev @10 at that threshold was 0.0447, 0.0543, and 0.0703. Higher thresholds were worse.

Catalog edges start at cost 1.0 (direct) or 1.6 (indirect). A learn trace whose top 10 missed a gold tool asks Jev Score whether a needed tool is missing. Score below 0.5 vetoes the edit. Otherwise the loader strengthens a catalog edge into the miss, or adds `FOLLOWED_BY` when the miss was not reached, and writes a one-line lesson. Each edit is a do/undo Cypher pair in `results/edits.jsonl`, plus an `:Edit` node. A batch is undone when dev-canary @10 falls.

Across the three seeds the improver recorded 177 traces with no miss in the top 10, 76 Score vetoes, and 17 edited traces. Five batches were rolled back.

## Jev

Status: enabled. Requests used model `jev-latest`.

| | |
| --- | ---: |
| HTTP calls | 2,847 |
| Cache hits (not re-sent) | 6,684 |
| Input tokens | 2,823,544 |
| Output tokens | 789,323 |
| HTTP errors | 0 |

The key is read from `JEV_API_KEY` only. Token counts are the `usage` object on each live response, summed from `results/jev_usage.jsonl`.

**Inferred:** Noul under-calls fictional prerequisites, so a threshold fit on dev still deletes gold tools and completeness collapses toward the flat arm. That is an interpretation of the measured gap between closure (0.662) and closure+Jev (0.063) at @10.

## HydraDB and SelfGraph

HydraDB `ghcr.io/hydra-db/hydradb:latest` ran with `CLOUD_PROVIDER=local` and `LOCAL_PATH`. Image id and those env values are in `results/hydra_runtime.json`. Queries used the HTTP API on port 8443. `algo.MSpaths` was called with `costProp: 'cost'` and the loader sorts by the returned `pathCost`. A second graph id and a second namespace were rejected with HTTP 403 for this token, so iterations share one graph and roll back with the undo statement.

SelfGraph `as_of` was run on embedded Kuzu, not on the tool graph. `retrieve(..., as_of="2021-06-01")` hid a `WORKS_AT` fact dated 2022-01-01 and kept the 2020 fact (measured, `results/selfgraph_as_of.json`). A current read still showed both facts: the SDK did not retire the older one without a contradiction rule. `as_of` filters SelfGraph relations. It does not snapshot HydraDB path topology. **Inferred:** it does not replace the undo pair for this loader.

## How to reproduce the curve

Python 3.12, the packages in `requirements.txt`, and a local HydraDB as above. Data files are the upstream ToolLinkOS JSON under `data/` (MIT, see `data/UPSTREAM_LICENSE`).

```bash
export JEV_API_KEY=...   # omit to record the Jev arm as "skipped: no key"
export PYTHONPATH=.
python3 -m toolgraph.eval_curve
```

The command rewrites `results/curve.json`, `results/completeness_curve.png`, `results/edits.jsonl`, and `results/jev_usage.jsonl`. It reads learn and dev while scoring, and sealed only inside `score_split`.

## Not in this repo

No MCP server, web UI, Dockerfile, or Compose file. No Loom script and no GTM post. Those were the step that follows a rising sealed curve.
