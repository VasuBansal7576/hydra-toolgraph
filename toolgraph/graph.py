"""Catalog nodes, learned edges, oracle edges, MSpaths, and recursive CTE.

v2 rule: catalog depends_on edges never enter a learned arm. They are loaded
only as ORACLE_DEP for the ceiling arm. Learned DEPENDS_ON and SCHEMA_DEP
come from traces and Jev-verified schema proposals.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from toolgraph.data import Tool
from toolgraph.hydra import HydraClient, as_float, as_str, unwrap
from toolgraph.protocol import (
    LEARNED_BASE_COST,
    MAX_LEN,
    PATH_COUNT,
    RESULT_LIMIT,
    SEED_PENALTY,
)


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


class GraphStore:
    def __init__(self, hydra: HydraClient, edit_log: Path) -> None:
        self.hydra = hydra
        self.edit_log = edit_log
        self.edit_log.parent.mkdir(parents=True, exist_ok=True)
        self.names: dict[str, int] = {}
        self.by_id: dict[int, str] = {}
        self.tools: dict[str, Tool] = {}
        self.lessons: dict[str, str] = {}
        self.edges: dict[tuple[int, int, str], Edge] = {}
        self.sqlite = sqlite3.connect(":memory:")
        self.sqlite.execute(
            "CREATE TABLE edges (src INTEGER, dst INTEGER, rel TEXT, cost REAL, support INTEGER, "
            "PRIMARY KEY (src, dst, rel))"
        )
        self._edit_seq = 1_000_000
        self.applied = 0
        self.undone = 0
        self.edge_version = 0
        self._hydra_synced_version = 0
        self._node_ids: list[int] = []

    def wipe(self, *, tool_count: int = 0) -> None:
        # Label-wide DETACH DELETE can stack-overflow the local graph-node.
        # Delete tracked ids plus the stable tool/param/meta ranges so a new
        # GraphStore on a reused HydraDB volume cannot inherit stale edges.
        ids = list(dict.fromkeys(self._node_ids))
        if tool_count > 0:
            ids.extend(range(1, tool_count + 1))
            ids.extend(range(10_001, 10_001 + max(2_000, tool_count * 3)))
            ids.extend(range(20_000, 20_010))
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
        self.sqlite.execute("DELETE FROM edges")
        self.sqlite.commit()
        self.edge_version += 1
        self._hydra_synced_version = self.edge_version

    def load_catalog(self, tools: list[Tool], *, include_oracle: bool = True) -> None:
        """Load tool nodes and param structure. Never loads catalog DEPENDS_ON into learned rels."""
        self.wipe(tool_count=len(tools))
        self.tools = {tool.name: tool for tool in tools}
        self.names = {tool.name: idx + 1 for idx, tool in enumerate(tools)}
        self.by_id = {idx: name for name, idx in self.names.items()}
        node_rows = [
            {
                "id": self.names[tool.name],
                "tool_id": tool.name,
                "description": tool.description[:500],
                "kind": tool.kind,
                "lesson": "",
            }
            for tool in tools
        ]
        self._unwind_nodes("Tool", node_rows)
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
        if include_oracle:
            self.load_oracle_edges(tools)

    def load_oracle_edges(self, tools: list[Tool]) -> None:
        """Ceiling arm only. Uses ORACLE_DEP so learned arms never see these edges."""
        oracle: list[Edge] = []
        seen: set[tuple[int, int]] = set()
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
                    )
                )
        self._insert_edges(oracle, record=False)

    def closure(self, seeds: list[str], rel_types: list[str]) -> dict[str, float]:
        """Min path cost via HydraDB algo.MSpaths over the given relationship types."""
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
            path_cost = as_float(row[1])
            if not isinstance(path, dict):
                continue
            nodes = []
            for node in path.get("nodes") or []:
                props = node.get("properties") or {}
                tool_id = as_str(props.get("tool_id"))
                if tool_id is None and "id" in node:
                    tool_id = self.by_id.get(int(node["id"]))
                if tool_id in self.tools:
                    nodes.append(tool_id)
            if not nodes:
                continue
            penalty = seed_index.get(nodes[0], 0) * SEED_PENALTY
            total = penalty + path_cost
            for name in nodes:
                costs[name] = min(costs.get(name, total), total)
        return costs

    def recursive_cte(self, seeds: list[str], rel_types: list[str], max_len: int = MAX_LEN) -> dict[str, float]:
        """Same-depth plain-store control: recursive CTE over SQLite edges."""
        costs = {name: index * SEED_PENALTY for index, name in enumerate(seeds)}
        if not seeds:
            return costs
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
                  WHERE walk.depth < ? AND e.rel IN ({placeholders})
                )
                SELECT node, MIN(cost) AS cost FROM walk GROUP BY node
                """,
                (src, index * SEED_PENALTY, max_len, *rel_types),
            )
            for node_id, cost in rows:
                tool = self.by_id.get(int(node_id))
                if tool is None:
                    continue
                costs[tool] = min(costs.get(tool, float(cost)), float(cost))
        return costs

    def learn_depends(self, src_name: str, dst_name: str, prov: str) -> bool:
        """Add or strengthen DEPENDS_ON(main -> helper) from an observed trace.

        Writes a do/undo record and updates SQLite immediately. HydraDB is
        refreshed in bulk via sync_learned_to_hydra() before MSpaths (the local
        graph-node stack-overflows on many per-edge MATCH/SET mutations).
        """
        if src_name not in self.names or dst_name not in self.names or src_name == dst_name:
            return False
        src = self.names[src_name]
        dst = self.names[dst_name]
        return self._memory_upsert_edge(src, dst, "DEPENDS_ON", LEARNED_BASE_COST, prov)

    def add_schema_edge(self, src_name: str, dst_name: str, prov: str, cost: float = LEARNED_BASE_COST) -> bool:
        if src_name not in self.names or dst_name not in self.names or src_name == dst_name:
            return False
        src = self.names[src_name]
        dst = self.names[dst_name]
        key = (src, dst, "SCHEMA_DEP")
        if key in self.edges:
            return False
        return self._memory_upsert_edge(src, dst, "SCHEMA_DEP", cost, prov, strengthen=False)

    def sync_learned_to_hydra(self) -> None:
        """Replace DEPENDS_ON and SCHEMA_DEP in HydraDB from the SQLite truth."""
        for rel in ("DEPENDS_ON", "SCHEMA_DEP"):
            existing = [
                (src, dst)
                for (src, dst, edge_rel), _edge in list(self.edges.items())
                if edge_rel == rel
            ]
            # Delete prior Hydra copies of these ids (may include stale rows).
            # We track learned keys in memory; delete by current+previous via sqlite.
            rows = self.sqlite.execute("SELECT src, dst FROM edges WHERE rel = ?", (rel,)).fetchall()
            pairs = list({(int(src), int(dst)) for src, dst in rows} | set(existing))
            for start in range(0, len(pairs), 50):
                chunk = pairs[start : start + 50]
                if not chunk:
                    continue
                self.hydra.query(
                    f"UNWIND $rows AS row MATCH (a {{id: row.src}})-[r:{rel}]->(b {{id: row.dst}}) DELETE r",
                    {"rows": [{"src": src, "dst": dst} for src, dst in chunk]},
                )
            fresh = [
                edge
                for (src, dst, edge_rel), edge in self.edges.items()
                if edge_rel == rel
            ]
            if fresh:
                self._insert_edges(fresh, record=False)
        self.edge_version += 1
        self._hydra_synced_version = self.edge_version

    def ensure_hydra_synced(self) -> None:
        if getattr(self, "_hydra_synced_version", None) != self.edge_version:
            self.sync_learned_to_hydra()

    def note(self, tool: str, text: str, prov: str) -> None:
        self._lesson(tool, text, prov)

    def rollback(self, count: int) -> None:
        if count <= 0:
            return
        lines = self.edit_log.read_text().splitlines()
        kept = lines[:-count]
        undone = lines[-count:]
        for line in reversed(undone):
            rec = json.loads(line)
            # Logical undo only; HydraDB is bulk-synced after the batch.
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

    def _memory_upsert_edge(
        self,
        src: int,
        dst: int,
        rel: str,
        base_cost: float,
        prov: str,
        *,
        strengthen: bool = True,
    ) -> bool:
        key = (src, dst, rel)
        current = self.edges.get(key)
        edit_id = self._next_edit()
        if current is None:
            cost = base_cost
            support = 1
            existed = False
            prev = None
            do_query = (
                f"UNWIND $rows AS row CREATE (a {{id: row.src}})-[:{rel}]->(b {{id: row.dst}}) "
                f"/* then SET cost/support via bulk sync */"
            )
            undo_query = f"MATCH (a {{id: $src}})-[r:{rel}]->(b {{id: $dst}}) DELETE r"
            do_parameters = {"rows": [{"src": src, "dst": dst}], "cost": cost, "support": support}
            undo_parameters = {"src": src, "dst": dst}
        else:
            if not strengthen:
                return False
            support = current.support + 1
            cost = 1.0 / (1.0 + support)
            existed = True
            prev = current
            do_query = (
                f"MATCH (a {{id: $src}})-[r:{rel}]->(b {{id: $dst}}) "
                "SET r.cost = $cost, r.support = $support, r.edit_id = $edit_id, r.prov = $prov"
            )
            undo_query = (
                f"MATCH (a {{id: $src}})-[r:{rel}]->(b {{id: $dst}}) "
                "SET r.cost = $cost, r.support = $support, r.edit_id = $edit_id, r.prov = $prov"
            )
            do_parameters = {
                "src": src,
                "dst": dst,
                "cost": cost,
                "support": support,
                "edit_id": edit_id,
                "prov": prov,
            }
            undo_parameters = {
                "src": src,
                "dst": dst,
                "cost": current.cost,
                "support": current.support,
                "edit_id": current.edit_id,
                "prov": current.prov,
            }
        rec = {
            "kind": "edge",
            "edit_id": edit_id,
            "provenance": prov,
            "src": src,
            "dst": dst,
            "rel": rel,
            "existed": existed,
            "prev_cost": None if prev is None else prev.cost,
            "prev_support": None if prev is None else prev.support,
            "prev_edit_id": None if prev is None else prev.edit_id,
            "prev_prov": None if prev is None else prev.prov,
            "do_query": do_query,
            "do_parameters": do_parameters,
            "undo_query": undo_query,
            "undo_parameters": undo_parameters,
            "hydra_mode": "bulk_sync",
        }
        self._write_record(rec)
        edge = Edge(src, dst, rel, cost, support, edit_id, prov)
        self.edges[key] = edge
        self.sqlite.execute(
            "INSERT OR REPLACE INTO edges (src, dst, rel, cost, support) VALUES (?, ?, ?, ?, ?)",
            (src, dst, rel, cost, support),
        )
        self.sqlite.commit()
        self.edge_version += 1
        return True

    def _revert_memory(self, rec: dict) -> None:
        kind = rec.get("kind")
        if kind == "lesson":
            self.lessons[rec["tool"]] = rec["previous_lesson"]
            return
        if kind != "edge":
            return
        src = rec["src"]
        dst = rec["dst"]
        rel = rec["rel"]
        key = (src, dst, rel)
        if rec["existed"]:
            edge = Edge(
                src=src,
                dst=dst,
                rel=rel,
                cost=rec["prev_cost"],
                support=rec["prev_support"],
                edit_id=rec["prev_edit_id"],
                prov=rec["prev_prov"],
            )
            self.edges[key] = edge
            self.sqlite.execute(
                "UPDATE edges SET cost = ?, support = ? WHERE src = ? AND dst = ? AND rel = ?",
                (edge.cost, edge.support, src, dst, rel),
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
                    "SET r.cost = row.cost, r.support = row.support, r.edit_id = row.edit_id, r.prov = row.prov",
                    {
                        "rows": [
                            {
                                "src": edge.src,
                                "dst": edge.dst,
                                "cost": edge.cost,
                                "support": edge.support,
                                "edit_id": edge.edit_id,
                                "prov": edge.prov,
                            }
                            for edge in chunk
                        ]
                    },
                )
        self.sqlite.executemany(
            "INSERT OR REPLACE INTO edges (src, dst, rel, cost, support) VALUES (?, ?, ?, ?, ?)",
            [(edge.src, edge.dst, edge.rel, edge.cost, edge.support) for edge in edges],
        )
        self.sqlite.commit()
        for edge in edges:
            self.edges[(edge.src, edge.dst, edge.rel)] = edge
        self.edge_version += 1
        if record:
            return

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


def rank_costs(costs: dict[str, float]) -> list[str]:
    return sorted(costs, key=lambda name: (costs[name], name))
