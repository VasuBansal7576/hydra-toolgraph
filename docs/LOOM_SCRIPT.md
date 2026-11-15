# Loom script (≈2 min) — draft, do not record until curve numbers are final

**Visual:** split screen — left: demo UI, right: `results/completeness_curve.png` + terminal.

1. **0:00–0:15 Hook**  
   “Agents fail when the tool set is incomplete. Anthropic’s tool search took Opus 4 from 49% to 74% — but flat search still misses prerequisites. We load the closure.”

2. **0:15–0:40 Product**  
   Type a task in the demo. Show seeds → walk → loaded tools with reason paths. Point at tokens saved vs full catalog.

3. **0:40–1:10 Learning**  
   Call `record_trace`. Show a new `DEPENDS_ON` in the edit log (do/undo). Undo the batch. “Warp-style reversible edits, on a tool graph.”

4. **1:10–1:40 Curve**  
   Open the sealed completeness plot. Read the pre-registered rise test from `docs/DECISIONS.md`. Say whether it passed. Compare HydraDB vs the same-depth SQLite recursive CTE. If they tie: say the honest HydraDB value is versioned edits, undo, provenance, weighted multi-hop, as_of — not a magic accuracy bump.

5. **1:40–2:00 Close**  
   “Track 2: agent context layer that earns its place over a vector store — or says so when it doesn’t. Repo + MCP + compose in the README.”

**Do not claim:** rising curve, HydraDB win, or Jev gains unless those numbers are in the committed `results/curve.json`.
