"""Check the SelfGraph README claim that retrieve(..., as_of=...) reads an earlier graph.

Measured against embedded Kuzu. This does not snapshot HydraDB MSpaths.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from selfgraph import KuzuStore, SelfGraph

OUT = Path(__file__).resolve().parents[1] / "results" / "selfgraph_as_of.json"
DB = Path("/tmp/toolgraph_selfgraph_asof.kuzu")


async def main() -> None:
    if DB.exists():
        shutil.rmtree(DB)
    async with SelfGraph(store=KuzuStore(str(DB))) as graph:
        await graph.entities.create("Ann", "Person", id="person:ann")
        await graph.entities.create("Acme", "Company", id="company:acme")
        await graph.entities.create("Globex", "Company", id="company:globex")
        await graph.relations.create("person:ann", "WORKS_AT", "company:acme", event_time="2020-01-01")
        await graph.relations.create("person:ann", "WORKS_AT", "company:globex", event_time="2022-01-01")
        current = await graph.retrieve("where does Ann work")
        past = await graph.retrieve("where does Ann work", as_of="2021-06-01")
    payload = {
        "label": "measured",
        "store": "KuzuStore",
        "current_context": current.context,
        "as_of_2021_06_01_context": past.context,
        "as_of_hides_globex": "Globex" not in past.context,
        "as_of_shows_acme": "Acme" in past.context,
        "current_shows_both": "Acme" in current.context and "Globex" in current.context,
        "note": (
            "as_of hid the relation whose event_time was after 2021-06-01. "
            "A current read still showed both relations: SelfGraph did not retire the "
            "2020 fact without a contradiction rule. as_of filters this SDK's own "
            "relations. It does not freeze HydraDB algo.MSpaths topology. "
            "Iteration rollback for the tool graph is the do/undo Cypher pair."
        ),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2))
    print(json.dumps({k: payload[k] for k in ("as_of_hides_globex", "as_of_shows_acme", "current_shows_both")}))


if __name__ == "__main__":
    asyncio.run(main())
