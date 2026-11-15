"""Checkpoint-1 sealed curve after the toolgraph sweep findings.

Hidden catalog edges. Trace-learned NEEDS with Beta confidence. I/O prior at
ζ≥0.6. Reverse PPR vs MSpaths. mAP@10, composition split, bootstrap CIs.
"""

from __future__ import annotations

import json
import math
import random
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
    apply_io_prior,
    calibrate_tau,
    composition_queries,
    ingest_trace_edges,
    propose_schema_candidates,
)
from toolgraph.ppr import adjacency_from_store, reverse_ppr
from toolgraph.protocol import (
    BOOTSTRAP_SAMPLES,
    BOOTSTRAP_SEED,
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

# Arm -> (backend, rel_types). Flat uses none.
# prior arms include SCHEMA_DEP; noprior arms use DEPENDS_ON only.
ARM_SPEC: dict[str, tuple[str, list[str] | None]] = {
    "bm25_dense": ("flat", None),
    "sqlite_one_hop": ("one_hop", ["DEPENDS_ON"]),
    "sqlite_cte": ("cte", ["DEPENDS_ON"]),
    "hydra_mspaths": ("mspaths", ["DEPENDS_ON"]),
    "hydra_mspaths_prior": ("mspaths", ["DEPENDS_ON", "SCHEMA_DEP"]),
    "hydra_ppr_prior": ("ppr", ["DEPENDS_ON", "SCHEMA_DEP"]),
    "oracle": ("cte", ["ORACLE_DEP"]),
}
ARMS = tuple(ARM_SPEC)


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
    backend, rels = ARM_SPEC[arm]
    if backend == "flat":
        ranked = list(fused)
    elif backend == "one_hop":
        assert rels is not None
        key = ("one", store.edge_version, store.tau, tuple(seeds), tuple(rels))
        costs = closure_cache.get(key)
        if costs is None:
            costs = store.one_hop(seeds, rels)
            closure_cache[key] = costs
        ranked = rank_costs(costs)
        for name in fused:
            if name not in costs:
                ranked.append(name)
    elif backend == "cte":
        assert rels is not None
        key = ("cte", store.edge_version, store.tau, tuple(seeds), tuple(rels), MAX_LEN)
        costs = closure_cache.get(key)
        if costs is None:
            costs = store.recursive_cte(seeds, rels, MAX_LEN)
            closure_cache[key] = costs
        ranked = rank_costs(costs)
        for name in fused:
            if name not in costs:
                ranked.append(name)
    elif backend == "mspaths":
        assert rels is not None
        key = ("hydra", store.edge_version, store.tau, tuple(seeds), tuple(rels))
        costs = closure_cache.get(key)
        if costs is None:
            costs = store.closure(seeds, rels)
            closure_cache[key] = costs
        ranked = rank_costs(costs)
        for name in fused:
            if name not in costs:
                ranked.append(name)
    elif backend == "ppr":
        assert rels is not None
        key = ("ppr", store.edge_version, store.tau, tuple(seeds), tuple(rels))
        costs = closure_cache.get(key)
        if costs is None:
            adj = adjacency_from_store(store, rels, store.tau)
            costs = reverse_ppr(seeds, adj)
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
    composition: list[Instance] | None = None,
) -> dict:
    hits = {k: 0 for k in KS}
    hits_seen = {k: 0 for k in KS}
    hits_unseen = {k: 0 for k in KS}
    n_seen = 0
    n_unseen = 0
    ap_values: list[float] = []
    c16_flags: list[float] = []
    latencies: list[float] = []
    tokens_k16: list[int] = []
    for number, query in enumerate(queries, start=1):
        ranked, latency = load_arm(arm, fusion[query.idx], store, closure_cache)
        latencies.append(latency)
        ap_values.append(average_precision(ranked, query.gold, 10))
        ok16 = completeness(ranked, query.gold, 16)
        c16_flags.append(1.0 if ok16 else 0.0)
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
    mean_ap, ap_lo, ap_hi = bootstrap_ci(ap_values)
    mean_c16, c16_lo, c16_hi = bootstrap_ci(c16_flags)
    out: dict = {
        "n": len(queries),
        "c10": hits[10] / n,
        "c16": hits[16] / n,
        "map10": mean_ap,
        "map10_ci95": [ap_lo, ap_hi],
        "c16_ci95": [c16_lo, c16_hi],
        "tokens_at_16": sum(tokens_k16) / n,
        "p95_ms": percentile_95(latencies),
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
        for query in composition:
            ranked, _latency = load_arm(arm, fusion[query.idx], store, closure_cache)
            for k in KS:
                if completeness(ranked, query.gold, k):
                    comp_hits[k] += 1
        cn = max(1, len(composition))
        out["composition_n"] = len(composition)
        out["composition_c10"] = comp_hits[10] / cn
        out["composition_c16"] = comp_hits[16] / cn
    return out


def canary_c10(canary: list[Instance], fusion: list[list[str]], store: GraphStore) -> float:
    hits = 0
    cache: dict[tuple, dict[str, float]] = {}
    for query in canary:
        ranked, _ = load_arm("hydra_mspaths", fusion[query.idx], store, cache)
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


def pick_operator_on_dev(
    store: GraphStore,
    dev: list[Instance],
    fusion: list[list[str]],
) -> dict:
    """Budget-matched MSpaths vs reverse PPR on dev; identical seeds and k."""
    cache: dict[tuple, dict[str, float]] = {}
    sample = dev[: min(80, len(dev))]
    scores = {"hydra_mspaths_prior": 0, "hydra_ppr_prior": 0}
    for query in sample:
        for arm in scores:
            ranked, _ = load_arm(arm, fusion[query.idx], store, cache)
            if completeness(ranked, query.gold, 16):
                scores[arm] += 1
    n = max(1, len(sample))
    report = {arm: hits / n for arm, hits in scores.items()}
    winner = max(report, key=report.get)  # type: ignore[arg-type]
    return {"dev_c16": report, "winner": winner, "n": len(sample)}


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
    learn = [instances[idx] for idx in learn_ids]
    dev = [instances[idx] for idx in dev_ids]
    sealed = [instances[idx] for idx in sealed_ids]
    canary = dev[:CANARY_SIZE]
    by_name = {tool.name: tool for tool in tools}
    budgets = resolve_budgets(len(learn))
    print(f"seed {seed}: reload catalog (no learned edges yet)", flush=True)
    store.load_catalog(tools, include_oracle=True)

    candidates = propose_schema_candidates(tools)
    io_stats = apply_io_prior(store, candidates, jev, by_name)
    store.sync_learned_to_hydra()
    print(f"seed {seed}: io prior {io_stats}", flush=True)

    # τ starts at 0 so early edges count; recalibrated after each budget on learn.
    store.tau = 0.0
    operator = pick_operator_on_dev(store, dev, fusion)
    print(f"seed {seed}: operator pick {operator}", flush=True)

    points: dict[str, list[dict]] = {arm: [] for arm in ARMS}
    rollbacks: list[dict] = []
    baseline = canary_c10(canary, fusion, store)
    cursor = 0
    edges_from_traces = 0

    def seen_mains_through(n: int) -> set[str]:
        return {query.main for query in learn[:n]}

    tau_history: list[dict] = []

    def evaluate_and_record(traces_used: int) -> None:
        if traces_used > 0:
            tau, grid = calibrate_tau(store, learn[:traces_used], fusion)
            store.tau = tau
            store.edge_version += 1
            store.sync_learned_to_hydra()
            tau_history.append({"traces": traces_used, "tau": tau, "grid": grid})
        else:
            store.tau = 0.0
            tau_history.append({"traces": 0, "tau": 0.0, "grid": {}})
        cache: dict[tuple, dict[str, float]] = {}
        seen = seen_mains_through(traces_used)
        comp = composition_queries(sealed, learn, traces_used)
        for arm in ARMS:
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
            )
            measured["traces"] = traces_used
            measured["budget_target"] = traces_used
            measured["tau"] = store.tau
            points[arm].append(measured)
            print(
                f"seed {seed} traces {traces_used} {arm} "
                f"c10={measured['c10']:.3f} c16={measured['c16']:.3f} "
                f"map10={measured['map10']:.3f} "
                f"comp_n={measured.get('composition_n')} comp16={measured.get('composition_c16')}",
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
        "n_learn": len(learn),
        "n_dev": len(dev),
        "n_sealed": len(sealed),
        "budgets": budgets,
        "io_prior": io_stats,
        "operator_pick": operator,
        "tau_history": tau_history,
        "edges_from_traces": edges_from_traces,
        "rollbacks": rollbacks,
        "points": {arm: points[arm] for arm in ARMS},
    }


