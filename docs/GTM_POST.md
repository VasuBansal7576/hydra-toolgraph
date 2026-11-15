# GTM post — draft only. Post nothing until Vasu approves.

**Working title:** Tool loading that learns prerequisites (HydraDB Track 2)

Agents don’t just need the right tool — they need every prerequisite too. Incomplete sets fail; complete ones succeed (arXiv 2610.41905). Anthropic’s tool search / `defer_loading` is the baseline everyone knows (Opus 4: 49% → 74%). We asked a harder question: can a **learned dependency graph** beat a plain store on sealed completeness as it sees more traces?

**What we built**
- A tool loader: `load_tools(task)` returns the fused seed plus the learned prerequisite closure, with reason paths.
- Learning from agent traces: each gold tool set becomes `DEPENDS_ON(main → helper)` with counts, provenance, and do/undo Cypher.
- Schema generalization: param PRODUCES/REQUIRES candidates, verified with Jev as an **edge** judge (not a closure pruner).
- Fair arms at the same depth: flat BM25+dense, SQLite recursive CTE, HydraDB learned ± Jev edges, oracle catalog ceiling.

**What the curve said**  
_Paste the measured table from README after `results/curve.json` lands. Label every number measured or inferred. If HydraDB ties the CTE, say so._

**Why HydraDB anyway**  
Even when accuracy ties a recursive CTE, the product still needs versioned edits, undo, provenance, weighted multi-hop queries, and as_of. That’s the honest pitch — not an invented accuracy gap.

**Try it**  
`docker compose up` → demo on :8080, MCP `load_tools` / `record_trace`. Deploy is gated — we did not ship to a public host without approval.

Refs: Anthropic advanced tool use; Anthropic writing tools for agents; Warp self-improving agents; arXiv 2610.41905.
