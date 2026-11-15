# gtm drafts — do not post from this file

factual only. lowercase-friendly. no hype.

---

## x thread (draft)

**1/** hydra-toolgraph is a small context layer: learn which tools to load from agent traces, store the edges in hydradb with versioning, undo, and provenance.

**2/** quickstart:

```bash
docker compose up --build
# demo http://localhost:5000
# mcp  http://localhost:8080/tools/load_tools
```

**3/** mcp tools: `load_tools`, `record_trace`, `undo_edit`, `as_of`, `explain`. no jev, no paid llm judge. i/o prior is deterministic schema overlap ≥ 0.6.

**4/** eval (toollinkos, grouped-by-main, matched fan-in=32): hydra mspaths+prior c16 **0.656**, metadata also-load **0.649**, sqlite cte **0.649**. tie within seed spread. hydra does not beat the vector-store control once fan-in matches. the old 4-seed gap was a confound (k4 ablation lands at 0.532).

**5/** leak finding: every main has 3 identical-gold queries (1569/1569). a query-level split inflates c16 from ~0.53 to ~0.91. grouped-by-main is the headline.

refs: grtf arxiv:2502.07223 (reference, different protocol), graph-of-skills arxiv:2604.05333 (graph never revised from traces).

**6/** code: github.com/VasuBansal7576/hydra-toolgraph — drafts only here; not posting from the agent.

---

## linkedin post (draft)

we open-sourced a small tool-loading layer on hydradb: learn prerequisite edges from agent traces, load a complete tool set with provenance, and undo edits.

honest numbers on toollinkos (grouped-by-main split, matched fan-in of 32 fused seeds): hydra multi-hop **0.656** c16, metadata also-load **0.649**, recursive cte **0.649**. after matching controls, hydra ties — it does not win completeness against a flat also-load list. what hydradb adds on this dataset is versioned, undoable, provenance-tagged edges (and as-of replay).

also documented: a query-level split on this dataset leaks sibling answer sets (3 identical golds per main) and inflates scores from ~0.53 to ~0.91.

mcp + one-page demo via `docker compose up`. no paid edge judge.

#agents #tooluse #graphrag
