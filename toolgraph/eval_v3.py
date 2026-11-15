"""v3 eval: arms, metrics, B1/B2/staleness/H4 scaffolding.

Sealed B1/B2 runs are step 4 — this module provides loaders and runners so
that step can invoke them without rewriting protocol. Catalog depends_on is
read only by ``iterative_search`` (simulator) and the B2 gold constructor.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from toolgraph.arms import ARM_KNOBS, ARMS, RETRIEVAL_SEEDED_ARMS
from toolgraph.data import Instance, Tool, load_dataset, split_by_main
from toolgraph.eval_curve import completeness, resolve_budgets
from toolgraph.graph import GraphStore, is_read_only, rank_costs
from toolgraph.hydra import HydraClient
from toolgraph.learn import apply_io_prior, ingest_trace_edges, propose_schema_candidates
from toolgraph.protocol import (
    CLOSURE_REBUILD_INTERVAL,
    FUSION_POOL,
    ITERATIVE_INITIAL_LOAD,
    ITERATIVE_MAX_ROUNDS,
    ITERATIVE_SEARCH_TOP,
    KS,
    MAX_LEN,
    SEEDS,
    TRACE_BUDGETS,
)
from toolgraph.retrieve import Bm25Index, build_fusion, tokenize
from toolgraph.sessions_b2 import plan_b2_sessions

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RESULTS = ROOT / "results"

TOP_K = max(KS)
ORACLE_RELS = ("ORACLE_DEP",)


class _FakeHydra:
    """Minimal Hydra stub for smoke / offline scaffolding."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.rows: list = []
        self.resolved_producers: list[str] = []

    def query(self, statement: str, parameters: dict | None = None) -> dict:
        del parameters
        self.statements.append(statement)
        if "algo.MSpaths" in statement:
            return {"columns": ["path", "pathCost"], "rows": self.rows, "bookmark": None}
        if "[:RESOLVED]" in statement or "-[:RESOLVED]->" in statement:
            return {
                "columns": ["e.producer"],
                "rows": [[name] for name in self.resolved_producers],
                "bookmark": None,
            }
        return {"columns": [], "rows": [], "bookmark": None}


@dataclass
class ArmLoadResult:
    ranked: list[str]
    tokens: int
    llm_turns: int
    store_requests: int
    latency_ms: float
    skipped: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskMetrics:
    completeness_16: bool
    tokens: int
    llm_turns: int
    store_requests: int
    latency_ms: float
    ranked: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    wrong_skip: bool = False
    tokens_saved: int = 0


def schema_tokens_for(store: GraphStore, names: list[str]) -> int:
    """Sum tool-schema tokens (store.schema_tokens / cl100k on JSON schema)."""
    total = 0
    for name in names:
        total += int(store.schema_tokens.get(name, 0))
    return total


def requires_adjacency(store: GraphStore) -> dict[str, list[str]]:
    """Live REQUIRES out-neighbors (same implication lists the graph walks)."""
    out: dict[str, list[str]] = defaultdict(list)
    for edge in store.active_edges(["REQUIRES"]):
        src = store.by_id.get(edge.src)
        dst = store.by_id.get(edge.dst)
        if src is None or dst is None:
            continue
        out[src].append(dst)
    return {src: sorted(set(dsts)) for src, dsts in out.items()}


def oracle_adjacency(store: GraphStore) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for edge in store.active_edges(["ORACLE_DEP"]):
        src = store.by_id.get(edge.src)
        dst = store.by_id.get(edge.dst)
        if src is None or dst is None:
            continue
        out[src].append(dst)
    return {src: sorted(set(dsts)) for src, dsts in out.items()}


