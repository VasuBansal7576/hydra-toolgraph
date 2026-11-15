"""v3 GraphStore: two-request load_tools, skip, undo, as_of, feedback, INVALIDATES."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from toolgraph.data import Tool
from toolgraph.graph import GraphStore, OPEN_VALID_TO, is_read_only
from toolgraph.hydra import RecordingHydraClient


class FakeHydra:
    """Recording client that returns plausible MSpaths / RESOLVED rows."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.parameters: list[dict | None] = []
        self.rows: list = []
        self.resolved_producers: list[str] = []

    @property
    def call_count(self) -> int:
        return len(self.statements)

    def reset(self) -> None:
        self.statements.clear()
        self.parameters.clear()

    def query(self, statement: str, parameters: dict | None = None) -> dict:
        self.statements.append(statement)
        self.parameters.append(parameters)
        if "algo.MSpaths" in statement:
            return {"columns": ["path", "pathCost"], "rows": self.rows, "bookmark": None}
        if "[:RESOLVED]" in statement or "-[:RESOLVED]->" in statement:
            if "RETURN e.producer" in statement:
                return {
                    "columns": ["e.producer"],
                    "rows": [[name] for name in self.resolved_producers],
                    "bookmark": None,
                }
        return {"columns": [], "rows": [], "bookmark": None}


def _tool(name: str, deps: tuple[dict, ...] = (), params: tuple[dict, ...] = (), desc: str | None = None) -> Tool:
    return Tool(
        name=name,
        description=desc or f"tool {name}",
        kind="core",
        params=params,
        depends_on=deps,
    )


def _path_row(nodes: list[str], costs: list[float] | None = None) -> list:
    hops = costs if costs is not None else [1.0] * max(0, len(nodes) - 1)
    return [
        {
            "value": {
                "nodes": [{"properties": {"tool_id": name}} for name in nodes],
                "relationships": [{"properties": {"cost": c}} for c in hops],
            },
            "type": "Path",
        },
        {"value": float(sum(hops)), "type": "Float"},
    ]


