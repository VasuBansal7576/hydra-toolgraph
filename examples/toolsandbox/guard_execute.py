from __future__ import annotations

import argparse
import hashlib
import json
import socket
import subprocess
import sys
from pathlib import Path

from replay import UPSTREAM_COMMIT, block_network, require, upstream_contacts

MANIFEST_SHA256 = "e8f5069a500c362f0a9a649a56c3f25ec1b9f266cc8e1d852c5401eeb62278b8"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("upstream", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    manifest_path = here / "guard_manifest.json"
    require(
        hashlib.sha256(manifest_path.read_bytes()).hexdigest() == MANIFEST_SHA256,
        "Guard protocol changed",
    )
    manifest = json.loads(manifest_path.read_text())
    root = args.upstream.resolve()
    actual = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    require(actual == UPSTREAM_COMMIT, "Wrong upstream commit")
    require(
        not subprocess.check_output(
            ["git", "-C", str(root), "status", "--porcelain"], text=True
        ).strip(),
        "Modified upstream checkout",
    )
    repo = here.parents[1]
    sys.path.insert(0, str(repo))
    from toolgraph.preconditions import Precondition, execute_guarded
    from toolgraph.data import Tool
    from toolgraph.graph import schema_token_count

    catalog_path = here / "transfer_manifest.json"
    require(
        hashlib.sha256(catalog_path.read_bytes()).hexdigest()
        == "1e507f0a6d627c0810c2ffa9ecac3d72e0c447eaa78fe1a06ce0ffb2a719d1d4",
        "Pinned catalog manifest changed",
    )
    catalog = json.loads(catalog_path.read_text())["tool_catalog"]
    allowed = manifest["loaded_tools_both_arms"]
    tools = [
        Tool(
            name=item["name"],
            description=item["description"],
            kind="regular",
            params=tuple(item["parameters"]),
            depends_on=(),
        )
        for item in catalog
        if item["name"] in allowed
    ]
    require(len(tools) == len(allowed), "Missing loaded tool schema")
    tokens = sum(schema_token_count(tool) for tool in tools)
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
    outcomes = []
    for arm in manifest["arms"]:
        context = ExecutionContext()
        set_current_context(context)
        context.add_to_database(DatabaseNamespace.CONTACT, contacts)

        def snapshot():
            return {
                "cellular": get_cellular_service_status(),
                "messages": context.get_database(
                    DatabaseNamespace.MESSAGING
                ).to_dicts(),
            }

        for task in manifest["tasks"]:
            environment = {
                "before": snapshot(),
                "action": task["environment_action_before"],
            }
            if task["environment_action_before"] == "disable_cellular":
                set_cellular_service_status(on=False)
            environment["after"] = snapshot()
            require(
                environment["after"]["cellular"] is task["expected_initial_cellular"],
                "Unexpected initial task state",
            )
            events = []
            guard_events = []

            def call(tool, **kwargs):
                require(tool.__name__ in allowed, "Attempted unloaded tool")
                before = snapshot()
                try:
                    result = tool(**kwargs)
                    event = {
                        "tool": tool.__name__,
                        "arguments": kwargs,
                        "result": result,
                        "status": "ok",
                    }
                except Exception as exc:
                    event = {
                        "tool": tool.__name__,
                        "arguments": kwargs,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "status": "error",
                    }
                    event.update(before=before, after=snapshot())
                    events.append(event)
                    raise
                event.update(before=before, after=snapshot())
                events.append(event)
                return result

            task_error = None
            try:
                lookup = call(search_contacts, name=task["recipient_name"])
                require(
                    len(lookup) == 1 and lookup[0]["name"] == task["recipient_name"],
                    "Recipient lookup ambiguous or incorrect",
                )
                phone = lookup[0]["phone_number"]

                def send():
                    return call(
                        send_message_with_phone_number,
                        phone_number=phone,
                        content=task["content"],
                    )

                if arm == "fixed_observed_order":
                    try:
                        call(set_cellular_service_status, on=True)
                    except Exception:
                        pass
                    send()
                else:
                    execute_guarded(
                        send,
                        [
                            Precondition(
                                "cellular_enabled",
                                check=lambda: call(get_cellular_service_status),
                                repair=lambda: call(
                                    set_cellular_service_status, on=True
                                ),
                            )
                        ],
                        emit=guard_events.append,
                    )
            except Exception as exc:
                task_error = {"type": type(exc).__name__, "message": str(exc)}
            final = snapshot()
            before_ids = {
                message["message_id"] for message in environment["after"]["messages"]
            }
            new_messages = [
                message
                for message in final["messages"]
                if message["message_id"] not in before_ids
            ]
            expected = next(
                contact
                for contact in contacts
                if contact["name"] == task["recipient_name"]
            )
            success = (
                len(new_messages) == 1
                and new_messages[0]["recipient_phone_number"]
                == expected["phone_number"]
                and new_messages[0]["content"] == task["content"]
            )
            outcomes.append(
                {
                    "arm": arm,
                    "task_id": task["id"],
                    "task": task,
                    "environment": environment,
                    "loaded_tools": allowed,
                    "loaded_schema_tokens": tokens,
                    "events": events,
                    "guard_events": guard_events,
                    "task_error": task_error,
                    "final_state": final,
                    "success": success,
                }
            )
    receipt = {
        "status": "completed",
        "label": "New explicitly authored live-state guard, actual upstream tools; original frozen comparison unchanged",
        "manifest_sha256": MANIFEST_SHA256,
        "upstream_commit": actual,
        "guard_source_sha256": hashlib.sha256(
            (repo / "toolgraph/preconditions.py").read_bytes()
        ).hexdigest(),
        "loaded_schema_tokens_each_arm": tokens,
        "outcomes": outcomes,
        "limits": manifest["limits"],
    }
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "tasks": len(outcomes),
                "successes": sum(item["success"] for item in outcomes),
                "tool_calls": {
                    arm: sum(
                        len(item["events"]) for item in outcomes if item["arm"] == arm
                    )
                    for arm in manifest["arms"]
                },
                "tool_errors": {
                    arm: sum(
                        event["status"] == "error"
                        for item in outcomes
                        if item["arm"] == arm
                        for event in item["events"]
                    )
                    for arm in manifest["arms"]
                },
            }
        )
    )


if __name__ == "__main__":
    main()
