"""v3 arm backends: iterative_search, kv_closure rebuild, B2 gold pruning."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from toolgraph.data import Instance, Tool
from toolgraph.eval_v3 import (
    KvClosureIndex,
    expand_kv_hop,
    iterative_search_load,
    load_arm,
)
from toolgraph.graph import GraphStore
from toolgraph.protocol import CLOSURE_REBUILD_INTERVAL, FUSION_POOL
from toolgraph.retrieve import Bm25Index, tokenize
from toolgraph.sessions_b2 import (
    apply_turn_resolution,
    build_b2_sessions,
    jaccard,
    plan_session_golds,
    prune_session_gold,
)


class FakeHydra:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.rows: list = []
        self.resolved_producers: list[str] = []

    def query(self, statement: str, parameters: dict | None = None) -> dict:
        del parameters
        self.statements.append(statement)
        if "algo.MSpaths" in statement:
            return {"columns": ["path", "pathCost"], "rows": self.rows, "bookmark": None}
        if "RESOLVED" in statement:
            return {
                "columns": ["e.producer"],
                "rows": [[name] for name in self.resolved_producers],
                "bookmark": None,
            }
        return {"columns": [], "rows": [], "bookmark": None}


def _tool(
    name: str,
    deps: tuple[dict, ...] = (),
    params: tuple[dict, ...] = (),
    desc: str | None = None,
) -> Tool:
    return Tool(
        name=name,
        description=desc or f"tool {name}",
        kind="core",
        params=params,
        depends_on=deps,
    )


class IterativeSearchTest(unittest.TestCase):
    def test_documents_depends_on_use_and_counts_turns(self) -> None:
        """iterative_search may read catalog depends_on (simulator only); each search = 1 turn."""
        producer = _tool(
            "get_system_timezone",
            desc="Returns the system timezone identifier UTC EST PST",
        )
        consumer = _tool(
            "get_current_time",
            params=({"name": "timezone", "type": "string", "description": "timezone for clock"},),
            deps=(
                {
                    "name": "get_system_timezone",
                    "dependence_type": "PARAMETER_DIRECTLY_DEPENDS_ON",
                    "parameter_name": "timezone",
                },
            ),
            desc="current time clock",
        )
        noise = _tool("unrelated_weather", desc="weather forecast rain")
        tools = {t.name: t for t in (consumer, producer, noise)}
        names = sorted(tools)
        bm25 = Bm25Index(names, [tokenize(tools[n].base_text()) for n in names])
        # Seed pool puts consumer first; producer not in initial top-8 slice of fused.
        fused = ["get_current_time", "unrelated_weather"] + ["get_system_timezone"]
        result = iterative_search_load(fused, tools, bm25, initial=2, search_top=3, max_rounds=6)
        self.assertTrue(result.extra.get("uses_catalog_depends_on"))
        self.assertGreaterEqual(result.llm_turns, 1)
        self.assertIn("get_system_timezone", result.ranked)

        # Source documents the exception.
        source = Path(__file__).resolve().parents[1] / "toolgraph" / "eval_v3.py"
        text = source.read_text()
        self.assertIn("catalog depends_on", text.lower())
        self.assertIn("simulator", text.lower())


class KvClosureRebuildTest(unittest.TestCase):
    def test_rebuild_interval_constant_is_50(self) -> None:
        self.assertEqual(CLOSURE_REBUILD_INTERVAL, 50)
        self.assertEqual(KvClosureIndex.rebuild_interval, 50)

    def test_rebuilds_every_fifty_edits(self) -> None:
        tools = [_tool("a"), _tool("b"), _tool("c")]
        with tempfile.TemporaryDirectory() as tmp:
            hydra = FakeHydra()
            store = GraphStore(hydra, Path(tmp) / "edits.jsonl")  # type: ignore[arg-type]
            store.load_catalog(tools, include_oracle=False)
            store.add_schema_edge("a", "b", "io_prior")
            index = KvClosureIndex(store)
            self.assertEqual(index.rebuild_count, 1)
            self.assertIn("b", index.closure["a"])
            for _ in range(CLOSURE_REBUILD_INTERVAL - 1):
                index.note_edit()
            self.assertEqual(index.edits_since_rebuild, CLOSURE_REBUILD_INTERVAL - 1)
            self.assertEqual(index.rebuild_count, 1)
            index.note_edit()
            self.assertEqual(index.edits_since_rebuild, 0)
            self.assertEqual(index.rebuild_count, 2)


class KvHopExpandTest(unittest.TestCase):
    def test_store_requests_are_hops_plus_one(self) -> None:
        adj = {"s": ["a"], "a": ["b"], "b": []}
        loaded, requests = expand_kv_hop(["s"], adj)
        self.assertEqual(loaded, ["s", "a", "b"])
        # depth-2 chain: 3 frontier fetches (s, a, b-empty) = hops + 1
        self.assertEqual(requests, 3)


class B2SessionGoldTest(unittest.TestCase):
    def test_jaccard_chaining_picks_high_overlap(self) -> None:
        sealed = [
            Instance(0, "q0", "m0", frozenset({"a", "b", "c"})),
            Instance(1, "q1", "m1", frozenset({"a", "b", "x"})),
            Instance(2, "q2", "m2", frozenset({"z", "y", "w"})),
            Instance(3, "q3", "m3", frozenset({"a", "b", "c", "d"})),
            Instance(4, "q4", "m4", frozenset({"q", "r"})),
            Instance(5, "q5", "m5", frozenset({"a", "c"})),
            Instance(6, "q6", "m6", frozenset({"m", "n"})),
            Instance(7, "q7", "m7", frozenset({"a", "b"})),
        ]
        sessions = build_b2_sessions(sealed, seed=0, n_sessions=5, turns=3, jaccard_pool=3)
        self.assertEqual(len(sessions), 5)
        for session in sessions:
            self.assertEqual(len(session), 3)
            # Second turn should be among the top-3 Jaccard neighbors of turn 1.
            prev = session[0]
            unused = [q for q in sealed if q.idx != prev.idx]
            scored = sorted(unused, key=lambda q: (-jaccard(q.gold, prev.gold), q.idx))
            pool = {q.idx for q in scored[:3]}
            self.assertIn(session[1].idx, pool)

    def test_gold_pruning_removes_resolved_readonly_and_restores_on_invalidates(self) -> None:
        tools = [
            _tool("get_alpha", deps=({"name": "get_helper", "dependence_type": "DIRECT"},)),
            _tool("get_helper"),
            _tool("set_alpha"),
            _tool("other_main", deps=({"name": "get_helper", "dependence_type": "DIRECT"},)),
        ]
        adj = {
            "get_alpha": ["get_helper"],
            "get_helper": [],
            "set_alpha": [],
            "other_main": ["get_helper"],
        }
        gold = frozenset({"get_alpha", "get_helper", "other_main"})
        resolved = {"get_alpha"}
        pruned = prune_session_gold(gold, resolved, adj)
        # get_alpha removed; get_helper still reachable via other_main → kept.
        self.assertNotIn("get_alpha", pruned)
        self.assertIn("get_helper", pruned)
        self.assertIn("other_main", pruned)

        # Only via removed: helper not reachable from remaining.
        gold2 = frozenset({"get_alpha", "get_helper"})
        pruned2 = prune_session_gold(gold2, {"get_alpha"}, adj)
        self.assertNotIn("get_alpha", pruned2)
        self.assertNotIn("get_helper", pruned2)

        # INVALIDATES restores: set_alpha clears get_alpha from resolved.
        resolved2 = {"get_alpha"}
        inv = {"set_alpha": {"get_alpha"}}
        apply_turn_resolution(resolved2, "set_alpha", inv)
        self.assertNotIn("get_alpha", resolved2)
        restored = prune_session_gold(gold2, resolved2, adj)
        self.assertEqual(restored, gold2)

    def test_plan_session_golds_applies_readonly_resolution(self) -> None:
        tools = [
            _tool("get_a", deps=({"name": "get_h", "dependence_type": "DIRECT"},)),
            _tool("get_h"),
            _tool("set_a"),
        ]
        session = [
            Instance(0, "t0", "get_a", frozenset({"get_a", "get_h"})),
            Instance(1, "t1", "get_a", frozenset({"get_a", "get_h"})),
            Instance(2, "t2", "set_a", frozenset({"set_a", "get_a", "get_h"})),
        ]
        plan = plan_session_golds(session, tools)
        self.assertEqual(plan.turns[0].gold, frozenset({"get_a", "get_h"}))
        # After turn 0, get_a and get_h are resolved read-only → turn 1 gold empty.
        self.assertEqual(plan.turns[1].gold, frozenset())
        # Turn 2 includes set_a which invalidates get_a; constructor resolves set_a
        # and clears get_a before... actually pruning uses state *before* the turn.
        # Before turn 2, get_a/get_h still resolved from turn 0 → pruned away; set_a remains.
        self.assertIn("set_a", plan.turns[2].gold)
        self.assertNotIn("get_a", plan.turns[2].gold)


class LoadArmSmokeTest(unittest.TestCase):
    def test_all_arms_load_on_fake_hydra(self) -> None:
        tools = [
            _tool("get_a", deps=({"name": "get_b", "parameter_name": "x", "dependence_type": "PARAMETER_DIRECTLY_DEPENDS_ON"},),
                  params=({"name": "x", "description": "needs b"},)),
            _tool("get_b", desc="producer b value"),
            _tool("c"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            hydra = FakeHydra()
            store = GraphStore(hydra, Path(tmp) / "edits.jsonl")  # type: ignore[arg-type]
            store.load_catalog(tools, include_oracle=True)
            store.add_schema_edge("get_a", "get_b", "io_prior")
            store.ensure_hydra_synced()
            fused = ["get_a", "c", "get_b"] + ["get_a"] * (FUSION_POOL - 3)
            fused = fused[:FUSION_POOL]
            kv = KvClosureIndex(store)
            names = sorted(store.names)
            bm25 = Bm25Index(names, [tokenize(store.tools[n].base_text()) for n in names])
            for arm in (
                "preload_all",
                "flat_topk",
                "iterative_search",
                "kv_hop",
                "kv_hop_session",
                "kv_closure",
                "hydra_graph",
                "oracle",
            ):
                result = load_arm(
                    arm,
                    fused,
                    store,
                    session_id="s" if arm in ("hydra_graph", "kv_hop_session") else None,
                    session_resolved=set(),
                    kv_closure=kv,
                    bm25=bm25,
                    all_tool_names=names,
                )
                self.assertTrue(result.ranked, arm)
                self.assertGreaterEqual(result.tokens, 0, arm)
                if arm == "preload_all":
                    self.assertEqual(len(result.ranked), 3)
                if arm == "kv_closure":
                    self.assertEqual(result.store_requests, 1)
                if arm == "hydra_graph":
                    self.assertEqual(result.store_requests, 2)
                if arm == "flat_topk":
                    self.assertEqual(result.ranked[:3], ["get_a", "c", "get_b"])


if __name__ == "__main__":
    unittest.main()
