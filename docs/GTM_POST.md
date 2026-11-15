# gtm drafts — do not post from this file

factual only. adjust handles/links before publishing.

---

## x thread (draft)

**1/** we shipped a small product layer on top of the hydra-toolgraph research repo: mcp server, http tools api, and a one-page demo.

learn `DEPENDS_ON` edges from agent traces (not from hidden catalog deps). deterministic schema prior at overlap ≥ 0.6. versioned edits + undo.

**2/** try it:

```bash
docker compose up
# demo http://localhost:5000
# mcp http://localhost:8080/tools/load_tools
```

stdio mcp: `python -m toolgraph.mcp_server`

**3/** what you see in the demo:

- `load_tools` — bm25+dense fusion, then multi-hop over learned edges with provenance on each hit
- `record_trace` — strengthen edges from observed tool use
- `undo_edit` — truncate edit log and rebuild
- `as_of` — replay edits with timestamps ≤ date
- `explain` — in/out edges for one tool

**4/** honest eval context (ToolLinkOS, grouped-by-main split, seeds 7/21/42): hydra mspaths+prior ties the sqlite recursive cte control; it does not beat the metadata-vector also-load control at full traces on this dataset. details in `results/curve.json`.

refs: GRTF arxiv:2502.07223 (hand-built deps), graph-of-skills arxiv:2604.05333 (graph not revised from traces).

**5/** code: github.com/…/hydra-toolgraph — mcp + demo are new; eval script unchanged. no jev, no paid llm judge in the loop.

---

## linkedin post (draft)

**title idea:** tool traces → versioned tool graph → mcp loader (hydra hackathon track 2)

we added a product-facing layer to our hydra-toolgraph experiment: an mcp server, http json endpoints, docker compose, and a minimal web demo.

the flow: rank tools for a task query, show *why* each tool appeared (learned or schema prior edges with provenance), record new traces to strengthen dependencies, undo mistakes, and reconstruct the graph as-of a past timestamp from the edit log.

under the hood: ToolLinkOS catalog, local HydraDB for multi-hop `algo.MSpaths`, sqlite as an equal-depth control, and a deterministic I/O prior (no external llm apis).

on the grouped-by-main evaluation we reported in the readme, hydra matches the sqlite cte arm within seed spread; the metadata-vector control remains stronger at full trace budget on this dataset. we're sharing the demo so others can inspect edits and provenance—not to oversell benchmark lifts.

`docker compose up` → demo on port 5000, mcp http on 8080.

repo link: [add URL]

hashtags (optional, low-key): #mcp #agents #tooluse #graphdatabase
