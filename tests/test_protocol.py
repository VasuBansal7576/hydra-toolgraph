import os
import unittest
from pathlib import Path

from toolgraph.data import load_dataset, split_indices
from toolgraph.eval_curve import completeness
from toolgraph.jev import redact
from toolgraph.protocol import SEEDS


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


if __name__ == "__main__":
    unittest.main()
