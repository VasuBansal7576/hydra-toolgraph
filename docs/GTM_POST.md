# gtm drafts — do not post from this file

factual only. lowercase-friendly. no hype. v3 numbers from `docs/RESULTS_V3.md` (pre-reg `6264be76…`).

---

## x thread (draft)

**1/** hydra-toolgraph learns which tools to load from agent traces, stores `REQUIRES` edges in hydradb with versioning, undo, provenance, and session `RESOLVED` skips.

**2/** quickstart:

```bash
docker compose up --build
# demo http://localhost:5000
# mcp  http://localhost:8080/tools/load_tools
```

single-container hf space notes: `deploy/hf-space/README.md` (sdk docker, port 7860).

**3/** mcp: `load_tools(query, session_id, k)`, `record_trace`, `record_result`, `undo_edit`, `as_of`, `explain`. no jev, no paid llm judge.

**4/** sealed v3 (toollinkos, grouped-by-main): hydra c16 **0.475** vs kv_hop **0.545** (tie within hydra seed spread), store reqs **2 vs 5.7**, staleness kv **0.03** / hydra phantoms **0**. H1/H2/H4 pass. **H3 fail**: wrong-skip **25%**, tokens not below kv_hop.

**5/** learning curve (seed 7): c16 0.013 → 0.472 over traces; tokens/complete 1277 → 1148.

**6/** code: github.com/VasuBansal7576/hydra-toolgraph — drafts only; not posting from the agent.

---

## linkedin post (draft)

we open-sourced a tool-loading layer on hydradb: learn `REQUIRES` from traces, load a bundle with why-paths, skip already-resolved tools in a session, undo edits, and as-of via `REQUIRES_HIST`.

honest v3 numbers on toollinkos (grouped-by-main): hydra completeness@16 **0.475** ties kv within seed spread; the clear edges are constant **2** store requests and live graph freshness under edits. session skipping is not ready (wrong-skip 25% — H3 failed). learning curve on seed 7 rises 0.013 → 0.472 while tokens/complete fall.

mcp + demo via `docker compose up`; hf spaces single-container recipe in-repo (not auto-deployed).

#agents #tooluse #graphrag
