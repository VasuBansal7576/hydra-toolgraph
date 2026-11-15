"""Matched-fan-in grouped-main sealed curve (post fix-first).

Headline graph arms seed from the same FUSION_POOL (32) fused hits as
metadata also-load. 4-seed variants are labelled ablations. No Jev.
"""

from __future__ import annotations

import json
import math
import random
import time
from collections import defaultdict
from pathlib import Path

import tiktoken

from toolgraph.arms import ARM_KNOBS, ARMS, HEADLINE_ARMS, MATCHED_FANIN_ARMS
from toolgraph.data import Instance, Tool, load_dataset, split_by_main, split_indices
from toolgraph.graph import GraphStore, rank_costs
from toolgraph.hydra import HydraClient
from toolgraph.learn import (
    apply_io_prior,
    calibrate_tau,
    composition_queries,
    ingest_trace_edges,
    propose_schema_candidates,
)
from toolgraph.protocol import (
    BOOTSTRAP_SAMPLES,
    BOOTSTRAP_SEED,
    CANARY_SIZE,
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

# Back-compat alias used by older guards.
ARM_SPEC = {
    name: (knob.backend, list(knob.rels) if knob.rels is not None else None)
    for name, knob in ARM_KNOBS.items()
}

# Appendix (leaky query split) keeps a smaller arm set.
APPENDIX_ARMS = (
    "bm25_dense",
    "metadata_vector",
    "sqlite_cte_prior",
    "hydra_mspaths_prior",
    "oracle",
)


def completeness(ranked: list[str], gold: frozenset[str], k: int) -> bool:
    return gold.issubset(set(ranked[:k]))


def average_precision(ranked: list[str], gold: frozenset[str], k: int) -> float:
    """AP@k for set-valued gold (mAP comparable to ToolLinkOS / 2502.07223)."""
    if not gold:
        return 0.0
    hits = 0
    total = 0.0
    for index, name in enumerate(ranked[:k], start=1):
        if name in gold:
            hits += 1
            total += hits / index
    return total / min(len(gold), k)


def percentile_95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(0.95 * len(ordered)) - 1))
    return ordered[index]


def bootstrap_ci(values: list[float], samples: int = BOOTSTRAP_SAMPLES, seed: int = BOOTSTRAP_SEED) -> tuple[float, float, float]:
    if not values:
        return (0.0, 0.0, 0.0)
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(samples):
        draw = [values[rng.randrange(n)] for _ in range(n)]
        means.append(sum(draw) / n)
    means.sort()
    lo = means[int(0.025 * (samples - 1))]
    hi = means[int(0.975 * (samples - 1))]
    return (sum(values) / n, lo, hi)


def bootstrap_ci_clustered(
    by_main: dict[str, list[float]],
    samples: int = BOOTSTRAP_SAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float, float]:
    """Bootstrap over mains: each main contributes the mean of its query scores."""
    mains = sorted(by_main)
    if not mains:
        return (0.0, 0.0, 0.0)
    main_scores = [sum(by_main[main]) / max(1, len(by_main[main])) for main in mains]
    mean = sum(main_scores) / len(main_scores)
    rng = random.Random(seed)
    n = len(main_scores)
    means = []
    for _ in range(samples):
        draw = [main_scores[rng.randrange(n)] for _ in range(n)]
        means.append(sum(draw) / n)
    means.sort()
    lo = means[int(0.025 * (samples - 1))]
    hi = means[int(0.975 * (samples - 1))]
    return (mean, lo, hi)


def token_count(encoder: tiktoken.Encoding, tools: dict[str, Tool], names: list[str], lessons: dict[str, str]) -> int:
    total = 0
    for name in names:
        text = tools[name].base_text()
        lesson = lessons.get(name)
        if lesson:
            text = f"{text} Lesson: {lesson}"
        total += len(encoder.encode(text))
    return total


