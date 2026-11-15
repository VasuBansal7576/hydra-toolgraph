import os
import unittest
from pathlib import Path

from toolgraph.data import load_dataset, split_indices
from toolgraph.eval_curve import completeness, rise_gate, resolve_budgets
from toolgraph.jev import redact
from toolgraph.learn import propose_schema_candidates
from toolgraph.protocol import RISE_SPREAD_MULTIPLIER, SEEDS, TRACE_BUDGETS


ROOT_DATA = os.path.join(os.path.dirname(__file__), "..", "data")


class ProtocolTest(unittest.TestCase):
    def test_completeness_requires_every_gold_tool(self) -> None:
        gold = frozenset({"a", "b"})
        self.assertTrue(completeness(["a", "b", "c"], gold, 2))
        self.assertFalse(completeness(["a", "c", "b"], gold, 2))

    def test_splits_are_disjoint_and_sealed_is_hidden_from_learn(self) -> None:
        for seed in SEEDS:
            learn, dev, sealed = split_indices(100, seed)
            self.assertFalse(set(learn) & set(dev))
            self.assertFalse(set(learn) & set(sealed))
            self.assertFalse(set(dev) & set(sealed))
            self.assertEqual(len(learn) + len(dev) + len(sealed), 100)

    def test_redact_strips_api_keys(self) -> None:
        secret = "api" + "key_example_secret"
        text = redact(f"denied {secret} for Authorization: Bearer {secret}")
        self.assertNotIn("api" + "key_", text)
        self.assertIn("[REDACTED]", text)

    def test_dataset_gold_stays_inside_the_catalog(self) -> None:
        tools, instances = load_dataset(Path(ROOT_DATA).resolve())
        names = {tool.name for tool in tools}
        self.assertEqual(len(tools), 573)
        self.assertEqual(len(instances), 1569)
        for query in instances[:50]:
            self.assertTrue(query.gold <= names)
            self.assertIn(query.main, query.gold)

    def test_trace_budgets_include_all_and_rise_multiplier(self) -> None:
        self.assertIn("all", TRACE_BUDGETS)
        self.assertEqual(RISE_SPREAD_MULTIPLIER, 3.0)
        self.assertEqual(resolve_budgets(941)[-1], 941)

    def test_schema_candidates_do_not_use_catalog_depends_on(self) -> None:
        tools, _instances = load_dataset(Path(ROOT_DATA).resolve())
        stripped = [
            type(tools[0])(
                name=tool.name,
                description=tool.description,
                kind=tool.kind,
                params=tool.params,
                depends_on=(),
            )
            for tool in tools
        ]
        candidates = propose_schema_candidates(stripped)
        self.assertGreater(len(candidates), 0)

    def test_rise_gate_requires_three_times_spread(self) -> None:
        def arm_series(start: float, end: float) -> list[dict]:
            return [
                {"traces": 0, "c10": start, "c16": start, "map10": start, "c16_spread": 0.0},
                {"traces": 100, "c10": end, "c16": end, "map10": end, "c16_spread": 0.02, "c16_by_seed": [end - 0.01, end, end + 0.01]},
            ]

        averaged = {
            "bm25_dense": arm_series(0.03, 0.03),
            "sqlite_one_hop": arm_series(0.03, 0.2),
            "sqlite_cte": arm_series(0.03, 0.25),
            "hydra_mspaths": arm_series(0.03, 0.25),
            "hydra_mspaths_prior": arm_series(0.04, 0.26),
            "hydra_ppr_prior": arm_series(0.04, 0.27),
            "oracle": arm_series(0.9, 0.95),
        }
        gate = rise_gate(averaged)
        self.assertTrue(gate["rises"])
        self.assertTrue(gate["passed"])


if __name__ == "__main__":
    unittest.main()
