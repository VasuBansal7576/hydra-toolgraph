"""ARM_KNOBS: declared seeds/fan-in, depth, rels, top-k, fallback per arm.

Matched-fan-in headline: metadata also-load, one-hop+prior, CTE+prior, and
Hydra+prior all expand from the same FUSION_POOL (32) fused hits. The old
4-seed graph variants are labelled ablations only.
"""

from __future__ import annotations

from dataclasses import dataclass

from toolgraph.protocol import FUSION_POOL, FUSION_SEEDS, KS, MAX_LEN


TOP_K = max(KS)


@dataclass(frozen=True)
class ArmKnob:
    name: str
    backend: str  # flat | metadata | one_hop | cte | mspaths
    fan_in: int | None  # fused hits used as seeds / expand sources; None = flat copy
    depth: int | None  # walk depth; metadata treated as 1 (also-load hop)
    rels: tuple[str, ...] | None
    top_k: int
    fallback: bool  # append remaining fused names after the walk
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


PRIOR_RELS = ("DEPENDS_ON", "SCHEMA_DEP")
TRACE_RELS = ("DEPENDS_ON",)
PRIOR_ONLY_RELS = ("SCHEMA_DEP",)
ORACLE_RELS = ("ORACLE_DEP",)

# Headline matched-fan-in set: same fan_in=32, same rels, same top_k, same fallback.
# Allowed diffs: backend and depth only.
_MATCHED = (
    "metadata_vector",
    "sqlite_one_hop_prior",
    "sqlite_cte_prior",
    "hydra_mspaths_prior",
)
_MATCHED_DIFF = ("backend", "depth", "name", "compares_with", "allowed_diff_keys", "headline")

ARM_KNOBS: dict[str, ArmKnob] = {
    "bm25_dense": _knob(
        "bm25_dense",
        backend="flat",
        fan_in=None,
        depth=None,
        rels=None,
        fallback=False,
        headline=True,
    ),
    "metadata_vector": _knob(
        "metadata_vector",
        backend="metadata",
        fan_in=FUSION_POOL,
        depth=1,
        rels=PRIOR_RELS,
        # At fan_in=FUSION_POOL the fused pool is already the seed set; fallback is a no-op.
        fallback=True,
        headline=True,
        compares_with=tuple(a for a in _MATCHED if a != "metadata_vector"),
        allowed_diff_keys=_MATCHED_DIFF,
    ),
    "sqlite_one_hop_prior": _knob(
        "sqlite_one_hop_prior",
        backend="one_hop",
        fan_in=FUSION_POOL,
        depth=1,
        rels=PRIOR_RELS,
        fallback=True,
        headline=True,
        compares_with=tuple(a for a in _MATCHED if a != "sqlite_one_hop_prior"),
        allowed_diff_keys=_MATCHED_DIFF,
    ),
    "sqlite_cte_prior": _knob(
        "sqlite_cte_prior",
        backend="cte",
        fan_in=FUSION_POOL,
        depth=MAX_LEN,
        rels=PRIOR_RELS,
        fallback=True,
        headline=True,
        compares_with=tuple(a for a in _MATCHED if a != "sqlite_cte_prior"),
        allowed_diff_keys=_MATCHED_DIFF,
    ),
    "hydra_mspaths_prior": _knob(
        "hydra_mspaths_prior",
        backend="mspaths",
        fan_in=FUSION_POOL,
        depth=MAX_LEN,
        rels=PRIOR_RELS,
        fallback=True,
        headline=True,
        compares_with=tuple(a for a in _MATCHED if a != "hydra_mspaths_prior"),
        allowed_diff_keys=_MATCHED_DIFF,
    ),
    # Labelled 4-seed ablations (old confound).
    "sqlite_one_hop_prior_k4": _knob(
        "sqlite_one_hop_prior_k4",
        backend="one_hop",
        fan_in=FUSION_SEEDS,
        depth=1,
        rels=PRIOR_RELS,
        fallback=True,
        headline=False,
        compares_with=("sqlite_one_hop_prior",),
        allowed_diff_keys=("fan_in", "name", "compares_with", "allowed_diff_keys", "headline"),
    ),
    "sqlite_cte_prior_k4": _knob(
        "sqlite_cte_prior_k4",
        backend="cte",
        fan_in=FUSION_SEEDS,
        depth=MAX_LEN,
        rels=PRIOR_RELS,
        fallback=True,
        headline=False,
        compares_with=("sqlite_cte_prior",),
        allowed_diff_keys=("fan_in", "name", "compares_with", "allowed_diff_keys", "headline"),
    ),
    "hydra_mspaths_prior_k4": _knob(
        "hydra_mspaths_prior_k4",
        backend="mspaths",
        fan_in=FUSION_SEEDS,
        depth=MAX_LEN,
        rels=PRIOR_RELS,
        fallback=True,
        headline=False,
        compares_with=("hydra_mspaths_prior",),
        allowed_diff_keys=("fan_in", "name", "compares_with", "allowed_diff_keys", "headline"),
    ),
    # Edge-set ablations at matched fan-in.
    "hydra_mspaths_trace_only": _knob(
        "hydra_mspaths_trace_only",
        backend="mspaths",
        fan_in=FUSION_POOL,
        depth=MAX_LEN,
        rels=TRACE_RELS,
        fallback=True,
        headline=True,
        compares_with=("hydra_mspaths_prior",),
        allowed_diff_keys=("rels", "name", "compares_with", "allowed_diff_keys"),
    ),
    "hydra_mspaths_prior_only": _knob(
        "hydra_mspaths_prior_only",
        backend="mspaths",
        fan_in=FUSION_POOL,
        depth=MAX_LEN,
        rels=PRIOR_ONLY_RELS,
        fallback=True,
        headline=True,
        compares_with=("hydra_mspaths_prior",),
        allowed_diff_keys=("rels", "name", "compares_with", "allowed_diff_keys"),
    ),
    "oracle": _knob(
        "oracle",
        backend="cte",
        fan_in=FUSION_POOL,
        depth=MAX_LEN,
        rels=ORACLE_RELS,
        fallback=True,
        headline=True,
        compares_with=("sqlite_cte_prior",),
        allowed_diff_keys=("rels", "name", "compares_with", "allowed_diff_keys"),
    ),
}

ARMS = tuple(ARM_KNOBS)
HEADLINE_ARMS = tuple(name for name, knob in ARM_KNOBS.items() if knob.headline)
MATCHED_FANIN_ARMS = _MATCHED


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
