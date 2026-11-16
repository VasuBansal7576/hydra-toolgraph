from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import json
import subprocess
import sys
from pathlib import Path

from replay import UPSTREAM_COMMIT, block_network, upstream_contacts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("upstream", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.upstream.resolve()
    actual = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    if (
        actual != UPSTREAM_COMMIT
        or subprocess.check_output(
            ["git", "-C", str(root), "status", "--porcelain"], text=True
        ).strip()
    ):
        raise ValueError("Expected unmodified pinned upstream checkout")
    import socket

    socket.create_connection = block_network
    socket.socket.connect = block_network
    socket.socket.connect_ex = block_network
    sys.path.insert(0, str(root))
    from tool_sandbox.common.execution_context import RoleType
    from tool_sandbox.common.utils import deterministic_uuid

    modules = [
        f"tool_sandbox.tools.{name}" for name in ["contact", "messaging", "setting"]
    ]
    catalog = []
    for module_name in modules:
        module = importlib.import_module(module_name)
        for name, tool in inspect.getmembers(module, inspect.isfunction):
            if (
                tool.__module__ != module_name
                or not getattr(tool, "is_tool", False)
                or RoleType.AGENT not in tool.visible_to
            ):
                continue
            signature = inspect.signature(tool)
            source = Path(inspect.getsourcefile(inspect.unwrap(tool)))
            catalog.append(
                {
                    "name": name,
                    "description": inspect.getdoc(tool) or "",
                    "signature": str(signature),
                    "parameters": [
                        {
                            "name": key,
                            "annotation": str(parameter.annotation),
                            "required": parameter.default is inspect.Parameter.empty,
                        }
                        for key, parameter in signature.parameters.items()
                    ],
                    "source_file": str(source.relative_to(root)),
                    "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                }
            )
    catalog.sort(key=lambda item: item["name"])
    contacts = upstream_contacts(root, deterministic_uuid)
    candidates = sorted(
        [
            item
            for item in contacts
            if not item["is_self"]
            and item["name"] not in {"Fredrik Thordendal", "John Petrucci"}
        ],
        key=lambda item: item["name"],
    )
    recipient = candidates[0]["name"]
    content = "Can we review the rehearsal schedule tomorrow?"
    manifest = {
        "protocol": "prospective-authored-transfer-v2",
        "pre_scoring_revision": "Adds budget 5 and cellular-on control before any comparison; prior manifest sha256 1617ccb9b36f7fcd977e2a32518d67ff6fc0ef6caf1705832f96531518909002",
        "status": "frozen before retrieval scoring",
        "upstream_commit": actual,
        "selection_rule": "Alphabetically first upstream non-self contact excluding both previously replayed recipients",
        "catalog_rule": "All agent-visible registered tools defined in contact, messaging and setting modules; imported aliases excluded; sorted by name",
        "tool_catalog": catalog,
        "task": {
            "recipient_name": recipient,
            "content": content,
            "query": f'Send {recipient} a message saying "{content}"',
            "initial_cellular": False,
            "initial_messages": 0,
        },
        "retrieval": {
            "budgets": [3, 5],
            "initial_cellular_states": [False, True],
            "query_access": "query only, not recipient phone or fixture database",
            "ranker": "existing BM25 over the same full catalog for all arms; no per-arm query rewriting or forced target seed",
            "arms": [
                "bm25_before_learning",
                "hydra_after_same_observed_episode",
                "local_closure_after_same_observed_episode",
            ],
            "training": "Only original successful Fredrik repair episode; failed calls excluded; no repetition or added traces",
            "no_tuning": "Freeze parameters, report full rankings and every arm; do not alter after outcome",
        },
        "execution": {
            "policy": "Scripted replay of observed successful operation order search_contacts, set_cellular_service_status, send_message_with_phone_number; only tools in selected bundle may run",
            "missing_lookup": "No phone supplied from hidden fixture; missing lookup prevents send",
            "missing_setter": "If lookup and send available but setter absent, real upstream send is attempted and can fail",
            "other_tools": "Not invoked by this restricted executor; count as unused tools",
            "setter_errors": "Fixed policy attempts setter when selected, records any exception, then attempts send if phone and send tool are available. Already-enabled setter may raise on cellular-on control; do not suppress the recorded error.",
            "success": "Exactly one new upstream message row to the named recipient with exact content; no external communication",
            "measurement": "No autonomous-agent or full ToolSandbox benchmark claim",
        },
        "limits": [
            "One prospectively frozen authored continuation, not a held-out benchmark dataset",
            "Recipient and tool catalog originate from previously inspected upstream fixtures",
            "The executor's operation order is scripted from the observed episode, not generated by Hydra",
            "Equal graph and local closure outcomes establish no graph quality advantage",
            "The upstream milestone graph is not used as training evidence",
        ],
    }
    args.output.write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        json.dumps(
            {
                "manifest": str(args.output),
                "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
                "catalog_size": len(catalog),
                "recipient": recipient,
                "scored": False,
            }
        )
    )


if __name__ == "__main__":
    main()