def expand_kv_hop(
    seeds: list[str],
    adj: dict[str, list[str]],
    *,
    edge_cost: float = 1.0,
) -> tuple[list[str], int]:
    """Expand one hop per store request until fixpoint.

    store_requests = number of frontier expansions until fixpoint (H2: constant 2
    vs kv_hop hops). Ranking mirrors hydra_graph / CTE: seed i costs
    ``i * SEED_PENALTY``, each hop adds ``edge_cost``, take min cost — so helpers
    can enter the top-16 ahead of late fused seeds.
    """
    from toolgraph.protocol import SEED_PENALTY

    if not seeds:
        return [], 0
    costs: dict[str, float] = {}
    for index, name in enumerate(seeds):
        if name not in costs:
            costs[name] = index * SEED_PENALTY
    frontier = [name for name in seeds if name in costs]
    # Unique frontier preserving seed order.
    seen_f: set[str] = set()
    ordered: list[str] = []
    for name in seeds:
        if name not in seen_f:
            seen_f.add(name)
            ordered.append(name)
    frontier = ordered
    requests = 0
    while frontier:
        requests += 1
        nxt: list[str] = []
        for node in frontier:
            base = costs[node]
            for neigh in adj.get(node, ()):
                cand = base + float(edge_cost)
                if neigh not in costs or cand < costs[neigh]:
                    costs[neigh] = cand
                    nxt.append(neigh)
        # Dedup nxt while allowing cost improvements to re-expand.
        frontier = list(dict.fromkeys(nxt))
    loaded = sorted(costs, key=lambda name: (costs[name], name))
    return loaded, requests


class KvClosureIndex:
    """Materialised closure per key; rebuild every CLOSURE_REBUILD_INTERVAL edits."""

    rebuild_interval: int = CLOSURE_REBUILD_INTERVAL

    def __init__(self, store: GraphStore) -> None:
        self.store = store
        # seed -> {tool: cost_from_that_seed} materialised at rebuild time
        self.closure_costs: dict[str, dict[str, float]] = {}
        self.closure: dict[str, list[str]] = {}
        self.edits_since_rebuild = 0
        self.rebuild_count = 0
        self.rebuild()

    def rebuild(self) -> None:
        from toolgraph.protocol import SEED_PENALTY

        adj = requires_adjacency(self.store)
        materialised: dict[str, dict[str, float]] = {}
        ranked: dict[str, list[str]] = {}
        for name in self.store.names:
            # Single-seed expand; remap costs so this seed is cost 0.
            loaded, _reqs = expand_kv_hop([name], adj)
            # expand_kv_hop with one seed: seed cost 0, hops add 1.
            # Re-derive costs from hop order in loaded (seed first, then BFS).
            costs: dict[str, float] = {name: 0.0}
            # Recompute via a tiny BFS so costs survive ranking changes.
            frontier = [name]
            while frontier:
                nxt: list[str] = []
                for node in frontier:
                    for neigh in adj.get(node, ()):
                        cand = costs[node] + 1.0
                        if neigh not in costs or cand < costs[neigh]:
                            costs[neigh] = cand
                            nxt.append(neigh)
                frontier = list(dict.fromkeys(nxt))
            materialised[name] = costs
            ranked[name] = sorted(costs, key=lambda tool: (costs[tool], tool))
        self.closure_costs = materialised
        self.closure = ranked
        self.edits_since_rebuild = 0
        self.rebuild_count += 1
        _ = SEED_PENALTY  # documented pairing with expand_kv_hop

    def note_edit(self) -> None:
        self.edits_since_rebuild += 1
        if self.edits_since_rebuild >= self.rebuild_interval:
            self.rebuild()

    def fetch(self, seeds: list[str]) -> list[str]:
        """One store request: union of materialised closures for seeds."""
        from toolgraph.protocol import SEED_PENALTY

        costs: dict[str, float] = {}
        for index, seed in enumerate(seeds):
            seed_penalty = index * SEED_PENALTY
            local = self.closure_costs.get(seed)
            if not local:
                if seed in self.store.names:
                    costs[seed] = min(costs.get(seed, seed_penalty), seed_penalty)
                continue
            for name, local_cost in local.items():
                cand = seed_penalty + float(local_cost)
                if name not in costs or cand < costs[name]:
                    costs[name] = cand
        return sorted(costs, key=lambda name: (costs[name], name))


def _params_needing_producer(tool: Tool) -> list[tuple[dict, str]]:
    """Catalog depends_on (simulator only): params whose producer is named."""
    needed: list[tuple[dict, str]] = []
    params_by_name = {str(param.get("name")): param for param in tool.params}
    for dep in tool.depends_on:
        param_name = dep.get("parameter_name")
        producer = dep.get("name")
        if not param_name or not producer:
            continue
        param = params_by_name.get(str(param_name))
        if param is None:
            continue
        needed.append((param, str(producer)))
    return needed


