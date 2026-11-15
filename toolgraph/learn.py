"""Trace-edge learning and schema I/O prior (GoS-style overlap ≥ 0.6).

Catalog depends_on is never read as a learning signal. Jev only adjudicates
borderline I/O pairs in [0.4, 0.6). Trace edges carry Beta confidence.
"""

from __future__ import annotations

import re
from collections import defaultdict

from toolgraph.data import Instance, Tool, tokenize
from toolgraph.graph import GraphStore
from toolgraph.jev import JevClient
from toolgraph.protocol import (
    IO_BORDER_HIGH,
    IO_BORDER_LOW,
    IO_OVERLAP_KEEP,
    SCHEMA_CANDIDATES_PER_TOOL,
    SCHEMA_VERIFY_MAX,
    TAU_GRID,
)

TOKEN_RE = re.compile(r"[a-z0-9]+")


def ingest_trace_edges(store: GraphStore, query: Instance) -> int:
    """Treat gold ∪ {main} as an observed agent trace. Returns edges touched."""
    store.note_src_seen(query.main)
    helpers = sorted(name for name in query.gold if name != query.main)
    if not helpers:
        return 0
    prov = f"trace_idx={query.idx};main={query.main}"
    touched = 0
    for helper in helpers:
        if store.learn_depends(query.main, helper, prov):
            touched += 1
    return touched


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
    by_name = {tool.name: tool for tool in tools}
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
    jev: JevClient,
    tools: dict[str, Tool],
) -> dict:
    """Keep overlap ≥ 0.6; Jev-judge only the borderline band [0.4, 0.6)."""
    auto = [(c, p, param, s) for c, p, param, s in candidates if s >= IO_OVERLAP_KEEP]
    border = [(c, p, param, s) for c, p, param, s in candidates if IO_BORDER_LOW <= s < IO_BORDER_HIGH]
    kept_auto = 0
    for consumer, producer, param, score in auto:
        prov = f"io_prior;overlap={round(score, 3)};param={param}"
        if store.add_schema_edge(consumer, producer, prov, cost=max(0.2, 1.0 - score)):
            kept_auto += 1
    kept_border = 0
    scored_border = 0
    if jev.enabled and border:
        by_consumer: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
        for consumer, producer, param, score in border:
            by_consumer[consumer].append((producer, param, score))
        for consumer, rows in by_consumer.items():
            payload = [
                (
                    producer,
                    tools[producer].description if producer in tools else producer,
                    param,
                )
                for producer, param, _score in rows
            ]
            answers = jev.edge_need(
                consumer,
                tools[consumer].description if consumer in tools else consumer,
                payload,
            )
            if not answers:
                continue
            param_by = {producer: (param, score) for producer, param, score in rows}
            for producer, noul in answers.items():
                scored_border += 1
                if float(noul) < 0.5:
                    continue
                param, score = param_by.get(producer, ("", 0.5))
                prov = f"io_border;overlap={round(score, 3)};noul={round(float(noul), 3)};param={param}"
                if store.add_schema_edge(consumer, producer, prov, cost=max(0.2, 1.0 - float(noul))):
                    kept_border += 1
    return {
        "candidates": len(candidates),
        "auto_kept": kept_auto,
        "border_scored": scored_border,
        "border_kept": kept_border,
        "overlap_keep": IO_OVERLAP_KEEP,
        "border_band": [IO_BORDER_LOW, IO_BORDER_HIGH],
    }


def calibrate_tau(store: GraphStore, learn: list[Instance], fusion: list[list[str]]) -> tuple[float, dict[str, float]]:
    """Pick τ on learn: maximize completeness@10 of CTE over confident edges."""
    from toolgraph.graph import rank_costs
    from toolgraph.protocol import FUSION_SEEDS

    def _complete(ranked: list[str], gold: frozenset[str], k: int) -> bool:
        return gold.issubset(set(ranked[:k]))

    grid: dict[str, float] = {}
    best_t = TAU_GRID[0]
    best = -1.0
    sample = learn[: min(120, len(learn))]
    for threshold in TAU_GRID:
        hits = 0
        for query in sample:
            costs = store.recursive_cte(
                fusion[query.idx][:FUSION_SEEDS],
                ["DEPENDS_ON"],
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


def composition_queries(
    sealed: list[Instance],
    learn: list[Instance],
    learn_limit: int,
) -> list[Instance]:
    """Sealed queries where no learn trace had the full gold, but every edge appeared."""
    used = learn[:learn_limit]
    full_golds = [query.gold for query in used]
    edge_seen: set[tuple[str, str]] = set()
    for query in used:
        for helper in query.gold:
            if helper != query.main:
                edge_seen.add((query.main, helper))
    out: list[Instance] = []
    for query in sealed:
        helpers = [h for h in query.gold if h != query.main]
        if not helpers:
            continue
        if any(query.gold <= g for g in full_golds):
            continue
        needed = {(query.main, h) for h in helpers}
        if needed <= edge_seen:
            out.append(query)
    return out
