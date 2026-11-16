from __future__ import annotations

import argparse
import hashlib
import json
import socket
import subprocess
import sys
from pathlib import Path

from replay import UPSTREAM_COMMIT, block_network, require, upstream_contacts

MANIFEST_SHA256 = "1e507f0a6d627c0810c2ffa9ecac3d72e0c447eaa78fe1a06ce0ffb2a719d1d4"


TRAINING_RECEIPT_SHA256 = (
    "1c6dbde9d65e71f256d6141f73565d6fac5043712680e17d3f04bd82fcafba17"
)


def validate_retrieval(manifest: dict, retrieval: dict) -> list[dict]:
    require(
        retrieval["manifest_sha256"] == MANIFEST_SHA256,
        "Retrieval used a different protocol",
    )
    require(
        retrieval["training_receipt_sha256"] == TRAINING_RECEIPT_SHA256,
        "Training receipt changed",
    )
    require(retrieval["query"] == manifest["task"]["query"], "Retrieval query changed")
    require(
        retrieval["catalog_size"] == len(manifest["tool_catalog"]),
        "Retrieval catalog size changed",
    )
    allowed = {tool["name"] for tool in manifest["tool_catalog"]}
    expected_pairs = {
        (name, budget)
        for name in manifest["retrieval"]["arms"]
        for budget in manifest["retrieval"]["budgets"]
    }
    arms = retrieval["arms"]
    require(len(arms) == len(expected_pairs), "Incorrect number of arms")
    require(
        {(arm["name"], arm["budget"]) for arm in arms} == expected_pairs,
        "Missing or duplicated arm/budget",
    )
    for arm in arms:
        require(
            len(arm["ranked"]) == len(set(arm["ranked"])), "Duplicate selected tool"
        )
        require(len(arm["ranked"]) <= arm["budget"], "Bundle exceeds budget")
        require(set(arm["ranked"]) <= allowed, "Unknown selected tool")
    for arm in arms:
        require(
            arm["ranked"] == retrieval["full_rankings"][arm["name"]][: arm["budget"]],
            "Selected bundle does not match full ranking",
        )
    return arms


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("upstream", type=Path)
    parser.add_argument("retrieval", type=Path)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).with_name("transfer_manifest.json"),
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest_bytes = args.manifest.read_bytes()
    require(
        hashlib.sha256(manifest_bytes).hexdigest() == MANIFEST_SHA256,
        "Frozen manifest changed",
    )
    manifest = json.loads(manifest_bytes)
    retrieval = json.loads(args.retrieval.read_text())
    require(
        retrieval["manifest_sha256"] == MANIFEST_SHA256,
        "Retrieval used a different protocol",
    )
    root = args.upstream.resolve()
    actual = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    require(actual == UPSTREAM_COMMIT, "Different upstream source")
    require(
        not subprocess.check_output(
            ["git", "-C", str(root), "status", "--porcelain"], text=True
        ).strip(),
        "Modified upstream source",
    )
    for tool in manifest["tool_catalog"]:
        require(
            hashlib.sha256((root / tool["source_file"]).read_bytes()).hexdigest()
            == tool["source_sha256"],
            "Catalog source mismatch",
        )
    arms = validate_retrieval(manifest, retrieval)
    sys.path.insert(0, str(root))
    socket.create_connection = block_network
    socket.socket.connect = block_network
    socket.socket.connect_ex = block_network
    from tool_sandbox.common.execution_context import (
        DatabaseNamespace,
        ExecutionContext,
        set_current_context,
    )
    from tool_sandbox.common.utils import deterministic_uuid
    from tool_sandbox.tools.contact import search_contacts
    from tool_sandbox.tools.messaging import send_message_with_phone_number
    from tool_sandbox.tools.setting import (
        get_cellular_service_status,
        set_cellular_service_status,
    )

    contacts = upstream_contacts(root, deterministic_uuid)
    task = manifest["task"]
    expected = next(item for item in contacts if item["name"] == task["recipient_name"])
    outcomes = []
    for arm in arms:
        for initial_cellular in manifest["retrieval"]["initial_cellular_states"]:
            context = ExecutionContext()
            set_current_context(context)
            context.add_to_database(DatabaseNamespace.CONTACT, contacts)
            if not initial_cellular:
                set_cellular_service_status(on=False)
            events = []
            selected = set(arm["ranked"])

            def snapshot():
                return {
                    "cellular": get_cellular_service_status(),
                    "messages": context.get_database(
                        DatabaseNamespace.MESSAGING
                    ).to_dicts(),
                }

            def call(tool, **kwargs):
                require(
                    tool.__name__ in selected, "Executor attempted an unloaded tool"
                )
                before = snapshot()
                try:
                    result = tool(**kwargs)
                    event = {
                        "tool": tool.__name__,
                        "arguments": kwargs,
                        "status": "ok",
                        "result": result,
                    }
                except Exception as exc:
                    event = {
                        "tool": tool.__name__,
                        "arguments": kwargs,
                        "status": "error",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                event.update(before=before, after=snapshot())
                events.append(event)
                return event

            initial = snapshot()
            phone = None
            blocked = []
            if "search_contacts" in selected:
                lookup = call(search_contacts, name=task["recipient_name"])
                if lookup["status"] == "ok" and len(lookup["result"]) == 1:
                    phone = lookup["result"][0]["phone_number"]
                else:
                    blocked.append(
                        "Lookup failed or did not return exactly one contact"
                    )
            else:
                blocked.append(
                    "Contact lookup tool was not loaded; no recipient phone injected"
                )
            if "set_cellular_service_status" in selected:
                call(set_cellular_service_status, on=True)
            if "send_message_with_phone_number" not in selected:
                blocked.append("Send tool was not loaded")
            elif phone is not None:
                call(
                    send_message_with_phone_number,
                    phone_number=phone,
                    content=task["content"],
                )
            final = snapshot()
            messages = final["messages"]
            success = (
                len(messages) == 1
                and messages[0]["recipient_phone_number"] == expected["phone_number"]
                and messages[0]["content"] == task["content"]
            )
            outcomes.append(
                {
                    "arm": arm["name"],
                    "budget": arm["budget"],
                    "initial_cellular": initial_cellular,
                    "selected_tools": arm["ranked"],
                    "success": success,
                    "blocked": blocked,
                    "unused_tools": sorted(
                        selected - {event["tool"] for event in events}
                    ),
                    "initial_state": initial,
                    "final_state": final,
                    "events": events,
                }
            )
    result = {
        "status": "completed",
        "label": "Prospectively frozen authored continuation using actual upstream tools and a fixed scripted executor; no real SMS or autonomous-agent score",
        "manifest_sha256": MANIFEST_SHA256,
        "retrieval_receipt_sha256": hashlib.sha256(
            args.retrieval.read_bytes()
        ).hexdigest(),
        "upstream_commit": actual,
        "outcomes": outcomes,
        "limits": manifest["limits"],
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "outcomes": len(outcomes),
                "successes": sum(item["success"] for item in outcomes),
                "manifest_sha256": MANIFEST_SHA256,
            }
        )
    )


if __name__ == "__main__":
    main()
