"""Checkpoint guards: hidden catalog edges, equal-depth control, MSpaths prefix costs."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from toolgraph.data import Instance, Tool, load_dataset, split_by_main
from toolgraph.eval_curve import ARM_SPEC, average_precision, metadata_vector_rank
from toolgraph.graph import Edge, GraphStore, apply_prefix_costs, edge_confidence
from toolgraph.learn import (
    apply_io_prior,
    composition_queries,
    ingest_trace_edges,
    io_overlap,
    propose_schema_candidates,
)
from toolgraph.protocol import IO_OVERLAP_KEEP, MAX_LEN, SEEDS


class FakeHydra:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.rows: list = []

    def query(self, statement: str, parameters: dict | None = None) -> dict:
        self.statements.append(statement)
        if "algo.MSpaths" in statement:
            return {"columns": ["path", "pathCost"], "rows": self.rows, "bookmark": None}
        return {"columns": [], "rows": [], "bookmark": None}


def _tool(name: str, deps: tuple[dict, ...] = (), params: tuple[dict, ...] = (), desc: str | None = None) -> Tool:
    return Tool(
        name=name,
        description=desc or f"tool {name}",
        kind="core",
        params=params,
        depends_on=deps,
    )


class HiddenEdgeGuardTest(unittest.TestCase):
    def test_learned_arm_rel_types_never_include_oracle(self) -> None:
        for arm, (_backend, rels) in ARM_SPEC.items():
            if arm == "oracle":
                self.assertEqual(rels, ["ORACLE_DEP"])
                continue
            if rels is None:
                continue
            self.assertNotIn("ORACLE_DEP", rels)

    def test_no_jev_arm_or_module(self) -> None:
        self.assertTrue(all("jev" not in arm for arm in ARM_SPEC))
        jev_path = Path(__file__).resolve().parents[1] / "toolgraph" / "jev.py"
        self.assertFalse(jev_path.exists())

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
            learned = [edge for key, edge in store.edges.items() if key[2] == "REQUIRES"]
            oracle = [edge for key, edge in store.edges.items() if key[2] == "ORACLE_DEP"]
            self.assertEqual(learned, [])
            self.assertEqual(len(oracle), 2)
            creates = [s for s in hydra.statements if "CREATE" in s and "]->(" in s]
            req_creates = [s for s in creates if "[:REQUIRES]" in s]
            self.assertEqual(req_creates, [])

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
            learned = {(store.by_id[s], store.by_id[d]) for (s, d, rel) in store.edges if rel == "REQUIRES"}
            # Co-occurrence: ordered pairs over gold ∪ {main}, not star main→helper.
            self.assertEqual(learned, {("alpha", "beta"), ("beta", "alpha")})
            self.assertNotIn(("alpha", "secret_helper"), learned)
            edge = next(e for e in store.edges.values() if e.rel == "REQUIRES" and e.source == "trace")
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

    def test_io_prior_is_deterministic_only(self) -> None:
        tools = [
            _tool("consumer", params=({"name": "timezone_id", "type": "string", "description": "tz"},)),
            _tool("get_system_timezone", desc="Returns the system timezone id string"),
            _tool("unrelated"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            hydra = FakeHydra()
            store = GraphStore(hydra, Path(tmp) / "edits.jsonl")  # type: ignore[arg-type]
            store.load_catalog(tools, include_oracle=False)
            candidates = propose_schema_candidates(tools)
            stats = apply_io_prior(store, candidates)
            self.assertEqual(stats["border_judged"], 0)
            self.assertEqual(stats["overlap_keep"], IO_OVERLAP_KEEP)
            schema = [e for e in store.edges.values() if e.rel == "REQUIRES" and e.source == "io_prior"]
            self.assertGreater(len(schema), 0)
            for edge in schema:
                self.assertGreaterEqual(edge.p, 1.0 - 1e-9)

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
    def test_hydra_graph_and_kv_hop_share_max_len(self) -> None:
        self.assertEqual(MAX_LEN, 4)
        graph = Path(__file__).resolve().parents[1].joinpath("toolgraph/graph.py").read_text()
        self.assertIn("maxLen: {MAX_LEN}", graph)
        eval_v3 = Path(__file__).resolve().parents[1].joinpath("toolgraph/eval_v3.py").read_text()
        self.assertIn("MAX_LEN", eval_v3)
        from toolgraph.arms import ARM_KNOBS

        self.assertEqual(ARM_KNOBS["hydra_graph"].depth, MAX_LEN)
        self.assertEqual(ARM_KNOBS["kv_hop"].depth, MAX_LEN)
        self.assertEqual(ARM_KNOBS["kv_closure"].depth, MAX_LEN)

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
                edge = Edge(src, dst, "REQUIRES", 1.0, 1, "test", "test", n_obs=1, n_seen_src=1, p=1.0)
                store.edges[(src, dst, "REQUIRES")] = edge
                store.sqlite.execute(
                    "INSERT INTO edges (src, dst, rel, cost, support, n_obs, n_seen_src, p) "
                    "VALUES (?, ?, 'REQUIRES', 1.0, 1, 1, 1, 1.0)",
                    (src, dst),
                )
            store.sqlite.commit()
            deep = store.recursive_cte(["t0"], ["REQUIRES"], max_len=MAX_LEN)
            shallow = store.recursive_cte(["t0"], ["REQUIRES"], max_len=1)
            self.assertIn("t4", deep)
            self.assertNotIn("t4", shallow)
            self.assertIn("t1", shallow)

    def test_map10_defined(self) -> None:
        score = average_precision(["a", "x", "b"], frozenset({"a", "b"}), 10)
        self.assertGreater(score, 0.0)


class MSpathsPrefixCostTest(unittest.TestCase):
    def test_prefix_costs_s_a_b_unit(self) -> None:
        costs: dict[str, float] = {"s": 0.0}
        apply_prefix_costs(costs, ["s", "a", "b"], [1.0, 1.0], penalty=0.0)
        self.assertEqual(costs, {"s": 0.0, "a": 1.0, "b": 2.0})

    def test_closure_uses_prefix_not_path_total(self) -> None:
        """Regression: pathCost total must not be stamped onto every node."""
        with tempfile.TemporaryDirectory() as tmp:
            hydra = FakeHydra()
            # Simulate Hydra returning s->a->b with total pathCost=2 and unit hop costs.
            hydra.rows = [
                [
                    {
                        "value": {
                            "nodes": [
                                {"properties": {"tool_id": "s"}},
                                {"properties": {"tool_id": "a"}},
                                {"properties": {"tool_id": "b"}},
                            ],
                            "relationships": [
                                {"properties": {"cost": 1.0}},
                                {"properties": {"cost": 1.0}},
                            ],
                        },
                        "type": "Path",
                    },
                    {"value": 2.0, "type": "Float"},
                ]
            ]
            store = GraphStore(hydra, Path(tmp) / "edits.jsonl")  # type: ignore[arg-type]
            tools = [_tool("s"), _tool("a"), _tool("b")]
            store.load_catalog(tools, include_oracle=False)
            store.tau = 0.0
            costs = store.closure(["s"], ["REQUIRES"])
            self.assertAlmostEqual(costs["s"], 0.0)
            self.assertAlmostEqual(costs["a"], 1.0)
            self.assertAlmostEqual(costs["b"], 2.0)
            # Bug would have set a=2 as well as b=2.
            self.assertNotAlmostEqual(costs["a"], 2.0)


class GroupedSplitAndCompositionTest(unittest.TestCase):
    def test_grouped_split_keeps_main_siblings_together(self) -> None:
        tools, instances = load_dataset(Path(__file__).resolve().parents[1] / "data")
        self.assertEqual(len(instances), 1569)
        for seed in SEEDS:
            learn, dev, sealed, assignment = split_by_main(instances, seed)
            for bucket in (learn, dev, sealed):
                mains = {query.main for query in bucket}
                for main in mains:
                    count = sum(1 for query in instances if query.main == main)
                    in_bucket = sum(1 for query in bucket if query.main == main)
                    self.assertEqual(in_bucket, count)
            learn_mains = {query.main for query in learn}
            sealed_mains = {query.main for query in sealed}
            self.assertFalse(learn_mains & sealed_mains)
            self.assertEqual(len(assignment), len({query.main for query in instances}))

    def test_toolinkos_every_main_has_three_identical_golds(self) -> None:
        _tools, instances = load_dataset(Path(__file__).resolve().parents[1] / "data")
        by_main: dict[str, list[Instance]] = {}
        for query in instances:
            by_main.setdefault(query.main, []).append(query)
        self.assertEqual(len(by_main), 523)
        for main, rows in by_main.items():
            self.assertEqual(len(rows), 3, main)
            golds = {row.gold for row in rows}
            self.assertEqual(len(golds), 1, main)

    def test_composition_prior_then_learned(self) -> None:
        """OpenTable-style: main has no learned out-edge; gold via prior→learned."""
        tools = [
            _tool("opentable_search"),
            _tool("resy_bridge"),
            _tool("get_current_weather"),
            _tool("get_us_national_holiday"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            hydra = FakeHydra()
            store = GraphStore(hydra, Path(tmp) / "edits.jsonl")  # type: ignore[arg-type]
            store.load_catalog(tools, include_oracle=False)
            store.add_schema_edge("opentable_search", "resy_bridge", "io_prior;overlap=0.7")
            # Learned edges hang off the bridge, not the sealed main.
            store.note_src_seen("resy_bridge")
            store.learn_depends("resy_bridge", "get_current_weather", "trace")
            store.learn_depends("resy_bridge", "get_us_national_holiday", "trace")
            for edge in store.edges.values():
                if edge.rel == "REQUIRES" and edge.source == "trace":
                    edge.p = 1.0
            store.tau = 0.15
            sealed = [
                Instance(
                    idx=0,
                    text="book opentable",
                    main="opentable_search",
                    gold=frozenset(
                        {
                            "opentable_search",
                            "resy_bridge",
                            "get_current_weather",
                            "get_us_national_holiday",
                        }
                    ),
                )
            ]
            found = composition_queries(sealed, store)
            self.assertEqual(len(found), 1)
            # If the main itself learned an out-edge, it drops out.
            store.note_src_seen("opentable_search")
            store.learn_depends("opentable_search", "get_current_weather", "trace")
            for edge in store.edges.values():
                if edge.rel == "REQUIRES" and edge.source == "trace":
                    edge.p = 1.0
            self.assertEqual(composition_queries(sealed, store), [])

    def test_metadata_vector_appends_also_load(self) -> None:
        also = {"seed": ["helper_a", "helper_b"], "other": ["x"]}
        ranked = metadata_vector_rank(["seed", "noise"], also)
        self.assertEqual(ranked[:3], ["seed", "helper_a", "helper_b"])
        self.assertIn("noise", ranked)


class MatplotlibOptionalTest(unittest.TestCase):
    def test_eval_curve_imports_without_matplotlib(self) -> None:
        # Import path used by unittest must not require matplotlib at module load.
        import toolgraph.eval_curve as mod

        self.assertTrue(hasattr(mod, "plot"))
        source = Path(mod.__file__).read_text()
        self.assertNotIn("import matplotlib\n", source.split("def plot")[0])


if __name__ == "__main__":
    unittest.main()
