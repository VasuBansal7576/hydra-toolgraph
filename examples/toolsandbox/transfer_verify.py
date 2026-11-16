from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from replay import require
from transfer_execute import MANIFEST_SHA256, validate_retrieval


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--retrieval", type=Path, required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).with_name("transfer_manifest.json"),
    )
    args = parser.parse_args()
    require(
        hashlib.sha256(args.manifest.read_bytes()).hexdigest() == MANIFEST_SHA256,
        "Manifest changed",
    )
    manifest = json.loads(args.manifest.read_text())
    receipt = json.loads(args.receipt.read_text())
    retrieval_bytes = args.retrieval.read_bytes()
    require(
        receipt["retrieval_receipt_sha256"]
        == hashlib.sha256(retrieval_bytes).hexdigest(),
        "Retrieval receipt hash mismatch",
    )
    retrieval = json.loads(retrieval_bytes)
    arms = validate_retrieval(manifest, retrieval)
    bundles = {(arm["name"], arm["budget"]): arm["ranked"] for arm in arms}
    require(receipt["manifest_sha256"] == MANIFEST_SHA256, "Wrong manifest in receipt")
    expected_cases = {
        (name, budget, initial)
        for name in manifest["retrieval"]["arms"]
        for budget in manifest["retrieval"]["budgets"]
        for initial in manifest["retrieval"]["initial_cellular_states"]
    }
    outcomes = receipt["outcomes"]
    require(len(outcomes) == len(expected_cases), "Incorrect number of outcomes")
    require(
        {(case["arm"], case["budget"], case["initial_cellular"]) for case in outcomes}
        == expected_cases,
        "Missing or repeated case",
    )
    for case in outcomes:
        require(
            case["initial_state"]["cellular"] is case["initial_cellular"],
            "Initial state mismatch",
        )
        require(case["initial_state"]["messages"] == [], "Context is not fresh")
        prior = case["initial_state"]
        selected = case["selected_tools"]
        require(
            selected == bundles[(case["arm"], case["budget"])],
            "Executed tools differ from retrieved bundle",
        )
        require(
            len(selected) <= case["budget"] and len(selected) == len(set(selected)),
            "Invalid bundle",
        )
        phone = None
        last_order = -1
        order = [
            "search_contacts",
            "set_cellular_service_status",
            "send_message_with_phone_number",
        ]
        for event in case["events"]:
            require(event["tool"] in selected, "Unloaded tool was executed")
            require(
                order.index(event["tool"]) > last_order, "Unexpected executor order"
            )
            last_order = order.index(event["tool"])
            require(event["before"] == prior, "Discontinuous state")
            if event["status"] == "error":
                require(event["before"] == event["after"], "Failed call changed state")
            if event["tool"] == "search_contacts" and event["status"] == "ok":
                require(
                    event["arguments"]["name"] == manifest["task"]["recipient_name"],
                    "Lookup targeted wrong recipient",
                )
                require(len(event["result"]) == 1, "Ambiguous lookup")
                require(
                    event["result"][0]["name"] == manifest["task"]["recipient_name"],
                    "Lookup returned wrong recipient",
                )
                phone = event["result"][0]["phone_number"]
            if event["tool"] == "send_message_with_phone_number":
                require(
                    phone is not None and event["arguments"]["phone_number"] == phone,
                    "Recipient did not come from permitted lookup",
                )
                require(
                    event["arguments"]["content"] == manifest["task"]["content"],
                    "Send content changed",
                )
            prior = event["after"]
        require(case["final_state"] == prior, "Final state does not match last call")
        messages = prior["messages"]
        actual_success = (
            phone is not None
            and len(messages) == 1
            and messages[0]["recipient_phone_number"] == phone
            and messages[0]["content"] == manifest["task"]["content"]
        )
        require(
            case["success"] is actual_success,
            "Reported success disagrees with raw state",
        )
    print(
        json.dumps(
            {
                "status": "passed",
                "verified_outcomes": len(outcomes),
                "verified_successes": sum(case["success"] for case in outcomes),
                "basis": "allowed calls, observed lookup, exact content, state continuity and final rows",
            }
        )
    )


if __name__ == "__main__":
    main()
