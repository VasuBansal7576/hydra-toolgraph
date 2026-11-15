"""Smoke-test ToolgraphService with FakeHydra when live HydraDB is unavailable."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Smoke stays fast: do not ingest the full learn split.
os.environ.setdefault("DEMO_SEED_LEARN", "0")

from toolgraph.data import load_dataset
from toolgraph.hydra import HydraClient
from toolgraph.service import ToolgraphService


class FakeHydra:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def query(self, statement: str, parameters: dict | None = None) -> dict:
        self.statements.append(statement)
        return {"columns": [], "rows": [], "bookmark": None}


def hydra_live() -> bool:
    client = HydraClient("http://127.0.0.1:8443", "local-development-token-32-bytes")
    try:
        client.query("RETURN 1")
        return True
    except Exception:
        return False


def main() -> int:
    use_live = hydra_live()
    hydra = HydraClient("http://127.0.0.1:8443", "local-development-token-32-bytes") if use_live else FakeHydra()  # type: ignore[assignment]
    with tempfile.TemporaryDirectory() as tmp:
        edit_log = Path(tmp) / "edits.jsonl"
        tools, instances = load_dataset(ROOT / "data")
        svc = ToolgraphService(hydra, data_dir=ROOT / "data", edit_log=edit_log)
        sample = instances[0]
        loaded = svc.load_tools(sample.text, k=8)
        assert loaded["ranked"], "load_tools returned empty ranking"
        trace_tools = [sample.main, *sorted(sample.gold - {sample.main})[:2]]
        rec = svc.record_trace(sample.text, trace_tools)
        assert "edit_ids" in rec
        if rec.get("edges_added", 0) > 0 and rec["edit_ids"]:
            undone = svc.undo_edit(rec["edit_ids"][0])
            assert undone.get("ok") is True, undone
        asof = svc.as_of("2099-01-01T00:00:00+00:00")
        assert asof["edits_replayed"] >= 0
        explained = svc.explain(sample.main)
        assert explained["tool"] == sample.main
        summary = {
            "hydra": "live" if use_live else "fake",
            "ranked_head": loaded["ranked"][:5],
            "trace": rec,
            "as_of_edges": asof.get("edges_active"),
            "explain_out_edges": len(explained.get("out_edges") or []),
        }
        print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
