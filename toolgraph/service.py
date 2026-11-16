"""Shared service layer for MCP server and web demo (v3 REQUIRES / sessions)."""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from toolgraph.data import Tool, load_dataset, split_by_main, tokenize
from toolgraph.graph import GraphStore, LoadToolsResult, ToolWhy, rank_costs, schema_token_count
from toolgraph.hydra import HydraClient, HydraError
from toolgraph.learn import apply_io_prior, ingest_trace_edges, propose_schema_candidates
from toolgraph.protocol import FUSION_POOL, MAX_LEN, SEED_PENALTY, SEEDS
from toolgraph.retrieve import Bm25Index, dense_top, embed_texts, rrf
from toolgraph.seed_policy import select_demo_seeds

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DEFAULT_EDIT = ROOT / "results" / "service_edits.jsonl"
HYDRA_URL = os.environ.get("HYDRA_URL", "http://127.0.0.1:8443")
HYDRA_TOKEN = os.environ.get("HYDRA_TOKEN", "local-development-token-32-bytes")
# Demo default query: sealed OpenTable reserve with multi-hop helpers.
DEFAULT_DEMO_QUERY = (
    "I'd like to book a table for dinner at The Italian Bistro at 7 PM. "
    "Can you handle an OpenTable reservation?"
)


class FusionIndex:
    """Retrieval fusion. ``mode='bm25'`` skips MiniLM (fits 512 MB free tiers)."""

    def __init__(self, tools: list[Tool], *, mode: str | None = None) -> None:
        self.tools = tools
        self.names = [tool.name for tool in tools]
        self.by_name = {tool.name: tool for tool in tools}
        docs = [tokenize(tool.base_text()) for tool in tools]
        self.bm25 = Bm25Index(self.names, docs)
        raw_mode = (mode or os.environ.get("DEMO_FUSION") or "dense").strip().lower()
        self.mode = "bm25" if raw_mode in {"bm25", "lexical", "light"} else "dense"
        self.tool_vecs = None
        if self.mode == "dense":
            print(f"[service] FusionIndex dense embed of {len(tools)} tools…", flush=True)
            texts = [tool.base_text() for tool in tools]
            self.tool_vecs = embed_texts(texts)
            print("[service] FusionIndex dense ready", flush=True)
        else:
            print("[service] FusionIndex BM25-only (no MiniLM load)", flush=True)

    def fuse(self, query: str) -> list[str]:
        lexical = self.bm25.top(query, 64)
        if self.mode == "bm25" or self.tool_vecs is None:
            return lexical[:FUSION_POOL]
        query_vec = embed_texts([query])
        dense = dense_top(query_vec, self.tool_vecs, self.names, 64)[0]
        return rrf([lexical, dense], FUSION_POOL)

    def fuse_demo(self, query: str) -> tuple[list[str], list[str], dict]:
        """Demo seed selection: IDF-anchor filter + relative expand cutoff.

        Returns ``(seeds, expand_seeds, debug)``. Dense mode still uses RRF for
        the ranking seed list, then intersects expand seeds with the IDF policy.
        """
        if self.mode == "bm25" or self.tool_vecs is None:
            return select_demo_seeds(
                self.bm25,
                query,
                pool=FUSION_POOL,
                expand_alpha=float(os.environ.get("DEMO_EXPAND_ALPHA", "0.5")),
                apply_idf_anchor=_env_truthy("DEMO_IDF_ANCHOR", default=True),
                apply_rel_cutoff=_env_truthy("DEMO_REL_CUTOFF", default=True),
            )
        # Dense path: keep RRF ranking, but only expand IDF-anchored tools.
        fused = self.fuse(query)
        _seeds, expand_seeds, debug = select_demo_seeds(
            self.bm25,
            query,
            pool=FUSION_POOL,
            expand_alpha=float(os.environ.get("DEMO_EXPAND_ALPHA", "0.5")),
            apply_idf_anchor=_env_truthy("DEMO_IDF_ANCHOR", default=True),
            apply_rel_cutoff=_env_truthy("DEMO_REL_CUTOFF", default=True),
        )
        expand_set = set(expand_seeds)
        expand = [name for name in fused if name in expand_set] or list(expand_seeds)
        return fused, expand, debug


