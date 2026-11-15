"""Runtime tool loader used by the MCP server and the web demo."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from toolgraph.data import Instance, Tool, load_dataset
from toolgraph.graph import GraphStore, rank_costs
from toolgraph.hydra import HydraClient
from toolgraph.learn import ingest_trace_edges
from toolgraph.protocol import FUSION_POOL, FUSION_SEEDS, MAX_LEN
from toolgraph.retrieve import Bm25Index, tokenize

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RESULTS = ROOT / "results"


@dataclass
class LoaderState:
    tools: dict[str, Tool]
    tool_list: list[Tool]
    store: GraphStore
    bm25: Bm25Index
    catalog_tokens: int = 0
    last_batch_edits: int = 0
    history: list[dict] = field(default_factory=list)


def build_loader(
    hydra_url: str = "http://127.0.0.1:8443",
    token: str = "local-development-token-32-bytes",
) -> LoaderState:
    tools, _instances = load_dataset(DATA)
    by_name = {tool.name: tool for tool in tools}
    hydra = HydraClient(hydra_url, token)
    store = GraphStore(hydra, RESULTS / "runtime_edits.jsonl")
    store.load_catalog(tools, include_oracle=False)
    # Warm from the latest curve edits if present (learned DEPENDS_ON only).
    curve_edits = RESULTS / "edits.jsonl"
    if curve_edits.exists():
        for line in curve_edits.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("kind") != "edge" or rec.get("rel") != "DEPENDS_ON":
                continue
            src = store.by_id.get(int(rec["src"]))
            dst = store.by_id.get(int(rec["dst"]))
            if src and dst:
                store.learn_depends(src, dst, rec.get("provenance") or "runtime_replay")
        store.sync_learned_to_hydra()
    names = [tool.name for tool in tools]
    bm25 = Bm25Index(names, [tokenize(tool.base_text()) for tool in tools])
    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        catalog_tokens = sum(len(enc.encode(tool.base_text())) for tool in tools)
    except Exception:
        catalog_tokens = 0
    return LoaderState(
        tools=by_name,
        tool_list=tools,
        store=store,
        bm25=bm25,
        catalog_tokens=catalog_tokens,
    )


def load_tools(state: LoaderState, task: str, k: int = 16) -> dict:
    started = time.perf_counter()
    fused = state.bm25.top(task, FUSION_POOL)
    seeds = fused[:FUSION_SEEDS]
    costs = state.store.closure(seeds, ["DEPENDS_ON", "SCHEMA_DEP"])
    ranked = rank_costs(costs)
    for name in fused:
        if name not in costs:
            ranked.append(name)
    loaded = ranked[:k]
    paths = state.store.reason_paths(seeds, loaded, ["DEPENDS_ON", "SCHEMA_DEP"])
    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        tokens = sum(
            len(enc.encode(state.tools[name].base_text()))
            for name in loaded
            if name in state.tools
        )
    except Exception:
        tokens = 0
    latency_ms = (time.perf_counter() - started) * 1000.0
    payload = {
        "task": task,
        "k": k,
        "seeds": seeds,
        "tools": [
            {
                "name": name,
                "description": state.tools[name].description if name in state.tools else "",
                "reason_path": paths.get(name, [name]),
            }
            for name in loaded
        ],
        "tokens_loaded": tokens,
        "catalog_tokens": state.catalog_tokens,
        "tokens_saved": max(0, state.catalog_tokens - tokens),
        "latency_ms": latency_ms,
        "max_len": MAX_LEN,
    }
    state.history.append({"op": "load_tools", **payload})
    return payload


def record_trace(state: LoaderState, task: str, tools_used: list[str]) -> dict:
    if not tools_used:
        return {"ok": False, "error": "tools_used is empty"}
    main = tools_used[0]
    gold = frozenset(tools_used)
    query = Instance(idx=-1, text=task, main=main, gold=gold)
    before = state.store.applied
    touched = ingest_trace_edges(state.store, query)
    state.store.sync_learned_to_hydra()
    edited = state.store.applied - before
    state.last_batch_edits = edited
    payload = {
        "ok": True,
        "main": main,
        "helpers": sorted(gold - {main}),
        "edges_touched": touched,
        "edits": edited,
    }
    state.history.append({"op": "record_trace", "task": task, **payload})
    return payload


def undo_last(state: LoaderState) -> dict:
    n = state.last_batch_edits or 1
    undone = state.store.undo_last_batch(n)
    state.last_batch_edits = 0
    return {"ok": True, "undone": undone}
