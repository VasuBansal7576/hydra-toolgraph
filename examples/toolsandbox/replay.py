from __future__ import annotations

import argparse
import ast
import hashlib
import inspect
from importlib.metadata import version
import json
import socket
import subprocess
import sys
from pathlib import Path

UPSTREAM_COMMIT = "c8571d7854316d2e1c5f288e59fe1e34e53f6dd1"
SCENARIO = "send_message_with_contact_content_cellular_off"


def upstream_contacts(root: Path, deterministic_uuid):
    tree = ast.parse((root / "tool_sandbox/scenarios/base_scenarios.py").read_text())
    for value in ast.walk(tree):
        if not isinstance(value, ast.Call):
            continue
        keywords = {item.arg: item.value for item in value.keywords}
        namespace = keywords.get("namespace")
        if isinstance(namespace, ast.Attribute) and namespace.attr == "CONTACT":
            expression = ast.Expression(keywords["rows"])
            return eval(
                compile(expression, "upstream-contact-fixture", "eval"),
                {
                    "__builtins__": {},
                    "deterministic_uuid": deterministic_uuid,
                },
            )
    raise RuntimeError("Upstream contact fixture not found")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def block_network(*args, **kwargs):
    raise RuntimeError("Network is disabled during this sandbox replay")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("upstream", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.upstream.resolve()
    actual = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    if actual != UPSTREAM_COMMIT:
        raise RuntimeError(f"Expected upstream {UPSTREAM_COMMIT}, got {actual}")
    dirty = subprocess.check_output(
        ["git", "-C", str(root), "status", "--porcelain"], text=True
    ).strip()
    if dirty:
        raise RuntimeError("Upstream checkout must be unmodified")
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

    tools = [
        search_contacts,
        send_message_with_phone_number,
        set_cellular_service_status,
        get_cellular_service_status,
    ]
    tool_catalog = []
    for tool in tools:
        signature = inspect.signature(tool)
        tool_catalog.append(
            {
                "name": tool.__name__,
                "description": inspect.getdoc(tool) or "",
                "signature": str(signature),
                "parameters": [
                    {
                        "name": name,
                        "annotation": str(parameter.annotation),
                        "required": parameter.default is inspect.Parameter.empty,
                    }
                    for name, parameter in signature.parameters.items()
                ],
                "source_file": str(
                    Path(inspect.getsourcefile(inspect.unwrap(tool))).relative_to(root)
                ),
            }
        )

    contacts = upstream_contacts(root, deterministic_uuid)
    events = []

    def fresh_context():
        context = ExecutionContext()
        set_current_context(context)
        context.add_to_database(DatabaseNamespace.CONTACT, contacts)
        set_cellular_service_status(on=False)
        return context

    def capture(context):
        return {
            "cellular": get_cellular_service_status(),
            "messages": context.get_database(DatabaseNamespace.MESSAGING).to_dicts(),
        }

    def call(context, tool, **kwargs):
        before = capture(context)
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
                "error": str(exc),
                "error_type": type(exc).__name__,
                "status": "error",
            }
        event.update(before=before, after=capture(context))
        events.append(event)
        return event

    content = "How's the new album coming along."
    context = fresh_context()
    lookup = call(context, search_contacts, name="Fredrik Thordendal")
    require(
        lookup["status"] == "ok" and len(lookup["result"]) == 1,
        'Proof invariant failed: lookup["status"] == "ok" and len(lookup["result"]) == 1',
    )
    phone = lookup["result"][0]["phone_number"]
    failed = call(
        context, send_message_with_phone_number, phone_number=phone, content=content
    )
    require(
        failed["error_type"] == "ConnectionError",
        'Proof invariant failed: failed["error_type"] == "ConnectionError"',
    )
    require(
        failed["after"]["messages"] == failed["before"]["messages"],
        'Proof invariant failed: failed["after"]["messages"] == failed["before"]["messages"]',
    )
    enabled = call(context, set_cellular_service_status, on=True)
    require(
        enabled["after"]["cellular"] is True,
        'Proof invariant failed: enabled["after"]["cellular"] is True',
    )
    sent = call(
        context, send_message_with_phone_number, phone_number=phone, content=content
    )
    require(sent["status"] == "ok", 'Proof invariant failed: sent["status"] == "ok"')
    messages = sent["after"]["messages"]
    require(
        len(messages) == 1 and messages[0]["recipient_phone_number"] == phone,
        'Proof invariant failed: len(messages) == 1 and messages[0]["recipient_phone_number"] == phone',
    )
    require(
        messages[0]["content"] == content,
        'Proof invariant failed: messages[0]["content"] == content',
    )
    second_lookup = call(context, search_contacts, name="John Petrucci")
    second_phone = second_lookup["result"][0]["phone_number"]
    require(second_phone != phone, "Proof invariant failed: second_phone != phone")
    changed_recipient = call(
        context,
        send_message_with_phone_number,
        phone_number=second_phone,
        content="Rehearsal tomorrow?",
    )
    require(
        changed_recipient["status"] == "ok",
        'Proof invariant failed: changed_recipient["status"] == "ok"',
    )
    final = changed_recipient["after"]["messages"]
    require(
        len(final) == 2 and final[-1]["recipient_phone_number"] == second_phone,
        'Proof invariant failed: len(final) == 2 and final[-1]["recipient_phone_number"] == second_phone',
    )
    reblocked = call(context, set_cellular_service_status, on=False)
    require(
        reblocked["after"]["cellular"] is False,
        'Proof invariant failed: reblocked["after"]["cellular"] is False',
    )
    invalidated = call(
        context,
        send_message_with_phone_number,
        phone_number=second_phone,
        content="One more question",
    )
    require(
        invalidated["error_type"] == "ConnectionError",
        'Proof invariant failed: invalidated["error_type"] == "ConnectionError"',
    )
    require(
        len(invalidated["after"]["messages"]) == 2,
        'Proof invariant failed: len(invalidated["after"]["messages"]) == 2',
    )

    files = [
        "tool_sandbox/tools/messaging.py",
        "tool_sandbox/tools/contact.py",
        "tool_sandbox/tools/setting.py",
        "tool_sandbox/scenarios/base_scenarios.py",
        "tool_sandbox/scenarios/multiple_tool_call_scenarios.py",
    ]
    receipt = {
        "status": "passed",
        "label": "Deterministic upstream ToolSandbox tool replay; no real SMS, no agent benchmark score",
        "upstream": {
            "repository": "https://github.com/apple-aiml-research/ToolSandbox",
            "commit": actual,
            "scenario": SCENARIO,
            "file_sha256": {
                name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                for name in files
            },
        },
        "fixture": "Unmodified upstream contact fixture; fresh upstream ExecutionContext; cellular initially disabled; no prior messages",
        "network": "Python socket connections blocked before upstream imports",
        "checks": {
            "actual_upstream_failure": True,
            "failure_has_no_message_side_effect": True,
            "state_repair_enables_actual_upstream_send": True,
            "correct_recipient_and_content": True,
            "changed_recipient_uses_new_lookup": True,
            "later_cellular_change_invalidates_prior_success": True,
        },
        "limits": [
            "Scripted calls, not an autonomous agent",
            "No ToolGraph or Hydra integration in this receipt",
            "No full upstream scenario evaluator or benchmark score",
            "The contact fixture and tool implementations are upstream; the replay harness and additional recipient/invalidation checks are ours",
            "ccy 1.4.0 replaces unavailable upstream pin 1.3.1; no currency function is invoked",
        ],
        "dependencies": {
            line.split("==")[0]: version(line.split("==")[0])
            for line in Path(__file__)
            .with_name("requirements.txt")
            .read_text()
            .splitlines()
            if "==" in line
        },
        "tool_catalog": tool_catalog,
        "events": events,
    }
    output = json.dumps(receipt, indent=2)
    if args.output:
        args.output.write_text(output + "\n")
    print(output)


if __name__ == "__main__":
    main()