def iterative_search_load(
    fused: list[str],
    tools: dict[str, Tool],
    bm25: Bm25Index,
    *,
    initial: int = ITERATIVE_INITIAL_LOAD,
    search_top: int = ITERATIVE_SEARCH_TOP,
    max_rounds: int = ITERATIVE_MAX_ROUNDS,
) -> ArmLoadResult:
    """Simulated Tool Search / MCP-Zero (no LLM).

    Loads fused top-``initial``. For each loaded tool, every parameter the
    simulator marks as needing a producer via catalog ``depends_on`` (documented
    exception) with no producer loaded triggers a BM25 search on
    param name+description; add top ``search_top``. Each search = 1 LLM turn.
    """
    started = time.perf_counter()
    loaded: list[str] = []
    seen: set[str] = set()
    for name in fused[:initial]:
        if name not in seen:
            seen.add(name)
            loaded.append(name)
    turns = 0
    for _round in range(max_rounds):
        # One search per missing producer (param name+description as query).
        searches: list[tuple[str, dict]] = []
        seen_producers: set[str] = set()
        for name in list(loaded):
            tool = tools.get(name)
            if tool is None:
                continue
            for param, producer in _params_needing_producer(tool):
                if producer in seen or producer in seen_producers:
                    continue
                seen_producers.add(producer)
                searches.append((producer, param))
        if not searches:
            break
        progressed = False
        for producer, param in searches:
            query = f"{param.get('name', '')} {param.get('description', '')}".strip()
            del producer  # need is catalog-side; retrieval is BM25 on param text only
            hits = bm25.top(query, search_top + len(seen))
            added = [hit for hit in hits if hit not in seen][:search_top]
            turns += 1
            if added:
                progressed = True
                for hit in added:
                    seen.add(hit)
                    loaded.append(hit)
        if not progressed:
            break
    ranked = loaded[:TOP_K] if len(loaded) > TOP_K else list(loaded)
    # Keep discovery order; pad with remaining fused for ranking fairness.
    for name in fused:
        if name not in seen:
            ranked.append(name)
            if len(ranked) >= TOP_K:
                break
    tokens = sum(int(getattr(tools.get(n), "schema_tokens", 0) or 0) for n in ranked[:TOP_K])
    # Prefer store.schema_tokens when available via side channel — filled by load_arm.
    latency = (time.perf_counter() - started) * 1000.0
    return ArmLoadResult(
        ranked=ranked,
        tokens=tokens,
        llm_turns=turns,
        store_requests=0,
        latency_ms=latency,
        extra={"uses_catalog_depends_on": True, "initial_load": initial},
    )


