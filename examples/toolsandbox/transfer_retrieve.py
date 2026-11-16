"""Frozen prospective continuation retrieval with matched graph/local evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from examples.toolsandbox.hydra_replay import (
    assert_catalog_footprint_clear,
    successful_repair_episode,
)
from toolgraph.data import Tool
from toolgraph.graph import (
    GraphStore,
    LoadToolsResult,
    positive_trace_provenance,
    schema_token_count,
)
from toolgraph.hydra import HydraClient
from toolgraph.service import FusionIndex, ToolgraphService

MANIFEST_SHA256 = "1e507f0a6d627c0810c2ffa9ecac3d72e0c447eaa78fe1a06ce0ffb2a719d1d4"

TRAINING_SHA256 = "1c6dbde9d65e71f256d6141f73565d6fac5043712680e17d3f04bd82fcafba17"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).with_name("transfer_manifest.json"),
    )
    parser.add_argument(
        "--training-receipt",
        type=Path,
        default=Path(__file__).with_name("receipt.json"),
    )
    parser.add_argument("--hydra-url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    manifest_bytes = args.manifest.read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != MANIFEST_SHA256:
        raise ValueError("Frozen manifest changed; comparison will not run")
    manifest = json.loads(manifest_bytes)
    training_bytes = args.training_receipt.read_bytes()
    if hashlib.sha256(training_bytes).hexdigest() != TRAINING_SHA256:
        raise ValueError("The frozen original training receipt changed")
    used = successful_repair_episode(json.loads(training_bytes))
    if urlsplit(args.hydra_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Use an exclusively owned fresh loopback Hydra instance")
    tools = [
        Tool(
            name=row["name"],
            description=row["description"],
            kind="external_upstream",
            params=tuple(row["parameters"]),
            depends_on=(),
        )
        for row in manifest["tool_catalog"]
    ]
    by_name = {tool.name: tool for tool in tools}
    if len(by_name) != len(tools) or not set(used).issubset(by_name):
        raise ValueError(
            "Frozen catalog is inconsistent with the observed training episode"
        )
    client = HydraClient(args.hydra_url, os.environ["HYDRA_TOKEN"])
    checked = assert_catalog_footprint_clear(client, tools)
    fusion = FusionIndex(tools, mode="bm25")
    seeds = fusion.fuse(manifest["task"]["query"])
    if not seeds:
        raise RuntimeError(
            "Lexical retrieval returned no seeds; no target will be injected"
        )
    with tempfile.TemporaryDirectory() as temporary:
        store = GraphStore(client, Path(temporary) / "edits.jsonl")
        store.load_catalog(tools, include_oracle=False)
        store.tau = 0.15
        service = ToolgraphService.__new__(ToolgraphService)
        service.store = store
        before = store.load_tools(seeds, None, len(tools))
        if before.tools != seeds:
            raise AssertionError(
                "Fresh graph must preserve lexical seed ranking before learning"
            )
        store.record_trace(None, "Original observed Fredrik repair episode", used, [])
        store.ensure_hydra_synced()
        started = time.perf_counter()
        raw = store.load_tools(seeds, None, len(tools))
        hybrid = service._merge_memory_expansion(
            raw, seeds, seeds, len(tools), evidence_scoped=True
        )
        hybrid_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        local = service._merge_memory_expansion(
            LoadToolsResult(tools=[], costs={}, why={}, skipped={}),
            seeds,
            seeds,
            len(tools),
            evidence_scoped=True,
        )
        local_ms = (time.perf_counter() - started) * 1000
        rankings = {
            "bm25_before_learning": seeds,
            "hydra_after_same_observed_episode": hybrid.tools,
            "local_closure_after_same_observed_episode": local.tools,
        }
        arms = []
        for name in manifest["retrieval"]["arms"]:
            for budget in manifest["retrieval"]["budgets"]:
                ranked = rankings[name][:budget]
                arms.append(
                    {
                        "name": name,
                        "budget": budget,
                        "ranked": ranked,
                        "schema_tokens": sum(
                            schema_token_count(by_name[tool]) for tool in ranked
                        ),
                    }
                )
        report = {
            "status": "completed",
            "label": "Frozen prospective authored ToolSandbox continuation; retrieval only, execution measured separately",
            "manifest_sha256": MANIFEST_SHA256,
            "training_receipt_sha256": hashlib.sha256(training_bytes).hexdigest(),
            "query": manifest["task"]["query"],
            "catalog_size": len(tools),
            "isolation": {
                "mutation_ids_checked": checked,
                "global_emptiness_verified": False,
                "requires_exclusive_fresh_instance": True,
            },
            "training_event_indices": [0, 2, 3],
            "full_rankings": rankings,
            "actual_hydra_before_learning": before.tools,
            "actual_hydra_after_learning": raw.tools,
            "same_evidence_hybrid_equals_local": hybrid.tools == local.tools,
            "timing_note": "Single warm observations, not a latency benchmark; graph timing includes backend query",
            "hybrid_observation_ms": hybrid_ms,
            "local_observation_ms": local_ms,
            "arms": arms,
            "learned_edges": [
                {
                    "source": store.by_id[edge.src],
                    "target": store.by_id[edge.dst],
                    "latest_positive_observation": positive_trace_provenance(edge),
                    "observations": edge.n_obs,
                    "weight": edge.p,
                }
                for edge in store.active_edges(["REQUIRES"])
            ],
            "limits": manifest["limits"]
            + [
                "Retrieval is given only the frozen query, not the contact phone or hidden cellular state",
                "Equal tool budgets do not imply equal token budgets; schema tokens are reported",
                "No target seed was forced, and no outcome-based tuning was performed",
                "This receipt does not establish executable success; use transfer_execute.py on these exact bundles",
            ],
        }
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
