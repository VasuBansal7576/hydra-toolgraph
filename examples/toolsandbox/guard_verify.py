from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from guard_execute import MANIFEST_SHA256
from replay import UPSTREAM_COMMIT, require


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    manifest_path = here / "guard_manifest.json"
    require(
        hashlib.sha256(manifest_path.read_bytes()).hexdigest() == MANIFEST_SHA256,
        "Manifest changed",
    )
    manifest = json.loads(manifest_path.read_text())
    raw = args.receipt.read_bytes()
    receipt = json.loads(raw)
    require(receipt["manifest_sha256"] == MANIFEST_SHA256, "Wrong protocol")
    require(receipt["upstream_commit"] == UPSTREAM_COMMIT, "Wrong upstream revision")
    require(
        receipt["guard_source_sha256"]
        == hashlib.sha256(
            (here.parents[1] / "toolgraph/preconditions.py").read_bytes()
        ).hexdigest(),
        "Guard source differs from reviewed code",
    )
    expected = {
        (arm, task["id"]) for arm in manifest["arms"] for task in manifest["tasks"]
    }
    outcomes = receipt["outcomes"]
    require(
        len(outcomes) == len(expected)
        and {(case["arm"], case["task_id"]) for case in outcomes} == expected,
        "Missing or duplicate cases",
    )
    prior_by_arm = {}
    for case in outcomes:
        task = next(task for task in manifest["tasks"] if task["id"] == case["task_id"])
        require(case["task"] == task, "Task changed")
        require(
            case["loaded_tools"] == manifest["loaded_tools_both_arms"],
            "Catalog differs",
        )
        require(
            case["loaded_schema_tokens"] == receipt["loaded_schema_tokens_each_arm"],
            "Schema cost differs",
        )
        environment = case["environment"]
        require(
            environment["action"] == task["environment_action_before"],
            "Environment action changed",
        )
        if environment["action"] == "none":
            require(
                environment["before"] == environment["after"],
                "No-change environment step changed state",
            )
        if case["arm"] in prior_by_arm:
            require(
                environment["before"] == prior_by_arm[case["arm"]],
                "Task context continuity broken",
            )
        else:
            require(environment["before"]["messages"] == [], "Arm did not start fresh")
        require(
            environment["after"]["messages"] == environment["before"]["messages"],
            "Environment changed messages",
        )
        require(
            environment["after"]["cellular"] is task["expected_initial_cellular"],
            "Wrong initial state",
        )
        prior = environment["after"]
        phone = None
        last_getter = None
        for event in case["events"]:
            require(event["tool"] in case["loaded_tools"], "Unloaded tool invoked")
            require(event["before"] == prior, "Tool state continuity broken")
            if event["status"] == "error":
                require(event["before"] == event["after"], "Failed tool changed state")
            if event["tool"] == "search_contacts" and event["status"] == "ok":
                require(
                    event["arguments"]["name"] == task["recipient_name"],
                    "Wrong lookup recipient",
                )
                require(
                    len(event["result"]) == 1
                    and event["result"][0]["name"] == task["recipient_name"],
                    "Wrong lookup result",
                )
                phone = event["result"][0]["phone_number"]
            if (
                event["tool"] == "get_cellular_service_status"
                and event["status"] == "ok"
            ):
                require(
                    event["result"] is event["before"]["cellular"],
                    "Getter differs from state",
                )
                last_getter = event["result"]
            if event["tool"] == "send_message_with_phone_number":
                require(
                    phone is not None and event["arguments"]["phone_number"] == phone,
                    "Send used stale or injected phone",
                )
                require(
                    event["arguments"]["content"] == task["content"],
                    "Wrong message content",
                )
                if case["arm"] == "authored_fresh_state_guard":
                    require(last_getter is True, "Guarded send lacks fresh true check")
            prior = event["after"]
        require(prior == case["final_state"], "Final state differs")
        before_ids = {
            message["message_id"] for message in environment["after"]["messages"]
        }
        created = [
            message
            for message in prior["messages"]
            if message["message_id"] not in before_ids
        ]
        success = (
            phone is not None
            and len(created) == 1
            and created[0]["recipient_phone_number"] == phone
            and created[0]["content"] == task["content"]
        )
        require(case["success"] is success, "Reported success differs from state")
        names = [event["tool"] for event in case["events"]]
        if case["arm"] == "fixed_observed_order":
            require(
                names
                == [
                    "search_contacts",
                    "set_cellular_service_status",
                    "send_message_with_phone_number",
                ],
                "Fixed executor did not follow frozen policy",
            )
        if case["arm"] == "authored_fresh_state_guard":
            expected_names = ["search_contacts", "get_cellular_service_status"]
            if not task["expected_initial_cellular"]:
                expected_names += [
                    "set_cellular_service_status",
                    "get_cellular_service_status",
                ]
            expected_names += ["send_message_with_phone_number"]
            require(names == expected_names, "Guard did not follow frozen policy")
        prior_by_arm[case["arm"]] = prior
    summary = {
        "label": "Actual upstream execution of a new explicitly authored state guard, not learned preconditions or a benchmark",
        "verification": "passed",
        "manifest_sha256": MANIFEST_SHA256,
        "source_receipt_sha256": hashlib.sha256(raw).hexdigest(),
        "guard_source_sha256": receipt["guard_source_sha256"],
        "loaded_schema_tokens_each_arm": receipt["loaded_schema_tokens_each_arm"],
        "loaded_tools_both_arms": manifest["loaded_tools_both_arms"],
        "outcomes": [
            {
                "arm": case["arm"],
                "task_id": case["task_id"],
                "initial_cellular": case["environment"]["after"]["cellular"],
                "success": case["success"],
                "messages_created": len(case["final_state"]["messages"])
                - len(case["environment"]["after"]["messages"]),
                "calls": [
                    {
                        "tool": event["tool"],
                        "status": event["status"],
                        "error_type": event.get("error_type"),
                        "before_cellular": event["before"]["cellular"],
                        "after_cellular": event["after"]["cellular"],
                    }
                    for event in case["events"]
                ],
            }
            for case in outcomes
        ],
        "limits": manifest["limits"],
    }
    if args.summary:
        args.summary.write_text(json.dumps(summary, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": "passed",
                "verified_cases": len(outcomes),
                "verified_successes": sum(case["success"] for case in outcomes),
            }
        )
    )


if __name__ == "__main__":
    main()