def load_arm(
    arm: str,
    fused: list[str],
    store: GraphStore,
    *,
    session_id: str | None = None,
    session_resolved: set[str] | None = None,
    kv_closure: KvClosureIndex | None = None,
    bm25: Bm25Index | None = None,
    all_tool_names: list[str] | None = None,
) -> ArmLoadResult:
    """Dispatch one v3 arm using ARM_KNOBS fan_in (not a hard-coded 4)."""
    started = time.perf_counter()
    knob = ARM_KNOBS[arm]
    fan_in = knob.fan_in
    seeds = list(fused) if fan_in is None else list(fused[:fan_in])
    backend = knob.backend

    if backend == "preload":
        names = all_tool_names or sorted(store.names)
        ranked = list(names)
        tokens = schema_tokens_for(store, ranked)
        return ArmLoadResult(
            ranked=ranked,
            tokens=tokens,
            llm_turns=0,
            store_requests=0,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            extra={"completeness_defined": 1.0},
        )

    if backend == "flat":
        # flat_topk: fused ranked list (fan_in pool); completeness@16 uses prefix.
        ranked = list(seeds)
        return ArmLoadResult(
            ranked=ranked,
            tokens=schema_tokens_for(store, ranked[:TOP_K]),
            llm_turns=0,
            store_requests=0,
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )

    if backend == "iterative":
        if bm25 is None:
            names = sorted(store.tools)
            docs = [tokenize(store.tools[n].base_text()) for n in names]
            bm25 = Bm25Index(names, docs)
        result = iterative_search_load(seeds, store.tools, bm25)
        result.tokens = schema_tokens_for(store, result.ranked[:TOP_K])
        result.latency_ms = (time.perf_counter() - started) * 1000.0
        return result

    if backend == "kv_hop":
        adj = requires_adjacency(store)
        loaded, requests = expand_kv_hop(seeds, adj)
        return ArmLoadResult(
            ranked=loaded,
            tokens=schema_tokens_for(store, loaded[:TOP_K]),
            llm_turns=0,
            store_requests=requests,
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )

    if backend == "kv_hop_session":
        # One extra request for session state, then hop expansion; prune resolved.
        adj = requires_adjacency(store)
        loaded, hop_requests = expand_kv_hop(seeds, adj)
        resolved = session_resolved or set()
        # Session-state fetch counts as +1 store request.
        store_requests = hop_requests + 1
        pruned = [name for name in loaded if name not in resolved]
        skipped = {name: "session_resolved" for name in loaded if name in resolved}
        return ArmLoadResult(
            ranked=pruned,
            tokens=schema_tokens_for(store, pruned[:TOP_K]),
            llm_turns=0,
            store_requests=store_requests,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            skipped=skipped,
        )

    if backend == "kv_closure":
        index = kv_closure or KvClosureIndex(store)
        loaded = index.fetch(seeds)
        return ArmLoadResult(
            ranked=loaded,
            tokens=schema_tokens_for(store, loaded[:TOP_K]),
            llm_turns=0,
            store_requests=1,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            extra={"edits_since_rebuild": index.edits_since_rebuild},
        )

    if backend == "hydra_graph":
        # Caller must ensure_hydra_synced outside this timed call.
        result = store.load_tools(seeds=seeds, session_id=session_id, k=TOP_K)
        store_requests = 2 if session_id is not None else 1
        ranked = list(result.tools)
        # Append remaining fused seeds not returned (rank padding).
        seen = set(ranked)
        for name in seeds:
            if name not in seen and name not in result.skipped:
                ranked.append(name)
        return ArmLoadResult(
            ranked=ranked,
            tokens=schema_tokens_for(store, ranked[:TOP_K]),
            llm_turns=0,
            store_requests=store_requests,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            skipped=dict(result.skipped),
        )

    if backend == "oracle":
        costs = store.recursive_cte(seeds, list(knob.rels or ORACLE_RELS), knob.depth or MAX_LEN)
        ranked = rank_costs(costs)
        for name in seeds:
            if name not in costs:
                ranked.append(name)
        return ArmLoadResult(
            ranked=ranked,
            tokens=schema_tokens_for(store, ranked[:TOP_K]),
            llm_turns=0,
            store_requests=1,
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )

    raise ValueError(f"unknown arm backend: {backend}")


def score_task(
    result: ArmLoadResult,
    gold: frozenset[str],
    *,
    skipped_should_be_in_gold: bool = True,
    completeness_defined: float | None = None,
) -> TaskMetrics:
    if completeness_defined is not None:
        c16 = bool(completeness_defined >= 1.0 - 1e-12)
    else:
        c16 = completeness(result.ranked, gold, TOP_K)
    wrong_skip = False
    tokens_saved = 0
    if result.skipped and skipped_should_be_in_gold:
        for name in result.skipped:
            if name in gold:
                wrong_skip = True
            tokens_saved += 0  # filled by caller with schema tokens when needed
    return TaskMetrics(
        completeness_16=c16,
        tokens=result.tokens,
        llm_turns=result.llm_turns,
        store_requests=result.store_requests,
        latency_ms=result.latency_ms,
        ranked=list(result.ranked),
        skipped=dict(result.skipped),
        wrong_skip=wrong_skip,
        tokens_saved=tokens_saved,
    )


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    # Nearest-rank at pct in (0, 1].
    index = min(len(ordered) - 1, max(0, int(round(pct * (len(ordered) - 1)))))
    return ordered[index]


