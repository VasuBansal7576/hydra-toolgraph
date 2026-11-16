"""Verify raw transfer receipts and export a phone-free public summary."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("execution", type=Path)
    parser.add_argument("--retrieval", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    checked = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("transfer_verify.py")),
            str(args.execution),
            "--retrieval",
            str(args.retrieval),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    execution_bytes = args.execution.read_bytes()
    retrieval_bytes = args.retrieval.read_bytes()
    execution = json.loads(execution_bytes)
    retrieval = json.loads(retrieval_bytes)
    summary = {
        "label": "Verified compact summary of one frozen authored continuation, not a benchmark",
        "verification": json.loads(checked.stdout),
        "source_execution_sha256": hashlib.sha256(execution_bytes).hexdigest(),
        "source_retrieval_sha256": hashlib.sha256(retrieval_bytes).hexdigest(),
        "manifest_sha256": retrieval["manifest_sha256"],
        "training_receipt_sha256": retrieval["training_receipt_sha256"],
        "catalog_size": retrieval["catalog_size"],
        "arms": retrieval["arms"],
        "same_evidence_hybrid_equals_local": retrieval[
            "same_evidence_hybrid_equals_local"
        ],
        "outcomes": [
            {
                "arm": case["arm"],
                "budget": case["budget"],
                "initial_cellular": case["initial_cellular"],
                "success": case["success"],
                "selected_tools": case["selected_tools"],
                "unused_tools": case["unused_tools"],
                "final_message_count": len(case["final_state"]["messages"]),
                "calls": [
                    {
                        "tool": event["tool"],
                        "status": event["status"],
                        "error_type": event.get("error_type"),
                    }
                    for event in case["events"]
                ],
            }
            for case in execution["outcomes"]
        ],
        "privacy": "Phone numbers, recipient names, message contents, raw arguments and database rows omitted. This summary does not replace raw-state verification.",
        "limits": [
            "One authored prospective task in four budget/state settings, not four independent held-out tasks",
            "Actual upstream tools under a scripted executor, not autonomous planning or real SMS",
            "Learned co-occurrence is not proof of causal necessity",
            "Hydra and equal-evidence local closure tie",
            "Already-enabled setter errors and unused tools remain visible",
            "Original artifact hashes identify this run; timestamps may change on reproduction",
        ],
    }
    args.output.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary["verification"]))


if __name__ == "__main__":
    main()
