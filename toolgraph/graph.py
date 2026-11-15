"""Catalog graph, MSpaths closure, SQLite one-hop, and do/undo edits.

SQLite holds the same edges as HydraDB and reads only one hop. HydraDB walks
DEPENDS_ON and FOLLOWED_BY with algo.MSpaths and the cost property.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from toolgraph.data import Tool
from toolgraph.hydra import HydraClient, as_float, as_str, unwrap
from toolgraph.protocol import (
    DIRECT_COST,
    INDIRECT_COST,
    MAX_LEN,
    PATH_COUNT,
    RESULT_LIMIT,
    SEED_PENALTY,
)


def edge_cost(dependence_type: str) -> float:
    if "INDIRECT" in dependence_type:
        return INDIRECT_COST
    return DIRECT_COST


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

    def wipe(self) -> None:
        for label in ("Tool", "Param", "Skill", "Server", "Edit"):
            self.hydra.query(f"MATCH (n:{label}) DETACH DELETE n")
        self.edges.clear()
        self.lessons.clear()
        self.sqlite.execute("DELETE FROM edges")
        self.sqlite.commit()

    def load_catalog(self, tools: list[Tool]) -> None:
        self.wipe()
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
        param_rows = []
        param_edges = []
        param_id = 10_000
        for tool in tools:
            for param in tool.params:
                param_id += 1
                param_rows.append(
                    {
                        "id": param_id,
                        "tool_id": f"{tool.name}.{param.get('name', 'param')}",
                        "description": str(param.get("description") or "")[:240],
                        "kind": "param",
                        "lesson": "",
                    }
                )
                param_edges.append((self.names[tool.name], param_id))
        if param_rows:
            self._unwind_nodes("Param", param_rows)
            self._unwind_bare_edges("REQUIRES_PARAM", param_edges)
        self._unwind_nodes(
            "Skill",
            [
                {"id": 20_001, "tool_id": "core", "description": "Core helper", "kind": "skill", "lesson": ""},
                {"id": 20_002, "tool_id": "regular", "description": "Regular tool", "kind": "skill", "lesson": ""},
            ],
        )
        self._unwind_nodes(
            "Server",
            [
                {
                    "id": 20_000,
                    "tool_id": "toollinkos",
                    "description": "ToolLinkOS catalog",
                    "kind": "server",
                    "lesson": "",
                }
            ],
        )
        skill_edges = []
        host_edges = []
        for tool in tools:
            skill = 20_001 if tool.kind == "core" else 20_002
            skill_edges.append((self.names[tool.name], skill))
            host_edges.append((self.names[tool.name], 20_000))
        self._unwind_bare_edges("OF_SKILL", skill_edges)
        self._unwind_bare_edges("HOSTED_BY", host_edges)
        dep_edges: list[Edge] = []
        seen_deps: set[tuple[int, int]] = set()
        for tool in tools:
            for dep in tool.depends_on:
                if dep["name"] not in self.names:
                    continue
                pair = (self.names[tool.name], self.names[dep["name"]])
                if pair in seen_deps or pair[0] == pair[1]:
                    continue
                seen_deps.add(pair)
                dep_edges.append(
                    Edge(
                        src=self.names[tool.name],
                        dst=self.names[dep["name"]],
                        rel="DEPENDS_ON",
                        cost=edge_cost(str(dep.get("dependence_type") or "")),
                        support=0,
                        edit_id="catalog",
                        prov="catalog",
                    )
                )
        self._insert_edges(dep_edges, record=False)

    def closure(self, seeds: list[str]) -> dict[str, float]:
        """Min path cost per reached tool, including the seed penalty."""
        costs = {name: index * SEED_PENALTY for index, name in enumerate(seeds)}
        if not seeds:
            return costs
        values = ", ".join(f"'{name}'" for name in seeds)
        statement = (
            "CALL algo.MSpaths({"
            "sourceLabel: 'Tool', sourceProperty: 'tool_id', "
            f"sourceValues: [{values}], "
            "relTypes: ['DEPENDS_ON', 'FOLLOWED_BY'], relDirection: 'outgoing', "
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

    def one_hop(self, seeds: list[str]) -> dict[str, float]:
        costs = {name: index * SEED_PENALTY for index, name in enumerate(seeds)}
        for index, name in enumerate(seeds):
            src = self.names[name]
            rows = self.sqlite.execute(
                "SELECT dst, cost FROM edges WHERE src = ? AND rel IN ('DEPENDS_ON', 'FOLLOWED_BY')",
                (src,),
            )
            for dst, cost in rows:
                tool = self.by_id.get(int(dst))
                if tool is None:
                    continue
                total = index * SEED_PENALTY + float(cost)
                costs[tool] = min(costs.get(tool, total), total)
        return costs

    def neighbors(self, name: str) -> set[str]:
        src = self.names[name]
        rows = self.sqlite.execute(
            "SELECT dst FROM edges WHERE src = ? AND rel IN ('DEPENDS_ON', 'FOLLOWED_BY')",
            (src,),
        )
        return {self.by_id[int(dst)] for (dst,) in rows if int(dst) in self.by_id}

    def reachable_from(self, seeds: list[str]) -> set[str]:
        # Used only to decide edit shape. Retrieval itself goes through Hydra or SQLite.
        seen = set(seeds)
        stack = list(seeds)
        while stack:
            name = stack.pop()
            for nxt in self.neighbors(name):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    def strengthen(self, src_name: str, dst_name: str, prov: str) -> bool:
        src = self.names[src_name]
        dst = self.names[dst_name]
        current = self.edges.get((src, dst, "DEPENDS_ON"))
        if current is None:
            return False
        new_support = current.support + 1
        new_cost = 1.0 / (1.0 + new_support)
        edit_id = self._next_edit()
        do = (
            "MATCH (a {id: $src})-[r:DEPENDS_ON]->(b {id: $dst}) "
            "SET r.cost = $cost, r.support = $support, r.edit_id = $edit_id, r.prov = $prov"
        )
        undo = (
            "MATCH (a {id: $src})-[r:DEPENDS_ON]->(b {id: $dst}) "
            "SET r.cost = $cost, r.support = $support, r.edit_id = $edit_id, r.prov = $prov"
        )
        do_params = {
            "src": src,
            "dst": dst,
            "cost": new_cost,
            "support": new_support,
            "edit_id": edit_id,
            "prov": prov,
        }
        undo_params = {
            "src": src,
            "dst": dst,
            "cost": current.cost,
            "support": current.support,
            "edit_id": current.edit_id,
            "prov": current.prov,
        }
        self._apply(do, do_params, undo, undo_params, prov, edit_id)
        updated = Edge(src, dst, "DEPENDS_ON", new_cost, new_support, edit_id, prov)
        self.edges[(src, dst, "DEPENDS_ON")] = updated
        self.sqlite.execute(
            "UPDATE edges SET cost = ?, support = ? WHERE src = ? AND dst = ? AND rel = 'DEPENDS_ON'",
            (new_cost, new_support, src, dst),
        )
        return True

    def link_followed(self, src_name: str, dst_name: str, prov: str) -> bool:
        src = self.names[src_name]
        dst = self.names[dst_name]
        key = (src, dst, "FOLLOWED_BY")
        current = self.edges.get(key)
        if current is not None:
            new_support = current.support + 1
            new_cost = 1.0 / (1.0 + new_support)
            edit_id = self._next_edit()
            do = (
                "MATCH (a {id: $src})-[r:FOLLOWED_BY]->(b {id: $dst}) "
                "SET r.cost = $cost, r.support = $support, r.edit_id = $edit_id, r.prov = $prov"
            )
            undo = (
                "MATCH (a {id: $src})-[r:FOLLOWED_BY]->(b {id: $dst}) "
                "SET r.cost = $cost, r.support = $support, r.edit_id = $edit_id, r.prov = $prov"
            )
            self._apply(
                do,
                {"src": src, "dst": dst, "cost": new_cost, "support": new_support, "edit_id": edit_id, "prov": prov},
                undo,
                {
                    "src": src,
                    "dst": dst,
                    "cost": current.cost,
                    "support": current.support,
                    "edit_id": current.edit_id,
                    "prov": current.prov,
                },
                prov,
                edit_id,
            )
            self.edges[key] = Edge(src, dst, "FOLLOWED_BY", new_cost, new_support, edit_id, prov)
            self.sqlite.execute(
                "UPDATE edges SET cost = ?, support = ? WHERE src = ? AND dst = ? AND rel = 'FOLLOWED_BY'",
                (new_cost, new_support, src, dst),
            )
        else:
            edit_id = self._next_edit()
            create = "UNWIND $rows AS row CREATE (a {id: row.src})-[:FOLLOWED_BY]->(b {id: row.dst})"
            unset = "MATCH (a {id: $src})-[r:FOLLOWED_BY]->(b {id: $dst}) DELETE r"
            self._apply(
                create,
                {"rows": [{"src": src, "dst": dst}]},
                unset,
                {"src": src, "dst": dst},
                prov,
                edit_id,
            )
            set_cost = (
                "MATCH (a {id: $src})-[r:FOLLOWED_BY]->(b {id: $dst}) "
                "SET r.cost = $cost, r.support = $support, r.edit_id = $edit_id, r.prov = $prov"
            )
            clear_cost = (
                "MATCH (a {id: $src})-[r:FOLLOWED_BY]->(b {id: $dst}) "
                "SET r.cost = 0.0, r.support = 0, r.edit_id = '', r.prov = ''"
            )
            self._apply(
                set_cost,
                {"src": src, "dst": dst, "cost": 1.0, "support": 1, "edit_id": edit_id, "prov": prov},
                clear_cost,
                {"src": src, "dst": dst},
                prov,
                edit_id,
            )
            self.edges[key] = Edge(src, dst, "FOLLOWED_BY", 1.0, 1, edit_id, prov)
            self.sqlite.execute(
                "INSERT INTO edges (src, dst, rel, cost, support) VALUES (?, ?, 'FOLLOWED_BY', 1.0, 1)",
                (src, dst),
            )
        return True

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
            self.hydra.query(rec["undo_query"], rec["undo_parameters"])
            self.undone += 1
            self._revert_memory(rec)
        self.edit_log.write_text("".join(line + "\n" for line in kept))
        self.sqlite.commit()

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
        do = "MATCH (a:Tool {id: $id}) SET a.lesson = $lesson"
        undo = "MATCH (a:Tool {id: $id}) SET a.lesson = $lesson"
        self.hydra.query(do, {"id": self.names[tool], "lesson": lesson})
        rec = {
            "kind": "lesson",
            "edit_id": edit_id,
            "provenance": prov,
            "tool": tool,
            "previous_lesson": previous,
            "do_query": do,
            "do_parameters": {"id": self.names[tool], "lesson": lesson},
            "undo_query": undo,
            "undo_parameters": {"id": self.names[tool], "lesson": previous},
        }
        self._write_record(rec)
        self.lessons[tool] = lesson

    def _apply(
        self,
        do_query: str,
        do_parameters: dict,
        undo_query: str,
        undo_parameters: dict,
        prov: str,
        edit_id: str,
    ) -> None:
        self.hydra.query(do_query, do_parameters)
        src = do_parameters.get("src", (do_parameters.get("rows") or [{}])[0].get("src"))
        dst = do_parameters.get("dst", (do_parameters.get("rows") or [{}])[0].get("dst"))
        rel = "FOLLOWED_BY" if "FOLLOWED_BY" in do_query else "DEPENDS_ON"
        key = (src, dst, rel)
        current = self.edges.get(key)
        rec = {
            "kind": "edge",
            "edit_id": edit_id,
            "provenance": prov,
            "src": src,
            "dst": dst,
            "rel": rel,
            "existed": current is not None,
            "prev_cost": None if current is None else current.cost,
            "prev_support": None if current is None else current.support,
            "prev_edit_id": None if current is None else current.edit_id,
            "prev_prov": None if current is None else current.prov,
            "do_query": do_query,
            "do_parameters": do_parameters,
            "undo_query": undo_query,
            "undo_parameters": undo_parameters,
        }
        self._write_record(rec)
        try:
            self._write_edit_node(edit_id, do_query, undo_query, prov)
        except Exception as exc:
            print(f"edit_node_skipped {edit_id} {exc}", flush=True)

    def _write_record(self, rec: dict) -> None:
        with self.edit_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(rec) + "\n")
        self.applied += 1

    def _write_edit_node(self, edit_id: str, do_query: str, undo_query: str, prov: str) -> None:
        try:
            numeric = int(edit_id[1:])
        except ValueError:
            return
        self.hydra.query(
            "UNWIND $rows AS row MERGE (e {id: row.id}) "
            "SET e:Edit, e.edit_id = row.edit_id, e.do_cypher = row.do_cypher, "
            "e.undo_cypher = row.undo_cypher, e.provenance = row.provenance",
            {
                "rows": [
                    {
                        "id": numeric,
                        "edit_id": edit_id,
                        "do_cypher": do_query[:500],
                        "undo_cypher": undo_query[:500],
                        "provenance": prov[:300],
                    }
                ]
            },
        )

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