def mean_points(seed_runs: list[dict]) -> dict[str, list[dict]]:
    averaged: dict[str, list[dict]] = {}
    for arm in ARMS:
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
            rows.append(row)
        averaged[arm] = rows
    return averaged


def plot(averaged: dict[str, list[dict]], path: Path) -> None:
    colors = {
        "bm25_dense": "#4b5563",
        "sqlite_one_hop": "#a16207",
        "sqlite_cte": "#b45309",
        "hydra_mspaths": "#0f766e",
        "hydra_mspaths_prior": "#115e59",
        "hydra_ppr_prior": "#1d4ed8",
        "oracle": "#6b7280",
    }
    labels = {
        "bm25_dense": "BM25+dense",
        "sqlite_one_hop": "SQLite one-hop",
        "sqlite_cte": "SQLite CTE depth=4",
        "hydra_mspaths": "HydraDB MSpaths",
        "hydra_mspaths_prior": "MSpaths + I/O prior",
        "hydra_ppr_prior": "PPR + I/O prior",
        "oracle": "Oracle catalog",
    }
    fig, axes = plt.subplots(1, 2, figsize=(12.0, 4.8), sharey=True)
    for axis, key, title in zip(axes, ("c16", "map10"), ("completeness@16", "mAP@10")):
        for arm in ARMS:
            xs = [point["traces"] for point in averaged[arm]]
            ys = [point[key] for point in averaged[arm]]
            axis.plot(xs, ys, marker="o", color=colors[arm], label=labels[arm], linewidth=2)
        axis.set_title(title)
        axis.set_xlabel("Learn traces")
        axis.set_ylim(0, 1)
        axis.grid(True, alpha=0.3)
    axes[0].set_ylabel("Sealed score")
    axes[1].legend(frameon=False, fontsize=7)
    fig.suptitle("Checkpoint 1: sealed learning curve (mean of 3 seeds)")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=140)
    plt.close(fig)


