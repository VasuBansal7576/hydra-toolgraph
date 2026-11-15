"""Fail if compared arms differ in more than the declared knob under test."""

from __future__ import annotations

import unittest
from pathlib import Path

from toolgraph.arms import (
    ARM_KNOBS,
    ARMS,
    MATCHED_FANIN_ARMS,
    RETRIEVAL_SEEDED_ARMS,
    parity_diffs,
)
from toolgraph.protocol import FUSION_POOL, MAX_LEN


class ArmParityTest(unittest.TestCase):
    def test_every_arm_is_registered(self) -> None:
        expected = {
            "preload_all",
            "flat_topk",
            "iterative_search",
            "kv_hop",
            "kv_hop_session",
            "kv_closure",
            "hydra_graph",
            "oracle",
        }
        self.assertEqual(set(ARM_KNOBS), expected)
        self.assertEqual(set(ARMS), expected)

    def test_every_compares_with_edge_is_declared(self) -> None:
        for name, knob in ARM_KNOBS.items():
            for peer in knob.compares_with:
                self.assertIn(peer, ARM_KNOBS, f"{name} compares_with unknown {peer}")

    def test_retrieval_seeded_share_fused_32(self) -> None:
        self.assertEqual(tuple(RETRIEVAL_SEEDED_ARMS), MATCHED_FANIN_ARMS)
        for name in RETRIEVAL_SEEDED_ARMS:
            knob = ARM_KNOBS[name]
            self.assertEqual(knob.fan_in, FUSION_POOL, name)
            self.assertEqual(knob.top_k, 16, name)
        self.assertIsNone(ARM_KNOBS["preload_all"].fan_in)

    def test_graph_kv_share_fan_in_and_depth(self) -> None:
        for name in ("kv_hop", "kv_closure", "hydra_graph"):
            knob = ARM_KNOBS[name]
            self.assertEqual(knob.fan_in, FUSION_POOL)
            self.assertEqual(knob.depth, MAX_LEN)
            self.assertEqual(knob.rels, ("REQUIRES",))

    def test_declared_comparisons_respect_allowed_diffs(self) -> None:
        for name, knob in ARM_KNOBS.items():
            for peer_name in knob.compares_with:
                peer = ARM_KNOBS[peer_name]
                allowed = set(knob.allowed_diff_keys)
                diffs = parity_diffs(knob, peer, allowed)
                self.assertEqual(
                    diffs,
                    {},
                    f"{name} vs {peer_name} differ beyond allowed {sorted(allowed)}: {diffs}",
                )

    def test_load_arm_uses_arm_knobs_fan_in(self) -> None:
        """Source guard: load_arm must read fan_in from ARM_KNOBS, not a hard-coded 4."""
        root = Path(__file__).resolve().parents[1] / "toolgraph"
        for rel in ("eval_v3.py", "eval_curve.py"):
            text = (root / rel).read_text()
            self.assertIn("ARM_KNOBS[arm]", text, rel)
            self.assertIn("knob.fan_in", text, rel)
            self.assertNotIn("fused[:FUSION_SEEDS]", text, rel)
            self.assertNotIn("fused[:4]", text, rel)


if __name__ == "__main__":
    unittest.main()