def aggregate_task_metrics(
    rows: list[TaskMetrics],
    *,
    mains: list[str] | None = None,
) -> dict[str, float | int | None]:
    """Micro mean, plus main-macro mean when ``mains`` aligns with ``rows``."""
    n = max(1, len(rows))
    latencies = [row.latency_ms for row in rows]
    out: dict[str, float | int | None] = {
        "n": len(rows),
        "c16_micro": sum(1.0 for row in rows if row.completeness_16) / n,
        "tokens": sum(row.tokens for row in rows) / n,
        "llm_turns": sum(row.llm_turns for row in rows) / n,
        "store_requests": sum(row.store_requests for row in rows) / n,
        "latency_ms_mean": sum(latencies) / n,
        "latency_p50_ms": _percentile(latencies, 0.50),
        "latency_p95_ms": _percentile(latencies, 0.95),
        "wrong_skip_rate": sum(1.0 for row in rows if row.wrong_skip) / n,
        "tokens_saved": sum(row.tokens_saved for row in rows) / n,
        "label": "measured",
    }
    if mains is not None and len(mains) == len(rows):
        by_main: dict[str, list[TaskMetrics]] = defaultdict(list)
        for main, row in zip(mains, rows):
            by_main[main].append(row)
        main_c16 = []
        main_tokens = []
        main_turns = []
        main_reqs = []
        for _main, group in by_main.items():
            g_n = max(1, len(group))
            main_c16.append(sum(1.0 for row in group if row.completeness_16) / g_n)
            main_tokens.append(sum(row.tokens for row in group) / g_n)
            main_turns.append(sum(row.llm_turns for row in group) / g_n)
            main_reqs.append(sum(row.store_requests for row in group) / g_n)
        m_n = max(1, len(main_c16))
        out["c16"] = sum(main_c16) / m_n
        out["tokens_main_macro"] = sum(main_tokens) / m_n
        out["llm_turns_main_macro"] = sum(main_turns) / m_n
        out["store_requests_main_macro"] = sum(main_reqs) / m_n
        out["n_mains"] = len(main_c16)
        out["scoring"] = "main_macro"
    else:
        out["c16"] = out["c16_micro"]
        out["scoring"] = "query_micro"
    return out


def _prepare_bm25(store: GraphStore) -> Bm25Index:
    names = sorted(store.tools)
    docs = [tokenize(store.tools[name].base_text()) for name in names]
    return Bm25Index(names, docs)


def run_b1(
    store: GraphStore,
    sealed: list[Instance],
    fusion: list[list[str]],
    *,
    arms: tuple[str, ...] = ARMS,
    session_id: str | None = "b1",
) -> dict[str, dict]:
    """Single-turn sealed completeness eval across arms (no learning here)."""
    store.ensure_hydra_synced()
    bm25 = _prepare_bm25(store)
    kv_closure = KvClosureIndex(store)
    all_names = sorted(store.names)
    out: dict[str, dict] = {}
    for arm in arms:
        rows: list[TaskMetrics] = []
        mains: list[str] = []
        for query in sealed:
            fused = fusion[query.idx]
            result = load_arm(
                arm,
                fused,
                store,
                session_id=session_id if arm == "hydra_graph" else None,
                kv_closure=kv_closure,
                bm25=bm25,
                all_tool_names=all_names,
            )
            defined = result.extra.get("completeness_defined")
            rows.append(
                score_task(
                    result,
                    query.gold,
                    completeness_defined=float(defined) if defined is not None else None,
                )
            )
            mains.append(query.main)
        out[arm] = aggregate_task_metrics(rows, mains=mains)
    return out


def run_b2(
    store: GraphStore,
    sealed: list[Instance],
    fusion: list[list[str]],
    tools: list[Tool],
    seed: int,
    *,
    arms: tuple[str, ...] = ("flat_topk", "kv_hop", "kv_hop_session", "hydra_graph"),
    n_sessions: int | None = None,
) -> dict[str, dict]:
    """Session-token eval (H3). Uses B2 session constructor with gold pruning."""
    kwargs = {}
    if n_sessions is not None:
        kwargs["n_sessions"] = n_sessions
    plans = plan_b2_sessions(sealed, tools, seed, **kwargs)
    store.ensure_hydra_synced()
    bm25 = _prepare_bm25(store)
    kv_closure = KvClosureIndex(store)
    out: dict[str, dict] = {}
    for arm in arms:
        rows: list[TaskMetrics] = []
        for plan in plans:
            resolved: set[str] = set()
            sid = f"b2-{seed}-{plan.session_index}"
            for turn in plan.turns:
                result = load_arm(
                    arm,
                    fusion[turn.query.idx],
                    store,
                    session_id=sid if arm in ("hydra_graph", "kv_hop_session") else None,
                    session_resolved=set(resolved),
                    kv_closure=kv_closure,
                    bm25=bm25,
                )
                metrics = score_task(result, turn.gold)
                # Tokens saved = schema tokens of correctly skipped tools.
                saved = 0
                for name, _via in result.skipped.items():
                    if name not in turn.gold:
                        saved += int(store.schema_tokens.get(name, 0))
                    else:
                        metrics.wrong_skip = True
                metrics.tokens_saved = saved
                rows.append(metrics)
                # Advance session: resolve read-only gold tools that were loaded.
                for name in turn.query.gold:
                    if is_read_only(name) and name in set(result.ranked[:TOP_K]):
                        resolved.add(name)
                        store.record_result(sid, name, args_hash="b2")
                    if name.startswith("set_") and name in set(result.ranked[:TOP_K]):
                        store.record_result(sid, name, args_hash="b2")
                        for producer in store.invalidates.get(name, ()):
                            resolved.discard(producer)
        out[arm] = aggregate_task_metrics(rows)
    return out


