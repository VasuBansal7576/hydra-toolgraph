"""Trace-edge learning and schema-signal candidate proposals.

Catalog depends_on is never read as a learning signal for edges. Schema
proposals use parameter names and tool name/description tokens only. Jev
verifies candidate edges; it does not prune closures.
"""

from __future__ import annotations

import re
from collections import defaultdict

from toolgraph.data import Instance, Tool, tokenize
from toolgraph.graph import GraphStore
from toolgraph.jev import JevClient
from toolgraph.protocol import (
    EDGE_NOUL_GRID,
    SCHEMA_CANDIDATES_PER_TOOL,
    SCHEMA_VERIFY_MAX,
)

TOKEN_RE = re.compile(r"[a-z0-9]+")


def ingest_trace_edges(store: GraphStore, query: Instance) -> int:
    """Treat gold ∪ {main} as an observed agent trace. Returns edges touched."""
    helpers = sorted(name for name in query.gold if name != query.main)
    if not helpers:
        return 0
    prov = f"trace_idx={query.idx};main={query.main}"
    touched = 0
    for helper in helpers:
        if store.learn_depends(query.main, helper, prov):
            touched += 1
    return touched


def _producer_tokens(tool: Tool) -> set[str]:
    """Tokens a tool is likely to produce, inferred from its name and description."""
    tokens = set(tokenize(tool.name.replace("_", " ")))
    tokens |= set(tokenize(tool.description))
    # Drop ultra-generic verbs that create noisy edges.
    tokens -= {"get", "set", "add", "create", "update", "delete", "list", "find", "check", "the", "a", "an", "to", "of", "for", "and", "or", "in", "on"}
    return tokens


def propose_schema_candidates(tools: list[Tool]) -> list[tuple[str, str, str]]:
    """Propose DEPENDS_ON(consumer -> producer) from REQUIRES_PARAM ↔ PRODUCES signals.

    Returns (consumer, producer, param_name) triples, capped per consumer.
    """
    by_name = {tool.name: tool for tool in tools}
    producers: dict[str, list[str]] = defaultdict(list)
    for tool in tools:
        for token in _producer_tokens(tool):
            if len(token) < 3:
                continue
            producers[token].append(tool.name)

    candidates: list[tuple[str, str, str, int]] = []
    for tool in tools:
        scored: list[tuple[int, str, str]] = []
        for param in tool.params:
            pname = str(param.get("name") or "").lower()
            if not pname:
                continue
            ptokens = set(TOKEN_RE.findall(pname))
            ptokens |= set(tokenize(str(param.get("description") or "")))
            ptokens -= {"the", "a", "an", "to", "of", "for", "and", "or", "in", "on", "is", "be"}
            for token in ptokens:
                if len(token) < 3:
                    continue
                for producer in producers.get(token, []):
                    if producer == tool.name:
                        continue
                    # Prefer producers whose name contains the param token.
                    bonus = 2 if token in producer.lower() else 0
                    # Prefer core helpers.
                    bonus += 1 if by_name[producer].kind == "core" else 0
                    scored.append((bonus, producer, pname or token))
        scored.sort(key=lambda row: (-row[0], row[1], row[2]))
        seen: set[str] = set()
        kept = 0
        for bonus, producer, param in scored:
            if producer in seen:
                continue
            seen.add(producer)
            candidates.append((tool.name, producer, param, bonus))
            kept += 1
            if kept >= SCHEMA_CANDIDATES_PER_TOOL:
                break
    # Prefer higher-bonus candidates globally when capping verification budget.
    candidates.sort(key=lambda row: (-row[3], row[0], row[1]))
    trimmed = candidates[:SCHEMA_VERIFY_MAX]
    return [(src, dst, param) for src, dst, param, _bonus in trimmed]


def verify_schema_edges(
    jev: JevClient,
    candidates: list[tuple[str, str, str]],
    tools: dict[str, Tool],
) -> list[tuple[str, str, float, str]]:
    """Ask Jev Noul whether consumer needs producer first. Returns scored edges."""
    scored: list[tuple[str, str, float, str]] = []
    if not jev.enabled or not candidates:
        return scored
    by_consumer: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for consumer, producer, param in candidates:
        by_consumer[consumer].append((producer, param))
    for consumer, producers in by_consumer.items():
        consumer_desc = tools[consumer].description if consumer in tools else consumer
        payload = [
            (
                producer,
                tools[producer].description if producer in tools else producer,
                param,
            )
            for producer, param in producers
        ]
        answers = jev.edge_need(consumer, consumer_desc, payload)
        if not answers:
            continue
        param_by = {producer: param for producer, param in producers}
        for producer, score in answers.items():
            scored.append((consumer, producer, float(score), param_by.get(producer, "")))
    return scored


def calibrate_edge_threshold(
    scored: list[tuple[str, str, float, str]],
    learn: list[Instance],
) -> tuple[float, dict[str, float]]:
    """Pick a Noul threshold on learn only: maximize F1 vs learn gold edges."""
    positives: set[tuple[str, str]] = set()
    for query in learn:
        for helper in query.gold:
            if helper != query.main:
                positives.add((query.main, helper))
    grid: dict[str, float] = {}
    best_t = EDGE_NOUL_GRID[0]
    best_f1 = -1.0
    for threshold in EDGE_NOUL_GRID:
        pred = {(src, dst) for src, dst, score, _param in scored if score >= threshold}
        if not pred:
            f1 = 0.0
        else:
            tp = len(pred & positives)
            precision = tp / max(1, len(pred))
            # Recall against the subset of positives that were proposed at all.
            proposed = {(src, dst) for src, dst, _score, _param in scored}
            relevant = positives & proposed
            recall = tp / max(1, len(relevant)) if relevant else 0.0
            f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
        grid[str(threshold)] = f1
        if f1 > best_f1:
            best_f1 = f1
            best_t = threshold
    return best_t, grid


def apply_verified_schema_edges(
    store: GraphStore,
    scored: list[tuple[str, str, float, str]],
    threshold: float,
) -> int:
    kept = 0
    for src, dst, score, param in scored:
        if score < threshold:
            continue
        prov = f"schema_param={param};noul={round(score, 3)}"
        if store.add_schema_edge(src, dst, prov, cost=1.0 / max(score, 0.05)):
            kept += 1
    return kept
