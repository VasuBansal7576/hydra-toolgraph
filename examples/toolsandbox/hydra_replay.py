"""Replay an observed ToolSandbox episode into a fresh self-hosted Hydra graph."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from toolgraph.data import Tool
from toolgraph.graph import GraphStore, LoadToolsResult, positive_trace_provenance
from toolgraph.hydra import HydraClient, unwrap
from toolgraph.service import ToolgraphService


UPSTREAM_COMMIT = "c8571d7854316d2e1c5f288e59fe1e34e53f6dd1"
TARGET = "send_message_with_phone_number"
EXPECTED_USED = {"search_contacts", "set_cellular_service_status", TARGET}


def successful_repair_episode(receipt: dict) -> list[str]:
    if (
        receipt.get("status") != "passed"
        or receipt.get("upstream", {}).get("commit") != UPSTREAM_COMMIT
    ):
        raise ValueError(
            "Expected a passing receipt from the pinned ToolSandbox revision"
        )
    events = receipt.get("events", [])
    if len(events) < 4:
        raise ValueError("Repair episode is incomplete")
    first, failure, repair, success = events[:4]
    if [event.get("tool") for event in events[:4]] != [
        "search_contacts",
        TARGET,
        "set_cellular_service_status",
        TARGET,
    ]:
        raise ValueError("Unexpected repair sequence")
    if (
        failure.get("status") != "error"
        or failure.get("error_type") != "ConnectionError"
    ):
        raise ValueError("Expected observed upstream cellular failure")
    if any(event.get("status") != "ok" for event in (first, repair, success)):
        raise ValueError("Only successful observed calls can form the positive episode")
    if first["before"]["cellular"] is not False:
        raise ValueError("The observed episode must begin with cellular disabled")
    if first["after"] != failure["before"] or failure["before"] != failure["after"]:
        raise ValueError(
            "Failure must follow the lookup without changing sandbox state"
        )
    if failure["arguments"] != success["arguments"]:
        raise ValueError(
            "The successful retry must request exactly the same simulated message"
        )
    if (
        repair.get("arguments", {}).get("on") is not True
        or repair["after"]["cellular"] is not True
    ):
        raise ValueError("Observed repair did not enable cellular")
    if (
        not first.get("result")
        or success["arguments"]["phone_number"] != first["result"][0]["phone_number"]
    ):
        raise ValueError(
            "Successful destination was not obtained from the observed contact lookup"
        )
    if failure["after"] != repair["before"] or repair["after"] != success["before"]:
        raise ValueError("Repair receipt state transitions are discontinuous")
    messages = success["after"]["messages"]
    if (
        len(messages) != 1
        or messages[0]["recipient_phone_number"] != success["arguments"]["phone_number"]
    ):
        raise ValueError(
            "Observed successful send did not reach the requested simulated recipient"
        )
    if messages[0]["content"] != success["arguments"]["content"]:
        raise ValueError("Observed send content differs")
    used = [first["tool"], repair["tool"], success["tool"]]
    if set(used) != EXPECTED_USED:
        raise ValueError("Unexpected successful used-tool set")
    return used


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--hydra-url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    receipt_bytes = args.receipt.read_bytes()
    receipt = json.loads(receipt_bytes)
    used = successful_repair_episode(receipt)
    endpoint = urlsplit(args.hydra_url)
    if endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(
            "This destructive setup proof requires a dedicated loopback Hydra instance"
        )
    client = HydraClient(args.hydra_url, os.environ["HYDRA_TOKEN"])
    count_result = client.query("MATCH (n) RETURN count(n)")
    rows = count_result.get("rows") or []
    if len(rows) != 1 or int(unwrap(rows[0][0])) != 0:
        raise RuntimeError(
            "Use an exclusively owned fresh Hydra instance with no nodes of any label"
        )
    catalog = {row["name"]: row for row in receipt.get("tool_catalog", [])}
    missing = EXPECTED_USED - set(catalog)
    if missing:
        raise ValueError(
            f"Receipt lacks upstream tool catalog entries: {sorted(missing)}"
        )
    tools = [
        Tool(
            name=name,
            description=catalog[name]["description"],
            kind="external_upstream",
            params=tuple(catalog[name].get("parameters", [])),
            depends_on=(),
        )
        for name in sorted(EXPECTED_USED)
    ]
    with tempfile.TemporaryDirectory() as temporary:
        store = GraphStore(client, Path(temporary) / "edits.jsonl")
        store.load_catalog(tools, include_oracle=False)
        store.tau = 0.15
        service = ToolgraphService.__new__(ToolgraphService)
        service.store = store
        seeds = [TARGET]
        before = store.load_tools(seeds, None, 3)
        if before.tools != seeds:
            raise AssertionError("Fresh graph unexpectedly supplied helpers")
        store.record_trace(None, "Observed ToolSandbox cellular repair", used, [])
        store.ensure_hydra_synced()
        raw_after = store.load_tools(seeds, None, 3)
        after = service._merge_memory_expansion(
            raw_after, seeds, seeds, 3, evidence_scoped=True
        )
        local = service._merge_memory_expansion(
            LoadToolsResult(tools=[], costs={}, why={}, skipped={}),
            seeds,
            seeds,
            3,
            evidence_scoped=True,
        )
        if set(after.tools) != EXPECTED_USED or after.tools != local.tools:
            raise AssertionError(
                "Learned replay and matched local control disagree with the observed used-tool set"
            )
        if not EXPECTED_USED.issubset(raw_after.tools):
            raise AssertionError(
                "Genuine Hydra paths did not retrieve the observed helpers"
            )
        edges = [
            {
                "source": store.by_id[edge.src],
                "target": store.by_id[edge.dst],
                "latest_update": edge.prov,
                "latest_positive_observation": positive_trace_provenance(edge),
                "weight": edge.p,
                "observations": edge.n_obs,
            }
            for edge in store.active_edges(["REQUIRES"])
        ]
        report = {
            "status": "passed",
            "label": "Actual ToolSandbox replay receipt followed by genuine self-hosted Hydra trace retrieval",
            "source_receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            "upstream_commit": UPSTREAM_COMMIT,
            "episode_event_indices": [0, 2, 3],
            "failed_event_indices_excluded": [1],
            "fixed_seed": TARGET,
            "k": 3,
            "before": before.tools,
            "raw_hydra_after": raw_after.tools,
            "hybrid_after": after.tools,
            "matched_local_after": local.tools,
            "observed_positive_edges": edges,
            "checks": {
                "fresh_graph_initially_returns_only_seed": True,
                "actual_hydra_returns_observed_helpers_after_learning": True,
                "hybrid_matches_same_evidence_local_control": True,
                "failed_call_not_counted_as_positive_observation": all(
                    edge["observations"] == 1 for edge in edges
                ),
            },
            "limits": [
                "The ToolSandbox execution is scripted and simulated; no real SMS or autonomous-agent score",
                "This script reads the upstream execution receipt; it does not execute SMS tools",
                "The seed and k are fixed, not discovered by a planner",
                "This is same-episode replay, not held-out generalization",
                "Learned edges are co-occurrence associations, not verified causal prerequisites",
                "The actual loaded-tool set was not measured; an empty list avoids fabricated negative feedback",
                "Matched local closure ties Hydra; this demonstrates trace integration, not graph superiority",
                "The recipient and cellular-invalidation checks belong to the source receipt, not a new graph-controlled execution",
            ],
        }
        if not all(report["checks"].values()):
            raise AssertionError("Proof checks failed")
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