def _true_bundle(store: GraphStore, seeds: list[str]) -> list[str]:
    adj = requires_adjacency(store)
    loaded, _ = expand_kv_hop(seeds, adj)
    return loaded


def run_staleness(
    store: GraphStore,
    sealed: list[Instance],
    fusion: list[list[str]],
    *,
    n_edits: int = 200,
    queries_per_edit: int = 50,
    edit_ops: list[Callable[[GraphStore], None]] | None = None,
) -> dict[str, Any]:
    """Compare kv_closure vs hydra_graph wrong-bundle rate under edits.

    After each edit, sample ``queries_per_edit`` sealed queries and count
    bundles that differ from the true current REQUIRES closure. kv_closure
    rebuilds every ``CLOSURE_REBUILD_INTERVAL`` edits; hydra_graph is live.
    """
    kv_closure = KvClosureIndex(store)
    store.ensure_hydra_synced()
    rng_queries = sealed
    if not rng_queries:
        return {"n_edits": 0, "kv_closure": {}, "hydra_graph": {}}

    def sample_queries(edit_index: int) -> list[Instance]:
        # Deterministic rotating window — no sealed tuning.
        start = (edit_index * queries_per_edit) % len(rng_queries)
        out: list[Instance] = []
        for offset in range(queries_per_edit):
            out.append(rng_queries[(start + offset) % len(rng_queries)])
        return out

    kv_wrong = 0
    hydra_wrong = 0
    total = 0
    ops = edit_ops or _default_edit_ops(store, n_edits)

    for edit_index, op in enumerate(ops[:n_edits]):
        op(store)
        kv_closure.note_edit()
        store.ensure_hydra_synced()
        for query in sample_queries(edit_index):
            seeds = list(fusion[query.idx][:FUSION_POOL])
            truth = _true_bundle(store, seeds)
            truth_set = set(truth)
            kv_bundle = set(kv_closure.fetch(seeds))
            # True bundle = memory REQUIRES closure. kv_closure may be stale.
            # hydra_graph bundle = reachable set from live HydraDB MSpaths (full
            # costs, not top-k truncation) — must not reuse expand_kv_hop(truth).
            if kv_bundle != truth_set:
                kv_wrong += 1
            # closure() calls ensure_hydra_synced; skip re-sync by matching version.
            hydra_costs = store.closure(seeds, ["REQUIRES"])
            hydra_set = set(hydra_costs)
            if hydra_set != truth_set:
                hydra_wrong += 1
            total += 1

    denom = max(1, total)
    return {
        "n_edits": min(n_edits, len(ops)),
        "queries_per_edit": queries_per_edit,
        "total_checks": total,
        "rebuild_interval": CLOSURE_REBUILD_INTERVAL,
        "kv_closure_rebuilds": kv_closure.rebuild_count,
        "kv_closure": {
            "wrong_bundles": kv_wrong,
            "wrong_bundle_rate": kv_wrong / denom,
        },
        "hydra_graph": {
            "wrong_bundles": hydra_wrong,
            "wrong_bundle_rate": hydra_wrong / denom,
        },
    }