def rise_gate(averaged: dict[str, list[dict]]) -> dict:
    report: dict = {
        "passed": False,
        "rises": False,
        "beats_flat": False,
        "hydra_vs_cte": [],
        "mspaths_vs_ppr": [],
        "notes": [],
        "rule": (
            f"(sealed @16 at all traces − @16 at 0 traces) > "
            f"{RISE_SPREAD_MULTIPLIER} × seed_spread at the full point"
        ),
        "positioning": (
            "Claim is trace-learned edges (GRTF 2502.07223 uses hand-built deps; "
            "GoS 2604.05333 never revises from traces). Report mAP@10 vs GRTF."
        ),
    }
    flat = averaged["bm25_dense"]
    for arm in ("hydra_mspaths", "hydra_mspaths_prior", "hydra_ppr_prior"):
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
    for h_point, c_point in zip(averaged["hydra_mspaths"], averaged["sqlite_cte"]):
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
    for m_point, p_point in zip(averaged["hydra_mspaths_prior"], averaged["hydra_ppr_prior"]):
        report["mspaths_vs_ppr"].append(
            {
                "traces": m_point["traces"],
                "mspaths_c16": m_point["c16"],
                "ppr_c16": p_point["c16"],
                "mspaths_map10": m_point["map10"],
                "ppr_map10": p_point["map10"],
            }
        )
    if report["hydra_vs_cte"] and all(row["within_spread"] for row in report["hydra_vs_cte"]):
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
        "version": "2.1-sweep",
        "dataset": "ToolLinkOS",
        "source": "https://github.com/EliasLumer/Graph-RAG-Tool-Fusion-ToolLinkOS",
        "prior_art": {
            "grtf_arxiv": "2502.07223",
            "grtf_map10_vector": 0.210,
            "grtf_map10_graph": 0.856,
            "gos_arxiv": "2604.05333",
            "gos_note": "graph never revised from execution traces",
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
        "design": {
            "catalog_depends_on_in_learned_arms": False,
            "edge_confidence": "p=(n_obs+1)/(n_seen_src+2); tau on learn/dev",
            "io_prior_threshold": 0.6,
            "jev_border_band": [0.4, 0.6],
            "ppr": {"alpha": 0.2, "iters": 50, "client_side": True},
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