def _also_load_map(store: GraphStore, rel_types: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for edge in store.active_edges(rel_types):
        src = store.by_id.get(edge.src)
        dst = store.by_id.get(edge.dst)
        if src is None or dst is None:
            continue
        out[src].append(dst)
    for src, rows in out.items():
        out[src] = sorted(set(rows))
    return out


def metadata_vector_rank(fused: list[str], also_load: dict[str, list[str]]) -> list[str]:
    """Retrieve fused names, then append each hit's also-load list (one hop only)."""
    ranked: list[str] = []
    seen: set[str] = set()
    for name in fused:
        if name not in seen:
            ranked.append(name)
            seen.add(name)
        for neigh in also_load.get(name, ()):
            if neigh not in seen:
                ranked.append(neigh)
                seen.add(neigh)
    return ranked


def load_arm(
    arm: str,
    fused: list[str],
    store: GraphStore,
    closure_cache: dict[tuple, dict[str, float] | list[str]],
) -> tuple[list[str], float]:
    """Dispatch one arm using ARM_KNOBS fan-in / depth / rels / fallback."""
    started = time.perf_counter()
    knob = ARM_KNOBS[arm]
    fan_in = knob.fan_in
    seeds = list(fused) if fan_in is None else list(fused[:fan_in])
    backend = knob.backend
    rels = list(knob.rels) if knob.rels is not None else None
    depth = knob.depth if knob.depth is not None else MAX_LEN
    if backend == "flat":
        ranked = list(fused)
    elif backend == "metadata":
        assert rels is not None
        key = ("meta", store.edge_version, store.tau, tuple(rels), fan_in)
        also = closure_cache.get(key)
        if also is None:
            also = _also_load_map(store, rels)
            closure_cache[key] = also  # type: ignore[assignment]
        ranked = metadata_vector_rank(seeds, also)  # type: ignore[arg-type]
        if knob.fallback:
            for name in fused:
                if name not in ranked:
                    ranked.append(name)
    elif backend == "one_hop":
        assert rels is not None
        key = ("one", store.edge_version, store.tau, tuple(seeds), tuple(rels))
        costs = closure_cache.get(key)
        if costs is None:
            costs = store.one_hop(seeds, rels)
            closure_cache[key] = costs
        ranked = rank_costs(costs)  # type: ignore[arg-type]
        if knob.fallback:
            for name in fused:
                if name not in costs:
                    ranked.append(name)
    elif backend == "cte":
        assert rels is not None
        key = ("cte", store.edge_version, store.tau, tuple(seeds), tuple(rels), depth)
        costs = closure_cache.get(key)
        if costs is None:
            costs = store.recursive_cte(seeds, rels, depth)
            closure_cache[key] = costs
        ranked = rank_costs(costs)  # type: ignore[arg-type]
        if knob.fallback:
            for name in fused:
                if name not in costs:
                    ranked.append(name)
    elif backend == "mspaths":
        assert rels is not None
        # Hydra maxLen is protocol MAX_LEN; depth knob must match for parity.
        if depth != MAX_LEN:
            raise ValueError(f"{arm}: mspaths depth {depth} != MAX_LEN {MAX_LEN}")
        key = ("hydra", store.edge_version, store.tau, tuple(seeds), tuple(rels))
        costs = closure_cache.get(key)
        if costs is None:
            costs = store.closure(seeds, rels)
            closure_cache[key] = costs
        ranked = rank_costs(costs)  # type: ignore[arg-type]
        if knob.fallback:
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
    closure_cache: dict[tuple, dict[str, float] | list[str]],
    *,
    seen_mains: set[str] | None = None,
    composition: list[Instance] | None = None,
    main_macro: bool = True,
) -> dict:
    hits = {k: 0 for k in KS}
    hits_seen = {k: 0 for k in KS}
    hits_unseen = {k: 0 for k in KS}
    n_seen = 0
    n_unseen = 0
    ap_by_main: dict[str, list[float]] = defaultdict(list)
    c16_by_main: dict[str, list[float]] = defaultdict(list)
    c10_by_main: dict[str, list[float]] = defaultdict(list)
    ap_values: list[float] = []
    c16_flags: list[float] = []
    latencies: list[float] = []
    tokens_k16: list[int] = []
    for number, query in enumerate(queries, start=1):
        ranked, latency = load_arm(arm, fusion[query.idx], store, closure_cache)
        latencies.append(latency)
        ap = average_precision(ranked, query.gold, 10)
        ap_values.append(ap)
        ap_by_main[query.main].append(ap)
        ok16 = completeness(ranked, query.gold, 16)
        ok10 = completeness(ranked, query.gold, 10)
        c16_flags.append(1.0 if ok16 else 0.0)
        c16_by_main[query.main].append(1.0 if ok16 else 0.0)
        c10_by_main[query.main].append(1.0 if ok10 else 0.0)
        for k in KS:
            ok = completeness(ranked, query.gold, k)
            if ok:
                hits[k] += 1
            if seen_mains is not None:
                if query.main in seen_mains:
                    if ok:
                        hits_seen[k] += 1
                elif ok:
                    hits_unseen[k] += 1
        if seen_mains is not None:
            if query.main in seen_mains:
                n_seen += 1
            else:
                n_unseen += 1
        tokens_k16.append(token_count(encoder, tools, ranked[:16], store.lessons))
        if number % 100 == 0:
            print(f"  {arm} {number}/{len(queries)}", flush=True)
    n = max(1, len(queries))
    if main_macro:
        mean_ap, ap_lo, ap_hi = bootstrap_ci_clustered(ap_by_main)
        mean_c16, c16_lo, c16_hi = bootstrap_ci_clustered(c16_by_main)
        mean_c10, _c10_lo, _c10_hi = bootstrap_ci_clustered(c10_by_main)
        c10_score = mean_c10
        c16_score = mean_c16
        n_mains = len(c16_by_main)
    else:
        mean_ap, ap_lo, ap_hi = bootstrap_ci(ap_values)
        mean_c16, c16_lo, c16_hi = bootstrap_ci(c16_flags)
        c10_score = hits[10] / n
        c16_score = hits[16] / n
        n_mains = len({query.main for query in queries})
    out: dict = {
        "n": len(queries),
        "n_mains": n_mains,
        "c10": c10_score,
        "c16": c16_score,
        "c10_micro": hits[10] / n,
        "c16_micro": hits[16] / n,
        "map10": mean_ap,
        "map10_ci95": [ap_lo, ap_hi],
        "c16_ci95": [c16_lo, c16_hi],
        "tokens_at_16": sum(tokens_k16) / n,
        "p95_ms": percentile_95(latencies),
        "scoring": "main_macro_clustered" if main_macro else "query_micro",
    }
    if seen_mains is not None:
        out["n_seen_main"] = n_seen
        out["n_unseen_main"] = n_unseen
        out["c10_seen"] = hits_seen[10] / max(1, n_seen)
        out["c16_seen"] = hits_seen[16] / max(1, n_seen)
        out["c10_unseen"] = hits_unseen[10] / max(1, n_unseen)
        out["c16_unseen"] = hits_unseen[16] / max(1, n_unseen)
    if composition is not None:
        comp_hits = {k: 0 for k in KS}
        comp_c16_by_main: dict[str, list[float]] = defaultdict(list)
        for query in composition:
            ranked, _latency = load_arm(arm, fusion[query.idx], store, closure_cache)
            ok16 = completeness(ranked, query.gold, 16)
            ok10 = completeness(ranked, query.gold, 10)
            comp_c16_by_main[query.main].append(1.0 if ok16 else 0.0)
            if ok10:
                comp_hits[10] += 1
            if ok16:
                comp_hits[16] += 1
        cn = max(1, len(composition))
        if main_macro and composition:
            comp_c16, comp_lo, comp_hi = bootstrap_ci_clustered(comp_c16_by_main)
            out["composition_c16"] = comp_c16
            out["composition_c16_ci95"] = [comp_lo, comp_hi]
            out["composition_c10"] = comp_hits[10] / cn
        else:
            out["composition_c10"] = comp_hits[10] / cn
            out["composition_c16"] = comp_hits[16] / cn
        out["composition_n"] = len(composition)
        out["composition_n_mains"] = len({query.main for query in composition})
    return out


