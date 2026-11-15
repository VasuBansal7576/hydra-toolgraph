"""ARM_KNOBS: declared seeds/fan-in, depth, rels, top-k, fallback per v3 arm.

Retrieval-seeded arms share FUSION_POOL (32) fused hits. Catalog depends_on
never enters learned arms; iterative_search may read it as a simulator only
(documented). Plain-store rivals (kv_hop / kv_closure) share fan_in and top_k
with hydra_graph so completeness ties are interpretable.
"""

from __future__ import annotations

from dataclasses import dataclass

from toolgraph.protocol import FUSION_POOL, KS, MAX_LEN


TOP_K = max(KS)


@dataclass(frozen=True)
class ArmKnob:
    name: str
    backend: str
    fan_in: int | None  # fused hits used as seeds; None = all tools / not retrieval-seeded
    depth: int | None
    rels: tuple[str, ...] | None
    top_k: int
    fallback: bool
    headline: bool
    compares_with: tuple[str, ...] = ()
    # Knobs that are allowed to differ vs each compares_with peer.
    allowed_diff_keys: tuple[str, ...] = ()


def _knob(
    name: str,
    *,
    backend: str,
    fan_in: int | None,
    depth: int | None,
    rels: tuple[str, ...] | None,
    fallback: bool,
    headline: bool,
    compares_with: tuple[str, ...] = (),
    allowed_diff_keys: tuple[str, ...] = (),
) -> ArmKnob:
    return ArmKnob(
        name=name,
        backend=backend,
        fan_in=fan_in,
        depth=depth,
        rels=rels,
        top_k=TOP_K,
        fallback=fallback,
        headline=headline,
        compares_with=compares_with,
        allowed_diff_keys=allowed_diff_keys,
    )


REQUIRES_RELS = ("REQUIRES",)
ORACLE_RELS = ("ORACLE_DEP",)

# Fair plain-store vs graph: same fan_in / top_k; backend (and session pruning) may differ.
_GRAPH_KV = ("kv_hop", "kv_closure", "hydra_graph")
_GRAPH_KV_DIFF = (
    "backend",
    "name",
    "compares_with",
    "allowed_diff_keys",
    "headline",
    "fallback",
)

# Arms that seed from fused retrieval (must share fan_in=FUSION_POOL).
RETRIEVAL_SEEDED_ARMS = (
    "flat_topk",
    "iterative_search",
    "kv_hop",
    "kv_hop_session",
    "kv_closure",
    "hydra_graph",
    "oracle",
)

ARM_KNOBS: dict[str, ArmKnob] = {
    "preload_all": _knob(
        "preload_all",
        backend="preload",
        fan_in=None,
        depth=None,
        rels=None,
        fallback=False,
        headline=True,
    ),
    "flat_topk": _knob(
        "flat_topk",
        backend="flat",
        fan_in=FUSION_POOL,
        depth=None,
        rels=None,
        fallback=False,
        headline=True,
    ),
    "iterative_search": _knob(
        "iterative_search",
        backend="iterative",
        fan_in=FUSION_POOL,
        depth=None,
        # Simulator reads catalog depends_on for producer-needed params only (documented).
        rels=None,
        fallback=False,
        headline=True,
        compares_with=("flat_topk", "hydra_graph"),
        allowed_diff_keys=(
            "backend",
            "depth",
            "rels",
            "fallback",
            "name",
            "compares_with",
            "allowed_diff_keys",
            "headline",
        ),
    ),
    "kv_hop": _knob(
        "kv_hop",
        backend="kv_hop",
        fan_in=FUSION_POOL,
        depth=MAX_LEN,
        rels=REQUIRES_RELS,
        fallback=False,
        headline=True,
        compares_with=tuple(a for a in _GRAPH_KV if a != "kv_hop"),
        allowed_diff_keys=_GRAPH_KV_DIFF,
    ),
    "kv_hop_session": _knob(
        "kv_hop_session",
        backend="kv_hop_session",
        fan_in=FUSION_POOL,
        depth=MAX_LEN,
        rels=REQUIRES_RELS,
        fallback=False,
        headline=True,
        compares_with=("kv_hop",),
        allowed_diff_keys=(
            "backend",
            "name",
            "compares_with",
            "allowed_diff_keys",
            "headline",
        ),
    ),
    "kv_closure": _knob(
        "kv_closure",
        backend="kv_closure",
        fan_in=FUSION_POOL,
        depth=MAX_LEN,
        rels=REQUIRES_RELS,
        fallback=False,
        headline=True,
        compares_with=tuple(a for a in _GRAPH_KV if a != "kv_closure"),
        allowed_diff_keys=_GRAPH_KV_DIFF,
    ),
    "hydra_graph": _knob(
        "hydra_graph",
        backend="hydra_graph",
        fan_in=FUSION_POOL,
        depth=MAX_LEN,
        rels=REQUIRES_RELS,
        fallback=False,
        headline=True,
        compares_with=tuple(a for a in _GRAPH_KV if a != "hydra_graph"),
        allowed_diff_keys=_GRAPH_KV_DIFF,
    ),
    "oracle": _knob(
        "oracle",
        backend="oracle",
        fan_in=FUSION_POOL,
        depth=MAX_LEN,
        rels=ORACLE_RELS,
        fallback=False,
        headline=True,
        compares_with=("hydra_graph",),
        allowed_diff_keys=(
            "backend",
            "rels",
            "name",
            "compares_with",
            "allowed_diff_keys",
            "headline",
        ),
    ),
}

ARMS = tuple(ARM_KNOBS)
HEADLINE_ARMS = tuple(name for name, knob in ARM_KNOBS.items() if knob.headline)
MATCHED_FANIN_ARMS = RETRIEVAL_SEEDED_ARMS


def knob_public_fields(knob: ArmKnob) -> dict[str, object]:
    """Fields that define retrieval behaviour (parity-relevant)."""
    return {
        "fan_in": knob.fan_in,
        "depth": knob.depth,
        "rels": knob.rels,
        "top_k": knob.top_k,
        "fallback": knob.fallback,
        "backend": knob.backend,
    }


def parity_diffs(left: ArmKnob, right: ArmKnob, allowed: set[str]) -> dict[str, tuple[object, object]]:
    """Return behavioural fields that differ beyond ``allowed``."""
    diffs: dict[str, tuple[object, object]] = {}
    for key, value in knob_public_fields(left).items():
        other = knob_public_fields(right)[key]
        if value != other and key not in allowed:
            diffs[key] = (value, other)
    return diffs