def _schema_tokens(tools: list[Tool], names: list[str]) -> int:
    """Match B1 eval: JSON schema payload via schema_token_count (cl100k)."""
    by_name = {tool.name: tool for tool in tools}
    total = 0
    for name in names:
        tool = by_name.get(name)
        if tool is None:
            continue
        total += schema_token_count(tool)
    return total


class ToolgraphService:
    def __init__(
        self,
        hydra: HydraClient,
        *,
        data_dir: Path = DATA,
        edit_log: Path | None = None,
        apply_prior: bool = True,
        read_only_mode: bool = False,
    ) -> None:
        self.data_dir = data_dir
        self.edit_log = edit_log or DEFAULT_EDIT
        self.edit_log.parent.mkdir(parents=True, exist_ok=True)
        self.read_only_mode = read_only_mode or _env_truthy("DEMO_READ_ONLY", default=False)
        # Public demos leave writes off unless DEMO_ALLOW_WRITES=1.
        self.allow_writes = (not self.read_only_mode) and _env_truthy(
            "DEMO_ALLOW_WRITES", default=False
        )
        print(
            f"[service] init read_only={self.read_only_mode} "
            f"allow_writes={self.allow_writes} fusion={os.environ.get('DEMO_FUSION', 'dense')}",
            flush=True,
        )
        tools, instances = load_dataset(data_dir)
        self.tools_list = tools
        self.tools = {tool.name: tool for tool in tools}
        self.instances = instances
        self.store = GraphStore(hydra, self.edit_log)
        self._seed_stats: dict = {}
        self._committed_results: dict | None = None
        if self.read_only_mode:
            self._load_committed_results()
            print("[service] load_catalog (readonly / null hydra)…", flush=True)
            self.store.load_catalog(tools, include_oracle=False)
            self._prior_stats = {"read_only": True}
        else:
            print("[service] load_catalog…", flush=True)
            self.store.load_catalog(tools, include_oracle=False)
            # Prefer cheap precomputed edges on free tiers; optional full learn.
            precomputed = self._load_precomputed_requires()
            if precomputed:
                self._seed_stats = precomputed
                # Skip io_prior when the dump already carries the demo graph —
                # prior scan is expensive on 0.1 CPU free hosts.
                self._prior_stats = {"skipped": "precomputed_seed"}
            else:
                if apply_prior:
                    print("[service] apply_io_prior…", flush=True)
                    self._prior_stats = apply_io_prior(
                        self.store, propose_schema_candidates(tools)
                    )
                else:
                    self._prior_stats = {}
                if _env_truthy("DEMO_SEED_LEARN", default=True):
                    print("[service] seed learn split…", flush=True)
                    self._seed_stats = self._seed_learn_split()
                    print(f"[service] seed done: {self._seed_stats}", flush=True)
            try:
                print("[service] ensure_hydra_synced…", flush=True)
                self.store.ensure_hydra_synced()
                print(
                    f"[service] hydra sync done edges={len(self.store.active_edges(['REQUIRES']))}",
                    flush=True,
                )
            except HydraError as exc:
                print(f"[service] hydra sync failed: {exc}", flush=True)
                raise
        self.fusion = FusionIndex(tools)
        self._last_loaded: dict[str, list[str]] = {}
        self._edit_timeline: list[dict] = []
        print("[service] ready", flush=True)

    def _load_committed_results(self) -> None:
        path = ROOT / "results" / "v3_report.json"
        if path.exists():
            self._committed_results = json.loads(path.read_text())
        else:
            self._committed_results = {"label": "missing", "note": "no committed results"}

    def _load_precomputed_requires(self) -> dict | None:
        """Load results/demo_requires_edges.json when present (cheap hosted-demo seed)."""
        if not _env_truthy("DEMO_LOAD_PRECOMPUTED_EDGES", default=True):
            return None
        path = ROOT / "results" / "demo_requires_edges.json"
        if not path.exists():
            return None
        print(f"[service] loading precomputed edges from {path}…", flush=True)
        payload = json.loads(path.read_text())
        tau = float(payload.get("tau") or os.environ.get("DEMO_TAU", "0.15"))
        rows = list(payload.get("edges") or [])
        loaded = self.store.import_requires_rows(rows, tau=tau)
        stats = {
            "mode": "precomputed",
            "path": str(path.name),
            "seed": payload.get("seed"),
            "tau": tau,
            "rows_in_file": len(rows),
            "edges_loaded": loaded,
            "total_edges": len(self.store.edges),
        }
        print(f"[service] precomputed edges loaded: {stats}", flush=True)
        return stats

    def _seed_learn_split(self) -> dict:
        """Ingest grouped-main learn-split traces so cold demo shows learned edges."""
        seed = int(os.environ.get("DEMO_SEED", str(SEEDS[0])))
        learn, _dev, _sealed, _assignment = split_by_main(self.instances, seed)
        touched = 0
        for query in learn:
            touched += ingest_trace_edges(self.store, query)
        self.store.tau = float(os.environ.get("DEMO_TAU", "0.15"))
        return {
            "mode": "learn_split",
            "seed": seed,
            "learn_queries": len(learn),
            "edges_touched": touched,
            "tau": self.store.tau,
            "total_edges": len(self.store.edges),
        }

    def load_tools(self, query: str, session_id: str | None = None, k: int = 16) -> dict:
        started = time.perf_counter()
        cache_key = (query, session_id or "", int(k), self.read_only_mode)
        cache = getattr(self, "_load_cache", None)
        if cache is None:
            self._load_cache = {}
            cache = self._load_cache
        cached = cache.get(cache_key)
        if cached is not None:
            payload = dict(cached)
            payload["cached"] = True
            payload["latency_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
            return payload

        seeds, expand_seeds, seed_debug = self.fusion.fuse_demo(query)
        if not seeds:
            seeds = self.fusion.fuse(query)[:FUSION_POOL]
            expand_seeds = list(seeds)
            seed_debug = {"fallback": "fuse_demo_empty"}

        # Read-only / null-Hydra: fusion ranking only — no live graph walk.
        if self.read_only_mode:
            ranked = seeds[:k]
            sid_key = session_id or "_anon"
            self._last_loaded[sid_key] = ranked
            tools_out = [
                {
                    "name": name,
                    "description": self.tools[name].description if name in self.tools else "",
                    "provenance": [],
                    "why_path": [name],
                }
                for name in ranked
                if name in self.tools
            ]
            payload = {
                "query": query,
                "session_id": session_id,
                "tools": tools_out,
                "ranked": ranked,
                "skipped": {},
                "context_meter": self._context_meter(ranked),
                "read_only": True,
                "bundle_mode": "no-graph retrieval fallback (fusion only; HydraDB not queried)",
                "store_requests": None,
                "store_requests_note": "not applicable in read-only / no-graph mode",
                "seed_policy": seed_debug,
                "latency_ms": round((time.perf_counter() - started) * 1000.0, 2),
                "cached": False,
            }
            cache[cache_key] = payload
            return payload

        fanout_raw = os.environ.get("DEMO_FANOUT_CAP", "").strip()
        fanout_cap = int(fanout_raw) if fanout_raw.isdigit() else None

        try:
            # Cheap when already synced; avoid repeating the 2.7k-edge bulk write.
            self.store.ensure_hydra_synced()
            result = self.store.load_tools(
                seeds,
                session_id,
                k,
                expand_seeds=expand_seeds,
                fanout_cap=fanout_cap,
            )
            # Fill gaps from local CTE so MSpaths truncation cannot drop helpers
            # of the (few) expand seeds we intentionally kept.
            result = self._merge_memory_expansion(result, seeds, expand_seeds, k)
            bundle_mode = "hydra_graph"
        except HydraError as exc:
            costs = self.store.recursive_cte(expand_seeds or seeds, ["REQUIRES"], MAX_LEN)
            for index, name in enumerate(seeds):
                costs.setdefault(name, index * SEED_PENALTY)
            ranked = rank_costs(costs)[:k]
            payload = {
                "query": query,
                "session_id": session_id,
                "tools": [
                    {
                        "name": name,
                        "description": self.tools[name].description if name in self.tools else "",
                        "provenance": [],
                        "why_path": [name],
                    }
                    for name in ranked
                    if name in self.tools
                ],
                "ranked": ranked,
                "skipped": {},
                "context_meter": self._context_meter(ranked),
                "bundle_mode": "memory-CTE fallback (Hydra unavailable)",
                "store_requests": None,
                "error": f"hydra unavailable: {exc.message}",
                "read_only": False,
                "seed_policy": seed_debug,
                "latency_ms": round((time.perf_counter() - started) * 1000.0, 2),
                "cached": False,
            }
            cache[cache_key] = payload
            return payload

        ranked = list(result.tools)
        sid_key = session_id or "_anon"
        self._last_loaded[sid_key] = ranked
        tools_out = []
        for name in ranked:
            tool = self.tools.get(name)
            if tool is None:
                continue
            why = result.why.get(name)
            path = list(why.path) if why else [name]
            provenance = []
            if why:
                for index in range(len(path) - 1):
                    src_name, dst_name = path[index], path[index + 1]
                    src_id = self.store.names.get(src_name)
                    dst_id = self.store.names.get(dst_name)
                    edge = None
                    if src_id is not None and dst_id is not None:
                        edge = self.store.edges.get((src_id, dst_id, "REQUIRES"))
                    provenance.append(
                        {
                            "src": src_name,
                            "dst": dst_name,
                            "rel": "REQUIRES",
                            "prov": edge.prov
                            if edge
                            else (why.sources[index] if index < len(why.sources) else ""),
                            "p": edge.p if edge else None,
                            "n_obs": edge.n_obs if edge else None,
                            "edge_id": why.edge_ids[index] if index < len(why.edge_ids) else None,
                        }
                    )
            tools_out.append(
                {
                    "name": name,
                    "description": tool.description,
                    "provenance": provenance,
                    "why_path": path,
                }
            )
        skipped_out = {
            name: f"skipped, already resolved by {producer}"
            for name, producer in (result.skipped or {}).items()
        }
        # Asserted by design (session → RESOLVED + MSpaths); unit-tested, not a
        # RecordingHydraClient count from this path.
        asserted_reqs = 2 if session_id else 1
        payload = {
            "query": query,
            "session_id": session_id,
            "tools": tools_out,
            "ranked": ranked,
            "skipped": skipped_out,
            "context_meter": self._context_meter(ranked),
            "store_requests": asserted_reqs,
            "store_requests_note": (
                "asserted by design (RESOLVED+MSpaths when session_id set); "
                "see tests/test_v3_store.py"
            ),
            "bundle_mode": bundle_mode,
            "seed_policy": seed_debug,
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 2),
            "cached": False,
            "read_only": False,
        }
        cache[cache_key] = payload
        return payload

    def _merge_memory_expansion(self, result, seeds: list[str], expand_seeds: list[str], k: int):
        """Merge local REQUIRES closure for expand_seeds into a Hydra load result.

        Keeps Hydra why_paths when present; fills helpers MSpaths truncated away.
        """
        cte = self.store.recursive_cte(expand_seeds or seeds, ["REQUIRES"], MAX_LEN)
        costs = dict(result.costs)
        why = dict(result.why)
        for index, name in enumerate(seeds):
            costs.setdefault(name, index * SEED_PENALTY)
            why.setdefault(
                name,
                ToolWhy(path=[name], edge_ids=[], sources=[], cost=costs[name]),
            )
        for name, cost in cte.items():
            if name not in costs or float(cost) < float(costs[name]):
                costs[name] = float(cost)
                if name not in why or len(why[name].path) <= 1:
                    path = [name]
                    for src in expand_seeds or seeds:
                        src_id = self.store.names.get(src)
                        dst_id = self.store.names.get(name)
                        if src_id is None or dst_id is None:
                            continue
                        if (src_id, dst_id, "REQUIRES") in self.store.edges:
                            path = [src, name]
                            break
                    why[name] = ToolWhy(
                        path=path,
                        edge_ids=[],
                        sources=[],
                        cost=float(cost),
                    )
        ranked = rank_costs(costs)[:k]
        return LoadToolsResult(
            tools=ranked,
            costs={name: costs[name] for name in ranked},
            why={name: why[name] for name in ranked if name in why},
            skipped=dict(result.skipped),
        )

    def _context_meter(self, ranked: list[str]) -> dict:
        # Same tokenizer/payload as B1 (JSON schema via schema_token_count).
        preload = _schema_tokens(self.tools_list, [t.name for t in self.tools_list])
        bundle = _schema_tokens(self.tools_list, ranked)
        return {
            "preload_all_tokens": preload,
            "bundle_tokens": bundle,
            "tokens_saved": max(0, preload - bundle),
            "bundle_size": len(ranked),
            "catalog_size": len(self.tools),
            "token_definition": "JSON schema (name/description/params) via cl100k — same as B1",
        }

    def _writes_blocked(self, op: str) -> dict | None:
        if self.read_only_mode:
            return {"ok": False, "error": f"read-only demo; {op} disabled"}
        if not self.allow_writes:
            return {
                "ok": False,
                "error": f"{op} disabled on public demo; set DEMO_ALLOW_WRITES=1 to enable",
            }
        return None

    def record_trace(
        self,
        query: str,
        tools_used: list[str],
        tools_loaded: list[str] | None = None,
        session_id: str | None = None,
    ) -> dict:
        blocked = self._writes_blocked("record_trace")
        if blocked:
            return {
                "query": query,
                "session_id": session_id,
                "edges_touched": 0,
                **blocked,
            }
        sid_key = session_id or "_anon"
        loaded = tools_loaded if tools_loaded is not None else list(self._last_loaded.get(sid_key) or [])
        before = len(self.store.edges)
        touched = self.store.record_trace(session_id, query, tools_used, loaded)
        try:
            self.store.ensure_hydra_synced()
        except HydraError:
            pass
        edit_ids = [
            edge.edit_id
            for edge in self.store.edges.values()
            if edge.rel == "REQUIRES" and edge.source == "trace" and edge.edit_id
        ][-max(1, touched) :]
        self._edit_timeline.append(
            {
                "op": "record_trace",
                "ts": time.time(),
                "session_id": session_id,
                "tools_used": tools_used,
                "tools_loaded": loaded,
                "edges_touched": touched,
            }
        )
        return {
            "query": query,
            "session_id": session_id,
            "edit_ids": edit_ids,
            "edges_touched": touched,
            "edges_now": len(self.store.edges),
            "edges_before": before,
        }

    def record_result(self, session_id: str, tool: str, args_hash: str | None = None) -> dict:
        blocked = self._writes_blocked("record_result")
        if blocked:
            return {"session_id": session_id, "tool": tool, **blocked}
        digest = args_hash or hashlib.sha1(f"{session_id}:{tool}".encode()).hexdigest()[:16]
        self.store.record_result(session_id, tool, digest)
        self._edit_timeline.append(
            {
                "op": "record_result",
                "ts": time.time(),
                "session_id": session_id,
                "tool": tool,
                "args_hash": digest,
            }
        )
        return {
            "session_id": session_id,
            "tool": tool,
            "args_hash": digest,
            "resolved": dict(self.store.resolved.get(session_id, {})),
        }

    def undo_edit(self, edit_id: str) -> dict:
        blocked = self._writes_blocked("undo_edit")
        if blocked:
            return {"undone": edit_id, **blocked}
        ok = self.store.undo_edit(edit_id)
        try:
            self.store.ensure_hydra_synced()
        except HydraError:
            pass
        self._edit_timeline.append({"op": "undo_edit", "ts": time.time(), "edit_id": edit_id, "ok": ok})
        return {"ok": ok, "undone": edit_id}

    def as_of(self, date_iso: str | None = None, ts: float | None = None) -> dict:
        if ts is None:
            if not date_iso:
                ts = time.time()
            else:
                cutoff = datetime.fromisoformat(date_iso.replace("Z", "+00:00"))
                if cutoff.tzinfo is None:
                    cutoff = cutoff.replace(tzinfo=timezone.utc)
                ts = cutoff.timestamp()
        try:
            payload = self.store.as_of(float(ts))
        except HydraError as exc:
            return {"ts": ts, "error": exc.message, "edges": [], "diff": {}}
        payload["date_iso"] = datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
        payload["note"] = "REQUIRES_HIST snapshot + diff vs live now"
        return payload

    def explain(self, tool: str) -> dict:
        if tool not in self.tools:
            return {"tool": tool, "error": "unknown tool", "out_edges": [], "in_edges": [], "lesson": ""}
        tool_id = self.store.names[tool]
        out_edges = []
        in_edges = []
        for (src, dst, rel), edge in self.store.edges.items():
            if rel != "REQUIRES":
                continue
            row = {
                "src": self.store.by_id[src],
                "dst": self.store.by_id[dst],
                "rel": rel,
                "prov": edge.prov,
                "n_obs": edge.n_obs,
                "n_seen_src": edge.n_seen_src,
                "p": edge.p,
                "cost": edge.cost,
                "edit_id": edge.edit_id,
                "source": edge.source,
            }
            if src == tool_id:
                out_edges.append(row)
            if dst == tool_id:
                in_edges.append(row)
        out_edges.sort(key=lambda row: (row["dst"], row["rel"]))
        in_edges.sort(key=lambda row: (row["src"], row["rel"]))
        return {
            "tool": tool,
            "description": self.tools[tool].description,
            "out_edges": out_edges,
            "in_edges": in_edges,
            "lesson": self.store.lessons.get(tool, ""),
        }

    def timeline(self) -> dict:
        return {"events": list(self._edit_timeline), "read_only": self.read_only_mode}

    def status(self) -> dict:
        return {
            "ok": True,
            "read_only": self.read_only_mode,
            "allow_writes": self.allow_writes,
            "tau": self.store.tau,
            "edges": len(self.store.edges),
            "seed": self._seed_stats,
            "prior": self._prior_stats,
            "committed_results": self._committed_results is not None,
            "fusion_mode": getattr(self.fusion, "mode", None),
        }


def _env_truthy(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


class _NullHydra:
    """Offline stand-in when DEMO_READ_ONLY (no live HydraDB process)."""

    def query(self, statement: str, parameters: dict | None = None) -> dict:
        return {"columns": [], "rows": [], "bookmark": None}


def default_service(*, data_dir: Path = DATA, edit_log: Path | None = None) -> ToolgraphService:
    if _env_truthy("DEMO_READ_ONLY", default=False):
        hydra: HydraClient | _NullHydra = _NullHydra()
    else:
        hydra = HydraClient(HYDRA_URL, HYDRA_TOKEN)
    return ToolgraphService(hydra, data_dir=data_dir, edit_log=edit_log)  # type: ignore[arg-type]