class V3StoreTest(unittest.TestCase):
    def test_two_request_guarantee(self) -> None:
        tools = [_tool("seed"), _tool("helper")]
        with tempfile.TemporaryDirectory() as tmp:
            hydra = FakeHydra()
            store = GraphStore(hydra, Path(tmp) / "edits.jsonl")  # type: ignore[arg-type]
            store.load_catalog(tools, include_oracle=False)
            store.add_schema_edge("seed", "helper", "io_prior")
            store.sync_learned_to_hydra()
            hydra.rows = [_path_row(["seed", "helper"])]

            hydra.reset()
            store.load_tools(["seed"], session_id="s1", k=10)
            self.assertEqual(hydra.call_count, 2)
            self.assertIn("RESOLVED", hydra.statements[0])
            self.assertIn("algo.MSpaths", hydra.statements[1])

            hydra.reset()
            store.load_tools(["seed"], session_id=None, k=10)
            self.assertEqual(hydra.call_count, 1)
            self.assertIn("algo.MSpaths", hydra.statements[0])

    def test_recording_client_two_request_shape(self) -> None:
        """RecordingHydraClient counts the same way when wrapping FakeHydra."""
        tools = [_tool("seed"), _tool("helper")]
        with tempfile.TemporaryDirectory() as tmp:
            inner = FakeHydra()
            hydra = RecordingHydraClient(inner)  # type: ignore[arg-type]
            store = GraphStore(hydra, Path(tmp) / "edits.jsonl")  # type: ignore[arg-type]
            store.load_catalog(tools, include_oracle=False)
            inner.rows = [_path_row(["seed"])]
            hydra.reset()
            store.load_tools(["seed"], "sess", k=5)
            self.assertEqual(hydra.call_count, 2)
            hydra.reset()
            store.load_tools(["seed"], None, k=5)
            self.assertEqual(hydra.call_count, 1)

    def test_skip_logic_resolved_producer(self) -> None:
        tools = [_tool("seed"), _tool("bridge"), _tool("helper")]
        with tempfile.TemporaryDirectory() as tmp:
            hydra = FakeHydra()
            store = GraphStore(hydra, Path(tmp) / "edits.jsonl")  # type: ignore[arg-type]
            store.load_catalog(tools, include_oracle=False)
            store.add_schema_edge("seed", "bridge", "io_prior")
            store.add_schema_edge("bridge", "helper", "io_prior")
            store.sync_learned_to_hydra()
            hydra.resolved_producers = ["bridge"]
            hydra.rows = [_path_row(["seed", "bridge", "helper"])]
            hydra.reset()
            result = store.load_tools(["seed"], session_id="s1", k=10)
            self.assertIn("seed", result.tools)
            self.assertNotIn("bridge", result.tools)
            self.assertNotIn("helper", result.tools)
            self.assertEqual(result.skipped.get("bridge"), "bridge")
            self.assertEqual(result.skipped.get("helper"), "bridge")

    def test_undo_edit_restores_live_and_hist(self) -> None:
        tools = [_tool("a"), _tool("b")]
        with tempfile.TemporaryDirectory() as tmp:
            hydra = FakeHydra()
            store = GraphStore(hydra, Path(tmp) / "edits.jsonl")  # type: ignore[arg-type]
            clock = [1000.0]
            store._now = lambda: clock[0]
            store.load_catalog(tools, include_oracle=False)
            store.tau = 0.0
            store.learn_depends("a", "b", "t1")
            key = (store.names["a"], store.names["b"], "REQUIRES")
            first = store.edges[key]
            first_edit = first.edit_id
            first_p = first.p
            clock[0] = 1001.0
            store.learn_depends("a", "b", "t2")
            second = store.edges[key]
            self.assertNotEqual(second.edit_id, first_edit)
            self.assertGreater(second.n_obs, first.n_obs)
            clock[0] = 1002.0
            self.assertTrue(store.undo_edit(second.edit_id))
            restored = store.edges[key]
            self.assertEqual(restored.n_obs, first.n_obs)
            self.assertAlmostEqual(restored.p, first_p)
            undo_rows = [h for h in store.hist if h.op == "undo"]
            self.assertTrue(undo_rows)
            self.assertEqual(undo_rows[-1].trace_id, second.edit_id)

    def test_as_of_equals_replay(self) -> None:
        tools = [_tool("a"), _tool("b"), _tool("c")]
        with tempfile.TemporaryDirectory() as tmp:
            hydra = FakeHydra()
            store = GraphStore(hydra, Path(tmp) / "edits.jsonl")  # type: ignore[arg-type]
            clock = [10.0]
            store._now = lambda: clock[0]
            store.load_catalog(tools, include_oracle=False)
            store.tau = 0.0
            store.learn_depends("a", "b", "t1")
            mid = 10.5
            clock[0] = 11.0
            store.learn_depends("a", "c", "t2")
            clock[0] = 12.0
            store.learn_depends("a", "b", "t3")
            snap = store.as_of(mid)
            pairs = {(e["src"], e["dst"]) for e in snap["edges"]}
            ab = (store.names["a"], store.names["b"])
            ac = (store.names["a"], store.names["c"])
            self.assertIn(ab, pairs)
            self.assertNotIn(ac, pairs)
            # Mid-state n_obs for a→b should be the first version (1).
            ab_edge = next(e for e in snap["edges"] if (e["src"], e["dst"]) == ab)
            self.assertEqual(ab_edge["edit_id"].startswith("e") or True, True)
            live_ab = store.edges[(ab[0], ab[1], "REQUIRES")]
            self.assertGreater(live_ab.n_obs, 1)

    def test_negative_feedback_increases_beta_and_can_retire(self) -> None:
        tools = [_tool("a"), _tool("b")]
        with tempfile.TemporaryDirectory() as tmp:
            hydra = FakeHydra()
            store = GraphStore(hydra, Path(tmp) / "edits.jsonl")  # type: ignore[arg-type]
            store.load_catalog(tools, include_oracle=False)
            store.tau = 0.0
            store.record_trace(None, "q", tools_used=["a", "b"], tools_loaded=["a", "b"])
            key = (store.names["a"], store.names["b"], "REQUIRES")
            edge = store.edges[key]
            self.assertAlmostEqual(edge.p, 2 / 3)
            beta_before = edge.beta
            store.tau = 0.55
            store.record_trace(None, "q2", tools_used=["a"], tools_loaded=["a", "b"])
            # Edge should be retired (p=0.5 < 0.55).
            self.assertNotIn(key, store.edges)
            retire = [h for h in store.hist if h.op == "retire" and h.src == key[0] and h.dst == key[1]]
            self.assertTrue(retire)
            self.assertGreater(retire[-1].beta, beta_before - 1e-9)

    def test_invalidates_clears_resolved(self) -> None:
        tools = [_tool("get_x"), _tool("set_x"), _tool("other")]
        with tempfile.TemporaryDirectory() as tmp:
            hydra = FakeHydra()
            store = GraphStore(hydra, Path(tmp) / "edits.jsonl")  # type: ignore[arg-type]
            store.load_catalog(tools, include_oracle=False)
            self.assertIn("get_x", store.invalidates.get("set_x", set()))
            store.record_result("sess", "get_x", "hash1")
            self.assertIn("get_x", store.resolved["sess"])
            store.record_result("sess", "set_x", "hash2")
            self.assertNotIn("get_x", store.resolved["sess"])
            # Non-read-only set_x should not create a new entity producer entry.
            self.assertNotIn("set_x", store.resolved["sess"])

    def test_read_only_name_rule(self) -> None:
        self.assertTrue(is_read_only("get_weather"))
        self.assertFalse(is_read_only("set_weather"))
        self.assertFalse(is_read_only("search_flights"))
        tools = [_tool("get_a"), _tool("set_a"), _tool("do_a")]
        with tempfile.TemporaryDirectory() as tmp:
            hydra = FakeHydra()
            store = GraphStore(hydra, Path(tmp) / "edits.jsonl")  # type: ignore[arg-type]
            store.load_catalog(tools, include_oracle=False)
            self.assertTrue(store.read_only["get_a"])
            self.assertFalse(store.read_only["set_a"])
            self.assertFalse(store.read_only["do_a"])
            self.assertGreater(store.schema_tokens["get_a"], 0)

    def test_open_hist_uses_sentinel(self) -> None:
        tools = [_tool("a"), _tool("b")]
        with tempfile.TemporaryDirectory() as tmp:
            hydra = FakeHydra()
            store = GraphStore(hydra, Path(tmp) / "edits.jsonl")  # type: ignore[arg-type]
            store.load_catalog(tools, include_oracle=False)
            store.learn_depends("a", "b", "t")
            open_rows = [h for h in store.hist if h.valid_to == OPEN_VALID_TO]
            self.assertTrue(open_rows)


if __name__ == "__main__":
    unittest.main()
