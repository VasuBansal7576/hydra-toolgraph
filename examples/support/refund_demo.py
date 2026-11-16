"""Demonstrate explicitly authored refund guards in a local support simulation."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from toolgraph.preconditions import Precondition, PreconditionError, execute_guarded


class SupportSandbox:
    def __init__(self):
        self.orders = {
            "demo-order-1": {
                "customer_approved": True,
                "policy_eligible": True,
                "amount_minor": 5000,
                "currency": "USD",
            },
            "demo-order-2": {
                "customer_approved": True,
                "policy_eligible": True,
                "amount_minor": 2500,
                "currency": "USD",
            },
        }
        self.ledger = []
        self.refund_requests = 0

    def has_refund(self, order_id):
        return any(row["order_id"] == order_id for row in self.ledger)

    def refund(self, order_id):
        self.refund_requests += 1
        order = self.orders[order_id]
        if (
            not order["customer_approved"]
            or not order["policy_eligible"]
            or self.has_refund(order_id)
        ):
            raise ValueError("Sandbox refund endpoint rejected the request")
        row = {
            "order_id": order_id,
            "amount_minor": order["amount_minor"],
            "currency": order["currency"],
            "status": "simulated_refund_recorded",
        }
        self.ledger.append(row)
        return row

    def run(self, order_id):
        events = []
        before = {
            "ledger": copy.deepcopy(self.ledger),
            "endpoint_calls": self.refund_requests,
        }
        try:
            result = execute_guarded(
                lambda: self.refund(order_id),
                [
                    Precondition(
                        "customer_approval_is_current",
                        lambda: self.orders[order_id]["customer_approved"],
                    ),
                    Precondition(
                        "order_is_eligible",
                        lambda: self.orders[order_id]["policy_eligible"],
                    ),
                    Precondition(
                        "refund_not_already_recorded",
                        lambda: not self.has_refund(order_id),
                    ),
                ],
                emit=events.append,
            )
            status = "simulated_refund_recorded"
            error = None
        except PreconditionError as exc:
            result = None
            status = "blocked_before_endpoint"
            error = str(exc)
        return {
            "order_id": order_id,
            "status": status,
            "result": result,
            "error": error,
            "guard_observations": events,
            "before": before,
            "after": {
                "ledger": copy.deepcopy(self.ledger),
                "endpoint_calls": self.refund_requests,
            },
        }


def require(condition, message):
    if not condition:
        raise ValueError(message)


def main():
    sandbox = SupportSandbox()
    approved = sandbox.run("demo-order-1")
    repeated = sandbox.run("demo-order-1")
    previous_approval = sandbox.orders["demo-order-2"]["customer_approved"]
    sandbox.orders["demo-order-2"]["customer_approved"] = False
    revoked = sandbox.run("demo-order-2")
    revoked["approval_observed_earlier"] = previous_approval
    revoked["environment_change"] = "Customer approval was revoked before the guard ran"
    require(
        approved["status"] == "simulated_refund_recorded"
        and len(approved["after"]["ledger"]) == 1,
        "Approved simulation did not record one refund",
    )
    for case in [repeated, revoked]:
        require(
            case["status"] == "blocked_before_endpoint",
            "Expected blocked support request",
        )
        require(
            case["before"] == case["after"],
            "Blocked request touched endpoint or ledger",
        )
    require(
        sandbox.refund_requests == 1 and len(sandbox.ledger) == 1,
        "Unexpected additional refund request",
    )
    receipt = {
        "label": "Authored local customer-support simulation using the actual execute_guarded implementation; no payment, commerce integration, production claim or benchmark",
        "status": "verified",
        "cases": {
            "approved_eligible_refund": approved,
            "repeat_request": repeated,
            "approval_revoked_since_last_read": revoked,
        },
        "limits": [
            "Policy and approval predicates are explicitly authored, not learned by Hydra",
            "Fixture amounts are invented test values, not real transactions",
            "No external endpoint, customer, order system or payment processor is connected",
            "Guards are not transactional and cannot replace server-side idempotency or authorization",
            "The sandbox endpoint also validates policy and duplicates as defense in depth",
        ],
    }
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