def canary_c10(canary: list[Instance], fusion: list[list[str]], store: GraphStore) -> float:
    hits = 0
    cache: dict[tuple, dict[str, float] | list[str]] = {}
    for query in canary:
        ranked, _ = load_arm("hydra_mspaths_prior", fusion[query.idx], store, cache)
        if completeness(ranked, query.gold, 10):
            hits += 1
    return hits / max(1, len(canary))


def resolve_budgets(n_learn: int) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for item in TRACE_BUDGETS:
        value = n_learn if item == "all" else min(int(item), n_learn)
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def run_seed(
    seed: int,
    tools: list[Tool],
    instances: list[Instance],
    fusion: list[list[str]],
    store: GraphStore,
    encoder: tiktoken.Encoding,
    *,
    split_mode: str = "grouped_main",
    arms: tuple[str, ...] = ARMS,
) -> dict:
    if split_mode == "grouped_main":
        learn, dev, sealed, assignment = split_by_main(instances, seed)
        main_macro = True
    elif split_mode == "query_level":
        learn_ids, dev_ids, sealed_ids = split_indices(len(instances), seed)
        learn = [instances[idx] for idx in learn_ids]
        dev = [instances[idx] for idx in dev_ids]
        sealed = [instances[idx] for idx in sealed_ids]
        assignment = {}
        main_macro = False
    else:
        raise ValueError(split_mode)
    sealed_set = {query.idx for query in sealed}
    canary = dev[:CANARY_SIZE]
    by_name = {tool.name: tool for tool in tools}
    budgets = resolve_budgets(len(learn))
    print(f"seed {seed} [{split_mode}]: reload catalog (no learned edges yet)", flush=True)
    store.load_catalog(tools, include_oracle=True)

    candidates = propose_schema_candidates(tools)
    io_stats = apply_io_prior(store, candidates)
    store.sync_learned_to_hydra()
    print(f"seed {seed}: io prior {io_stats}", flush=True)

    store.tau = 0.0
    points: dict[str, list[dict]] = {arm: [] for arm in arms}
    rollbacks: list[dict] = []
    baseline = canary_c10(canary, fusion, store)
    cursor = 0
    edges_from_traces = 0

    def seen_mains_through(n: int) -> set[str]:
        return {query.main for query in learn[:n]}

    tau_history: list[dict] = []

    def evaluate_and_record(traces_used: int) -> None:
        if traces_used > 0:
            # τ on dev only (never sealed, never learn-for-selection after the fact).
            tau, grid = calibrate_tau(store, dev, fusion)
            store.tau = tau
            store.edge_version += 1
            store.sync_learned_to_hydra()
            tau_history.append({"traces": traces_used, "tau": tau, "grid": grid})
        else:
            store.tau = 0.0
            tau_history.append({"traces": 0, "tau": 0.0, "grid": {}})
        cache: dict[tuple, dict[str, float] | list[str]] = {}
        seen = seen_mains_through(traces_used)
        comp = composition_queries(sealed, store)
        for arm in arms:
            measured = score_split(
                arm,
                sealed,
                fusion,
                store,
                by_name,
                encoder,
                cache,
                seen_mains=seen,
                composition=comp,
                main_macro=main_macro,
            )
            measured["traces"] = traces_used
            measured["budget_target"] = traces_used
            measured["tau"] = store.tau
            points[arm].append(measured)
            print(
                f"seed {seed} [{split_mode}] traces {traces_used} {arm} "
                f"c10={measured['c10']:.3f} c16={measured['c16']:.3f} "
                f"map10={measured['map10']:.3f} "
                f"comp_n={measured.get('composition_n')} "
                f"comp_mains={measured.get('composition_n_mains')} "
                f"comp16={measured.get('composition_c16')}",
                flush=True,
            )

    evaluate_and_record(0)
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
        evaluate_and_record(target)

    return {
        "seed": seed,
        "split_mode": split_mode,
        "n_learn": len(learn),
        "n_dev": len(dev),
        "n_sealed": len(sealed),
        "n_learn_mains": len({query.main for query in learn}),
        "n_dev_mains": len({query.main for query in dev}),
        "n_sealed_mains": len({query.main for query in sealed}),
        "budgets": budgets,
        "io_prior": io_stats,
        "tau_history": tau_history,
        "edges_from_traces": edges_from_traces,
        "rollbacks": rollbacks,
        "points": {arm: points[arm] for arm in arms},
        "main_assignment_size": len(assignment),
    }


