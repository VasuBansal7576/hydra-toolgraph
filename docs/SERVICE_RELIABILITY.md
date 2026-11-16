# Service reliability fixes

Baseline: `bbcdbb87677fe367ef7beeb9c75035a0e1a67c8c`.

These are deterministic correctness regressions, not a new retrieval benchmark or evidence of beating KV. The original v3 results and preregistration remain unchanged.

## What broke

The local closure used to repair truncated Hydra paths did not preserve the rules of the original retrieval:

- Resolved producers and their dependent branches could be loaded again.
- Restricting expansion to selected seeds changed their original ranking penalties.
- A recovered multi-hop tool could have an explanation containing only its own name.
- An empty expansion list expanded every seed instead.
- Trace updates, recorded results, and undo left cached bundles unchanged.
- Returning to an earlier cached query left feedback attributed to the last uncached query's tools.
- An outage used the old expansion rules and cached the failure indefinitely.
- A trace with no changes could offer an unrelated old edit as its undo target.

## What changed

Recovery traverses active learned edges to the existing depth limit, excludes resolved boundaries, retains original seed penalties, and carries complete path evidence. Shared dependencies remain reachable from another unresolved branch. Cycles cannot extend a path indefinitely. Mutations invalidate the bundle cache and cache hits restore the tools-loaded feedback context.

The public service no longer skips a producer simply because it appeared earlier in the session. A lookup for one customer does not prove a lookup for another customer is satisfied. Session identifiers remain useful for trace attribution, but safe reuse needs argument-bound evidence. The experimental GraphStore session API and historical evaluation remain available without silently changing the original protocol.

The live service labels its retrieval as hybrid: Hydra paths plus local learned-edge closure. This is not evidence of a Hydra-specific retrieval advantage.

## Verification

Run:

```bash
python -m unittest discover -s tests -q
python -m unittest discover -s tests -p test_service_consistency.py -v
```

The same 13 regression tests produce 15 failed assertions on the baseline and pass after the fix. The full suite runs 65 tests successfully, with one live Hydra integration test skipped because the local engine was unavailable. Tests use a deterministic fake Hydra client; they do not prove distributed consistency, live-engine latency, or benchmark superiority.

Successful bundles are cached in one service process. This prototype does not detect mutations from another process or provide distributed cache coherence. Backend request counts are marked uninstrumented rather than asserted; a cache hit explicitly reports zero.

A live-engine comparison with identical seeds, learned edges, budgets, and queries is still needed before publishing new performance claims. Fresh random split seeds over the same ToolLinkOS groups are not a new unseen dataset.
