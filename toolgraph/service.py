"""Shared service layer for MCP server and web demo."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from toolgraph.data import Tool, load_dataset, tokenize
from toolgraph.graph import Edge, GraphStore, rank_costs
from toolgraph.hydra import HydraClient, HydraError
from toolgraph.learn import apply_io_prior, propose_schema_candidates
from toolgraph.protocol import FUSION_POOL, MAX_LEN
from toolgraph.retrieve import Bm25Index, dense_top, embed_texts, rrf

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DEFAULT_EDIT = ROOT / "results" / "service_edits.jsonl"
HYDRA_URL = os.environ.get("HYDRA_URL", "http://127.0.0.1:8443")
HYDRA_TOKEN = os.environ.get("HYDRA_TOKEN", "local-development-token-32-bytes")
REL_TYPES = ["DEPENDS_ON", "SCHEMA_DEP"]


class ServiceGraphStore(GraphStore):
    """GraphStore that timestamps every edit for as-of reconstruction."""

    def _write_record(self, rec: dict) -> None:
        stamped = dict(rec)
        stamped.setdefault("ts", datetime.now(timezone.utc).isoformat())
        with self.edit_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(stamped) + "\n")
        self.applied += 1


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


class ToolgraphService:
    def __init__(
        self,
        hydra: HydraClient,
        *,
        data_dir: Path = DATA,
        edit_log: Path | None = None,
        apply_prior: bool = True,
    ) -> None:
        self.data_dir = data_dir
        self.edit_log = edit_log or DEFAULT_EDIT
        self.edit_log.parent.mkdir(parents=True, exist_ok=True)
        tools, _instances = load_dataset(data_dir)
        self.tools_list = tools
        self.tools = {tool.name: tool for tool in tools}
        self.store = ServiceGraphStore(hydra, self.edit_log)
        self._as_of_snapshot: dict | None = None
        if self.edit_log.exists() and self.edit_log.read_text().strip():
            self._replay_edits(
                [line for line in self.edit_log.read_text().splitlines() if line.strip()],
                sync_hydra=True,
            )
            self._prior_stats = {"replayed_from_log": True}
        else:
            self.store.load_catalog(tools, include_oracle=False)
            if apply_prior:
                self._prior_stats = apply_io_prior(self.store, propose_schema_candidates(tools))
                try:
                    self.store.sync_learned_to_hydra()
                except HydraError:
                    pass
            else:
                self._prior_stats = {}
        self.fusion = FusionIndex(tools)

    def _rebuild_from_disk(self) -> None:
        if not self.edit_log.exists():
            return
        lines = [line for line in self.edit_log.read_text().splitlines() if line.strip()]
        if not lines:
            return
        self._replay_edits(lines, sync_hydra=True)

    def _replay_edits(self, lines: list[str], *, sync_hydra: bool) -> None:
        self.store.edges.clear()
        self.store.lessons.clear()
        self.store.n_seen_src.clear()
        self.store.applied = 0
        self.store.load_catalog(self.tools_list, include_oracle=False)
        for line in lines:
            rec = json.loads(line)
            self._apply_forward(rec)
        if sync_hydra:
            try:
                self.store.sync_learned_to_hydra()
            except HydraError:
                pass

    def _apply_forward_to(self, rec: dict, store: GraphStore) -> None:
        kind = rec.get("kind")
        if kind == "lesson":
            tool = rec["tool"]
            params = rec.get("do_parameters") or {}
            lesson = str(params.get("lesson") or "")
            store.lessons[tool] = lesson[:180]
            return
        if kind != "edge":
            return
        src = int(rec["src"])
        dst = int(rec["dst"])
        rel = str(rec["rel"])
        prov = str(rec.get("provenance") or "")
        source = str(rec.get("source") or "trace")
        params = rec.get("do_parameters") or {}
        if isinstance(params, dict) and "rows" in params:
            n_obs = 1 if source == "trace" else 0
            support = 1
            src_name = store.by_id[src]
            seen = store.n_seen_src.get(src_name, 0)
            cost = float(params.get("cost") or (1.0 / (1.0 + n_obs)))
            p = float(params.get("p") or ((n_obs + 1) / (seen + 2)))
        else:
            n_obs = int(params.get("n_obs") or 0)
            support = int(params.get("support") or 1)
            seen = int(params.get("n_seen_src") or 0)
            cost = float(params.get("cost") or 1.0)
            p = float(params.get("p") or 0.5)
        edge = Edge(
            src=src,
            dst=dst,
            rel=rel,
            cost=cost,
            support=support,
            edit_id=str(rec.get("edit_id") or ""),
            prov=prov,
            n_obs=n_obs,
            n_seen_src=seen,
            p=p,
            source=source,
        )
        store.edges[(src, dst, rel)] = edge
        store.sqlite.execute(
            "INSERT OR REPLACE INTO edges (src, dst, rel, cost, support, n_obs, n_seen_src, p) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (src, dst, rel, cost, support, n_obs, seen, p),
        )
        store.sqlite.commit()

    def _apply_forward(self, rec: dict) -> None:
        self._apply_forward_to(rec, self.store)
        self.store.applied += 1

    def _active_store(self) -> GraphStore:
        if self._as_of_snapshot:
            return self._as_of_snapshot["store"]
        return self.store

    def _rank_tools(self, query: str, k: int) -> tuple[list[str], dict[str, list[str]]]:
        store = self._active_store()
        fused = self.fusion.fuse(query)
        seeds = fused[:FUSION_POOL]
        costs: dict[str, float]
        if store is self.store:
            try:
                self.store.ensure_hydra_synced()
                costs = self.store.closure(seeds, REL_TYPES)
            except HydraError:
                costs = self.store.recursive_cte(seeds, REL_TYPES, MAX_LEN)
        else:
            costs = store.recursive_cte(seeds, REL_TYPES, MAX_LEN)
        ranked = rank_costs(costs)
        for name in fused:
            if name not in costs:
                ranked.append(name)
        paths = store.reason_paths(seeds, ranked[:k], REL_TYPES)
        return ranked[:k], paths

    def _provenance_for(self, tool: str, paths: dict[str, list[str]]) -> list[dict]:
        chain = paths.get(tool) or [tool]
        out: list[dict] = []
        for index in range(len(chain) - 1):
            src_name = chain[index]
            dst_name = chain[index + 1]
            store = self._active_store()
            src_id = store.names.get(src_name)
            dst_id = store.names.get(dst_name)
            if src_id is None or dst_id is None:
                continue
            edge = None
            for rel in REL_TYPES:
                edge = store.edges.get((src_id, dst_id, rel))
                if edge is not None:
                    break
            if edge is None:
                continue
            out.append(
                {
                    "src": src_name,
                    "dst": dst_name,
                    "rel": edge.rel,
                    "prov": edge.prov,
                    "n_obs": edge.n_obs,
                    "n_seen_src": edge.n_seen_src,
                    "p": edge.p,
                }
            )
        return out

    def load_tools(self, query: str, k: int = 16) -> dict:
        ranked, paths = self._rank_tools(query, k)
        tools_out = []
        for name in ranked:
            tool = self.tools.get(name)
            if tool is None:
                continue
            tools_out.append(
                {
                    "name": name,
                    "description": tool.description,
                    "provenance": self._provenance_for(name, paths),
                }
            )
        return {"query": query, "tools": tools_out, "ranked": ranked}

    def record_trace(self, query: str, tools_used: list[str]) -> dict:
        if not tools_used:
            return {"query": query, "edit_ids": [], "edges_added": 0}
        main = tools_used[0]
        if main not in self.tools:
            return {"query": query, "edit_ids": [], "edges_added": 0, "error": f"unknown main tool {main!r}"}
        prov = f"trace_query={query[:120]};main={main}"
        self.store.note_src_seen(main)
        edges_added = 0
        edit_ids: list[str] = []
        for helper in tools_used[1:]:
            if helper == main or helper not in self.tools:
                continue
            if self.store.learn_depends(main, helper, prov):
                edges_added += 1
                key = (self.store.names[main], self.store.names[helper], "DEPENDS_ON")
                edit_ids.append(self.store.edges[key].edit_id)
        try:
            self.store.sync_learned_to_hydra()
        except HydraError:
            pass
        self.clear_as_of()
        return {"query": query, "edit_ids": edit_ids, "edges_added": edges_added}

    def undo_edit(self, edit_id: str) -> dict:
        if not self.edit_log.exists():
            return {"ok": False, "undone": edit_id, "reason": "no edit log"}
        lines = [line for line in self.edit_log.read_text().splitlines() if line.strip()]
        index = None
        for idx, line in enumerate(lines):
            rec = json.loads(line)
            if rec.get("edit_id") == edit_id:
                index = idx
        if index is None:
            return {"ok": False, "undone": edit_id, "reason": "edit_id not found"}
        kept = lines[:index]
        self.edit_log.write_text("".join(line + "\n" for line in kept))
        self._replay_edits(kept, sync_hydra=True)
        return {"ok": True, "undone": edit_id}

    def as_of(self, date_iso: str) -> dict:
        cutoff = datetime.fromisoformat(date_iso.replace("Z", "+00:00"))
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
        lines: list[str] = []
        if self.edit_log.exists():
            for line in self.edit_log.read_text().splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                ts_raw = rec.get("ts")
                if not ts_raw:
                    continue
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts <= cutoff:
                    lines.append(line)
        snapshot = ServiceGraphStore(self.store.hydra, self.edit_log)
        snapshot.load_catalog(self.tools_list, include_oracle=False)
        for line in lines:
            self._apply_forward_to(json.loads(line), snapshot)
        self._as_of_snapshot = {
            "date_iso": date_iso,
            "edges": len(snapshot.edges),
            "edits_replayed": len(lines),
            "store": snapshot,
        }
        return {
            "date_iso": date_iso,
            "edges_active": len(snapshot.edges),
            "edits_replayed": len(lines),
            "note": "subsequent load_tools uses this snapshot until cleared",
        }

    def clear_as_of(self) -> None:
        self._as_of_snapshot = None

    def explain(self, tool: str) -> dict:
        if tool not in self.tools:
            return {"tool": tool, "error": "unknown tool", "out_edges": [], "in_edges": [], "lesson": ""}
        store = self._as_of_snapshot["store"] if self._as_of_snapshot else self.store
        tool_id = store.names[tool]
        out_edges = []
        in_edges = []
        for (src, dst, rel), edge in store.edges.items():
            if rel not in REL_TYPES:
                continue
            row = {
                "src": store.by_id[src],
                "dst": store.by_id[dst],
                "rel": rel,
                "prov": edge.prov,
                "n_obs": edge.n_obs,
                "n_seen_src": edge.n_seen_src,
                "p": edge.p,
                "edit_id": edge.edit_id,
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
            "lesson": store.lessons.get(tool, ""),
        }


def default_service(*, data_dir: Path = DATA, edit_log: Path | None = None) -> ToolgraphService:
    hydra = HydraClient(HYDRA_URL, HYDRA_TOKEN)
    return ToolgraphService(hydra, data_dir=data_dir, edit_log=edit_log)