"""Fail if compared arms differ in more than the declared knob under test."""

from __future__ import annotations

import unittest

from toolgraph.arms import (
    ARM_KNOBS,
    MATCHED_FANIN_ARMS,
    parity_diffs,
)
from toolgraph.protocol import FUSION_POOL, FUSION_SEEDS


class ArmParityTest(unittest.TestCase):
    def test_every_compares_with_edge_is_declared(self) -> None:
        for name, knob in ARM_KNOBS.items():
            for peer in knob.compares_with:
                self.assertIn(peer, ARM_KNOBS, f"{name} compares_with unknown {peer}")

    def test_matched_fanin_arms_share_fan_in_32(self) -> None:
        for name in MATCHED_FANIN_ARMS:
            knob = ARM_KNOBS[name]
            self.assertEqual(knob.fan_in, FUSION_POOL, name)
            self.assertEqual(knob.rels, ("DEPENDS_ON", "SCHEMA_DEP"), name)
            self.assertTrue(knob.fallback, name)
            self.assertEqual(knob.top_k, 16, name)

    def test_k4_ablations_only_change_fan_in(self) -> None:
        pairs = (
            ("sqlite_one_hop_prior_k4", "sqlite_one_hop_prior"),
            ("sqlite_cte_prior_k4", "sqlite_cte_prior"),
            ("hydra_mspaths_prior_k4", "hydra_mspaths_prior"),
        )
        for ablation, headline in pairs:
            left = ARM_KNOBS[ablation]
            right = ARM_KNOBS[headline]
            self.assertEqual(left.fan_in, FUSION_SEEDS)
            self.assertEqual(right.fan_in, FUSION_POOL)
            allowed = set(left.allowed_diff_keys) | {"backend"}  # backend must match
            allowed.discard("backend")
            # backend must be identical for k4 ablations
            self.assertEqual(left.backend, right.backend)
            diffs = parity_diffs(left, right, set(left.allowed_diff_keys))
            self.assertEqual(diffs, {}, f"{ablation} vs {headline}: unexpected {diffs}")

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
        from pathlib import Path

        source = Path(__file__).resolve().parents[1] / "toolgraph" / "eval_curve.py"
        text = source.read_text()
        self.assertIn("ARM_KNOBS[arm]", text)
        self.assertIn("knob.fan_in", text)
        # Hard-coded FUSION_SEEDS seed slice in load_arm would reintroduce the confound.
        self.assertNotIn("fused[:FUSION_SEEDS]", text)


if __name__ == "__main__":
    unittest.main()
