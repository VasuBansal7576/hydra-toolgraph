"""B2 session constructor (label constructed; not measured until sealed run).

Per seed: 200 sessions of 8 turns from sealed queries.
Turn 1 uniform; each next turn drawn uniformly from the 20 unused sealed
queries with highest gold-set Jaccard to the previous turn.

Turn gold = gold minus read-only tools resolved earlier in the session and
not invalidated since, minus helpers reachable only through those tools
(catalog depends_on graph — benchmark constructor only).
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from toolgraph.data import Instance, Tool
from toolgraph.graph import is_read_only
from toolgraph.protocol import B2_JACCARD_POOL, B2_SESSIONS_PER_SEED, B2_TURNS


def jaccard(a: frozenset[str] | set[str], b: frozenset[str] | set[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def catalog_adjacency(tools: list[Tool] | dict[str, Tool]) -> dict[str, list[str]]:
    """Outbound catalog depends_on (benchmark constructor / iterative_search only)."""
    items = tools.values() if isinstance(tools, dict) else tools
    adj: dict[str, list[str]] = {}
    for tool in items:
        outs = sorted({dep["name"] for dep in tool.depends_on if dep.get("name")})
        adj[tool.name] = outs
    return adj


def catalog_closure(seeds: set[str], adj: dict[str, list[str]]) -> set[str]:
    seen = set(seeds)
    frontier = list(seeds)
    while frontier:
        node = frontier.pop()
        for neigh in adj.get(node, ()):
            if neigh not in seen:
                seen.add(neigh)
                frontier.append(neigh)
    return seen


def prune_session_gold(
    gold: frozenset[str],
    resolved_readonly: set[str],
    adj: dict[str, list[str]],
) -> frozenset[str]:
    """Drop resolved read-only tools and helpers reachable only through them.

    A helper in the catalog depends_on closure of removed tools is dropped
    unless some kept non-helper gold tool still reaches it. INVALIDATES
    restores a producer by removing it from ``resolved_readonly`` before
    calling this (caller responsibility).
    """
    removed = set(resolved_readonly) & set(gold)
    if not removed:
        return gold
    helper_of_removed = catalog_closure(removed, adj) - removed
    kept_seeds = set(gold) - removed - helper_of_removed
    still_needed = catalog_closure(kept_seeds, adj)
    drop = removed | (helper_of_removed - still_needed)
    return frozenset(gold - drop)


@dataclass
class SessionTurn:
    query: Instance
    gold: frozenset[str]  # pruned gold for this turn
    resolved_readonly_before: frozenset[str]


@dataclass
class SessionPlan:
    turns: list[SessionTurn]
    seed: int
    session_index: int


def build_b2_sessions(
    sealed: list[Instance],
    seed: int,
    *,
    n_sessions: int = B2_SESSIONS_PER_SEED,
    turns: int = B2_TURNS,
    jaccard_pool: int = B2_JACCARD_POOL,
) -> list[list[Instance]]:
    """Construct query sequences only (no gold pruning)."""
    if len(sealed) < turns:
        raise ValueError(f"need at least {turns} sealed queries, got {len(sealed)}")
    rng = random.Random(seed)
    sessions: list[list[Instance]] = []
    for _ in range(n_sessions):
        unused = list(sealed)
        session: list[Instance] = []
        first = unused.pop(rng.randrange(len(unused)))
        session.append(first)
        for _turn in range(1, turns):
            prev = session[-1]
            scored = sorted(
                unused,
                key=lambda query: (-jaccard(query.gold, prev.gold), query.idx),
            )
            pool = scored[: min(jaccard_pool, len(scored))]
            pick = pool[rng.randrange(len(pool))]
            unused.remove(pick)
            session.append(pick)
        sessions.append(session)
    return sessions


def apply_turn_resolution(
    resolved_readonly: set[str],
    tool: str,
    invalidates: dict[str, set[str]],
) -> None:
    """Update session resolved set after a tool is used.

    Read-only tools become resolved. INVALIDATES clears producers (e.g. set_X → get_X).
    """
    for producer in invalidates.get(tool, ()):
        resolved_readonly.discard(producer)
    if is_read_only(tool):
        resolved_readonly.add(tool)


def plan_session_golds(
    session: list[Instance],
    tools: list[Tool] | dict[str, Tool],
    *,
    invalidates: dict[str, set[str]] | None = None,
    seed: int = 0,
    session_index: int = 0,
) -> SessionPlan:
    """Prune gold per turn given earlier resolved read-only tools.

    Resolution model for the constructor: every read-only tool in the *raw*
    turn gold is treated as resolved after the turn (simulating successful use),
    and every non-read-only gold tool may fire INVALIDATES.
    """
    adj = catalog_adjacency(tools)
    inv = invalidates or _default_invalidates(tools)
    resolved: set[str] = set()
    turns_out: list[SessionTurn] = []
    for query in session:
        before = frozenset(resolved)
        pruned = prune_session_gold(query.gold, resolved, adj)
        turns_out.append(
            SessionTurn(query=query, gold=pruned, resolved_readonly_before=before)
        )
        for name in sorted(query.gold):
            apply_turn_resolution(resolved, name, inv)
    return SessionPlan(turns=turns_out, seed=seed, session_index=session_index)


def plan_b2_sessions(
    sealed: list[Instance],
    tools: list[Tool] | dict[str, Tool],
    seed: int,
    *,
    n_sessions: int = B2_SESSIONS_PER_SEED,
    turns: int = B2_TURNS,
    jaccard_pool: int = B2_JACCARD_POOL,
    invalidates: dict[str, set[str]] | None = None,
) -> list[SessionPlan]:
    sequences = build_b2_sessions(
        sealed,
        seed,
        n_sessions=n_sessions,
        turns=turns,
        jaccard_pool=jaccard_pool,
    )
    return [
        plan_session_golds(
            seq,
            tools,
            invalidates=invalidates,
            seed=seed,
            session_index=index,
        )
        for index, seq in enumerate(sequences)
    ]


def _default_invalidates(tools: list[Tool] | dict[str, Tool]) -> dict[str, set[str]]:
    names = {tool.name for tool in (tools.values() if isinstance(tools, dict) else tools)}
    out: dict[str, set[str]] = {}
    for name in names:
        if name.startswith("set_"):
            get_name = "get_" + name[4:]
            if get_name in names:
                out.setdefault(name, set()).add(get_name)
    return out
