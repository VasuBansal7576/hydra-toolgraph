"""Demo/live seed selection policies for cleaner graph expansion.

These filters are applied in the product `ToolgraphService.load_tools` path.
Sealed eval continues to pass the full fused fan-in into `GraphStore.load_tools`
unchanged (see `eval_v3.load_arm`).
"""

from __future__ import annotations

from collections import defaultdict

from toolgraph.data import tokenize
from toolgraph.retrieve import Bm25Index

# Function words + generic verbs that should not seed expansion alone.
# "book" matches both OpenTable and Uber; alone it pollutes the demo bundle.
_STOP = frozenset(
    {
        "a",
        "an",
        "the",
        "to",
        "for",
        "on",
        "at",
        "of",
        "in",
        "is",
        "be",
        "can",
        "you",
        "i",
        "d",
        "like",
        "handle",
        "and",
        "or",
        "with",
        "from",
        "by",
        "as",
        "it",
        "this",
        "that",
        "my",
        "me",
        "we",
        "our",
        "please",
        "want",
        "need",
        "help",
    }
)

_GENERIC_VERBS = frozenset(
    {
        "book",
        "get",
        "set",
        "cancel",
        "download",
        "read",
        "send",
        "make",
        "take",
        "find",
        "call",
        "put",
        "add",
        "remove",
        "update",
        "create",
        "delete",
        "check",
        "start",
        "stop",
        "open",
        "close",
        "play",
        "share",
        "search",
        "reserve",
    }
)


def query_anchors(bm25: Bm25Index, query: str) -> set[str]:
    """Content terms that must be hit for a seed to count as on-topic.

    Drops stopwords and generic verbs so a lone 'book' cannot seed Uber when
    the query also contains distinctive nouns like 'opentable' / 'table'.
    """
    anchors: set[str] = set()
    for term in set(tokenize(query)):
        if term in _STOP or term in _GENERIC_VERBS:
            continue
        if len(term) < 3:
            continue
        if term not in bm25.idf:
            continue
        anchors.add(term)
    return anchors


def bm25_scored(bm25: Bm25Index, query: str, limit: int) -> list[tuple[str, float, dict[str, float]]]:
    """Return (name, score, per-term contribution) sorted by score desc."""
    scores: dict[int, float] = defaultdict(float)
    contrib: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for term in set(tokenize(query)):
        idf = bm25.idf.get(term)
        if idf is None:
            continue
        for idx in bm25.inverted.get(term, []):
            freq = bm25.tfs[idx][term]
            length = len(bm25.docs[idx])
            denom = freq + bm25.k1 * (1.0 - bm25.b + bm25.b * length / bm25.avg)
            add = idf * (freq * (bm25.k1 + 1.0)) / denom
            scores[idx] += add
            contrib[idx][term] += add
    ranked = sorted(scores, key=lambda idx: (-scores[idx], bm25.names[idx]))[:limit]
    return [(bm25.names[idx], float(scores[idx]), dict(contrib[idx])) for idx in ranked]


def idf_anchor_filter(
    scored: list[tuple[str, float, dict[str, float]]],
    anchors: set[str],
    *,
    fallback_n: int = 3,
) -> list[tuple[str, float]]:
    """Keep seeds that match ≥1 anchor term; fall back to top-N if none match."""
    if not anchors:
        return [(name, score) for name, score, _contrib in scored]
    kept = [
        (name, score)
        for name, score, term_scores in scored
        if anchors.intersection(term_scores)
    ]
    if kept:
        return kept
    return [(name, score) for name, score, _contrib in scored[:fallback_n]]


def relative_expand_cutoff(
    scored_seeds: list[tuple[str, float]],
    *,
    alpha: float = 0.5,
) -> list[str]:
    """Expand REQUIRES only from seeds within ``alpha`` of the top seed score."""
    if not scored_seeds:
        return []
    top = scored_seeds[0][1]
    if top <= 0:
        return [name for name, _score in scored_seeds]
    return [name for name, score in scored_seeds if score >= alpha * top]


def explicit_namespaces(names: list[str], query: str) -> set[str]:
    """Find explicitly requested catalog prefixes, excluding generic operations."""
    generic = _STOP | _GENERIC_VERBS | {
        "analyze", "calculate", "convert", "monitor", "fetch", "list", "retrieve",
        "detect", "generate", "track", "measure", "control", "enable", "disable",
        "schedule", "manage", "record", "resolve", "validate", "execute", "export", "log",
    }
    counts = defaultdict(int)
    for name in names:
        prefix, separator, _ = name.partition("_")
        if separator and prefix not in generic:
            counts[prefix.lower()] += 1
    terms = set(tokenize(query))
    return {prefix for prefix, count in counts.items() if count >= 2 and prefix in terms}


def select_demo_seeds(
    bm25: Bm25Index,
    query: str,
    *,
    pool: int = 32,
    expand_alpha: float = 0.5,
    apply_idf_anchor: bool = True,
    apply_rel_cutoff: bool = True,
) -> tuple[list[str], list[str], dict]:
    """Select ranking seeds + expansion seeds for the live demo path.

    Returns ``(seeds, expand_seeds, debug)``.
    """
    scored = bm25_scored(bm25, query, pool)
    anchors = query_anchors(bm25, query) if apply_idf_anchor else set()
    if apply_idf_anchor:
        filtered = idf_anchor_filter(scored, anchors)
    else:
        filtered = [(name, score) for name, score, _contrib in scored]
    namespaces = explicit_namespaces(bm25.names, query)
    scoped = [(name, score) for name, score in filtered if name.split("_", 1)[0] in namespaces]
    if namespaces and scoped and filtered[0][0].split("_", 1)[0] in namespaces:
        filtered = scoped
    seeds = [name for name, _score in filtered]
    if apply_rel_cutoff:
        expand_seeds = relative_expand_cutoff(filtered, alpha=expand_alpha)
    else:
        expand_seeds = list(seeds)
    debug = {
        "anchors": sorted(anchors),
        "explicit_namespaces": sorted(namespaces),
        "n_scored": len(scored),
        "n_seeds": len(seeds),
        "n_expand": len(expand_seeds),
        "top_scores": [(name, round(score, 3)) for name, score in filtered[:8]],
        "expand_alpha": expand_alpha if apply_rel_cutoff else None,
        "idf_anchor": apply_idf_anchor,
    }
    return seeds, expand_seeds, debug
