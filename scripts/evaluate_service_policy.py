"""Exploratory development-only local closure comparison, not live-engine proof."""

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

from toolgraph.data import load_dataset, split_by_main
from toolgraph.graph import GraphStore
from toolgraph.service import FusionIndex, ToolgraphService, _NullHydra


def evaluate(output: Path) -> None:
    tools, queries = load_dataset(Path("data"))
    _, development, _, _ = split_by_main(queries, 7)
    with tempfile.TemporaryDirectory() as temporary:
        service = ToolgraphService.__new__(ToolgraphService)
        service.read_only_mode = False
        service._last_loaded = {}
        service.tools = {tool.name: tool for tool in tools}
        service.tools_list = tools
        service.store = GraphStore(_NullHydra(), Path(temporary) / "edits.jsonl")
        service.store.load_catalog(tools, include_oracle=False)
        graph = json.loads(Path("results/demo_requires_edges.json").read_text())
        service.store.import_requires_rows(graph["edges"], tau=graph["tau"])
        service.fusion = FusionIndex(tools, mode="bm25")
        service._context_meter = lambda ranked: {}
        rows = []
        started = time.monotonic()
        for query in development:
            result = service.load_tools(query.text, k=12)
            rows.append({
                "idx": query.idx,
                "main": query.main,
                "complete": query.gold.issubset(result["ranked"]),
                "gold_count": len(query.gold),
                "size": len(result["ranked"]),
                "ranked": result["ranked"],
            })
        report = {
            "label": "exploratory dev local closure reference; empty Hydra response, not live-engine benchmark",
            "n": len(rows),
            "complete": sum(row["complete"] for row in rows) / len(rows),
            "mean_size": sum(row["size"] for row in rows) / len(rows),
            "elapsed": time.monotonic() - started,
            "rows": rows,
        }
        output.write_text(json.dumps(report, indent=2) + "\n")
        print({key: value for key, value in report.items() if key != "rows"})


if __name__ == "__main__":
    evaluate(Path(sys.argv[1]))
