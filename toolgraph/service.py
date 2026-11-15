"""Shared service layer for MCP server and web demo (v3 REQUIRES / sessions)."""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from toolgraph.data import Tool, load_dataset, split_by_main, tokenize
from toolgraph.graph import GraphStore
from toolgraph.hydra import HydraClient, HydraError
from toolgraph.learn import apply_io_prior, ingest_trace_edges, propose_schema_candidates
from toolgraph.protocol import FUSION_POOL, SEEDS
from toolgraph.retrieve import Bm25Index, dense_top, embed_texts, rrf

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
# Rough preload-all token estimate when tiktoken is unavailable (chars/4).
_CHARS_PER_TOKEN = 4


class FusionIndex:
    def __init__(self, tools: list[Tool]) -> None:
        self.tools = tools
        self.names = [tool.name for tool in tools]
        self.by_name = {tool.name: tool for tool in tools}
        docs = [tokenize(tool.base_text()) for tool in tools]
        self.bm25 = Bm25Index(self.names, docs)
        texts = [tool.base_text() for tool in tools]
        self.tool_vecs = embed_texts(texts)

    def fuse(self, query: str) -> list[str]:
        lexical = self.bm25.top(query, 64)
        query_vec = embed_texts([query])
        dense = dense_top(query_vec, self.tool_vecs, self.names, 64)[0]
        return rrf([lexical, dense], FUSION_POOL)


def _schema_tokens(tools: list[Tool], names: list[str]) -> int:
    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        total = 0
        by_name = {tool.name: tool for tool in tools}
        for name in names:
            tool = by_name.get(name)
            if tool is None:
                continue
            total += len(enc.encode(tool.base_text()))
        return total
    except Exception:
        by_name = {tool.name: tool for tool in tools}
        chars = sum(len(by_name[n].base_text()) for n in names if n in by_name)
        return max(1, chars // _CHARS_PER_TOKEN)


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
        tools, instances = load_dataset(data_dir)
        self.tools_list = tools
        self.tools = {tool.name: tool for tool in tools}
        self.instances = instances
        self.store = GraphStore(hydra, self.edit_log)
        self._seed_stats: dict = {}
        self._committed_results: dict | None = None
        if self.read_only_mode:
            self._load_committed_results()
            self.store.load_catalog(tools, include_oracle=False)
            self._prior_stats = {"read_only": True}
        else:
            self.store.load_catalog(tools, include_oracle=False)
            if apply_prior:
                self._prior_stats = apply_io_prior(self.store, propose_schema_candidates(tools))
            else:
                self._prior_stats = {}
            if _env_truthy("DEMO_SEED_LEARN", default=True):
                self._seed_stats = self._seed_learn_split()
            try:
                self.store.ensure_hydra_synced()
            except HydraError:
                pass
        self.fusion = FusionIndex(tools)
        self._last_loaded: dict[str, list[str]] = {}
        self._edit_timeline: list[dict] = []

    def _load_committed_results(self) -> None:
        path = ROOT / "results" / "v3_report.json"
        if path.exists():
            self._committed_results = json.loads(path.read_text())
        else:
            self._committed_results = {"label": "missing", "note": "no committed results"}

    def _seed_learn_split(self) -> dict:
        """Ingest grouped-main learn-split traces so cold demo shows learned edges."""
        seed = int(os.environ.get("DEMO_SEED", str(SEEDS[0])))
        learn, _dev, _sealed, _assignment = split_by_main(self.instances, seed)
        touched = 0
        for query in learn:
            touched += ingest_trace_edges(self.store, query)
        self.store.tau = float(os.environ.get("DEMO_TAU", "0.15"))
        return {
            "seed": seed,
            "learn_queries": len(learn),
            "edges_touched": touched,
            "tau": self.store.tau,
            "total_edges": len(self.store.edges),
        }

    def load_tools(self, query: str, session_id: str | None = None, k: int = 16) -> dict:
        fused = self.fusion.fuse(query)
        seeds = fused[:FUSION_POOL]
        try:
            if not self.read_only_mode:
                self.store.ensure_hydra_synced()
            result = self.store.load_tools(seeds, session_id, k)
        except HydraError as exc:
            # Offline / FakeHydra fallback: memory CTE over REQUIRES.
            costs = self.store.recursive_cte(seeds, ["REQUIRES"], 4)
            ranked = sorted(costs, key=costs.get)[:k]
            return {
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
                "error": f"hydra unavailable: {exc.message}",
            }

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
                            "prov": edge.prov if edge else (why.sources[index] if index < len(why.sources) else ""),
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
        return {
            "query": query,
            "session_id": session_id,
            "tools": tools_out,
            "ranked": ranked,
            "skipped": skipped_out,
            "context_meter": self._context_meter(ranked),
            "store_requests": 2 if session_id else 1,
            "read_only": self.read_only_mode,
        }

    def _context_meter(self, ranked: list[str]) -> dict:
        preload = _schema_tokens(self.tools_list, list(self.tools))
        bundle = _schema_tokens(self.tools_list, ranked)
        return {
            "preload_all_tokens": preload,
            "bundle_tokens": bundle,
            "tokens_saved": max(0, preload - bundle),
            "bundle_size": len(ranked),
            "catalog_size": len(self.tools),
        }

    def record_trace(
        self,
        query: str,
        tools_used: list[str],
        tools_loaded: list[str] | None = None,
        session_id: str | None = None,
    ) -> dict:
        if self.read_only_mode:
            return {
                "query": query,
                "session_id": session_id,
                "edges_touched": 0,
                "error": "read-only demo; learning disabled",
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
        if self.read_only_mode:
            return {"session_id": session_id, "tool": tool, "error": "read-only demo"}
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
        if self.read_only_mode:
            return {"ok": False, "undone": edit_id, "reason": "read-only demo"}
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
            "tau": self.store.tau,
            "edges": len(self.store.edges),
            "seed": self._seed_stats,
            "prior": self._prior_stats,
            "committed_results": self._committed_results is not None,
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