def _default_edit_ops(store: GraphStore, n_edits: int) -> list[Callable[[GraphStore], None]]:
    """Build retire / reweight / undo ops from current live REQUIRES edges."""
    edges = [
        (store.by_id[e.src], store.by_id[e.dst])
        for e in store.active_edges(["REQUIRES"])
        if e.src in store.by_id and e.dst in store.by_id
    ]
    if not edges:
        return [lambda _s: None for _ in range(n_edits)]
    ops: list[Callable[[GraphStore], None]] = []
    for index in range(n_edits):
        src, dst = edges[index % len(edges)]
        kind = index % 3
        if kind == 0:
            # Negative feedback can retire when τ is set by caller.
            def retire_op(s: GraphStore, a: str = src, b: str = dst) -> None:
                prev_tau = s.tau
                s.tau = max(prev_tau, 0.55)
                s.record_trace(
                    None,
                    f"stale-retire-{a}-{b}",
                    tools_used=[a],
                    tools_loaded=[a, b],
                )
                s.tau = prev_tau

            ops.append(retire_op)
        elif kind == 1:
            def reweight_op(s: GraphStore, a: str = src, b: str = dst) -> None:
                s.record_trace(
                    None,
                    f"stale-reweight-{a}-{b}",
                    tools_used=[a, b],
                    tools_loaded=[a, b],
                )

            ops.append(reweight_op)
        else:
            def undo_op(s: GraphStore, fallback_src: str = src, fallback_dst: str = dst) -> None:
                if s.edit_log.exists():
                    lines = [ln for ln in s.edit_log.read_text().splitlines() if ln.strip()]
                    if lines:
                        try:
                            payload = json.loads(lines[-1])
                            edit_id = payload.get("edit_id")
                            if edit_id and s.undo_edit(str(edit_id)):
                                return
                        except json.JSONDecodeError:
                            pass
                s.record_trace(
                    None,
                    f"stale-undo-fallback-{fallback_src}",
                    tools_used=[fallback_src],
                    tools_loaded=[fallback_src, fallback_dst],
                )

            ops.append(undo_op)
    return ops


def run_h4_curve(
    store: GraphStore,
    learn: list[Instance],
    sealed: list[Instance],
    fusion: list[list[str]],
    *,
    budgets: tuple = TRACE_BUDGETS,
    dev: list[Instance] | None = None,
) -> dict[str, Any]:
    """H4: hydra_graph tokens and c16 over TRACE_BUDGETS (learn then score sealed)."""
    from toolgraph.learn import calibrate_tau

    resolved_budgets = resolve_budgets(len(learn))
    _ = budgets
    points: list[dict] = []
    cursor = 0
    bm25 = _prepare_bm25(store)
    tau_history: list[dict] = []

    def score_hydra() -> dict:
        store.ensure_hydra_synced()
        rows: list[TaskMetrics] = []
        mains: list[str] = []
        for query in sealed:
            result = load_arm(
                "hydra_graph",
                fusion[query.idx],
                store,
                session_id=None,
                bm25=bm25,
            )
            rows.append(score_task(result, query.gold))
            mains.append(query.main)
        complete = [row for row in rows if row.completeness_16]
        tokens_per_complete = (
            sum(row.tokens for row in complete) / len(complete) if complete else 0.0
        )
        agg = aggregate_task_metrics(rows, mains=mains)
        agg["tokens_per_complete"] = tokens_per_complete
        agg["tau"] = store.tau
        return agg

    store.tau = 0.0
    tau_history.append({"traces": 0, "tau": 0.0})
    points.append({"traces": 0, **score_hydra()})
    for target in resolved_budgets:
        if target == 0:
            continue
        while cursor < target:
            query = learn[cursor]
            cursor += 1
            ingest_trace_edges(store, query)
        if dev:
            tau, grid = calibrate_tau(store, dev, fusion)
            store.tau = tau
            tau_history.append({"traces": target, "tau": tau, "grid": grid})
        store.ensure_hydra_synced()
        points.append({"traces": target, **score_hydra()})
    return {
        "arm": "hydra_graph",
        "points": points,
        "budgets": resolved_budgets,
        "tau_history": tau_history,
        "label": "measured",
    }


def learn_all_traces(store: GraphStore, learn: list[Instance]) -> int:
    count = 0
    for query in learn:
        count += ingest_trace_edges(store, query)
    return count


