"""Demo seed-policy guards: IDF anchors drop lone generic verbs like 'book'."""

from __future__ import annotations

import unittest
from pathlib import Path

from toolgraph.data import load_dataset, tokenize
from toolgraph.retrieve import Bm25Index
from toolgraph.seed_policy import query_anchors, select_demo_seeds


class SeedPolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        tools, _instances = load_dataset(Path(__file__).resolve().parents[1] / "data")
        names = [tool.name for tool in tools]
        docs = [tokenize(tool.base_text()) for tool in tools]
        cls.bm25 = Bm25Index(names, docs)

    def test_opentable_short_query_drops_uber_book_seed(self) -> None:
        query = "book a table on OpenTable"
        anchors = query_anchors(self.bm25, query)
        self.assertIn("opentable", anchors)
        self.assertIn("table", anchors)
        self.assertNotIn("book", anchors)
        seeds, expand, _debug = select_demo_seeds(self.bm25, query, pool=32)
        self.assertIn("opentable_reserve_restaurant_table", seeds[:3])
        self.assertNotIn("book_uber_ride", seeds)
        self.assertNotIn("cancel_uber_ride", seeds)
        self.assertTrue(expand)
        self.assertTrue(set(expand).issubset(set(seeds)))

    def test_opentable_long_query_keeps_resy_opentable_not_uber(self) -> None:
        query = (
            "I'd like to book a table for dinner at The Italian Bistro at 7 PM. "
            "Can you handle an OpenTable reservation?"
        )
        seeds, expand, debug = select_demo_seeds(self.bm25, query, pool=32)
        self.assertIn("opentable", debug["anchors"])
        self.assertNotIn("book_uber_ride", seeds)
        self.assertNotIn("cancel_uber_ride", seeds)
        self.assertIn("opentable_reserve_restaurant_table", seeds)
        self.assertTrue(set(expand).issubset(set(seeds)))


if __name__ == "__main__":
    unittest.main()