def mean_points(seed_runs: list[dict], arms: tuple[str, ...] = ARMS) -> dict[str, list[dict]]:
    averaged: dict[str, list[dict]] = {}
    for arm in arms:
        rows = []
        series = [run["points"][arm] for run in seed_runs]
        for index in range(len(series[0])):
            group = [series[seed_index][index] for seed_index in range(len(series))]
            c16s = [point["c16"] for point in group]
            c10s = [point["c10"] for point in group]
            maps = [point["map10"] for point in group]
            row = {
                "traces": sum(point["traces"] for point in group) / len(group),
                "budget_target": group[0]["budget_target"],
                "c10": sum(c10s) / len(c10s),
                "c16": sum(c16s) / len(c16s),
                "map10": sum(maps) / len(maps),
                "tokens_at_16": sum(point.get("tokens_at_16") or 0.0 for point in group) / len(group),
                "p95_ms": sum(point.get("p95_ms") or 0.0 for point in group) / len(group),
                "c10_by_seed": c10s,
                "c16_by_seed": c16s,
                "map10_by_seed": maps,
                "c16_spread": max(c16s) - min(c16s),
                "c10_spread": max(c10s) - min(c10s),
                "c16_ci95_by_seed": [point.get("c16_ci95") for point in group],
                "map10_ci95_by_seed": [point.get("map10_ci95") for point in group],
            }
            if all("c16_seen" in point for point in group):
                row["c16_seen"] = sum(point["c16_seen"] for point in group) / len(group)
                row["c16_unseen"] = sum(point["c16_unseen"] for point in group) / len(group)
            if all(point.get("composition_c16") is not None for point in group):
                row["composition_c16"] = sum(point["composition_c16"] for point in group) / len(group)
                row["composition_c10"] = sum(point["composition_c10"] for point in group) / len(group)
                row["composition_n"] = sum(point.get("composition_n") or 0 for point in group) / len(group)
                row["composition_n_mains"] = sum(point.get("composition_n_mains") or 0 for point in group) / len(group)
            rows.append(row)
        averaged[arm] = rows
    return averaged