def bootstrap_store(
    hydra: HydraClient,
    tools: list[Tool],
    edit_log: Path,
    *,
    include_oracle: bool = True,
    apply_prior: bool = True,
) -> GraphStore:
    store = GraphStore(hydra, edit_log)
    store.load_catalog(tools, include_oracle=include_oracle)
    if apply_prior:
        candidates = propose_schema_candidates(tools)
        apply_io_prior(store, candidates)
    store.ensure_hydra_synced()
    return store


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="v3 B1/B2/staleness/H4 scaffolding")
    parser.add_argument(
        "command",
        choices=("b1", "b2", "staleness", "h4", "smoke"),
        help="Which scaffolding runner to invoke (step 4 sealed runs use these).",
    )
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--hydra-url", default="http://127.0.0.1:8443")
    parser.add_argument("--hydra-token", default="local-development-token-32-bytes")
    parser.add_argument("--fake", action="store_true", help="Use in-memory FakeHydra")
    parser.add_argument("--sessions", type=int, default=None, help="B2 session count override")
    parser.add_argument("--n-edits", type=int, default=200)
    parser.add_argument("--queries-per-edit", type=int, default=50)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    tools, instances = load_dataset(DATA)
    learn, dev, sealed, _asn = split_by_main(instances, args.seed)

    if args.fake or args.command == "smoke":
        hydra: Any = _FakeHydra()
    else:
        hydra = HydraClient(args.hydra_url, args.hydra_token)

    RESULTS.mkdir(parents=True, exist_ok=True)
    edit_log = RESULTS / f"v3_edits_seed{args.seed}.jsonl"
    if edit_log.exists():
        edit_log.unlink()

    print("building fusion (may embed)…", flush=True)
    fusion = build_fusion(tools, [query.text for query in instances])

    if args.command == "h4":
        store = bootstrap_store(hydra, tools, edit_log)
        # Curve starts at 0 traces; run_h4_curve learns progressively.
        store.tau = 0.0
        store.ensure_hydra_synced()
    else:
        store = bootstrap_store(hydra, tools, edit_log)
        learn_all_traces(store, learn)
        if args.command == "smoke" or args.fake:
            store.tau = 0.0
        elif dev:
            from toolgraph.learn import calibrate_tau

            tau, grid = calibrate_tau(store, dev, fusion)
            store.tau = tau
            print(f"calibrated tau={tau} on dev (grid={grid})", flush=True)
        else:
            store.tau = 0.0
        store.ensure_hydra_synced()

    if args.command == "smoke":
        sample = sealed[:5]
        store.ensure_hydra_synced()
        bm25 = _prepare_bm25(store)
        kv = KvClosureIndex(store)
        report = {}
        for arm in ARMS:
            result = load_arm(
                arm,
                fusion[sample[0].idx],
                store,
                session_id="smoke" if arm in ("hydra_graph", "kv_hop_session") else None,
                kv_closure=kv,
                bm25=bm25,
                all_tool_names=sorted(store.names),
            )
            report[arm] = {
                "n_ranked": len(result.ranked),
                "tokens": result.tokens,
                "llm_turns": result.llm_turns,
                "store_requests": result.store_requests,
            }
        payload = {"smoke": report, "arms": list(ARMS), "retrieval_seeded": list(RETRIEVAL_SEEDED_ARMS)}
    elif args.command == "b1":
        payload = {"seed": args.seed, "b1": run_b1(store, sealed, fusion)}
    elif args.command == "b2":
        payload = {
            "seed": args.seed,
            "b2": run_b2(store, sealed, fusion, tools, args.seed, n_sessions=args.sessions),
        }
    elif args.command == "staleness":
        payload = {
            "seed": args.seed,
            "staleness": run_staleness(
                store,
                sealed,
                fusion,
                n_edits=args.n_edits,
                queries_per_edit=args.queries_per_edit,
            ),
        }
    elif args.command == "h4":
        payload = {
            "seed": args.seed,
            "h4": run_h4_curve(store, learn, sealed, fusion, dev=dev),
        }
    else:
        raise ValueError(args.command)

    out_path = args.out or (RESULTS / f"v3_{args.command}_seed{args.seed}.json")
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload, indent=2, sort_keys=True)[:2000], flush=True)
    print(f"wrote {out_path}", flush=True)


if __name__ == "__main__":
    main()
