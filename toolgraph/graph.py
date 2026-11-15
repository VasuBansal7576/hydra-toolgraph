"""v3 GraphStore: Tool/Session/Entity, live REQUIRES + REQUIRES_HIST, load_tools.

Catalog depends_on edges never enter a learned arm. They are loaded only as
ORACLE_DEP for the ceiling arm. Live REQUIRES come from traces (source=trace)
and the deterministic I/O prior (source=io_prior). INVALIDATES follows the
set_X → get_X name rule.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tiktoken

from toolgraph.data import Tool
from toolgraph.hydra import HydraClient, as_float, as_str, unwrap
from toolgraph.protocol import (
    LEARNED_BASE_COST,
    MAX_LEN,
    PATH_COUNT,
    RESULT_LIMIT,
    SEED_PENALTY,
)

# Open hist edges use a far-future sentinel (HydraDB has no native time travel;
# prefer this over IS NULL even though v0.4.0 appears to accept IS NULL).
OPEN_VALID_TO = 9e15
MIN_EDGE_COST = 0.05

_SCHEMA_ENCODER: Any = None


def _schema_encoder():
    global _SCHEMA_ENCODER
    if _SCHEMA_ENCODER is None:
        _SCHEMA_ENCODER = tiktoken.get_encoding("cl100k_base")
    return _SCHEMA_ENCODER


def schema_token_count(tool: Tool) -> int:
    """Tokenize tool JSON schema (name, description, params) with cl100k_base."""
    payload = {
        "name": tool.name,
        "description": tool.description,
        "params": list(tool.params),
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return len(_schema_encoder().encode(text))


def is_read_only(name: str) -> bool:
    """Name rule (labelled): read_only iff name.startswith('get_')."""
    return name.startswith("get_")


def edge_cost_from_dependence(dependence_type: str) -> float:
    if "INDIRECT" in dependence_type:
        return 1.6
    return 1.0


@dataclass
class Edge:
    src: int
    dst: int
    rel: str
    cost: float
    support: int
    edit_id: str
    prov: str
    n_obs: int = 0
    n_seen_src: int = 0
    p: float = 0.5
    source: str = "trace"  # trace | io_prior | oracle
    alpha: float = 1.0
    beta: float = 1.0
    beta_penalty: float = 0.0
    valid_from: float = 0.0
    edge_id: str = ""


@dataclass
class HistRow:
    edge_id: str
    src: int
    dst: int
    cost: float
    alpha: float
    beta: float
    beta_penalty: float
    source: str
    trace_id: str
    valid_from: float
    valid_to: float
    op: str  # add | reweight | retire | undo
    edit_id: str
    n_obs: int = 0
    n_seen_src: int = 0
    p: float = 0.5
    support: int = 1
    prov: str = ""


@dataclass
class ToolWhy:
    path: list[str]
    edge_ids: list[str]
    sources: list[str]
    cost: float


@dataclass
class LoadToolsResult:
    tools: list[str]
    costs: dict[str, float]
    why: dict[str, ToolWhy] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)


def edge_confidence(n_obs: int, n_seen_src: int) -> float:
    """Beta(1,1) posterior mean: p = (n_obs + 1) / (n_seen_src + 2)."""
    return (n_obs + 1) / (n_seen_src + 2)


def _derive_alpha_beta(n_ab: int, n_src: int, beta_penalty: float = 0.0) -> tuple[float, float, float]:
    alpha = float(n_ab + 1)
    beta = float(max(0, n_src - n_ab) + 1) + float(beta_penalty)
    p = alpha / (alpha + beta) if (alpha + beta) > 0 else 0.5
    return alpha, beta, p


def _cost_from_p(p: float) -> float:
    return max(MIN_EDGE_COST, 1.0 - float(p))


def _logical_edge_id(src: int, dst: int) -> str:
    return f"req:{src}:{dst}"


class GraphStore:
    def __init__(self, hydra: HydraClient, edit_log: Path) -> None:
        self.hydra = hydra
        self.edit_log = edit_log
        self.edit_log.parent.mkdir(parents=True, exist_ok=True)
        self.names: dict[str, int] = {}
        self.by_id: dict[int, str] = {}
        self.tools: dict[str, Tool] = {}
        self.read_only: dict[str, bool] = {}
        self.schema_tokens: dict[str, int] = {}
        self.lessons: dict[str, str] = {}
        self.edges: dict[tuple[int, int, str], Edge] = {}
        self.n_seen_src: dict[str, int] = {}
        self.n_ab: dict[tuple[str, str], int] = {}
        self.beta_penalty: dict[tuple[str, str], float] = {}
        self.tau: float = 0.0
        self.invalidates: dict[str, set[str]] = {}  # set_X -> {get_X, ...}
        self.hist: list[HistRow] = []
        self.sessions: dict[str, int] = {}  # sid -> numeric id
        self.entities: dict[str, int] = {}  # entity key -> numeric id
        self.resolved: dict[str, dict[str, float]] = {}  # sid -> {producer: resolved_at}
        # check_same_thread=False: demo/MCP serve requests on worker threads.
        self.sqlite = sqlite3.connect(":memory:", check_same_thread=False)
        self.sqlite.execute(
            "CREATE TABLE edges (src INTEGER, dst INTEGER, rel TEXT, cost REAL, support INTEGER, "
            "n_obs INTEGER, n_seen_src INTEGER, p REAL, "
            "PRIMARY KEY (src, dst, rel))"
        )
        self._edit_seq = 1_000_000
        self._session_seq = 30_000
        self._entity_seq = 40_000
        self.applied = 0
        self.undone = 0
        self.edge_version = 0
        self._hydra_synced_version = 0
        self._synced_requires: set[tuple[int, int]] = set()
        self._node_ids: list[int] = []
        self._now = time.time  # wall-clock float for valid_from/resolved_at

    def wipe(self, *, tool_count: int = 0) -> None:
        # Label-wide DETACH DELETE can stack-overflow the local graph-node.
        # Delete tracked ids plus the stable tool/param/meta ranges so a new
        # GraphStore on a reused HydraDB volume cannot inherit stale edges.
        ids = list(dict.fromkeys(self._node_ids))
        if tool_count > 0:
            ids.extend(range(1, tool_count + 1))
            ids.extend(range(10_001, 10_001 + max(2_000, tool_count * 3)))
            ids.extend(range(20_000, 20_010))
            ids.extend(range(30_000, 30_000 + 200))
            ids.extend(range(40_000, 40_000 + 2_000))
        ids = list(dict.fromkeys(ids))
        for start in range(0, len(ids), 50):
            chunk = ids[start : start + 50]
            self.hydra.query(
                "UNWIND $rows AS row MATCH (n {id: row.id}) DETACH DELETE n",
                {"rows": [{"id": node_id} for node_id in chunk]},
            )
        self._node_ids = []
        self.edges.clear()
        self.lessons.clear()
        self.n_seen_src.clear()
        self.n_ab.clear()
        self.beta_penalty.clear()
        self.tau = 0.0
        self.invalidates.clear()
        self.hist.clear()
        self.sessions.clear()
        self.entities.clear()
        self.resolved.clear()
        self.read_only.clear()
        self.schema_tokens.clear()
        self._synced_requires.clear()
        self.sqlite.execute("DELETE FROM edges")
        self.sqlite.commit()
        self.edge_version += 1
        self._hydra_synced_version = self.edge_version

    def load_catalog(self, tools: list[Tool], *, include_oracle: bool = True) -> None:
        """Load tool nodes and param structure. Never loads catalog depends_on into learned rels."""
        self.wipe(tool_count=len(tools))
        self.tools = {tool.name: tool for tool in tools}
        self.names = {tool.name: idx + 1 for idx, tool in enumerate(tools)}
        self.by_id = {idx: name for name, idx in self.names.items()}
        self.read_only = {tool.name: is_read_only(tool.name) for tool in tools}
        self.schema_tokens = {tool.name: schema_token_count(tool) for tool in tools}
        node_rows = [
            {
                "id": self.names[tool.name],
                "tool_id": tool.name,
                "read_only": self.read_only[tool.name],
                "schema_tokens": self.schema_tokens[tool.name],
                "description": tool.description[:500],
                "kind": tool.kind,
                "lesson": "",
            }
            for tool in tools
        ]
        self._unwind_tool_nodes(node_rows)
        self._node_ids.extend(row["id"] for row in node_rows)
        param_rows = []
        param_edges = []
        param_id = 10_000
        for tool in tools:
            for param in tool.params:
                param_id += 1
                pname = str(param.get("name") or "param")
                param_rows.append(
                    {
                        "id": param_id,
                        "tool_id": f"{tool.name}.{pname}",
                        "description": str(param.get("description") or "")[:240],
                        "kind": "param",
                        "lesson": "",
                    }
                )
                param_edges.append((self.names[tool.name], param_id))
        if param_rows:
            self._unwind_nodes("Param", param_rows)
            self._unwind_bare_edges("REQUIRES_PARAM", param_edges)
            self._node_ids.extend(row["id"] for row in param_rows)
        skill_rows = [
            {"id": 20_001, "tool_id": "core", "description": "Core helper", "kind": "skill", "lesson": ""},
            {"id": 20_002, "tool_id": "regular", "description": "Regular tool", "kind": "skill", "lesson": ""},
        ]
        server_rows = [
            {
                "id": 20_000,
                "tool_id": "toollinkos",
                "description": "ToolLinkOS catalog",
                "kind": "server",
                "lesson": "",
            }
        ]
        self._unwind_nodes("Skill", skill_rows)
        self._unwind_nodes("Server", server_rows)
        self._node_ids.extend(row["id"] for row in skill_rows)
        self._node_ids.extend(row["id"] for row in server_rows)
        skill_edges = []
        host_edges = []
        for tool in tools:
            skill = 20_001 if tool.kind == "core" else 20_002
            skill_edges.append((self.names[tool.name], skill))
            host_edges.append((self.names[tool.name], 20_000))
        self._unwind_bare_edges("OF_SKILL", skill_edges)
        self._unwind_bare_edges("HOSTED_BY", host_edges)
        self._load_invalidates(tools)
        if include_oracle:
            self.load_oracle_edges(tools)

    def _load_invalidates(self, tools: list[Tool]) -> None:
        """Name rule: set_X INVALIDATES get_X when both exist in the catalog."""
        pairs: list[tuple[int, int]] = []
        for tool in tools:
            if not tool.name.startswith("set_"):
                continue
            get_name = "get_" + tool.name[4:]
            if get_name not in self.names:
                continue
            src = self.names[tool.name]
            dst = self.names[get_name]
            pairs.append((src, dst))
            self.invalidates.setdefault(tool.name, set()).add(get_name)
        if pairs:
            self._unwind_bare_edges("INVALIDATES", pairs)

    def load_oracle_edges(self, tools: list[Tool]) -> None:
        """Ceiling arm only. Uses ORACLE_DEP so learned arms never see these edges."""
        oracle: list[Edge] = []
        seen: set[tuple[int, int]] = set()
        now = float(self._now())
        for tool in tools:
            for dep in tool.depends_on:
                if dep["name"] not in self.names:
                    continue
                pair = (self.names[tool.name], self.names[dep["name"]])
                if pair in seen or pair[0] == pair[1]:
                    continue
                seen.add(pair)
                oracle.append(
                    Edge(
                        src=pair[0],
                        dst=pair[1],
                        rel="ORACLE_DEP",
                        cost=edge_cost_from_dependence(str(dep.get("dependence_type") or "")),
                        support=0,
                        edit_id="oracle",
                        prov="catalog_oracle",
                        n_obs=0,
                        n_seen_src=0,
                        p=1.0,
                        source="oracle",
                        alpha=1.0,
                        beta=1.0,
                        beta_penalty=0.0,
                        valid_from=now,
                        edge_id=f"oracle:{pair[0]}:{pair[1]}",
                    )
                )
        self._insert_edges(oracle, record=False)

    def closure(self, seeds: list[str], rel_types: list[str]) -> dict[str, float]:
        """Min path cost via HydraDB algo.MSpaths over the given relationship types.

        HydraDB yields (path, pathCost) where pathCost is the *total* path cost.
        Assigning that total to every node on the path breaks cost parity with the
        SQLite CTE (s→a→b would become {s:0,a:2,b:2} instead of {s:0,a:1,b:2}).
        We therefore use the minimum *prefix* cost per node.
        """
        self.ensure_hydra_synced()
        costs = {name: index * SEED_PENALTY for index, name in enumerate(seeds)}
        if not seeds:
            return costs
        values = ", ".join(f"'{name}'" for name in seeds)
        rels = ", ".join(f"'{rel}'" for rel in rel_types)
        statement = (
            "CALL algo.MSpaths({"
            "sourceLabel: 'Tool', sourceProperty: 'tool_id', "
            f"sourceValues: [{values}], "
            f"relTypes: [{rels}], relDirection: 'outgoing', "
            f"maxLen: {MAX_LEN}, costProp: 'cost', pathCount: {PATH_COUNT}, "
            f"resultLimit: {RESULT_LIMIT}"
            "}) YIELD path, pathCost RETURN path, pathCost"
        )
        result = self.hydra.query(statement)
        seed_index = {name: index for index, name in enumerate(seeds)}
        for row in result["rows"]:
            path = unwrap(row[0])
            if not isinstance(path, dict):
                continue
            nodes = self._path_tool_ids(path)
            if not nodes:
                continue
            penalty = seed_index.get(nodes[0], 0) * SEED_PENALTY
            edge_costs = self._path_edge_costs(path, nodes, rel_types)
            apply_prefix_costs(costs, nodes, edge_costs, penalty)
        return costs

    def load_tools(
        self,
        seeds: list[str],
        session_id: str | None,
        k: int,
        *,
        fresh_after: float = 0.0,
    ) -> LoadToolsResult:
        """EXACTLY two store requests (one if session_id is None).

        Request 1 (skipped when session_id is None): resolved producers for the session.
        Request 2: algo.MSpaths over REQUIRES only. Caller must sync live REQUIRES first;
        this method does not call ensure_hydra_synced (would add extra requests).
        """
        resolved: set[str] = set()
        if session_id is not None:
            result = self.hydra.query(
                "MATCH (s:Session {sid: $sid})-[:RESOLVED]->(e:Entity) "
                "WHERE e.resolved_at >= $fresh_after "
                "RETURN e.producer",
                {"sid": session_id, "fresh_after": float(fresh_after)},
            )
            for row in result.get("rows") or []:
                producer = as_str(unwrap(row[0]) if row else None)
                if producer is None and row:
                    cell = unwrap(row[0])
                    producer = str(cell) if cell is not None else None
                if producer:
                    resolved.add(producer)
            # Merge local shadow so FakeHydra / offline tests still skip.
            for producer, ts in self.resolved.get(session_id, {}).items():
                if ts >= fresh_after:
                    resolved.add(producer)

        if not seeds:
            return LoadToolsResult(tools=[], costs={}, why={}, skipped={})

        values = ", ".join(f"'{name}'" for name in seeds)
        statement = (
            "CALL algo.MSpaths({"
            "sourceLabel: 'Tool', sourceProperty: 'tool_id', "
            f"sourceValues: [{values}], "
            "relTypes: ['REQUIRES'], relDirection: 'outgoing', "
            f"maxLen: {MAX_LEN}, costProp: 'cost', pathCount: {PATH_COUNT}, "
            f"resultLimit: {RESULT_LIMIT}"
            "}) YIELD path, pathCost RETURN path, pathCost"
        )
        result = self.hydra.query(statement)

        costs: dict[str, float] = {name: index * SEED_PENALTY for index, name in enumerate(seeds)}
        why: dict[str, ToolWhy] = {
            name: ToolWhy(path=[name], edge_ids=[], sources=[], cost=costs[name]) for name in seeds
        }
        skipped: dict[str, str] = {}
        seed_index = {name: index for index, name in enumerate(seeds)}
        best_path_meta: dict[str, tuple[float, list[str], list[str], list[str]]] = {
            name: (costs[name], [name], [], []) for name in seeds
        }

        for row in result.get("rows") or []:
            path = unwrap(row[0])
            if not isinstance(path, dict):
                continue
            nodes = self._path_tool_ids(path)
            if not nodes:
                continue
            # Walk from seed; stop at first resolved producer (that node + suffix skipped).
            keep: list[str] = []
            stop_at: str | None = None
            stop_index = len(nodes)
            for index, node in enumerate(nodes):
                if node in resolved:
                    stop_at = node
                    stop_index = index
                    break
                keep.append(node)
            if stop_at is not None:
                for node in nodes[stop_index:]:
                    skipped.setdefault(node, stop_at)
            if not keep:
                continue
            penalty = seed_index.get(keep[0], 0) * SEED_PENALTY
            edge_costs = self._path_edge_costs(path, keep, ["REQUIRES"])
            apply_prefix_costs(costs, keep, edge_costs, penalty)
            edge_ids, sources = self._path_edge_meta(keep)
            running = penalty
            for index, name in enumerate(keep):
                if index > 0:
                    step = edge_costs[index - 1] if index - 1 < len(edge_costs) else 1.0
                    running += float(step)
                meta = (running, keep[: index + 1], edge_ids[:index], sources[:index])
                prev = best_path_meta.get(name)
                if prev is None or meta[0] < prev[0]:
                    best_path_meta[name] = meta

        for name, (cost, path_nodes, edge_ids, sources) in best_path_meta.items():
            if name in skipped:
                continue
            why[name] = ToolWhy(path=path_nodes, edge_ids=edge_ids, sources=sources, cost=cost)

        ranked = rank_costs({name: cost for name, cost in costs.items() if name not in skipped})
        ranked = ranked[:k]
        return LoadToolsResult(
            tools=ranked,
            costs={name: costs[name] for name in ranked},
            why={name: why[name] for name in ranked if name in why},
            skipped=skipped,
        )

    def _path_tool_ids(self, path: dict) -> list[str]:
        nodes: list[str] = []
        for node in path.get("nodes") or []:
            props = node.get("properties") or {}
            tool_id = as_str(props.get("tool_id"))
            if tool_id is None and "id" in node:
                tool_id = self.by_id.get(int(node["id"]))
            if tool_id in self.tools or tool_id in self.names:
                nodes.append(tool_id)  # type: ignore[arg-type]
            elif tool_id is not None:
                nodes.append(tool_id)
        return nodes

    def _path_edge_meta(self, nodes: list[str]) -> tuple[list[str], list[str]]:
        edge_ids: list[str] = []
        sources: list[str] = []
        for index in range(len(nodes) - 1):
            src_id = self.names.get(nodes[index])
            dst_id = self.names.get(nodes[index + 1])
            edge = None
            if src_id is not None and dst_id is not None:
                edge = self.edges.get((src_id, dst_id, "REQUIRES"))
            if edge is None:
                edge_ids.append("")
                sources.append("")
            else:
                edge_ids.append(edge.edge_id or _logical_edge_id(edge.src, edge.dst))
                sources.append(edge.source)
        return edge_ids, sources

    def _path_edge_costs(
        self,
        path: dict,
        nodes: list[str],
        rel_types: list[str],
    ) -> list[float]:
        """Per-hop costs along nodes. Prefer relationship properties, else local store."""
        rels = path.get("relationships") or path.get("edges") or []
        out: list[float] = []
        for index in range(len(nodes) - 1):
            step: float | None = None
            if index < len(rels) and isinstance(rels[index], dict):
                props = rels[index].get("properties") or {}
                if "cost" in props:
                    step = as_float(props["cost"])
            if step is None:
                src_id = self.names.get(nodes[index])
                dst_id = self.names.get(nodes[index + 1])
                step = 1.0
                if src_id is not None and dst_id is not None:
                    for rel in rel_types:
                        edge = self.edges.get((src_id, dst_id, rel))
                        if edge is not None:
                            step = float(edge.cost)
                            break
            out.append(float(step))
        return out

    def recursive_cte(
        self,
        seeds: list[str],
        rel_types: list[str],
        max_len: int = MAX_LEN,
        *,
        tau: float | None = None,
    ) -> dict[str, float]:
        """Same-depth plain-store control: recursive CTE over SQLite edges."""
        costs = {name: index * SEED_PENALTY for index, name in enumerate(seeds)}
        if not seeds:
            return costs
        threshold = self.tau if tau is None else tau
        placeholders = ",".join("?" for _ in rel_types)
        for index, name in enumerate(seeds):
            src = self.names[name]
            rows = self.sqlite.execute(
                f"""
                WITH RECURSIVE walk(node, depth, cost) AS (
                  SELECT ?, 0, ?
                  UNION ALL
                  SELECT e.dst, walk.depth + 1, walk.cost + e.cost
                  FROM walk
                  JOIN edges e ON e.src = walk.node
                  WHERE walk.depth < ?
                    AND e.rel IN ({placeholders})
                    AND (e.rel != 'REQUIRES' OR e.p >= ?)
                )
                SELECT node, MIN(cost) AS cost FROM walk GROUP BY node
                """,
                (src, index * SEED_PENALTY, max_len, *rel_types, threshold),
            )
            for node_id, cost in rows:
                tool = self.by_id.get(int(node_id))
                if tool is None:
                    continue
                costs[tool] = min(costs.get(tool, float(cost)), float(cost))
        return costs

    def one_hop(
        self,
        seeds: list[str],
        rel_types: list[str],
        *,
        tau: float | None = None,
    ) -> dict[str, float]:
        """One-hop plain-store contrast for the composition split."""
        return self.recursive_cte(seeds, rel_types, max_len=1, tau=tau)

    def note_src_seen(self, src_name: str) -> None:
        """Count a learn trace whose main tool is src (denominator for confidence).

        v3: updates n_src and refreshes derived alpha/beta on live REQUIRES outs.
        """
        self.n_seen_src[src_name] = self.n_seen_src.get(src_name, 0) + 1
        if src_name not in self.names:
            return
        src_id = self.names[src_name]
        seen = self.n_seen_src[src_name]
        for (s, d, rel), edge in list(self.edges.items()):
            if s != src_id or rel != "REQUIRES" or edge.source != "trace":
                continue
            dst_name = self.by_id[d]
            n_ab = self.n_ab.get((src_name, dst_name), edge.n_obs)
            penalty = self.beta_penalty.get((src_name, dst_name), edge.beta_penalty)
            alpha, beta, p = _derive_alpha_beta(n_ab, seen, penalty)
            updated = Edge(
                src=edge.src,
                dst=edge.dst,
                rel=edge.rel,
                cost=_cost_from_p(p),
                support=edge.support,
                edit_id=edge.edit_id,
                prov=edge.prov,
                n_obs=n_ab,
                n_seen_src=seen,
                p=p,
                source=edge.source,
                alpha=alpha,
                beta=beta,
                beta_penalty=penalty,
                valid_from=edge.valid_from,
                edge_id=edge.edge_id,
            )
            self.edges[(s, d, rel)] = updated
            self.sqlite.execute(
                "UPDATE edges SET n_seen_src = ?, p = ?, cost = ? WHERE src = ? AND dst = ? AND rel = ?",
                (seen, p, updated.cost, s, d, rel),
            )
        self.sqlite.commit()
        self.edge_version += 1

    def learn_from_tools(self, tools_used: list[str], *, prov: str = "trace", trace_id: str = "") -> int:
        """Co-occurrence learning over all ordered pairs in tools_used. Returns edges touched."""
        used = [name for name in tools_used if name in self.names]
        if len(used) < 2:
            return 0
        unique = list(dict.fromkeys(used))
        for name in unique:
            self.n_seen_src[name] = self.n_seen_src.get(name, 0) + 1
        touched = 0
        for src_name in unique:
            for dst_name in unique:
                if src_name == dst_name:
                    continue
                key = (src_name, dst_name)
                self.n_ab[key] = self.n_ab.get(key, 0) + 1
                if self._upsert_requires_from_counts(src_name, dst_name, prov=prov, trace_id=trace_id, op="add"):
                    touched += 1
        return touched

    def record_trace(
        self,
        session_id: str | None,
        query: str,
        tools_used: list[str],
        tools_loaded: list[str],
    ) -> int:
        """Learn from an observed agent trace: co-occurrence + negative feedback."""
        del query  # provenance only; kept in the signature for the v3 API
        trace_id = self._next_edit()
        prov = f"trace:{trace_id}"
        if session_id is not None:
            self._ensure_session(session_id)
        touched = self.learn_from_tools(tools_used, prov=prov, trace_id=trace_id)
        used_set = set(tools_used)
        unused = [name for name in tools_loaded if name not in used_set]
        for dst_name in unused:
            for src_name in used_set:
                if src_name not in self.names or dst_name not in self.names:
                    continue
                src = self.names[src_name]
                dst = self.names[dst_name]
                key = (src, dst, "REQUIRES")
                if key not in self.edges:
                    continue
                pair = (src_name, dst_name)
                self.beta_penalty[pair] = self.beta_penalty.get(pair, 0.0) + 1.0
                self._upsert_requires_from_counts(
                    src_name,
                    dst_name,
                    prov=f"neg:{trace_id}",
                    trace_id=trace_id,
                    op="reweight",
                )
                touched += 1
        return touched

    def learn_depends(self, src_name: str, dst_name: str, prov: str) -> bool:
        """Legacy: treat as a co-occurrence pair strengthen on REQUIRES."""
        if src_name not in self.names or dst_name not in self.names or src_name == dst_name:
            return False
        self.n_seen_src[src_name] = self.n_seen_src.get(src_name, 0) + 1
        pair = (src_name, dst_name)
        self.n_ab[pair] = self.n_ab.get(pair, 0) + 1
        return self._upsert_requires_from_counts(src_name, dst_name, prov=prov, trace_id=prov, op="add")

    def add_schema_edge(self, src_name: str, dst_name: str, prov: str, cost: float = LEARNED_BASE_COST) -> bool:
        if src_name not in self.names or dst_name not in self.names or src_name == dst_name:
            return False
        src = self.names[src_name]
        dst = self.names[dst_name]
        key = (src, dst, "REQUIRES")
        if key in self.edges:
            return False
        now = float(self._now())
        edit_id = self._next_edit()
        edge_id = _logical_edge_id(src, dst)
        # I/O prior: treat as confident (p=1) with the provided cost.
        edge = Edge(
            src=src,
            dst=dst,
            rel="REQUIRES",
            cost=float(cost),
            support=1,
            edit_id=edit_id,
            prov=prov,
            n_obs=0,
            n_seen_src=0,
            p=1.0,
            source="io_prior",
            alpha=1.0,
            beta=1.0,
            beta_penalty=0.0,
            valid_from=now,
            edge_id=edge_id,
        )
        self._commit_requires(edge, existed=False, prev=None, op="add", trace_id="")
        return True

    def _upsert_requires_from_counts(
        self,
        src_name: str,
        dst_name: str,
        *,
        prov: str,
        trace_id: str,
        op: str,
    ) -> bool:
        src = self.names[src_name]
        dst = self.names[dst_name]
        key = (src, dst, "REQUIRES")
        n_ab = self.n_ab.get((src_name, dst_name), 0)
        n_src = self.n_seen_src.get(src_name, 0)
        penalty = self.beta_penalty.get((src_name, dst_name), 0.0)
        alpha, beta, p = _derive_alpha_beta(n_ab, n_src, penalty)
        cost = _cost_from_p(p)
        now = float(self._now())
        edit_id = self._next_edit()
        edge_id = _logical_edge_id(src, dst)
        current = self.edges.get(key)
        live = p >= self.tau

        if current is not None and current.source == "io_prior" and n_ab == 0:
            # Negative feedback on an io_prior-only edge: keep source.
            source = "io_prior"
        elif current is not None and current.source == "io_prior" and n_ab > 0:
            source = "trace"
        elif current is not None:
            source = current.source if current.source != "oracle" else "trace"
        else:
            source = "trace"

        if not live:
            # Below τ: retire live edge if present; always write hist with post-update stats.
            retired = Edge(
                src=src,
                dst=dst,
                rel="REQUIRES",
                cost=cost,
                support=(current.support if current is not None else 1),
                edit_id=edit_id,
                prov=prov,
                n_obs=n_ab,
                n_seen_src=n_src,
                p=p,
                source=source,
                alpha=alpha,
                beta=beta,
                beta_penalty=penalty,
                valid_from=now,
                edge_id=edge_id,
            )
            if current is not None:
                self._retire_requires(
                    current,
                    edit_id=edit_id,
                    prov=prov,
                    trace_id=trace_id,
                    now=now,
                    retired_stats=retired,
                )
                return True
            # Never live: still record hist for the attempt.
            self._append_hist(
                HistRow(
                    edge_id=edge_id,
                    src=src,
                    dst=dst,
                    cost=cost,
                    alpha=alpha,
                    beta=beta,
                    beta_penalty=penalty,
                    source=source,
                    trace_id=trace_id,
                    valid_from=now,
                    valid_to=OPEN_VALID_TO,
                    op="retire",
                    edit_id=edit_id,
                    n_obs=n_ab,
                    n_seen_src=n_src,
                    p=p,
                    support=1,
                    prov=prov,
                )
            )
            self._write_record(
                {
                    "kind": "edge",
                    "edit_id": edit_id,
                    "provenance": prov,
                    "src": src,
                    "dst": dst,
                    "rel": "REQUIRES",
                    "source": source,
                    "existed": False,
                    "op": "retire",
                    "prev": None,
                    "next": self._edge_snapshot(retired),
                    "hydra_mode": "bulk_sync",
                }
            )
            return True

        edge = Edge(
            src=src,
            dst=dst,
            rel="REQUIRES",
            cost=cost,
            support=(current.support + 1) if current is not None else 1,
            edit_id=edit_id,
            prov=prov,
            n_obs=n_ab,
            n_seen_src=n_src,
            p=p,
            source=source,
            alpha=alpha,
            beta=beta,
            beta_penalty=penalty,
            valid_from=now,
            edge_id=edge_id,
        )
        hist_op = "reweight" if current is not None else op
        self._commit_requires(edge, existed=current is not None, prev=current, op=hist_op, trace_id=trace_id)
        return True

    def _retire_requires(
        self,
        current: Edge,
        *,
        edit_id: str,
        prov: str,
        trace_id: str,
        now: float,
        retired_stats: Edge | None = None,
    ) -> None:
        key = (current.src, current.dst, "REQUIRES")
        self.edges.pop(key, None)
        self.sqlite.execute(
            "DELETE FROM edges WHERE src = ? AND dst = ? AND rel = ?",
            (current.src, current.dst, "REQUIRES"),
        )
        self.sqlite.commit()
        stats = retired_stats or current
        self._close_open_hist(current.edge_id or _logical_edge_id(current.src, current.dst), now)
        self._append_hist(
            HistRow(
                edge_id=current.edge_id or _logical_edge_id(current.src, current.dst),
                src=current.src,
                dst=current.dst,
                cost=stats.cost,
                alpha=stats.alpha,
                beta=stats.beta,
                beta_penalty=stats.beta_penalty,
                source=stats.source,
                trace_id=trace_id,
                valid_from=now,
                valid_to=OPEN_VALID_TO,
                op="retire",
                edit_id=edit_id,
                n_obs=stats.n_obs,
                n_seen_src=stats.n_seen_src,
                p=stats.p,
                support=stats.support,
                prov=prov,
            )
        )
        self._write_record(
            {
                "kind": "edge",
                "edit_id": edit_id,
                "provenance": prov,
                "src": current.src,
                "dst": current.dst,
                "rel": "REQUIRES",
                "source": stats.source,
                "existed": True,
                "op": "retire",
                "prev": self._edge_snapshot(current),
                "next": self._edge_snapshot(stats),
                "hydra_mode": "bulk_sync",
            }
        )
        self.edge_version += 1

    def _commit_requires(
        self,
        edge: Edge,
        *,
        existed: bool,
        prev: Edge | None,
        op: str,
        trace_id: str,
    ) -> None:
        key = (edge.src, edge.dst, edge.rel)
        now = edge.valid_from
        if existed and prev is not None:
            self._close_open_hist(prev.edge_id or edge.edge_id, now)
        self.edges[key] = edge
        self.sqlite.execute(
            "INSERT OR REPLACE INTO edges (src, dst, rel, cost, support, n_obs, n_seen_src, p) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (edge.src, edge.dst, edge.rel, edge.cost, edge.support, edge.n_obs, edge.n_seen_src, edge.p),
        )
        self.sqlite.commit()
        self._append_hist(
            HistRow(
                edge_id=edge.edge_id,
                src=edge.src,
                dst=edge.dst,
                cost=edge.cost,
                alpha=edge.alpha,
                beta=edge.beta,
                beta_penalty=edge.beta_penalty,
                source=edge.source,
                trace_id=trace_id,
                valid_from=now,
                valid_to=OPEN_VALID_TO,
                op=op,
                edit_id=edge.edit_id,
                n_obs=edge.n_obs,
                n_seen_src=edge.n_seen_src,
                p=edge.p,
                support=edge.support,
                prov=edge.prov,
            )
        )
        self._write_record(
            {
                "kind": "edge",
                "edit_id": edge.edit_id,
                "provenance": edge.prov,
                "src": edge.src,
                "dst": edge.dst,
                "rel": edge.rel,
                "source": edge.source,
                "existed": existed,
                "op": op,
                "prev": None if prev is None else self._edge_snapshot(prev),
                "next": self._edge_snapshot(edge),
                "hydra_mode": "bulk_sync",
            }
        )
        self.edge_version += 1

    def _edge_snapshot(self, edge: Edge) -> dict:
        return {
            "src": edge.src,
            "dst": edge.dst,
            "rel": edge.rel,
            "cost": edge.cost,
            "support": edge.support,
            "edit_id": edge.edit_id,
            "prov": edge.prov,
            "n_obs": edge.n_obs,
            "n_seen_src": edge.n_seen_src,
            "p": edge.p,
            "source": edge.source,
            "alpha": edge.alpha,
            "beta": edge.beta,
            "beta_penalty": edge.beta_penalty,
            "valid_from": edge.valid_from,
            "edge_id": edge.edge_id,
        }

    def _edge_from_snapshot(self, snap: dict) -> Edge:
        return Edge(
            src=int(snap["src"]),
            dst=int(snap["dst"]),
            rel=str(snap["rel"]),
            cost=float(snap["cost"]),
            support=int(snap["support"]),
            edit_id=str(snap["edit_id"]),
            prov=str(snap["prov"]),
            n_obs=int(snap.get("n_obs") or 0),
            n_seen_src=int(snap.get("n_seen_src") or 0),
            p=float(snap.get("p") or 0.5),
            source=str(snap.get("source") or "trace"),
            alpha=float(snap.get("alpha") or 1.0),
            beta=float(snap.get("beta") or 1.0),
            beta_penalty=float(snap.get("beta_penalty") or 0.0),
            valid_from=float(snap.get("valid_from") or 0.0),
            edge_id=str(snap.get("edge_id") or ""),
        )

    def _close_open_hist(self, edge_id: str, now: float) -> None:
        for row in reversed(self.hist):
            if row.edge_id == edge_id and row.valid_to >= OPEN_VALID_TO / 2:
                row.valid_to = now
                break

    def _append_hist(self, row: HistRow) -> None:
        self.hist.append(row)
        # Hydra write: CREATE then SET props (one relationship type, UNWIND batch).
        self.hydra.query(
            "UNWIND $rows AS row MATCH (a {id: row.src}), (b {id: row.dst}) "
            "CREATE (a)-[:REQUIRES_HIST]->(b)",
            {"rows": [{"src": row.src, "dst": row.dst}]},
        )
        self.hydra.query(
            "UNWIND $rows AS row MATCH (a {id: row.src})-[r:REQUIRES_HIST]->(b {id: row.dst}) "
            "SET r.edge_id = row.edge_id, r.cost = row.cost, r.alpha = row.alpha, r.beta = row.beta, "
            "r.beta_penalty = row.beta_penalty, r.source = row.source, r.trace_id = row.trace_id, "
            "r.valid_from = row.valid_from, r.valid_to = row.valid_to, r.op = row.op, "
            "r.edit_id = row.edit_id, r.n_obs = row.n_obs, r.n_seen_src = row.n_seen_src, "
            "r.p = row.p, r.support = row.support, r.prov = row.prov",
            {
                "rows": [
                    {
                        "src": row.src,
                        "dst": row.dst,
                        "edge_id": row.edge_id,
                        "cost": row.cost,
                        "alpha": row.alpha,
                        "beta": row.beta,
                        "beta_penalty": row.beta_penalty,
                        "source": row.source,
                        "trace_id": row.trace_id,
                        "valid_from": row.valid_from,
                        "valid_to": row.valid_to,
                        "op": row.op,
                        "edit_id": row.edit_id,
                        "n_obs": row.n_obs,
                        "n_seen_src": row.n_seen_src,
                        "p": row.p,
                        "support": row.support,
                        "prov": row.prov,
                    }
                ]
            },
        )

    def active_edges(self, rel_types: list[str], *, tau: float | None = None) -> list[Edge]:
        threshold = self.tau if tau is None else tau
        out: list[Edge] = []
        for (_src, _dst, rel), edge in self.edges.items():
            if rel not in rel_types:
                continue
            if rel == "REQUIRES" and edge.p < threshold:
                continue
            out.append(edge)
        return out

    def sync_learned_to_hydra(self) -> None:
        """Replace live REQUIRES in HydraDB from the in-memory / SQLite truth."""
        rel = "REQUIRES"
        existing = [
            (src, dst)
            for (src, dst, edge_rel), _edge in list(self.edges.items())
            if edge_rel == rel
        ]
        rows = self.sqlite.execute("SELECT src, dst FROM edges WHERE rel = ?", (rel,)).fetchall()
        pairs = list(
            {(int(src), int(dst)) for src, dst in rows}
            | set(existing)
            | set(self._synced_requires)
        )
        for start in range(0, len(pairs), 50):
            chunk = pairs[start : start + 50]
            if not chunk:
                continue
            self.hydra.query(
                f"UNWIND $rows AS row MATCH (a {{id: row.src}})-[r:{rel}]->(b {{id: row.dst}}) DELETE r",
                {"rows": [{"src": src, "dst": dst} for src, dst in chunk]},
            )
        fresh = []
        for (_src, _dst, edge_rel), edge in self.edges.items():
            if edge_rel != rel:
                continue
            if edge.p < self.tau:
                continue
            fresh.append(edge)
        if fresh:
            self._insert_edges(fresh, record=False)
        self._synced_requires = {(edge.src, edge.dst) for edge in fresh}
        self.edge_version += 1
        self._hydra_synced_version = self.edge_version

    def ensure_hydra_synced(self) -> None:
        if getattr(self, "_hydra_synced_version", None) != self.edge_version:
            self.sync_learned_to_hydra()

    def note(self, tool: str, text: str, prov: str) -> None:
        self._lesson(tool, text, prov)

    def record_result(self, session_id: str, tool: str, args_hash: str) -> None:
        """If read_only, write Entity+RESOLVED. Clear RESOLVED for INVALIDATES outs."""
        if tool not in self.names:
            return
        self._ensure_session(session_id)
        now = float(self._now())
        if self.read_only.get(tool, is_read_only(tool)):
            key = f"{tool}:{args_hash}"
            entity_id = self.entities.get(key)
            if entity_id is None:
                self._entity_seq += 1
                entity_id = self._entity_seq
                self.entities[key] = entity_id
                self._node_ids.append(entity_id)
                self.hydra.query(
                    "UNWIND $rows AS row MERGE (a {id: row.id}) "
                    "SET a:Entity, a.key = row.key, a.producer = row.producer, a.resolved_at = row.resolved_at",
                    {
                        "rows": [
                            {
                                "id": entity_id,
                                "key": key,
                                "producer": tool,
                                "resolved_at": now,
                            }
                        ]
                    },
                )
            else:
                self.hydra.query(
                    "UNWIND $rows AS row MERGE (a {id: row.id}) "
                    "SET a:Entity, a.key = row.key, a.producer = row.producer, a.resolved_at = row.resolved_at",
                    {
                        "rows": [
                            {
                                "id": entity_id,
                                "key": key,
                                "producer": tool,
                                "resolved_at": now,
                            }
                        ]
                    },
                )
            sid = self.sessions[session_id]
            self.hydra.query(
                "UNWIND $rows AS row MATCH (s {id: row.sid}), (e {id: row.eid}) "
                "CREATE (s)-[:RESOLVED]->(e)",
                {"rows": [{"sid": sid, "eid": entity_id}]},
            )
            self.hydra.query(
                "UNWIND $rows AS row MATCH (s {id: row.sid})-[r:RESOLVED]->(e {id: row.eid}) "
                "SET r.resolved_at = row.resolved_at",
                {"rows": [{"sid": sid, "eid": entity_id, "resolved_at": now}]},
            )
            self.resolved.setdefault(session_id, {})[tool] = now

        # INVALIDATES: set_X clears RESOLVED entities produced by get_X for this session.
        for producer in self.invalidates.get(tool, ()):
            self._clear_resolved_producer(session_id, producer)

    def _clear_resolved_producer(self, session_id: str, producer: str) -> None:
        self.resolved.get(session_id, {}).pop(producer, None)
        if session_id not in self.sessions:
            return
        sid = self.sessions[session_id]
        # Delete RESOLVED edges to entities with this producer (local entity map).
        rows = []
        for key, eid in list(self.entities.items()):
            if key.startswith(producer + ":"):
                rows.append({"sid": sid, "eid": eid})
        for start in range(0, len(rows), 50):
            chunk = rows[start : start + 50]
            if not chunk:
                continue
            self.hydra.query(
                "UNWIND $rows AS row MATCH (s {id: row.sid})-[r:RESOLVED]->(e {id: row.eid}) DELETE r",
                {"rows": chunk},
            )

    def _ensure_session(self, session_id: str) -> int:
        if session_id in self.sessions:
            return self.sessions[session_id]
        self._session_seq += 1
        sid = self._session_seq
        self.sessions[session_id] = sid
        self._node_ids.append(sid)
        self.hydra.query(
            "UNWIND $rows AS row MERGE (a {id: row.id}) SET a:Session, a.sid = row.sid",
            {"rows": [{"id": sid, "sid": session_id}]},
        )
        self.resolved.setdefault(session_id, {})
        return sid

    def as_of(self, ts: float) -> dict:
        """Edges valid at ts from REQUIRES_HIST, plus a diff against live now."""
        # Prefer local hist (always consistent). Also issue the Hydra statement for parity.
        self.hydra.query(
            "MATCH (a)-[r:REQUIRES_HIST]->(b) "
            "WHERE r.valid_from <= $ts AND r.valid_to > $ts "
            "RETURN r.edge_id, a.id, b.id, r.cost, r.alpha, r.beta, r.source, r.op, r.edit_id",
            {"ts": float(ts)},
        )
        # Replay local hist: for each edge_id, take the latest row with valid_from<=ts < valid_to
        # and op != retire (retire means absent).
        by_id: dict[str, HistRow] = {}
        for row in self.hist:
            if row.valid_from <= ts < row.valid_to:
                by_id[row.edge_id] = row
        edges_then: list[dict] = []
        for row in by_id.values():
            if row.op == "retire":
                continue
            edges_then.append(
                {
                    "edge_id": row.edge_id,
                    "src": row.src,
                    "dst": row.dst,
                    "cost": row.cost,
                    "alpha": row.alpha,
                    "beta": row.beta,
                    "source": row.source,
                    "p": row.p,
                    "edit_id": row.edit_id,
                    "op": row.op,
                }
            )
        now_keys = {
            (e.src, e.dst)
            for e in self.edges.values()
            if e.rel == "REQUIRES" and e.p >= self.tau
        }
        then_keys = {(e["src"], e["dst"]) for e in edges_then}
        then_by_pair = {(e["src"], e["dst"]): e for e in edges_then}
        now_by_pair = {
            (e.src, e.dst): e
            for e in self.edges.values()
            if e.rel == "REQUIRES" and e.p >= self.tau
        }
        added = [now_by_pair[k] for k in sorted(now_keys - then_keys)]
        removed = [then_by_pair[k] for k in sorted(then_keys - now_keys)]
        changed = []
        for key in sorted(now_keys & then_keys):
            live = now_by_pair[key]
            past = then_by_pair[key]
            if abs(float(live.cost) - float(past["cost"])) > 1e-9 or abs(float(live.p) - float(past["p"])) > 1e-9:
                changed.append({"then": past, "now": self._edge_snapshot(live)})
        return {
            "ts": ts,
            "edges": edges_then,
            "diff": {
                "added": [self._edge_snapshot(e) for e in added],
                "removed": removed,
                "changed": changed,
            },
        }

    def undo_edit(self, edit_id: str) -> bool:
        """Restore previous live REQUIRES version and append HIST op='undo'."""
        if not self.edit_log.exists():
            return False
        lines = self.edit_log.read_text().splitlines()
        target: dict | None = None
        target_index = -1
        for index, line in enumerate(lines):
            rec = json.loads(line)
            if rec.get("edit_id") == edit_id:
                target = rec
                target_index = index
        if target is None or target.get("kind") != "edge":
            return False
        now = float(self._now())
        undo_id = self._next_edit()
        src = int(target["src"])
        dst = int(target["dst"])
        key = (src, dst, "REQUIRES")
        prev = target.get("prev")
        current = self.edges.get(key)
        edge_id = (
            (prev or {}).get("edge_id")
            or (target.get("next") or {}).get("edge_id")
            or (current.edge_id if current else None)
            or _logical_edge_id(src, dst)
        )
        self._close_open_hist(edge_id, now)

        if prev is None:
            # Edit created the edge: remove live.
            self.edges.pop(key, None)
            self.sqlite.execute(
                "DELETE FROM edges WHERE src = ? AND dst = ? AND rel = ?",
                (src, dst, "REQUIRES"),
            )
            self.sqlite.commit()
            restored = None
        else:
            restored = self._edge_from_snapshot(prev)
            restored.edit_id = undo_id
            restored.valid_from = now
            self.edges[key] = restored
            self.sqlite.execute(
                "INSERT OR REPLACE INTO edges (src, dst, rel, cost, support, n_obs, n_seen_src, p) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    restored.src,
                    restored.dst,
                    restored.rel,
                    restored.cost,
                    restored.support,
                    restored.n_obs,
                    restored.n_seen_src,
                    restored.p,
                ),
            )
            self.sqlite.commit()
            # Restore counters used by learning.
            src_name = self.by_id.get(src)
            dst_name = self.by_id.get(dst)
            if src_name and dst_name:
                self.n_ab[(src_name, dst_name)] = restored.n_obs
                self.n_seen_src[src_name] = max(self.n_seen_src.get(src_name, 0), restored.n_seen_src)
                self.beta_penalty[(src_name, dst_name)] = restored.beta_penalty

        hist = HistRow(
            edge_id=edge_id,
            src=src,
            dst=dst,
            cost=0.0 if restored is None else restored.cost,
            alpha=1.0 if restored is None else restored.alpha,
            beta=1.0 if restored is None else restored.beta,
            beta_penalty=0.0 if restored is None else restored.beta_penalty,
            source=str(target.get("source") or "trace"),
            trace_id=edit_id,
            valid_from=now,
            valid_to=OPEN_VALID_TO,
            op="undo",
            edit_id=undo_id,
            n_obs=0 if restored is None else restored.n_obs,
            n_seen_src=0 if restored is None else restored.n_seen_src,
            p=0.0 if restored is None else restored.p,
            support=0 if restored is None else restored.support,
            prov=f"undo:{edit_id}",
        )
        self._append_hist(hist)
        self._write_record(
            {
                "kind": "edge",
                "edit_id": undo_id,
                "provenance": f"undo:{edit_id}",
                "src": src,
                "dst": dst,
                "rel": "REQUIRES",
                "source": target.get("source") or "trace",
                "existed": restored is not None,
                "op": "undo",
                "undo_of": edit_id,
                "prev": self._edge_snapshot(current) if current is not None else None,
                "next": None if restored is None else self._edge_snapshot(restored),
                "hydra_mode": "bulk_sync",
            }
        )
        # Drop the undone edit from the logical tip (keep history lines but mark).
        del lines[target_index]
        self.edit_log.write_text("".join(line + "\n" for line in lines))
        self.undone += 1
        self.edge_version += 1
        self.sync_learned_to_hydra()
        return True

    def rollback(self, count: int) -> None:
        if count <= 0:
            return
        lines = self.edit_log.read_text().splitlines()
        kept = lines[:-count]
        undone = lines[-count:]
        for line in reversed(undone):
            rec = json.loads(line)
            self.undone += 1
            self._revert_memory(rec)
        self.edit_log.write_text("".join(line + "\n" for line in kept))
        self.sqlite.commit()
        self.edge_version += 1
        self.sync_learned_to_hydra()

    def undo_last_batch(self, n: int = 1) -> int:
        """Public undo used by the demo / MCP."""
        before = self.applied
        self.rollback(n)
        return max(0, before - self.applied)

    def reason_paths(self, seeds: list[str], ranked: list[str], rel_types: list[str]) -> dict[str, list[str]]:
        """Cheap BFS parent pointers over SQLite for demo reason paths."""
        parents: dict[str, str | None] = {name: None for name in seeds}
        queue = list(seeds)
        seen = set(seeds)
        placeholders = ",".join("?" for _ in rel_types)
        while queue:
            name = queue.pop(0)
            src = self.names[name]
            rows = self.sqlite.execute(
                f"SELECT dst FROM edges WHERE src = ? AND rel IN ({placeholders})",
                (src, *rel_types),
            )
            for (dst,) in rows:
                child = self.by_id.get(int(dst))
                if child is None or child in seen:
                    continue
                seen.add(child)
                parents[child] = name
                queue.append(child)
        paths: dict[str, list[str]] = {}
        for tool in ranked:
            if tool not in parents and tool not in seeds:
                paths[tool] = [tool]
                continue
            chain = [tool]
            cur = tool
            guard = 0
            while parents.get(cur) is not None and guard < MAX_LEN + 2:
                cur = parents[cur]  # type: ignore[assignment]
                chain.append(cur)
                guard += 1
            chain.reverse()
            paths[tool] = chain
        return paths

    def _revert_memory(self, rec: dict) -> None:
        kind = rec.get("kind")
        if kind == "lesson":
            self.lessons[rec["tool"]] = rec["previous_lesson"]
            return
        if kind != "edge":
            return
        src = rec["src"]
        dst = rec["dst"]
        rel = rec.get("rel") or "REQUIRES"
        key = (src, dst, rel)
        prev = rec.get("prev")
        if rec.get("existed") and prev is not None:
            edge = self._edge_from_snapshot(prev)
            self.edges[key] = edge
            self.sqlite.execute(
                "UPDATE edges SET cost = ?, support = ?, n_obs = ?, n_seen_src = ?, p = ? "
                "WHERE src = ? AND dst = ? AND rel = ?",
                (edge.cost, edge.support, edge.n_obs, edge.n_seen_src, edge.p, src, dst, rel),
            )
        elif rec.get("existed") and "prev_cost" in rec:
            # Legacy edit-log shape from v2.
            edge = Edge(
                src=src,
                dst=dst,
                rel=rel,
                cost=rec["prev_cost"],
                support=rec["prev_support"],
                edit_id=rec["prev_edit_id"],
                prov=rec["prev_prov"],
                n_obs=int(rec.get("prev_n_obs") or 0),
                n_seen_src=int(rec.get("prev_n_seen_src") or 0),
                p=float(rec.get("prev_p") or 0.5),
                source=str(rec.get("source") or "trace"),
            )
            self.edges[key] = edge
            self.sqlite.execute(
                "UPDATE edges SET cost = ?, support = ?, n_obs = ?, n_seen_src = ?, p = ? "
                "WHERE src = ? AND dst = ? AND rel = ?",
                (edge.cost, edge.support, edge.n_obs, edge.n_seen_src, edge.p, src, dst, rel),
            )
        else:
            self.edges.pop(key, None)
            self.sqlite.execute(
                "DELETE FROM edges WHERE src = ? AND dst = ? AND rel = ?",
                (src, dst, rel),
            )

    def _lesson(self, tool: str, text: str, prov: str) -> None:
        previous = self.lessons.get(tool, "")
        lesson = text[:180]
        if previous == lesson:
            return
        edit_id = self._next_edit()
        rec = {
            "kind": "lesson",
            "edit_id": edit_id,
            "provenance": prov,
            "tool": tool,
            "previous_lesson": previous,
            "do_query": "SET lesson (memory)",
            "do_parameters": {"id": self.names[tool], "lesson": lesson},
            "undo_query": "SET lesson (memory)",
            "undo_parameters": {"id": self.names[tool], "lesson": previous},
        }
        self._write_record(rec)
        self.lessons[tool] = lesson

    def _write_record(self, rec: dict) -> None:
        with self.edit_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(rec) + "\n")
        self.applied += 1

    def _insert_edges(self, edges: list[Edge], record: bool) -> None:
        by_rel: dict[str, list[Edge]] = {}
        for edge in edges:
            by_rel.setdefault(edge.rel, []).append(edge)
        for rel, group in by_rel.items():
            for start in range(0, len(group), 100):
                chunk = group[start : start + 100]
                self.hydra.query(
                    f"UNWIND $rows AS row CREATE (a {{id: row.src}})-[:{rel}]->(b {{id: row.dst}})",
                    {"rows": [{"src": edge.src, "dst": edge.dst} for edge in chunk]},
                )
                self.hydra.query(
                    f"UNWIND $rows AS row MATCH (a {{id: row.src}})-[r:{rel}]->(b {{id: row.dst}}) "
                    "SET r.cost = row.cost, r.support = row.support, r.edit_id = row.edit_id, "
                    "r.prov = row.prov, r.n_obs = row.n_obs, r.n_seen_src = row.n_seen_src, "
                    "r.p = row.p, r.source = row.source, r.alpha = row.alpha, r.beta = row.beta, "
                    "r.beta_penalty = row.beta_penalty, r.valid_from = row.valid_from, "
                    "r.edge_id = row.edge_id",
                    {
                        "rows": [
                            {
                                "src": edge.src,
                                "dst": edge.dst,
                                "cost": edge.cost,
                                "support": edge.support,
                                "edit_id": edge.edit_id,
                                "prov": edge.prov,
                                "n_obs": edge.n_obs,
                                "n_seen_src": edge.n_seen_src,
                                "p": edge.p,
                                "source": edge.source,
                                "alpha": edge.alpha,
                                "beta": edge.beta,
                                "beta_penalty": edge.beta_penalty,
                                "valid_from": edge.valid_from,
                                "edge_id": edge.edge_id,
                            }
                            for edge in chunk
                        ]
                    },
                )
        self.sqlite.executemany(
            "INSERT OR REPLACE INTO edges (src, dst, rel, cost, support, n_obs, n_seen_src, p) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (edge.src, edge.dst, edge.rel, edge.cost, edge.support, edge.n_obs, edge.n_seen_src, edge.p)
                for edge in edges
            ],
        )
        self.sqlite.commit()
        for edge in edges:
            self.edges[(edge.src, edge.dst, edge.rel)] = edge
        self.edge_version += 1
        if record:
            return

    def _unwind_tool_nodes(self, rows: list[dict]) -> None:
        for start in range(0, len(rows), 100):
            chunk = rows[start : start + 100]
            self.hydra.query(
                "UNWIND $rows AS row MERGE (a {id: row.id}) "
                "SET a:Tool, a.tool_id = row.tool_id, a.read_only = row.read_only, "
                "a.schema_tokens = row.schema_tokens, a.description = row.description, "
                "a.kind = row.kind, a.lesson = row.lesson",
                {"rows": chunk},
            )

    def _unwind_nodes(self, label: str, rows: list[dict]) -> None:
        for start in range(0, len(rows), 100):
            chunk = rows[start : start + 100]
            self.hydra.query(
                "UNWIND $rows AS row MERGE (a {id: row.id}) "
                f"SET a:{label}, a.tool_id = row.tool_id, a.description = row.description, "
                "a.kind = row.kind, a.lesson = row.lesson",
                {"rows": chunk},
            )

    def _unwind_bare_edges(self, rel: str, pairs: list[tuple[int, int]]) -> None:
        for start in range(0, len(pairs), 100):
            chunk = pairs[start : start + 100]
            self.hydra.query(
                f"UNWIND $rows AS row CREATE (a {{id: row.src}})-[:{rel}]->(b {{id: row.dst}})",
                {"rows": [{"src": src, "dst": dst} for src, dst in chunk]},
            )

    def _next_edit(self) -> str:
        self._edit_seq += 1
        return f"e{self._edit_seq}"


def apply_prefix_costs(
    costs: dict[str, float],
    nodes: list[str],
    edge_costs: list[float],
    penalty: float,
) -> None:
    """Write minimum prefix costs for nodes along a path into ``costs``.

    For unit-cost s→a→b with penalty 0 this yields {s:0, a:1, b:2}, matching
    the recursive CTE. Using the path total for every node would wrongly yield
    {s:0, a:2, b:2}.
    """
    if not nodes:
        return
    running = penalty
    costs[nodes[0]] = min(costs.get(nodes[0], running), running)
    for index, name in enumerate(nodes[1:]):
        step = edge_costs[index] if index < len(edge_costs) else 1.0
        running += float(step)
        costs[name] = min(costs.get(name, running), running)


def rank_costs(costs: dict[str, float]) -> list[str]:
    return sorted(costs, key=lambda name: (costs[name], name))
