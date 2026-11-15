"""Sealed completeness@k versus the number of learn traces.

The improver reads learn traces and a dev canary. It never receives the sealed split.
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
from toolgraph.protocol import (
    BATCH,
    CANARY_SIZE,
    FUSION_SEEDS,
    JEV_MAX_SEEDS,
    JEV_MIN_PROB,
    KS,
    MISS_RANK,
    NOUL_GRID,
    SCORE_APPLY_AT,
    SEEDS,
    TRACE_BUDGETS,
)
from toolgraph.retrieve import build_fusion

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RESULTS = ROOT / "results"
ARMS = ("bm25_dense", "sqlite_one_hop", "hydra_closure", "closure_jev")


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


def choose_seeds(jev: JevClient, task: str, fused: list[str], tools: dict[str, Tool]) -> tuple[list[str], bool]:
    """Jev Choice over the fused pool. Falls back to fusion order if the call fails."""
    picked = jev.choice_seeds(task, [(name, tools[name].description) for name in fused])
    if picked is None:
        return fused[:FUSION_SEEDS], False
    chosen = [name for name, prob in picked if prob >= JEV_MIN_PROB][:JEV_MAX_SEEDS]
    if not chosen and picked:
        chosen = [picked[0][0]]
    return chosen or fused[:FUSION_SEEDS], True


def prune(ranked: list[str], scores: dict[str, float] | None, threshold: float) -> list[str]:
    if scores is None:
        return ranked
    kept = [name for name in ranked if name not in scores or scores[name] >= threshold]
    if not kept and ranked:
        return ranked[:1]
    return kept


def load_arm(
    arm: str,
    query: Instance,
    fused: list[str],
    store: GraphStore,
    jev: JevClient | None,
    tools: dict[str, Tool],
    threshold: float,
    closure_cache: dict[tuple[str, ...], dict[str, float]],
) -> tuple[list[str], float, bool]:
    started = time.perf_counter()
    live = False
    if arm == "bm25_dense":
        ranked = list(fused)
    elif arm == "sqlite_one_hop":
        ranked = rank_costs(store.one_hop(fused[:FUSION_SEEDS]))
    elif arm == "hydra_closure":
        seeds = tuple(fused[:FUSION_SEEDS])
        costs = closure_cache.get(seeds)
        if costs is None:
            costs = store.closure(list(seeds))
            closure_cache[seeds] = costs
        ranked = rank_costs(costs)
    elif arm == "closure_jev":
        if jev is None:
            raise RuntimeError("closure_jev requires a Jev client")
        before_calls = jev.calls
        seeds, _ok = choose_seeds(jev, query.text, fused, tools)
        key = tuple(seeds)
        costs = closure_cache.get(key)
        if costs is None:
            costs = store.closure(list(seeds))
            closure_cache[key] = costs
        ranked = rank_costs(costs)
        head = ranked[:40]
        entry = seeds[0] if seeds else ""
        scores = jev.nouls(query.text, entry, [(name, tools[name].description) for name in head])
        ranked = prune(ranked, scores, threshold)
        live = jev.calls > before_calls
    else:
        raise ValueError(arm)
    return ranked, (time.perf_counter() - started) * 1000.0, live


def score_split(
    arm: str,
    queries: list[Instance],
    fusion: list[list[str]],
    store: GraphStore,
    jev: JevClient,
    tools: dict[str, Tool],
    threshold: float,
    encoder: tiktoken.Encoding,
    closure_cache: dict[tuple[str, ...], dict[str, float]],
) -> dict:
    hits = {k: 0 for k in KS}
    latencies: list[float] = []
    cold: list[float] = []
    tokens_k16: list[int] = []
    tokens_full: list[int] = []
    for number, query in enumerate(queries, start=1):
        ranked, latency, live = load_arm(
            arm, query, fusion[query.idx], store, jev, tools, threshold, closure_cache
        )
        latencies.append(latency)
        if live:
            cold.append(latency)
        for k in KS:
            if completeness(ranked, query.gold, k):
                hits[k] += 1
        tokens_k16.append(token_count(encoder, tools, ranked[:16], store.lessons))
        tokens_full.append(token_count(encoder, tools, ranked, store.lessons))
        if number % 100 == 0:
            print(f"  {arm} {number}/{len(queries)}", flush=True)
    n = max(1, len(queries))
    return {
        "n": len(queries),
        "c10": hits[10] / n,
        "c16": hits[16] / n,
        "tokens_at_16": sum(tokens_k16) / n,
        "tokens_full_closure": sum(tokens_full) / n,
        "p95_ms": percentile_95(latencies),
        "p95_cold_ms": percentile_95(cold),
    }


def calibrate_threshold(
    dev: list[Instance],
    fusion: list[list[str]],
    store: GraphStore,
    jev: JevClient,
    tools: dict[str, Tool],
    closure_cache: dict[tuple[str, ...], dict[str, float]],
) -> tuple[float, dict[str, float]]:
    prepared: list[tuple[Instance, list[str], dict[str, float] | None]] = []
    for number, query in enumerate(dev, start=1):
        seeds, _ok = choose_seeds(jev, query.text, fusion[query.idx], tools)
        key = tuple(seeds)
        costs = closure_cache.get(key)
        if costs is None:
            costs = store.closure(list(seeds))
            closure_cache[key] = costs
        ranked = rank_costs(costs)
        scores = jev.nouls(
            query.text,
            seeds[0] if seeds else "",
            [(name, tools[name].description) for name in ranked[:40]],
        )
        prepared.append((query, ranked, scores))
        if number % 50 == 0:
            print(f"  calibrate {number}/{len(dev)} jev_calls={jev.calls}", flush=True)
    grid: dict[str, float] = {}
    best_threshold = NOUL_GRID[0]
    best_score = -1.0
    for threshold in NOUL_GRID:
        hits = 0
        for query, ranked, scores in prepared:
            if completeness(prune(ranked, scores, threshold), query.gold, 10):
                hits += 1
        value = hits / max(1, len(prepared))
        grid[str(threshold)] = value
        if value > best_score:
            best_score = value
            best_threshold = threshold
    return best_threshold, grid


def ingest_trace(
    query: Instance,
    fused: list[str],
    store: GraphStore,
    jev: JevClient,
    tools: dict[str, Tool],
    sealed_ids: set[int],
) -> str:
    if query.idx in sealed_ids:
        raise RuntimeError("improver was handed a sealed query")
    seeds = fused[:FUSION_SEEDS]
    costs = store.closure(seeds)
    ranked = rank_costs(costs)
    loaded = ranked[:MISS_RANK]
    misses = [name for name in sorted(query.gold) if name not in set(loaded)]
    if not misses:
        return "no_miss"
    score = jev.score_miss(query.text, loaded, misses)
    if score is not None and score < SCORE_APPLY_AT:
        return "score_veto"
    prov = f"learn_idx={query.idx};score={'' if score is None else round(score, 3)}"
    reached = set(costs)
    for dst in misses:
        incoming = [
            store.by_id[src]
            for (src, edge_dst, rel), _edge in store.edges.items()
            if rel == "DEPENDS_ON" and edge_dst == store.names[dst] and store.by_id[src] in reached
        ]
        if dst in reached and incoming:
            for src_name in incoming:
                store.strengthen(src_name, dst, prov)
        else:
            anchor = next((name for name in loaded if name in query.gold), seeds[0])
            if anchor != dst:
                store.link_followed(anchor, dst, prov)
        store.note(dst, f"Missed beside {', '.join(loaded[:3])}.", prov)
    return "edited"


def canary_c10(
    canary: list[Instance],
    fusion: list[list[str]],
    store: GraphStore,
    tools: dict[str, Tool],
) -> float:
    hits = 0
    cache: dict[tuple[str, ...], dict[str, float]] = {}
    for query in canary:
        ranked, _latency, _live = load_arm(
            "hydra_closure", query, fusion[query.idx], store, None, tools, 0.7, cache
        )
        if completeness(ranked, query.gold, 10):
            hits += 1
    return hits / max(1, len(canary))


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
    print(f"seed {seed}: reload catalog", flush=True)
    store.load_catalog(tools)
    closure_cache: dict[tuple[str, ...], dict[str, float]] = {}
    threshold, grid = calibrate_threshold(dev, fusion, store, jev, by_name, closure_cache)
    print(f"seed {seed}: noul threshold {threshold} dev grid {grid}", flush=True)
    closure_cache.clear()
    baseline = canary_c10(canary, fusion, store, by_name)
    points: dict[str, list[dict]] = {arm: [] for arm in ARMS}
    rollbacks: list[dict] = []
    kept = 0
    cursor = 0
    actions = {"no_miss": 0, "score_veto": 0, "edited": 0}

    def evaluate(traces_kept: int) -> None:
        cache: dict[tuple[str, ...], dict[str, float]] = {}
        for arm in ARMS:
            measured = score_split(arm, sealed, fusion, store, jev, by_name, threshold, encoder, cache)
            measured["traces"] = traces_kept
            measured["budget_target"] = traces_kept
            points[arm].append(measured)
            print(
                f"seed {seed} traces {traces_kept} {arm} "
                f"c10={measured['c10']:.3f} c16={measured['c16']:.3f} "
                f"p95={measured['p95_ms']:.1f}ms",
                flush=True,
            )

    evaluate(0)
    for target in TRACE_BUDGETS:
        if target == 0:
            continue
        while cursor < target:
            batch = learn[cursor : min(target, cursor + BATCH)]
            cursor += len(batch)
            start_applied = store.applied
            for query in batch:
                action = ingest_trace(query, fusion[query.idx], store, jev, by_name, sealed_set)
                actions[action] += 1
            if store.applied == start_applied:
                kept += len(batch)
                continue
            score = canary_c10(canary, fusion, store, by_name)
            if score + 1e-12 < baseline:
                rolled = store.applied - start_applied
                store.rollback(rolled)
                rollbacks.append(
                    {
                        "seed": seed,
                        "at_kept": kept,
                        "batch": len(batch),
                        "canary_before": baseline,
                        "canary_after": score,
                        "edits_undone": rolled,
                    }
                )
                print(f"seed {seed}: rolled back batch canary {score:.3f} < {baseline:.3f}", flush=True)
            else:
                baseline = score
                kept += len(batch)
        evaluate(kept)
        for arm in points:
            points[arm][-1]["budget_target"] = target
    bookmark = store.hydra.query("MATCH (a:Tool {id: 1}) RETURN a.tool_id AS tool_id")["bookmark"]
    return {
        "seed": seed,
        "n_learn": len(learn),
        "n_dev": len(dev),
        "n_sealed": len(sealed),
        "noul_threshold": threshold,
        "noul_dev_grid": grid,
        "actions": actions,
        "rollbacks": rollbacks,
        "bookmark": bookmark,
        "points": points,
    }


def mean_points(seed_runs: list[dict]) -> dict[str, list[dict]]:
    averaged: dict[str, list[dict]] = {}
    for arm in ARMS:
        rows = []
        series = [run["points"][arm] for run in seed_runs]
        for index in range(len(series[0])):
            group = [series[seed_index][index] for seed_index in range(len(series))]
            rows.append(
                {
                    "traces": sum(point["traces"] for point in group) / len(group),
                    "budget_target": group[0]["budget_target"],
                    "c10": sum(point["c10"] for point in group) / len(group),
                    "c16": sum(point["c16"] for point in group) / len(group),
                    "tokens_at_16": sum(point["tokens_at_16"] for point in group) / len(group),
                    "tokens_full_closure": sum(point["tokens_full_closure"] for point in group) / len(group),
                    "p95_ms": sum(point["p95_ms"] or 0.0 for point in group) / len(group),
                    "c10_by_seed": [point["c10"] for point in group],
                    "c16_by_seed": [point["c16"] for point in group],
                }
            )
        averaged[arm] = rows
    return averaged


def plot(averaged: dict[str, list[dict]], path: Path) -> None:
    colors = {
        "bm25_dense": "#4b5563",
        "sqlite_one_hop": "#b45309",
        "hydra_closure": "#0f766e",
        "closure_jev": "#1d4ed8",
    }
    labels = {
        "bm25_dense": "BM25+dense flat",
        "sqlite_one_hop": "SQLite one-hop",
        "hydra_closure": "HydraDB MSpaths",
        "closure_jev": "Closure + Jev",
    }
    fig, axes = plt.subplots(1, 2, figsize=(10.2, 4.4), sharey=True)
    for axis, key, title in zip(axes, ("c10", "c16"), ("completeness@10", "completeness@16")):
        for arm in ARMS:
            xs = [point["traces"] for point in averaged[arm]]
            ys = [point[key] for point in averaged[arm]]
            axis.plot(xs, ys, marker="o", color=colors[arm], label=labels[arm], linewidth=2)
        axis.set_title(title)
        axis.set_xlabel("Learn traces kept")
        axis.set_ylim(0, 1)
        axis.grid(True, alpha=0.3)
    axes[0].set_ylabel("Sealed completeness")
    axes[1].legend(frameon=False, fontsize=8)
    fig.suptitle("ToolLinkOS sealed completeness vs learn traces (mean of 3 seeds)")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=140)
    plt.close(fig)


def gate(averaged: dict[str, list[dict]]) -> dict:
    flat = averaged["bm25_dense"]
    report = {"passed": False, "rises": False, "beats_flat": False, "notes": []}
    for arm in ("hydra_closure", "closure_jev"):
        for point, base in zip(averaged[arm], flat):
            if point["c10"] > base["c10"] + 1e-9 or point["c16"] > base["c16"] + 1e-9:
                report["beats_flat"] = True
        first = averaged[arm][0]
        last = averaged[arm][-1]
        if last["c10"] > first["c10"] + 0.01 or last["c16"] > first["c16"] + 0.01:
            report["rises"] = True
            report["notes"].append(
                f"{arm} rose c10 {first['c10']:.3f}->{last['c10']:.3f} "
                f"c16 {first['c16']:.3f}->{last['c16']:.3f}"
            )
    report["passed"] = bool(report["beats_flat"] and report["rises"])
    if report["beats_flat"] and not report["rises"]:
        report["notes"].append("graph beat flat but the sealed curve did not rise with more traces")
    if not report["beats_flat"]:
        report["notes"].append("graph arms did not beat BM25+dense on the sealed split")
    return report


def main() -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    tools, instances = load_dataset(DATA)
    by_name = {tool.name: tool for tool in tools}
    encoder = tiktoken.get_encoding("cl100k_base")
    catalog_tokens = sum(len(encoder.encode(tool.base_text())) for tool in tools)
    print(f"embedding {len(tools)} tools and {len(instances)} queries", flush=True)
    fusion = build_fusion(tools, [query.text for query in instances])
    hydra = HydraClient("http://127.0.0.1:8443", "local-development-token-32-bytes")
    jev = JevClient(RESULTS / "jev_cache.json", RESULTS / "jev_usage.jsonl")
    print(f"jev {jev.status()}", flush=True)
    store = GraphStore(hydra, RESULTS / "edits.jsonl")
    if store.edit_log.exists():
        store.edit_log.unlink()
    seed_runs = []
    for seed in SEEDS:
        seed_runs.append(run_seed(seed, tools, instances, fusion, store, jev, encoder))
        (RESULTS / f"seed_{seed}.json").write_text(json.dumps(seed_runs[-1], indent=2))
        jev.flush()
    averaged = mean_points(seed_runs)
    decision = gate(averaged)
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
        "dataset": "ToolLinkOS",
        "source": "https://github.com/EliasLumer/Graph-RAG-Tool-Fusion-ToolLinkOS",
        "tools": len(tools),
        "queries": len(instances),
        "embed_model": "sentence-transformers/all-MiniLM-L6-v2",
        "embedder": "fastembed local ONNX",
        "catalog_tokens": catalog_tokens,
        "tokenizer": "cl100k_base",
        "seeds": list(SEEDS),
        "trace_budgets": list(TRACE_BUDGETS),
        "k": list(KS),
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
        "catalog_lessons_unused_in_flat_tokens": True,
    }
    (RESULTS / "curve.json").write_text(json.dumps(payload, indent=2))
    plot(averaged, RESULTS / "completeness_curve.png")
    jev.flush()
    print(json.dumps({"gate": decision, "jev_tokens_in": usage_tokens_in, "jev_tokens_out": usage_tokens_out}, indent=2))


if __name__ == "__main__":
    main()
