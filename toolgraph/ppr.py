"""Reverse-aware weighted personalized PageRank (client-side).

HydraDB's pageRank rejects relationship weights, so we pull the adjacency
from SQLite and run power iteration locally (GoS α=0.2, 50 iters).
"""

from __future__ import annotations

from collections import defaultdict

from toolgraph.protocol import (
    PPR_ALPHA,
    PPR_ITERS,
    PPR_WEIGHT_IO,
    PPR_WEIGHT_TRACE,
    SEED_PENALTY,
)


def reverse_ppr(
    seeds: list[str],
    out_edges: dict[str, list[tuple[str, str, float]]],
    *,
    alpha: float = PPR_ALPHA,
    iters: int = PPR_ITERS,
) -> dict[str, float]:
    """Scores: higher is better. out_edges[src] = [(dst, rel, p_or_weight), ...].

    Reverse-aware: we walk edges as dst ← src for NEEDS-style deps so a seed
    tool pulls in its prerequisites (helpers that the seed needs).
    """
    # Build reverse adjacency with weights.
    rev: dict[str, list[tuple[str, float]]] = defaultdict(list)
    nodes = set(seeds)
    for src, outs in out_edges.items():
        nodes.add(src)
        for dst, rel, weight in outs:
            nodes.add(dst)
            w = float(weight)
            if rel == "SCHEMA_DEP":
                w *= PPR_WEIGHT_IO
            else:
                w *= PPR_WEIGHT_TRACE
            # Reverse: mass flows from consumer (src) to producer (dst).
            rev[src].append((dst, w))
    if not nodes:
        return {}
    personal = {name: 0.0 for name in nodes}
    for index, name in enumerate(seeds):
        if name in personal:
            # Prefer earlier seeds slightly, matching SEED_PENALTY ordering.
            personal[name] += 1.0 / (1.0 + index * (SEED_PENALTY / 10.0))
    total = sum(personal.values()) or 1.0
    for name in personal:
        personal[name] /= total
    score = dict(personal)
    for _ in range(iters):
        nxt = {name: alpha * personal[name] for name in nodes}
        for src, targets in rev.items():
            mass = score.get(src, 0.0)
            if mass <= 0.0 or not targets:
                continue
            weight_sum = sum(w for _dst, w in targets) or 1.0
            share = (1.0 - alpha) * mass
            for dst, w in targets:
                nxt[dst] = nxt.get(dst, 0.0) + share * (w / weight_sum)
        score = nxt
    # Convert to costs (lower better) for rank_costs compatibility.
    # cost = seed_penalty_like + (1 - normalized_score)
    max_s = max(score.values()) if score else 1.0
    costs: dict[str, float] = {}
    for index, name in enumerate(seeds):
        costs[name] = index * SEED_PENALTY
    for name, value in score.items():
        # Higher PPR → lower cost.
        norm = value / max_s if max_s > 0 else 0.0
        cost = (1.0 - norm)
        if name in costs:
            costs[name] = min(costs[name], cost)
        else:
            costs[name] = 1.0 + cost
    return costs


def adjacency_from_store(store, rel_types: list[str], tau: float) -> dict[str, list[tuple[str, str, float]]]:
    """Export confident edges for PPR."""
    out: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
    for (src_id, dst_id, rel), edge in store.edges.items():
        if rel not in rel_types:
            continue
        if rel == "DEPENDS_ON" and edge.p < tau:
            continue
        src = store.by_id.get(src_id)
        dst = store.by_id.get(dst_id)
        if src is None or dst is None:
            continue
        weight = edge.p if rel == "DEPENDS_ON" else max(0.05, 1.0 / max(edge.cost, 0.05))
        out[src].append((dst, rel, weight))
    return out
