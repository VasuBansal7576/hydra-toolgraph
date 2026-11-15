"""v2 sealed completeness curve: learn edges from traces, hide catalog deps.

Arms share seeds, budgets, depth, and scoring. Catalog depends_on is the
oracle ceiling only. Rise test is pre-registered in docs/DECISIONS.md.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import tiktoken

from toolgraph.data import Instance, Tool, load_dataset, split_indices
from toolgraph.graph import GraphStore, rank_costs
from toolgraph.hydra import HydraClient
from toolgraph.jev import JevClient
from toolgraph.learn import (
    apply_verified_schema_edges,
    calibrate_edge_threshold,
    ingest_trace_edges,
    propose_schema_candidates,
    verify_schema_edges,
)
from toolgraph.protocol import (
    CANARY_SIZE,
    FUSION_SEEDS,
    KS,
    MAX_LEN,
    RISE_SPREAD_MULTIPLIER,
    SEEDS,
    TRACE_BUDGETS,
)
from toolgraph.retrieve import build_fusion

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RESULTS = ROOT / "results"

# Arm -> relationship types walked. Flat uses none.
ARM_RELS: dict[str, list[str] | None] = {
    "bm25_dense": None,
    "sqlite_cte": ["DEPENDS_ON"],
    "hydra_learned": ["DEPENDS_ON"],
    "hydra_learned_jev": ["DEPENDS_ON", "SCHEMA_DEP"],
    "oracle": ["ORACLE_DEP"],
}
ARMS = tuple(ARM_RELS)


def completeness(ranked: list[str], gold: frozenset[str], k: int) -> bool:
    return gold.issubset(set(ranked[:k]))


def percentile_95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(0.95 * len(ordered)) - 1))
    return ordered[index]


def token_count(encoder: tiktoken.Encoding, tools: dict[str, Tool], names: list[str], lessons: dict[str, str]) -> int:
    total = 0
    for name in names:
        text = tools[name].base_text()
        lesson = lessons.get(name)
        if lesson:
            text = f"{text} Lesson: {lesson}"
        total += len(encoder.encode(text))
    return total


def load_arm(
    arm: str,
    fused: list[str],
    store: GraphStore,
    closure_cache: dict[tuple, dict[str, float]],
) -> tuple[list[str], float]:
    started = time.perf_counter()
    seeds = fused[:FUSION_SEEDS]
    rels = ARM_RELS[arm]
    if rels is None:
        ranked = list(fused)
    elif arm == "sqlite_cte":
        key = ("cte", store.edge_version, tuple(seeds), tuple(rels), MAX_LEN)
        costs = closure_cache.get(key)
        if costs is None:
            costs = store.recursive_cte(seeds, rels, MAX_LEN)
            closure_cache[key] = costs
        # Merge unused fused tools after the closure so ranking length stays useful.
        ranked = rank_costs(costs)
        for name in fused:
            if name not in costs:
                ranked.append(name)
    elif arm == "oracle":
        key = ("cte_oracle", store.edge_version, tuple(seeds), tuple(rels), MAX_LEN)
        costs = closure_cache.get(key)
        if costs is None:
            costs = store.recursive_cte(seeds, rels, MAX_LEN)
            closure_cache[key] = costs
        ranked = rank_costs(costs)
        for name in fused:
            if name not in costs:
                ranked.append(name)
    elif arm in {"hydra_learned", "hydra_learned_jev"}:
        key = ("hydra", store.edge_version, tuple(seeds), tuple(rels))
        costs = closure_cache.get(key)
        if costs is None:
            costs = store.closure(seeds, rels)
            closure_cache[key] = costs
        ranked = rank_costs(costs)
        for name in fused:
            if name not in costs:
                ranked.append(name)
    else:
        raise ValueError(arm)
    return ranked, (time.perf_counter() - started) * 1000.0


def score_split(
    arm: str,
    queries: list[Instance],
    fusion: list[list[str]],
    store: GraphStore,
    tools: dict[str, Tool],
    encoder: tiktoken.Encoding,
    closure_cache: dict[tuple, dict[str, float]],
    *,
    seen_mains: set[str] | None = None,
) -> dict:
    hits = {k: 0 for k in KS}
    hits_seen = {k: 0 for k in KS}
    hits_unseen = {k: 0 for k in KS}
    n_seen = 0
    n_unseen = 0
    latencies: list[float] = []
    tokens_k16: list[int] = []
    tokens_full: list[int] = []
    for number, query in enumerate(queries, start=1):
        ranked, latency = load_arm(arm, fusion[query.idx], store, closure_cache)
        latencies.append(latency)
        for k in KS:
            ok = completeness(ranked, query.gold, k)
            if ok:
                hits[k] += 1
            if seen_mains is not None:
                if query.main in seen_mains:
                    if ok:
                        hits_seen[k] += 1
                else:
                    if ok:
                        hits_unseen[k] += 1
        if seen_mains is not None:
            if query.main in seen_mains:
                n_seen += 1
            else:
                n_unseen += 1
        tokens_k16.append(token_count(encoder, tools, ranked[:16], store.lessons))
        tokens_full.append(token_count(encoder, tools, ranked, store.lessons))
        if number % 100 == 0:
            print(f"  {arm} {number}/{len(queries)}", flush=True)
    n = max(1, len(queries))
    out: dict = {
        "n": len(queries),
        "c10": hits[10] / n,
        "c16": hits[16] / n,
        "tokens_at_16": sum(tokens_k16) / n,
        "tokens_full_closure": sum(tokens_full) / n,
        "p95_ms": percentile_95(latencies),
    }
    if seen_mains is not None:
        out["n_seen_main"] = n_seen
        out["n_unseen_main"] = n_unseen
        out["c10_seen"] = hits_seen[10] / max(1, n_seen)
        out["c16_seen"] = hits_seen[16] / max(1, n_seen)
        out["c10_unseen"] = hits_unseen[10] / max(1, n_unseen)
        out["c16_unseen"] = hits_unseen[16] / max(1, n_unseen)
    return out


def canary_c10(
    canary: list[Instance],
    fusion: list[list[str]],
    store: GraphStore,
) -> float:
    hits = 0
    cache: dict[tuple, dict[str, float]] = {}
    for query in canary:
        ranked, _latency = load_arm("hydra_learned", fusion[query.idx], store, cache)
        if completeness(ranked, query.gold, 10):
            hits += 1
    return hits / max(1, len(canary))


def resolve_budgets(n_learn: int) -> list[int]:
    out: list[int] = []
    for item in TRACE_BUDGETS:
        if item == "all":
            out.append(n_learn)
        else:
            out.append(min(int(item), n_learn))
    # unique, sorted, keep order
    seen: set[int] = set()
    ordered: list[int] = []
    for value in out:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def run_seed(
    seed: int,
    tools: list[Tool],
    instances: list[Instance],
    fusion: list[list[str]],
    store: GraphStore,
    jev: JevClient,
    encoder: tiktoken.Encoding,
) -> dict:
    learn_ids, dev_ids, sealed_ids = split_indices(len(instances), seed)
    sealed_set = set(sealed_ids)
    if set(learn_ids) & sealed_set:
        raise RuntimeError("learn/sealed overlap")
    learn = [instances[idx] for idx in learn_ids]
    dev = [instances[idx] for idx in dev_ids]
    sealed = [instances[idx] for idx in sealed_ids]
    canary = dev[:CANARY_SIZE]
    by_name = {tool.name: tool for tool in tools}
    budgets = resolve_budgets(len(learn))
    print(f"seed {seed}: reload catalog (no learned edges yet)", flush=True)
    store.load_catalog(tools, include_oracle=True)
    if store.edit_log.exists():
        # Keep a per-seed section; truncate at seed start for a clean undo log.
        pass

    # Schema generalization + Jev edge verification (learn-only calibration).
    candidates = propose_schema_candidates(tools)
    print(f"seed {seed}: schema candidates {len(candidates)}", flush=True)
    scored = verify_schema_edges(jev, candidates, by_name)
    threshold, grid = calibrate_edge_threshold(scored, learn)
    schema_kept = apply_verified_schema_edges(store, scored, threshold) if jev.enabled else 0
    store.sync_learned_to_hydra()
    print(
        f"seed {seed}: edge noul threshold {threshold} grid={grid} "
        f"verified={len(scored)} kept={schema_kept} jev={jev.status()}",
        flush=True,
    )

    points: dict[str, list[dict]] = {arm: [] for arm in ARMS}
    rollbacks: list[dict] = []
    baseline = canary_c10(canary, fusion, store)
    cursor = 0
    edges_from_traces = 0

    def seen_mains_through(n: int) -> set[str]:
        return {query.main for query in learn[:n]}

    def evaluate(traces_used: int) -> None:
        cache: dict[tuple, dict[str, float]] = {}
        seen = seen_mains_through(traces_used)
        for arm in ARMS:
            if arm == "hydra_learned_jev" and not jev.enabled and schema_kept == 0:
                measured = {
                    "n": len(sealed),
                    "c10": None,
                    "c16": None,
                    "tokens_at_16": None,
                    "tokens_full_closure": None,
                    "p95_ms": None,
                    "skipped": "no Jev key / no schema edges",
                }
            else:
                measured = score_split(
                    arm, sealed, fusion, store, by_name, encoder, cache, seen_mains=seen
                )
            measured["traces"] = traces_used
            measured["budget_target"] = traces_used
            points[arm].append(measured)
            c10 = measured.get("c10")
            c16 = measured.get("c16")
            print(
                f"seed {seed} traces {traces_used} {arm} "
                f"c10={c10 if c10 is None else f'{c10:.3f}'} "
                f"c16={c16 if c16 is None else f'{c16:.3f}'}",
                flush=True,
            )

    evaluate(0)
    for target in budgets:
        if target == 0:
            continue
        while cursor < target:
            batch = learn[cursor : min(target, cursor + 10)]
            cursor += len(batch)
            start_applied = store.applied
            for query in batch:
                if query.idx in sealed_set:
                    raise RuntimeError("improver was handed a sealed query")
                edges_from_traces += ingest_trace_edges(store, query)
            if store.applied == start_applied:
                continue
            score = canary_c10(canary, fusion, store)
            if score + 1e-12 < baseline:
                rolled = store.applied - start_applied
                store.rollback(rolled)
                rollbacks.append(
                    {
                        "seed": seed,
                        "at_traces": cursor,
                        "batch": len(batch),
                        "canary_before": baseline,
                        "canary_after": score,
                        "edits_undone": rolled,
                    }
                )
                print(f"seed {seed}: rolled back batch canary {score:.3f} < {baseline:.3f}", flush=True)
            else:
                baseline = score
        store.sync_learned_to_hydra()
        evaluate(target)

    return {
        "seed": seed,
        "n_learn": len(learn),
        "n_dev": len(dev),
        "n_sealed": len(sealed),
        "budgets": budgets,
        "edge_noul_threshold": threshold,
        "edge_noul_grid": grid,
        "schema_candidates": len(candidates),
        "schema_scored": len(scored),
        "schema_kept": schema_kept,
        "edges_from_traces": edges_from_traces,
        "rollbacks": rollbacks,
        "points": points,
    }


def mean_points(seed_runs: list[dict]) -> dict[str, list[dict]]:
    averaged: dict[str, list[dict]] = {}
    for arm in ARMS:
        rows = []
        series = [run["points"][arm] for run in seed_runs]
        for index in range(len(series[0])):
            group = [series[seed_index][index] for seed_index in range(len(series))]
            # Drop skipped arms from the mean.
            usable = [point for point in group if point.get("c16") is not None]
            if not usable:
                rows.append(
                    {
                        "traces": group[0]["traces"],
                        "budget_target": group[0]["budget_target"],
                        "c10": None,
                        "c16": None,
                        "skipped": True,
                    }
                )
                continue
            c16s = [point["c16"] for point in usable]
            c10s = [point["c10"] for point in usable]
            row = {
                "traces": sum(point["traces"] for point in usable) / len(usable),
                "budget_target": group[0]["budget_target"],
                "c10": sum(c10s) / len(c10s),
                "c16": sum(c16s) / len(c16s),
                "tokens_at_16": sum(point.get("tokens_at_16") or 0.0 for point in usable) / len(usable),
                "tokens_full_closure": sum(point.get("tokens_full_closure") or 0.0 for point in usable) / len(usable),
                "p95_ms": sum(point.get("p95_ms") or 0.0 for point in usable) / len(usable),
                "c10_by_seed": c10s,
                "c16_by_seed": c16s,
                "c16_spread": max(c16s) - min(c16s),
                "c10_spread": max(c10s) - min(c10s),
            }
            if all("c16_seen" in point for point in usable):
                row["c16_seen"] = sum(point["c16_seen"] for point in usable) / len(usable)
                row["c16_unseen"] = sum(point["c16_unseen"] for point in usable) / len(usable)
                row["c10_seen"] = sum(point["c10_seen"] for point in usable) / len(usable)
                row["c10_unseen"] = sum(point["c10_unseen"] for point in usable) / len(usable)
            rows.append(row)
        averaged[arm] = rows
    return averaged


def plot(averaged: dict[str, list[dict]], path: Path) -> None:
    colors = {
        "bm25_dense": "#4b5563",
        "sqlite_cte": "#b45309",
        "hydra_learned": "#0f766e",
        "hydra_learned_jev": "#1d4ed8",
        "oracle": "#6b7280",
    }
    labels = {
        "bm25_dense": "BM25+dense flat",
        "sqlite_cte": "SQLite recursive CTE",
        "hydra_learned": "HydraDB learned",
        "hydra_learned_jev": "HydraDB + Jev edges",
        "oracle": "Oracle catalog",
    }
    fig, axes = plt.subplots(1, 2, figsize=(11.2, 4.6), sharey=True)
    for axis, key, title in zip(axes, ("c10", "c16"), ("completeness@10", "completeness@16")):
        for arm in ARMS:
            xs = []
            ys = []
            for point in averaged[arm]:
                if point.get(key) is None:
                    continue
                xs.append(point["traces"])
                ys.append(point[key])
            if not xs:
                continue
            axis.plot(xs, ys, marker="o", color=colors[arm], label=labels[arm], linewidth=2)
        axis.set_title(title)
        axis.set_xlabel("Learn traces")
        axis.set_ylim(0, 1)
        axis.grid(True, alpha=0.3)
    axes[0].set_ylabel("Sealed completeness")
    axes[1].legend(frameon=False, fontsize=8)
    fig.suptitle("v2: sealed completeness vs learn traces (mean of 3 seeds)")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=140)
    plt.close(fig)


def rise_gate(averaged: dict[str, list[dict]]) -> dict:
    """Pre-registered: (c16_all - c16_0) > 3 × seed_spread at the full point."""
    report: dict = {
        "passed": False,
        "rises": False,
        "beats_flat": False,
        "hydra_vs_cte": {},
        "notes": [],
        "rule": (
            f"(sealed @16 at all traces − @16 at 0 traces) > "
            f"{RISE_SPREAD_MULTIPLIER} × seed_spread at the full point"
        ),
    }
    flat = averaged["bm25_dense"]
    for arm in ("hydra_learned", "hydra_learned_jev"):
        series = averaged[arm]
        if not series or series[0].get("c16") is None or series[-1].get("c16") is None:
            report["notes"].append(f"{arm} skipped or incomplete")
            continue
        first = series[0]
        last = series[-1]
        delta = last["c16"] - first["c16"]
        spread = last.get("c16_spread")
        if spread is None:
            by_seed = last.get("c16_by_seed") or [last["c16"]]
            spread = max(by_seed) - min(by_seed)
        need = RISE_SPREAD_MULTIPLIER * spread
        rose = delta > need + 1e-12
        report["notes"].append(
            f"{arm}: delta_c16={delta:.4f} spread={spread:.4f} need>{need:.4f} rose={rose}"
        )
        if rose:
            report["rises"] = True
        for point, base in zip(series, flat):
            if point.get("c16") is None or base.get("c16") is None:
                continue
            if point["c16"] > base["c16"] + 1e-9 or (point.get("c10") or 0) > (base.get("c10") or 0) + 1e-9:
                report["beats_flat"] = True
    # HydraDB vs same-depth recursive CTE on the learned (no-Jev) edge set.
    hydra = averaged["hydra_learned"]
    cte = averaged["sqlite_cte"]
    comparisons = []
    for h_point, c_point in zip(hydra, cte):
        if h_point.get("c16") is None or c_point.get("c16") is None:
            continue
        spread = h_point.get("c16_spread") or 0.0
        diff = h_point["c16"] - c_point["c16"]
        comparisons.append(
            {
                "traces": h_point["traces"],
                "hydra_c16": h_point["c16"],
                "cte_c16": c_point["c16"],
                "diff": diff,
                "within_spread": abs(diff) <= spread + 1e-12,
            }
        )
    report["hydra_vs_cte"] = comparisons
    if comparisons and all(row["within_spread"] for row in comparisons):
        report["notes"].append(
            "HydraDB tied the recursive-CTE control within seed spread; "
            "honest HydraDB value is versioned edits, undo, provenance, weighted multi-hop, as_of"
        )
    report["passed"] = bool(report["rises"])
    return report


def main() -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    tools, instances = load_dataset(DATA)
    encoder = tiktoken.get_encoding("cl100k_base")
    catalog_tokens = sum(len(encoder.encode(tool.base_text())) for tool in tools)
    print(f"embedding {len(tools)} tools and {len(instances)} queries", flush=True)
    fusion = build_fusion(tools, [query.text for query in instances])
    hydra = HydraClient("http://127.0.0.1:8443", "local-development-token-32-bytes")
    jev = JevClient(RESULTS / "jev_cache.json", RESULTS / "jev_usage.jsonl")
    print(f"jev {jev.status()}", flush=True)
    edit_log = RESULTS / "edits.jsonl"
    if edit_log.exists():
        edit_log.unlink()
    if jev.usage_log.exists():
        jev.usage_log.unlink()
    store = GraphStore(hydra, edit_log)
    seed_runs = []
    for seed in SEEDS:
        seed_runs.append(run_seed(seed, tools, instances, fusion, store, jev, encoder))
        (RESULTS / f"seed_{seed}.json").write_text(json.dumps(seed_runs[-1], indent=2))
        jev.flush()
    averaged = mean_points(seed_runs)
    decision = rise_gate(averaged)
    usage_tokens_in = 0
    usage_tokens_out = 0
    usage_lines = 0
    if jev.usage_log.exists():
        for line in jev.usage_log.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            usage_tokens_in += int(row["input_tokens"])
            usage_tokens_out += int(row["output_tokens"])
            usage_lines += 1
    payload = {
        "version": 2,
        "dataset": "ToolLinkOS",
        "source": "https://github.com/EliasLumer/Graph-RAG-Tool-Fusion-ToolLinkOS",
        "tools": len(tools),
        "queries": len(instances),
        "embed_model": "sentence-transformers/all-MiniLM-L6-v2",
        "embedder": "fastembed local ONNX",
        "catalog_tokens": catalog_tokens,
        "tokenizer": "cl100k_base",
        "seeds": list(SEEDS),
        "trace_budgets": [item if item != "all" else "all" for item in TRACE_BUDGETS],
        "max_len": MAX_LEN,
        "k": list(KS),
        "arms": list(ARMS),
        "design": {
            "catalog_depends_on_in_learned_arms": False,
            "oracle_rel": "ORACLE_DEP",
            "learned_rel": "DEPENDS_ON",
            "schema_rel": "SCHEMA_DEP",
            "jev_prunes_closure": False,
            "rise_test": decision["rule"],
        },
        "jev": {
            "status": jev.status(),
            "model": "jev-latest",
            "calls": usage_lines,
            "cache_hits": jev.cache_hits,
            "input_tokens": usage_tokens_in,
            "output_tokens": usage_tokens_out,
            "errors": jev.errors,
        },
        "mean": averaged,
        "by_seed": seed_runs,
        "gate": decision,
    }
    (RESULTS / "curve.json").write_text(json.dumps(payload, indent=2))
    plot(averaged, RESULTS / "completeness_curve.png")
    jev.flush()
    print(json.dumps({"gate": decision, "jev_tokens_in": usage_tokens_in, "jev_tokens_out": usage_tokens_out}, indent=2))


if __name__ == "__main__":
    main()