def plot(averaged: dict[str, list[dict]], path: Path) -> bool:
    """Optional plot. Returns False if matplotlib is unavailable."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed; skipping plot", flush=True)
        return False
    colors = {
        "bm25_dense": "#4b5563",
        "metadata_vector": "#7c3aed",
        "sqlite_one_hop_prior": "#a16207",
        "sqlite_cte_prior": "#b45309",
        "hydra_mspaths_prior": "#115e59",
        "sqlite_one_hop_prior_k4": "#ca8a04",
        "sqlite_cte_prior_k4": "#d97706",
        "hydra_mspaths_prior_k4": "#14b8a6",
        "hydra_mspaths_trace_only": "#0f766e",
        "hydra_mspaths_prior_only": "#0891b2",
        "oracle": "#6b7280",
    }
    labels = {
        "bm25_dense": "BM25+dense",
        "metadata_vector": "Metadata also-load (fan-in 32)",
        "sqlite_one_hop_prior": "SQLite one-hop+prior (32)",
        "sqlite_cte_prior": "SQLite CTE+prior (32)",
        "hydra_mspaths_prior": "Hydra MSpaths+prior (32)",
        "sqlite_one_hop_prior_k4": "one-hop k4 ablation",
        "sqlite_cte_prior_k4": "CTE k4 ablation",
        "hydra_mspaths_prior_k4": "Hydra k4 ablation",
        "hydra_mspaths_trace_only": "Hydra trace-only (32)",
        "hydra_mspaths_prior_only": "Hydra prior-only (32)",
        "oracle": "Oracle catalog",
    }
    fig, axes = plt.subplots(1, 2, figsize=(13.0, 5.0), sharey=True)
    for axis, key, title in zip(axes, ("c16", "map10"), ("completeness@16", "mAP@10")):
        for arm in ARMS:
            if arm not in averaged:
                continue
            xs = [point["traces"] for point in averaged[arm]]
            ys = [point[key] for point in averaged[arm]]
            style = "--" if arm.endswith("_k4") else "-"
            axis.plot(
                xs,
                ys,
                marker="o",
                color=colors.get(arm, "#111"),
                label=labels.get(arm, arm),
                linewidth=2,
                linestyle=style,
            )
        axis.set_title(title)
        axis.set_xlabel("Learn traces")
        axis.set_ylim(0, 1)
        axis.grid(True, alpha=0.3)
    axes[0].set_ylabel("Sealed main-macro score")
    axes[1].legend(frameon=False, fontsize=6)
    fig.suptitle("Matched fan-in=32 grouped-main curve (mean of 3 seeds)")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return True


def rise_gate(averaged: dict[str, list[dict]]) -> dict:
    report: dict = {
        "passed": False,
        "rises": False,
        "beats_flat": False,
        "beats_metadata": False,
        "hydra_vs_cte": [],
        "hydra_vs_metadata": [],
        "notes": [],
        "rule": (
            f"(sealed @16 at all traces − @16 at 0 traces) > "
            f"{RISE_SPREAD_MULTIPLIER} × seed_spread at the full point"
        ),
        "positioning": (
            "Claim is trace-learned edges. GRTF 2502.07223 mAP@10 0.856 is "
            "reference, different protocol (hand-built deps + query-level split). "
            "GoS 2604.05333 never revises from traces."
        ),
        "claims_hygiene": {
            "trace_only_endpoint": "hydra_mspaths_trace_only: report 0-trace and full separately from prior-enabled",
            "prior_enabled_starts_nonzero": "hydra_mspaths_prior c16 at 0 traces is prior-only cold start",
            "grtf_map10": "0.856 reference, different protocol",
        },
    }
    flat = averaged["bm25_dense"]
    meta = averaged["metadata_vector"]
    for arm in ("hydra_mspaths_prior", "hydra_mspaths_trace_only"):
        series = averaged[arm]
        first, last = series[0], series[-1]
        delta = last["c16"] - first["c16"]
        spread = last.get("c16_spread") or 0.0
        need = RISE_SPREAD_MULTIPLIER * spread
        rose = delta > need + 1e-12
        report["notes"].append(
            f"{arm}: delta_c16={delta:.4f} spread={spread:.4f} need>{need:.4f} rose={rose}"
        )
        if rose:
            report["rises"] = True
        for point, base in zip(series, flat):
            if point["c16"] > base["c16"] + 1e-9:
                report["beats_flat"] = True
    for h_point, c_point in zip(averaged["hydra_mspaths_prior"], averaged["sqlite_cte_prior"]):
        spread = h_point.get("c16_spread") or 0.0
        diff = h_point["c16"] - c_point["c16"]
        report["hydra_vs_cte"].append(
            {
                "traces": h_point["traces"],
                "hydra_c16": h_point["c16"],
                "cte_c16": c_point["c16"],
                "diff": diff,
                "within_spread": abs(diff) <= spread + 1e-12,
            }
        )
    for h_point, m_point in zip(averaged["hydra_mspaths_prior"], meta):
        spread = h_point.get("c16_spread") or 0.0
        diff = h_point["c16"] - m_point["c16"]
        beats = diff > spread + 1e-12
        if beats:
            report["beats_metadata"] = True
        report["hydra_vs_metadata"].append(
            {
                "traces": h_point["traces"],
                "hydra_c16": h_point["c16"],
                "metadata_c16": m_point["c16"],
                "hydra_map10": h_point["map10"],
                "metadata_map10": m_point["map10"],
                "diff": diff,
                "beats_beyond_spread": beats,
            }
        )
    if report["hydra_vs_cte"] and all(row["within_spread"] for row in report["hydra_vs_cte"]):
        report["notes"].append(
            "HydraDB tied the recursive-CTE+prior control within seed spread; "
            "honest HydraDB value is versioned edits, undo, provenance, weighted multi-hop, as_of"
        )
    if report["hydra_vs_metadata"] and not report["beats_metadata"]:
        report["notes"].append(
            "Matched fan-in=32: HydraDB did not beat metadata also-load beyond seed spread"
        )
    elif report["beats_metadata"]:
        report["notes"].append(
            "Matched fan-in=32: HydraDB beat metadata also-load beyond seed spread"
        )
    if "hydra_mspaths_prior_k4" in averaged:
        h32 = averaged["hydra_mspaths_prior"][-1]["c16"]
        h4 = averaged["hydra_mspaths_prior_k4"][-1]["c16"]
        report["notes"].append(
            f"fan-in ablation: hydra@32 c16={h32:.4f} vs hydra@4 c16={h4:.4f} (delta={h32 - h4:.4f})"
        )
    report["matched_fan_in"] = list(MATCHED_FANIN_ARMS)
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
    edit_log = RESULTS / "edits.jsonl"
    if edit_log.exists():
        edit_log.unlink()
    store = GraphStore(hydra, edit_log)

    print("=== HEADLINE: grouped-by-main matched fan-in=32 ===", flush=True)
    print(f"arms={list(ARMS)}", flush=True)
    print(f"matched={list(MATCHED_FANIN_ARMS)}", flush=True)
    seed_runs = []
    for seed in SEEDS:
        seed_runs.append(
            run_seed(seed, tools, instances, fusion, store, encoder, split_mode="grouped_main", arms=ARMS)
        )
        (RESULTS / f"seed_{seed}.json").write_text(json.dumps(seed_runs[-1], indent=2))

    averaged = mean_points(seed_runs, ARMS)
    decision = rise_gate(averaged)

    # Reuse prior leaky query-split appendix if present (not re-run; headline is grouped).
    appendix_payload: dict
    prev = RESULTS / "curve.json"
    if prev.exists():
        old = json.loads(prev.read_text())
        appendix_payload = old.get("appendix_query_split") or {
            "label": "leaky query-level split; not re-run in matched-fan-in pass",
            "arms": list(APPENDIX_ARMS),
            "mean": {},
            "by_seed": [],
        }
        appendix_payload["label"] = (
            "leaky query-level split; carried from prior run (not the headline)"
        )
    else:
        appendix_payload = {
            "label": "leaky query-level split; not re-run in matched-fan-in pass",
            "arms": list(APPENDIX_ARMS),
            "mean": {},
            "by_seed": [],
        }

    payload = {
        "version": "2.3-matched-fanin",
        "dataset": "ToolLinkOS",
        "source": "https://github.com/EliasLumer/Graph-RAG-Tool-Fusion-ToolLinkOS",
        "headline_split": "grouped_main",
        "matched_fan_in": 32,
        "leak_fact": (
            "Every main tool has exactly 3 queries with an identical gold set "
            "(1569/1569). Query-level split leaks sibling answer sets and inflates "
            "c16 from ~0.53 (grouped) to ~0.91 (query-level)."
        ),
        "prior_art": {
            "grtf_arxiv": "2502.07223",
            "grtf_map10_vector": 0.210,
            "grtf_map10_graph": 0.856,
            "grtf_note": "reference, different protocol (hand-built deps; not grouped-main)",
            "gos_arxiv": "2604.05333",
            "gos_note": "graph never revised from execution traces",
        },
        "claims_hygiene": {
            "trace_only": "hydra_mspaths_trace_only endpoints are not mixed with prior-enabled",
            "grtf_map10": "0.856 is reference, different protocol",
            "jev": "removed; I/O prior is deterministic overlap >= 0.6 only",
            "fan_in": "headline graph arms use fan_in=32 matching metadata; k4 is ablation",
        },
        "tools": len(tools),
        "queries": len(instances),
        "embed_model": "sentence-transformers/all-MiniLM-L6-v2",
        "catalog_tokens": catalog_tokens,
        "seeds": list(SEEDS),
        "trace_budgets": [item if item != "all" else "all" for item in TRACE_BUDGETS],
        "max_len": MAX_LEN,
        "k": list(KS),
        "arms": list(ARMS),
        "headline_arms": list(HEADLINE_ARMS),
        "matched_fanin_arms": list(MATCHED_FANIN_ARMS),
        "arm_knobs": {
            name: {
                "backend": knob.backend,
                "fan_in": knob.fan_in,
                "depth": knob.depth,
                "rels": list(knob.rels) if knob.rels else None,
                "top_k": knob.top_k,
                "fallback": knob.fallback,
                "headline": knob.headline,
            }
            for name, knob in ARM_KNOBS.items()
        },
        "design": {
            "catalog_depends_on_in_learned_arms": False,
            "edge_confidence": "p=(n_obs+1)/(n_seen_src+2); tau on dev only",
            "io_prior_threshold": 0.6,
            "jev": False,
            "mspaths_cost": "minimum prefix cost per node",
            "composition": "sealed mains with no learned out-edges; gold via SCHEMA_DEP then DEPENDS_ON",
            "scoring": "main-macro with bootstrap CIs clustered by main",
            "matched_fan_in": "metadata / one-hop / CTE / Hydra all seed from fused[:32]",
            "rise_test": decision["rule"],
        },
        "mean": averaged,
        "by_seed": seed_runs,
        "gate": decision,
        "appendix_query_split": appendix_payload,
    }
    (RESULTS / "curve.json").write_text(json.dumps(payload, indent=2))
    plot(averaged, RESULTS / "completeness_curve.png")
    print(json.dumps({"gate": decision}, indent=2))


if __name__ == "__main__":
    main()
