#!/usr/bin/env python3
"""Reproduce the v2.3 star-graph / deep-ToolLinkOS structural diagnosis.

Why Hydra ties metadata also-load at matched fan-in: ingest_trace_edges writes
main → every helper, so the learned graph is a set of stars. A walk over a star
is one dictionary lookup. Completeness on ToolLinkOS cannot separate a graph
from a good KV store holding the same implication lists.

Writes results/structure_diagnosis.json. Numbers are labelled measured.
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from pathlib import Path

from toolgraph.data import Instance, Tool, load_dataset, split_by_main
from toolgraph.protocol import LEARN_FRACTION, SEEDS

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = ROOT / "results" / "structure_diagnosis.json"


def catalog_deps(tools: list[Tool]) -> dict[str, list[str]]:
    return {tool.name: [dep["name"] for dep in tool.depends_on] for tool in tools}


def hop_distance(main: str, helper: str, deps: dict[str, list[str]]) -> int | None:
    if main == helper:
        return 0
    dist = {main: 0}
    queue: deque[str] = deque([main])
    while queue:
        node = queue.popleft()
        for nxt in deps.get(node, ()):
            if nxt in dist:
                continue
            dist[nxt] = dist[node] + 1
            if nxt == helper:
                return dist[nxt]
            queue.append(nxt)
    return None


def gold_hop_histogram(
    instances: list[Instance],
    tools: list[Tool],
) -> dict:
    deps = catalog_deps(tools)
    core = {tool.name for tool in tools if tool.kind == "core"}
    by_hop: dict[str, int] = defaultdict(int)
    helper_loads = 0
    core_helper_loads = 0
    for query in instances:
        for helper in query.gold:
            if helper == query.main:
                continue
            helper_loads += 1
            if helper in core:
                core_helper_loads += 1
            hop = hop_distance(query.main, helper, deps)
            key = "unreachable" if hop is None else str(hop)
            by_hop[key] += 1
    deep = sum(by_hop[str(h)] for h in range(2, 16) if str(h) in by_hop)
    return {
        "label": "measured",
        "helper_loads": helper_loads,
        "by_hop": dict(sorted(by_hop.items(), key=lambda row: (row[0] == "unreachable", int(row[0]) if row[0].isdigit() else 99))),
        "deep_ge_2": deep,
        "deep_ge_2_fraction": deep / max(1, helper_loads),
        "core_helper_loads": core_helper_loads,
        "core_helper_fraction": core_helper_loads / max(1, helper_loads),
        "n_core_tools": len(core),
        "n_tools": len(tools),
        "n_instances": len(instances),
    }


def star_out_degree_stats(learn: list[Instance]) -> dict:
    """ingest_trace_edges writes main→every helper: stars of out-degree = |helpers|."""
    out_deg: dict[str, int] = {}
    for query in learn:
        helpers = [name for name in query.gold if name != query.main]
        # Same main always has the same gold on ToolLinkOS; take max for safety.
        out_deg[query.main] = max(out_deg.get(query.main, 0), len(helpers))
    degrees = list(out_deg.values())
    return {
        "label": "measured",
        "n_star_sources": len(out_deg),
        "mean_out_degree": sum(degrees) / max(1, len(degrees)),
        "max_out_degree": max(degrees) if degrees else 0,
        "min_out_degree": min(degrees) if degrees else 0,
        "note": "ingest_trace_edges writes main→every helper; walk over a star is one dict lookup",
    }


def implication_edges(
    learn: list[Instance],
    *,
    min_count: int = 2,
    min_p: float = 0.9,
) -> dict[str, set[str]]:
    """Helper→helper implication: A→B if count(A)≥min_count and P(B|A)≥min_p."""
    count_a: dict[str, int] = defaultdict(int)
    count_ab: dict[tuple[str, str], int] = defaultdict(int)
    for query in learn:
        tools_used = sorted(query.gold)
        for a in tools_used:
            count_a[a] += 1
            for b in tools_used:
                if a == b:
                    continue
                count_ab[(a, b)] += 1
    out: dict[str, set[str]] = defaultdict(set)
    for (a, b), n_ab in count_ab.items():
        n_a = count_a[a]
        if n_a < min_count:
            continue
        if n_ab / n_a >= min_p:
            out[a].add(b)
    return out


def star_edges_from_learn(learn: list[Instance]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for query in learn:
        for helper in query.gold:
            if helper != query.main:
                out[query.main].add(helper)
    return out


def recover_deep_helpers(
    sealed: list[Instance],
    deps: dict[str, list[str]],
    edges: dict[str, set[str]],
    *,
    seed_with_hop1: bool,
) -> dict:
    """Seed with held-out main (+ oracle hop-1 if requested); recover ≥2-hop helpers."""
    recovered_flags: list[float] = []
    extra_tools: list[float] = []
    deep_total = 0
    deep_hit = 0
    for query in sealed:
        hop1 = set(deps.get(query.main, ()))
        deep = set()
        for helper in query.gold:
            if helper == query.main:
                continue
            hop = hop_distance(query.main, helper, deps)
            if hop is not None and hop >= 2:
                deep.add(helper)
        if not deep:
            continue
        seeds = {query.main}
        if seed_with_hop1:
            seeds |= hop1
        # One-hop expand from seeds over `edges` (star or implication).
        found: set[str] = set()
        for seed in seeds:
            found |= edges.get(seed, set())
        # Also include seeds themselves when measuring extras.
        hit = deep & found
        deep_total += len(deep)
        deep_hit += len(hit)
        recovered_flags.append(len(hit) / len(deep))
        # Extra tools = found tools that are not in gold (noise), measured vs gold size.
        extras = found - set(query.gold)
        extra_tools.append(len(extras))
    mean_rec = sum(recovered_flags) / max(1, len(recovered_flags))
    mean_extra = sum(extra_tools) / max(1, len(extra_tools))
    return {
        "label": "measured",
        "n_queries_with_deep": len(recovered_flags),
        "deep_helper_micro_recall": deep_hit / max(1, deep_total),
        "deep_helper_macro_recall": mean_rec,
        "mean_extra_tools_per_query": mean_extra,
    }


def kv_one_lookup_recover(
    sealed: list[Instance],
    deps: dict[str, list[str]],
    implications: dict[str, set[str]],
) -> dict:
    """A KV store holding the same implication lists recovers in one lookup."""
    recovered_flags: list[float] = []
    for query in sealed:
        hop1 = set(deps.get(query.main, ()))
        deep = set()
        for helper in query.gold:
            if helper == query.main:
                continue
            hop = hop_distance(query.main, helper, deps)
            if hop is not None and hop >= 2:
                deep.add(helper)
        if not deep:
            continue
        # One lookup per seed (main + hop-1): union of implication lists.
        found: set[str] = set()
        for seed in {query.main} | hop1:
            found |= implications.get(seed, set())
        recovered_flags.append(len(deep & found) / len(deep))
    return {
        "label": "measured",
        "n_queries_with_deep": len(recovered_flags),
        "deep_helper_macro_recall": sum(recovered_flags) / max(1, len(recovered_flags)),
        "note": "same implication lists as a plain dict; one lookup; completeness cannot separate graph from KV",
    }


def diagnose_seed(seed: int, tools: list[Tool], instances: list[Instance]) -> dict:
    learn, _dev, sealed, _assignment = split_by_main(instances, seed)
    deps = catalog_deps(tools)
    n_mains = len({q.main for q in instances})
    n_sealed_mains = len({q.main for q in sealed})
    held_out_frac = n_sealed_mains / max(1, n_mains)
    stars = star_edges_from_learn(learn)
    imps = implication_edges(learn)
    star_rec = recover_deep_helpers(sealed, deps, stars, seed_with_hop1=True)
    imp_rec = recover_deep_helpers(sealed, deps, imps, seed_with_hop1=True)
    kv_rec = kv_one_lookup_recover(sealed, deps, imps)
    # Extra tools for implication: recovered tools beyond gold that implication adds
    # relative to star — brief says "adding 0.5–0.75 extra tools per query".
    # Measure implication-found tools that are in gold but not in star one-hop from seeds.
    extras: list[float] = []
    for query in sealed:
        hop1 = set(deps.get(query.main, ()))
        seeds = {query.main} | hop1
        star_found: set[str] = set()
        imp_found: set[str] = set()
        for seed_name in seeds:
            star_found |= stars.get(seed_name, set())
            imp_found |= imps.get(seed_name, set())
        gold_helpers = set(query.gold) - {query.main}
        extras.append(len((imp_found & gold_helpers) - star_found))
    return {
        "seed": seed,
        "n_learn": len(learn),
        "n_sealed": len(sealed),
        "n_mains": n_mains,
        "n_sealed_mains": n_sealed_mains,
        "held_out_main_fraction": held_out_frac,
        "star_out_degree": star_out_degree_stats(learn),
        "n_implication_edges": sum(len(v) for v in imps.values()),
        "star_deep_recovery": star_rec,
        "implication_deep_recovery": imp_rec,
        "implication_extra_gold_vs_star": {
            "label": "measured",
            "mean_extra_gold_tools_per_query": sum(extras) / max(1, len(extras)),
        },
        "kv_implication_one_lookup": kv_rec,
    }


def main() -> None:
    tools, instances = load_dataset(DATA)
    hops = gold_hop_histogram(instances, tools)
    per_seed = [diagnose_seed(seed, tools, instances) for seed in SEEDS]
    star_recalls = [row["star_deep_recovery"]["deep_helper_macro_recall"] for row in per_seed]
    imp_recalls = [row["implication_deep_recovery"]["deep_helper_macro_recall"] for row in per_seed]
    kv_recalls = [row["kv_implication_one_lookup"]["deep_helper_macro_recall"] for row in per_seed]
    extras = [row["implication_deep_recovery"]["mean_extra_tools_per_query"] for row in per_seed]
    held = [row["held_out_main_fraction"] for row in per_seed]
    payload = {
        "label": "measured",
        "protocol": {
            "split": "grouped_by_main",
            "seeds": list(SEEDS),
            "learn_fraction": LEARN_FRACTION,
            "held_out_is_remainder_after_learn_dev": True,
            "held_out_note": (
                "measured sealed-main fraction is ~20% under LEARN=0.60/DEV=0.20; "
                "brief prose said ~30% held out — recorded as a wording difference, not a protocol change"
            ),
            "oracle_first_hop_seeding": True,
            "implication_rule": "A→B if count(A)≥2 and P(B|A)≥0.9 over learn traces",
            "extra_tools_definition": (
                "mean |implication_found − gold| per sealed query that has ≥1 deep helper "
                "(non-gold tools the implication expansion adds)"
            ),
        },
        "gold_hop_histogram": hops,
        "per_seed": per_seed,
        "summary": {
            "label": "measured",
            "held_out_main_fraction_by_seed": held,
            "star_deep_macro_recall_by_seed": star_recalls,
            "implication_deep_macro_recall_by_seed": imp_recalls,
            "kv_deep_macro_recall_by_seed": kv_recalls,
            "implication_extra_tools_by_seed": extras,
            "star_recall_range": [min(star_recalls), max(star_recalls)],
            "implication_recall_range": [min(imp_recalls), max(imp_recalls)],
            "kv_recall_range": [min(kv_recalls), max(kv_recalls)],
            "extra_tools_range": [min(extras), max(extras)],
        },
        "inferred": {
            "learned_graph_is_stars": (
                "ingest_trace_edges writes main→every helper; a walk over a star "
                "is one dictionary lookup, so Hydra ties metadata also-load by construction"
            ),
            "completeness_cannot_separate_graph_from_kv": (
                "KV holding the same implication lists recovers ~the same deep-helper recall "
                "in one lookup because ToolLinkOS gold sets are full closures"
            ),
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload["summary"], indent=2))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
