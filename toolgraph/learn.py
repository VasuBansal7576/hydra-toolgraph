"""Trace-edge learning and schema I/O prior (GoS-style overlap ≥ 0.6).

Catalog depends_on is never read as a learning signal. The I/O prior is
deterministic: keep an edge iff schema overlap ≥ 0.6. Trace edges carry Beta
confidence on live REQUIRES (source=trace). Composition uses prior→learned
reachability on sealed mains with no learned out-edges.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque

from toolgraph.data import Instance, Tool
from toolgraph.graph import GraphStore
from toolgraph.protocol import (
    IO_OVERLAP_KEEP,
    SCHEMA_CANDIDATES_PER_TOOL,
    SCHEMA_VERIFY_MAX,
    TAU_GRID,
)

TOKEN_RE = re.compile(r"[a-z0-9]+")


def ingest_trace_edges(store: GraphStore, query: Instance) -> int:
    """Treat gold ∪ {main} as an observed agent trace (co-occurrence). Returns edges touched."""
    tools_used = sorted(query.gold)
    if query.main not in tools_used:
        tools_used = [query.main, *tools_used]
    return store.learn_from_tools(tools_used, prov=f"trace_idx={query.idx};main={query.main}")


def _tokens(text: str) -> set[str]:
    return set(TOKEN_RE.findall(text.lower())) - {
        "the", "a", "an", "to", "of", "for", "and", "or", "in", "on", "is", "be", "get", "set"
    }


def io_overlap(consumer: Tool, producer: Tool, param: dict) -> float:
    """Name/type/description overlap in [0, 1], GoS-style."""
    pname = str(param.get("name") or "")
    ptype = str(param.get("type") or "")
    pdesc = str(param.get("description") or "")
    need = _tokens(pname) | _tokens(ptype) | _tokens(pdesc)
    if not need:
        return 0.0
    have = _tokens(producer.name.replace("_", " ")) | _tokens(producer.description)
    if not have:
        return 0.0
    inter = len(need & have)
    union = len(need | have)
    jaccard = inter / max(1, union)
    # Bonus if param token appears in producer name.
    name_hit = 1.0 if any(tok in producer.name.lower() for tok in need if len(tok) >= 3) else 0.0
    return min(1.0, 0.7 * jaccard + 0.3 * name_hit)


def propose_schema_candidates(tools: list[Tool]) -> list[tuple[str, str, str, float]]:
    """(consumer, producer, param, overlap) with overlap > 0, capped."""
    producers = tools
    candidates: list[tuple[str, str, str, float]] = []
    for tool in tools:
        scored: list[tuple[float, str, str]] = []
        for param in tool.params:
            for producer in producers:
                if producer.name == tool.name:
                    continue
                score = io_overlap(tool, producer, param)
                if score <= 0.0:
                    continue
                scored.append((score, producer.name, str(param.get("name") or "")))
        scored.sort(key=lambda row: (-row[0], row[1]))
        seen: set[str] = set()
        kept = 0
        for score, producer, param in scored:
            if producer in seen:
                continue
            seen.add(producer)
            candidates.append((tool.name, producer, param, score))
            kept += 1
            if kept >= SCHEMA_CANDIDATES_PER_TOOL:
                break
    candidates.sort(key=lambda row: (-row[3], row[0], row[1]))
    return candidates[:SCHEMA_VERIFY_MAX]


def apply_io_prior(
    store: GraphStore,
    candidates: list[tuple[str, str, str, float]],
) -> dict:
    """Keep overlap ≥ 0.6 only. No LLM / Jev adjudication."""
    auto = [(c, p, param, s) for c, p, param, s in candidates if s >= IO_OVERLAP_KEEP]
    kept_auto = 0
    for consumer, producer, param, score in auto:
        prov = f"io_prior;overlap={round(score, 3)};param={param}"
        if store.add_schema_edge(consumer, producer, prov, cost=max(0.2, 1.0 - score)):
            kept_auto += 1
    return {
        "candidates": len(candidates),
        "auto_kept": kept_auto,
        "overlap_keep": IO_OVERLAP_KEEP,
        "border_judged": 0,
    }


def calibrate_tau(
    store: GraphStore,
    queries: list[Instance],
    fusion: list[list[str]],
) -> tuple[float, dict[str, float]]:
    """Pick τ on the given split (dev only in the headline protocol)."""
    from toolgraph.graph import rank_costs
    from toolgraph.protocol import FUSION_POOL

    def _complete(ranked: list[str], gold: frozenset[str], k: int) -> bool:
        return gold.issubset(set(ranked[:k]))

    grid: dict[str, float] = {}
    best_t = TAU_GRID[0]
    best = -1.0
    sample = queries[: min(120, len(queries))]
    for threshold in TAU_GRID:
        hits = 0
        for query in sample:
            costs = store.recursive_cte(
                fusion[query.idx][:FUSION_POOL],
                ["REQUIRES"],
                tau=threshold,
            )
            ranked = rank_costs(costs)
            for name in fusion[query.idx]:
                if name not in costs:
                    ranked.append(name)
            if _complete(ranked, query.gold, 10):
                hits += 1
        value = hits / max(1, len(sample))
        grid[str(threshold)] = value
        if value > best:
            best = value
            best_t = threshold
    return best_t, grid


def composition_queries(sealed: list[Instance], store: GraphStore) -> list[Instance]:
    """Sealed queries whose main has no learned out-edges, but gold is reachable
    via an io_prior first hop then trace REQUIRES walks.

    Example shape: main -REQUIRES(io_prior)→ bridge -REQUIRES(trace)→ helper.
    """
    threshold = store.tau
    prior_out: dict[str, set[str]] = defaultdict(set)
    learned_out: dict[str, set[str]] = defaultdict(set)
    for (src, dst, rel), edge in store.edges.items():
        if rel != "REQUIRES":
            continue
        src_name = store.by_id.get(src)
        dst_name = store.by_id.get(dst)
        if src_name is None or dst_name is None:
            continue
        if edge.source == "io_prior":
            prior_out[src_name].add(dst_name)
        elif edge.source == "trace" and edge.p >= threshold:
            learned_out[src_name].add(dst_name)

    out: list[Instance] = []
    for query in sealed:
        helpers = {name for name in query.gold if name != query.main}
        if not helpers:
            continue
        if learned_out.get(query.main):
            continue
        first = prior_out.get(query.main) or set()
        if not first:
            continue
        reachable: set[str] = set(first)
        queue: deque[str] = deque(first)
        seen = set(first)
        while queue:
            node = queue.popleft()
            for nxt in learned_out.get(node, ()):
                if nxt in seen:
                    continue
                seen.add(nxt)
                reachable.add(nxt)
                queue.append(nxt)
        if helpers <= reachable:
            out.append(query)
    return out
