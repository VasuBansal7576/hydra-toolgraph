"""Regression checks for truncation recovery without corrupting session state."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from toolgraph.graph import GraphStore, LoadToolsResult, ToolWhy
from toolgraph.hydra import HydraError
from toolgraph.protocol import SEED_PENALTY
from toolgraph.service import ToolgraphService
from test_v3_store import FakeHydra, _tool


class ServiceConsistencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = ToolgraphService.__new__(ToolgraphService)
        self.service.store = GraphStore(FakeHydra(), Path(self.tmp.name) / "edits.jsonl")
        self.service.store.load_catalog(
            [_tool(name) for name in ("root", "other", "get_id", "helper", "leaf")],
            include_oracle=False,
        )
        self.service.store.add_schema_edge("root", "get_id", "io_prior", cost=0.4)
        self.service.store.add_schema_edge("get_id", "helper", "io_prior", cost=0.4)
        self.service.store.add_schema_edge("helper", "leaf", "io_prior", cost=0.4)

    def merge(self, seeds, expanded, skipped=None):
        result = LoadToolsResult(tools=[], costs={}, why={}, skipped=skipped or {})
        return self.service._merge_memory_expansion(result, seeds, expanded, 16)

    def test_resolved_branch_is_not_resurrected(self):
        result = self.merge(["root", "get_id"], ["root"], {"get_id": "get_id"})
        self.assertEqual(result.tools, ["root"])
        self.assertNotIn("get_id", result.costs)

    def test_shared_helper_remains_reachable_through_unresolved_branch(self):
        self.service.store.add_schema_edge("other", "helper", "io_prior", cost=0.6)
        result = self.merge(["root", "other"], ["root", "other"], {"get_id": "get_id"})
        self.assertIn("helper", result.tools)
        self.assertEqual(result.why["helper"].path, ["other", "helper"])
        self.assertNotIn("get_id", result.tools)

    def test_expansion_preserves_original_seed_cost(self):
        result = self.merge(["other", "root"], ["root"])
        self.assertAlmostEqual(result.costs["get_id"], SEED_PENALTY + 0.4)

    def test_multihop_recovery_carries_full_evidence(self):
        result = self.merge(["root"], ["root"])
        self.assertEqual(result.why["leaf"].path, ["root", "get_id", "helper", "leaf"])
        self.assertEqual(len(result.why["leaf"].edge_ids), 3)
        self.assertEqual(result.why["leaf"].sources, ["io_prior"] * 3)

    def test_no_expansion_means_no_expansion(self):
        self.assertEqual(self.merge(["root"], []).tools, ["root"])

    def test_truncated_hydra_response_still_exposes_resolved_boundary(self):
        self.service.store.resolved["session"] = {"get_id": 1.0}
        result = self.service.store.load_tools(["root"], "session", 16)
        recovered = self.service._merge_memory_expansion(result, ["root"], ["root"], 16)
        self.assertEqual(recovered.tools, ["root"])
        self.assertEqual(recovered.skipped, {"get_id": "get_id"})

    def test_resolved_seed_without_expansion_is_removed(self):
        self.service.store.resolved["session"] = {"get_id": 1.0}
        result = self.service.store.load_tools(["get_id"], "session", 16, expand_seeds=[])
        self.assertEqual(result.tools, [])

    def test_cycles_are_bounded_and_below_threshold_edges_are_ignored(self):
        self.service.store.add_schema_edge("leaf", "root", "io_prior", cost=0.4)
        self.service.store.add_schema_edge("root", "other", "io_prior", cost=0.1)
        edge = self.service.store.edges[(self.service.store.names["root"], self.service.store.names["other"], "REQUIRES")]
        edge.p = 0.0
        self.service.store.tau = 0.15
        result = self.merge(["root"], ["root"])
        self.assertNotIn("other", result.tools)
        self.assertEqual(len(result.tools), 4)
        self.assertEqual(len(result.why["leaf"].path), 4)

    def test_live_service_does_not_reuse_unbound_session_results(self):
        svc = self.service
        svc.read_only_mode = False
        svc._last_loaded = {}
        svc.tools = svc.store.tools
        svc.fusion = Mock()
        svc.fusion.fuse_demo.return_value = (["root"], ["root"], {})
        svc._context_meter = Mock(return_value={})
        original = svc.store.load_tools
        svc.store.load_tools = Mock(wraps=original)
        svc.store.resolved["customer-session"] = {"get_id": 1.0}
        for edge in svc.store.edges.values():
            edge.source = "trace"
            edge.prov = "observed-episode"
        payload = svc.load_tools("lookup a different customer", "customer-session")
        self.assertIsNone(svc.store.load_tools.call_args.args[1])
        self.assertIn("get_id", payload["ranked"])
        self.assertFalse(payload["session_reuse"])
        self.assertEqual(payload["skipped"], {})

    def test_cached_bundle_restores_feedback_context(self):
        svc = self.service
        svc.read_only_mode = False
        svc._last_loaded = {"session": ["other"]}
        svc._load_cache = {("query", "session", 16, False): {"ranked": ["root"]}}
        payload = svc.load_tools("query", "session")
        self.assertTrue(payload["cached"])
        self.assertEqual(svc._last_loaded["session"], ["root"])

    def test_outage_respects_expansion_and_retries_backend(self):
        svc = self.service
        svc.read_only_mode = False
        svc._last_loaded = {}
        svc.tools = svc.store.tools
        svc.fusion = Mock()
        svc.fusion.fuse_demo.return_value = (["root"], [], {})
        svc._context_meter = Mock(return_value={})
        svc.store.ensure_hydra_synced = Mock(side_effect=HydraError("offline"))
        first = svc.load_tools("query", "session")
        second = svc.load_tools("query", "session")
        self.assertEqual(first["ranked"], ["root"])
        self.assertFalse(second["cached"])
        self.assertEqual(svc.store.ensure_hydra_synced.call_count, 2)
        self.assertIsNone(first["store_requests"])

    def test_noop_trace_never_returns_an_unrelated_undo_target(self):
        svc = self.service
        svc.read_only_mode = False
        svc.allow_writes = True
        svc._edit_timeline = []
        svc._last_loaded = {}
        svc.store.learn_from_tools(["root", "helper"])
        original_ids = {edge.edit_id for edge in svc.store.edges.values()}
        self.assertTrue(original_ids)
        payload = svc.record_trace("nothing happened", [], [])
        self.assertEqual(payload["edges_touched"], 0)
        self.assertEqual(payload["edit_ids"], [])

    def test_evidence_scope_blocks_unrelated_episode_and_unverified_prior(self):
        store = self.service.store
        for edge in store.edges.values():
            edge.source = "trace"
            edge.prov = "episode-a"
        store.edges[(store.names["helper"], store.names["leaf"], "REQUIRES")].prov = "episode-b"
        store.add_schema_edge("root", "other", "similar words", cost=0.1)
        empty = LoadToolsResult(tools=[], costs={}, why={}, skipped={})
        result = self.service._merge_memory_expansion(empty, ["root"], ["root"], 16, evidence_scoped=True)
        self.assertEqual(set(result.tools), {"root", "get_id", "helper"})
        self.assertEqual(result.why["helper"].path, ["root", "get_id", "helper"])

    def test_provider_and_generic_task_both_remain_retrievable(self):
        from toolgraph.retrieve import Bm25Index
        from toolgraph.seed_policy import select_demo_seeds
        index = Bm25Index(
            ["alpha_book", "alpha_search", "get_weather"],
            [["alpha", "book", "table"], ["alpha", "search", "table"], ["weather", "forecast"]],
        )
        seeds, _, _ = select_demo_seeds(index, "alpha book table and weather")
        self.assertIn("alpha_book", seeds)
        self.assertIn("get_weather", seeds)

    def test_raw_hydra_paths_require_active_complete_anchored_evidence(self):
        store = self.service.store
        for edge in store.edges.values():
            edge.source = "trace"
            edge.prov = "episode-a"
        store.tau = 0.15
        edge = store.edges[(store.names["root"], store.names["get_id"], "REQUIRES")]
        edge.p = 0.0
        for path in (["root", "get_id"], [], ["other", "get_id"], ["root"]):
            with self.subTest(path=path):
                raw = LoadToolsResult(
                    tools=["get_id"], costs={"get_id": 0.1}, skipped={},
                    why={"get_id": ToolWhy(path=path, edge_ids=[], sources=[], cost=0.1)},
                )
                result = self.service._merge_memory_expansion(raw, ["root"], ["root"], 16, evidence_scoped=True)
                self.assertEqual(result.tools, ["root"])
        edge.p = 1.0
        raw = LoadToolsResult(
            tools=["get_id"], costs={"get_id": 0.4}, skipped={},
            why={"get_id": ToolWhy(path=["root", "get_id"], edge_ids=[edge.edge_id], sources=["trace"], cost=0.4)},
        )
        result = self.service._merge_memory_expansion(raw, ["root"], ["root"], 16, evidence_scoped=True)
        self.assertIn("get_id", result.tools)

    def test_negative_feedback_is_not_reported_as_positive_support(self):
        from toolgraph.graph import positive_trace_provenance
        svc = self.service
        store = svc.store
        for _ in range(3):
            store.record_trace(None, "observed", ["root", "get_id"], ["root", "get_id"])
        key = (store.names["root"], store.names["get_id"], "REQUIRES")
        positive = positive_trace_provenance(store.edges[key])
        store.record_trace(None, "helper unused", ["root"], ["root", "get_id"])
        edge = store.edges[key]
        negative_edit = edge.edit_id
        self.assertAlmostEqual(edge.p, 2 / 3)
        self.assertTrue(edge.prov.startswith("neg:"))
        self.assertEqual(positive_trace_provenance(edge), positive)
        svc.tools = store.tools
        row = next(row for row in svc.explain("root")["out_edges"] if row["dst"] == "get_id")
        self.assertEqual(row["latest_update"], edge.prov)
        self.assertEqual(row["latest_positive_observation"], positive)
        self.assertIn("not a verified dependency", row["evidence_note"])
        self.assertTrue(store.undo_edit(negative_edit))
        restored = store.edges[key]
        self.assertTrue(restored.prov.startswith("undo:"))
        self.assertEqual(positive_trace_provenance(restored), positive)
        snapshot = store._edge_snapshot(restored)
        self.assertEqual(positive_trace_provenance(store._edge_from_snapshot(snapshot)), positive)

    def test_legacy_negative_only_provenance_does_not_invent_support(self):
        from toolgraph.graph import positive_trace_provenance
        store = self.service.store
        edge = next(iter(store.edges.values()))
        edge.source = "trace"
        edge.prov = "neg:legacy"
        edge.positive_prov = ""
        self.assertEqual(positive_trace_provenance(edge), "")

    def test_mutations_invalidate_cached_bundles(self):
        for operation in ("record_trace", "record_result", "undo_edit"):
            with self.subTest(operation=operation):
                svc = ToolgraphService.__new__(ToolgraphService)
                svc.allow_writes = True
                svc.read_only_mode = False
                svc._load_cache = {"stale": {"ranked": ["root"]}}
                svc._edit_timeline = []
                svc._last_loaded = {}
                svc.store = Mock()
                svc.store.edges = {}
                svc.store.resolved = {}
                svc.store.record_trace.return_value = 0
                if operation == "record_trace":
                    svc.record_trace("q", ["root"])
                elif operation == "record_result":
                    svc.record_result("session", "get_id")
                else:
                    svc.undo_edit("edit")
                self.assertEqual(svc._load_cache, {})
