from __future__ import annotations

import json
import sys
from pathlib import Path


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def main() -> None:
    receipt = json.loads(Path(sys.argv[1]).read_text())
    require(
        receipt["upstream"]["commit"] == "c8571d7854316d2e1c5f288e59fe1e34e53f6dd1",
        "Unexpected upstream source",
    )
    events = receipt["events"]
    require(len(events) == 8, "Expected eight actual calls")
    lookup, failed, enabled, sent, next_lookup, next_sent, disabled, invalidated = (
        events
    )
    require(
        [event["tool"] for event in events]
        == [
            "search_contacts",
            "send_message_with_phone_number",
            "set_cellular_service_status",
            "send_message_with_phone_number",
            "search_contacts",
            "send_message_with_phone_number",
            "set_cellular_service_status",
            "send_message_with_phone_number",
        ],
        "Unexpected tool chronology",
    )
    first_phone = lookup["result"][0]["phone_number"]
    second_phone = next_lookup["result"][0]["phone_number"]
    require(first_phone != second_phone, "Recipients must differ")
    require(
        failed["error_type"] == "ConnectionError",
        "Initial call must fail in upstream tool",
    )
    require(failed["before"] == failed["after"], "Failed call must not change state")
    require(
        enabled["before"]["cellular"] is False and enabled["after"]["cellular"] is True,
        "Cellular must change from off to on",
    )
    require(
        sent["arguments"]["phone_number"] == first_phone,
        "First destination must come from lookup",
    )
    first = sent["after"]["messages"]
    require(len(first) == 1, "First successful call must produce exactly one message")
    require(
        first[0]["recipient_phone_number"] == first_phone,
        "First row has wrong recipient",
    )
    require(
        first[0]["content"] == sent["arguments"]["content"],
        "First row has wrong content",
    )
    second = next_sent["after"]["messages"]
    require(
        len(second) == 2,
        "Second successful call must produce exactly one additional message",
    )
    require(
        second[-1]["recipient_phone_number"] == second_phone,
        "Second row reused stale recipient",
    )
    require(
        second[-1]["content"] == next_sent["arguments"]["content"],
        "Second row has wrong content",
    )
    require(disabled["after"]["cellular"] is False, "Cellular should be disabled again")
    require(
        invalidated["error_type"] == "ConnectionError",
        "Earlier success must not bypass new state",
    )
    require(
        invalidated["before"] == invalidated["after"],
        "Invalidated call must not change state",
    )
    print(
        json.dumps(
            {
                "status": "passed",
                "verified": "raw returned values and state transitions, not receipt checks booleans",
                "events": len(events),
            }
        )
    )


if __name__ == "__main__":
    main()
