"""Checkpoint-1 guards: hidden catalog edges, equal-depth plain-store control."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from toolgraph.data import Instance, Tool, load_dataset
from toolgraph.eval_curve import ARM_SPEC, average_precision
from toolgraph.graph import Edge, GraphStore, edge_confidence
from toolgraph.learn import ingest_trace_edges, io_overlap, propose_schema_candidates
from toolgraph.protocol import IO_OVERLAP_KEEP, MAX_LEN


class FakeHydra:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def query(self, statement: str, parameters: dict | None = None) -> dict:
        self.statements.append(statement)
        return {"columns": [], "rows": [], "bookmark": None}


def _tool(name: str, deps: tuple[dict, ...] = (), params: tuple[dict, ...] = ()) -> Tool:
    return Tool(name=name, description=f"tool {name}", kind="core", params=params, depends_on=deps)


class HiddenEdgeGuardTest(unittest.TestCase):
    def test_learned_arm_rel_types_never_include_oracle(self) -> None:
        for arm, (_backend, rels) in ARM_SPEC.items():
            if arm == "oracle":
                self.assertEqual(rels, ["ORACLE_DEP"])
                continue
            if rels is None:
                continue
            self.assertNotIn("ORACLE_DEP", rels)

    def test_load_catalog_hides_depends_on_from_learned_rels(self) -> None:
        tools = [
            _tool("main_tool", deps=({"name": "helper_tool", "dependence_type": "DIRECT"},)),
            _tool("helper_tool"),
            _tool(
                "needs_tz",
                params=({"name": "timezone", "description": "tz"},),
                deps=(
                    {
                        "name": "helper_tool",
                        "dependence_type": "PARAMETER_DIRECTLY_DEPENDS_ON",
                        "parameter_name": "timezone",
                    },
                ),
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            hydra = FakeHydra()
            store = GraphStore(hydra, Path(tmp) / "edits.jsonl")  # type: ignore[arg-type]
            store.load_catalog(tools, include_oracle=True)
            learned = [edge for key, edge in store.edges.items() if key[2] == "DEPENDS_ON"]
            oracle = [edge for key, edge in store.edges.items() if key[2] == "ORACLE_DEP"]
            self.assertEqual(learned, [])
            self.assertEqual(len(oracle), 2)
            creates = [s for s in hydra.statements if "CREATE" in s and "]->(" in s]
            dep_creates = [s for s in creates if "DEPENDS_ON" in s and "ORACLE" not in s]
            self.assertEqual(dep_creates, [])

    def test_trace_learning_does_not_read_catalog_depends_on(self) -> None:
        tools = [
            _tool("alpha", deps=({"name": "secret_helper", "dependence_type": "DIRECT"},)),
            _tool("beta"),
            _tool("secret_helper"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            hydra = FakeHydra()
            store = GraphStore(hydra, Path(tmp) / "edits.jsonl")  # type: ignore[arg-type]
            store.load_catalog(tools, include_oracle=False)
            query = Instance(idx=0, text="do alpha", main="alpha", gold=frozenset({"alpha", "beta"}))
            ingest_trace_edges(store, query)
            learned = {(store.by_id[s], store.by_id[d]) for (s, d, rel) in store.edges if rel == "DEPENDS_ON"}
            self.assertEqual(learned, {("alpha", "beta")})
            self.assertNotIn(("alpha", "secret_helper"), learned)
            edge = next(e for e in store.edges.values() if e.rel == "DEPENDS_ON")
            self.assertGreater(edge.p, 0.0)
            self.assertEqual(edge.n_obs, 1)

    def test_schema_proposals_ignore_depends_on_field(self) -> None:
        tools, _ = load_dataset(Path(__file__).resolve().parents[1] / "data")
        full = {(a, b, c) for a, b, c, _s in propose_schema_candidates(tools)}
        stripped = [
            Tool(name=t.name, description=t.description, kind=t.kind, params=t.params, depends_on=())
            for t in tools
        ]
        without = {(a, b, c) for a, b, c, _s in propose_schema_candidates(stripped)}
        self.assertEqual(full, without)

    def test_edge_confidence_formula(self) -> None:
        self.assertAlmostEqual(edge_confidence(0, 0), 0.5)
        self.assertAlmostEqual(edge_confidence(1, 1), 2 / 3)

    def test_io_overlap_threshold_constant(self) -> None:
        self.assertEqual(IO_OVERLAP_KEEP, 0.6)
        producer = _tool("get_system_timezone", params=())
        consumer = _tool("get_current_time", params=({"name": "timezone", "type": "string", "description": "tz"},))
        score = io_overlap(consumer, producer, consumer.params[0])
        self.assertGreaterEqual(score, 0.0)


class EqualDepthGuardTest(unittest.TestCase):
    def test_sqlite_cte_and_hydra_arms_share_max_len(self) -> None:
        self.assertEqual(MAX_LEN, 4)
        source = Path(__file__).resolve().parents[1] / "toolgraph" / "eval_curve.py"
        text = source.read_text()
        self.assertIn("store.recursive_cte(seeds, rels, MAX_LEN)", text)
        graph = Path(__file__).resolve().parents[1].joinpath("toolgraph/graph.py").read_text()
        self.assertIn("maxLen: {MAX_LEN}", graph)

    def test_recursive_cte_reaches_same_depth_as_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hydra = FakeHydra()
            store = GraphStore(hydra, Path(tmp) / "edits.jsonl")  # type: ignore[arg-type]
            names = [f"t{i}" for i in range(5)]
            store.names = {name: i + 1 for i, name in enumerate(names)}
            store.by_id = {i + 1: name for i, name in enumerate(names)}
            store.tools = {name: _tool(name) for name in names}
            for i in range(4):
                src, dst = store.names[names[i]], store.names[names[i + 1]]
                edge = Edge(src, dst, "DEPENDS_ON", 1.0, 1, "test", "test", n_obs=1, n_seen_src=1, p=1.0)
                store.edges[(src, dst, "DEPENDS_ON")] = edge
                store.sqlite.execute(
                    "INSERT INTO edges (src, dst, rel, cost, support, n_obs, n_seen_src, p) "
                    "VALUES (?, ?, 'DEPENDS_ON', 1.0, 1, 1, 1, 1.0)",
                    (src, dst),
                )
            store.sqlite.commit()
            deep = store.recursive_cte(["t0"], ["DEPENDS_ON"], max_len=MAX_LEN)
            shallow = store.recursive_cte(["t0"], ["DEPENDS_ON"], max_len=1)
            self.assertIn("t4", deep)
            self.assertNotIn("t4", shallow)
            self.assertIn("t1", shallow)

    def test_map10_defined(self) -> None:
        score = average_precision(["a", "x", "b"], frozenset({"a", "b"}), 10)
        self.assertGreater(score, 0.0)


if __name__ == "__main__":
    unittest.main()
