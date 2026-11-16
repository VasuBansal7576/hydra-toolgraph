# Evidence-scoped live retrieval protocol

Fixed before measuring the new policy against the comparison set.

Baseline: service commit `66ccb661b3205a130dc5b09aad66e502923c6428`.

Problem observations: the default OpenTable request includes Resy, noise-pollution and UberEats tools. Schema-name similarity is not proof that one reservation action produces inputs for another. Co-occurrence edges can connect unrelated episodes through common helpers.

Policy: narrow ranking seeds when a query explicitly names a catalog namespace; otherwise preserve seed selection. Treat schema-overlap-only links as candidates, not verified dependencies. A multi-hop trace path must carry the same recorded provenance throughout, rather than joining unrelated episodes. Preserve depth and tool budget. Apply only to the live service, not original sealed experiments.

Comparison: paired old/new service with the same 2,726 precomputed edges, BM25, k=12, and fixed queries. Include named single-provider tasks, an explicit two-provider task, generic no-provider tasks, and the observed default query. Report full ranked sets, schema-token counts, seed retention, off-request named namespaces, latency and mode. Also use the existing development split (seed7), reporting whole-set completeness and mean tool count, without optimizing parameters based on results. This is exploratory development evidence, not new held-out data.

Success requires fewer unsupported cross-episode paths and no silently omitted explicitly requested namespace. Report completeness losses and ambiguous cases; do not declare graph superiority over KV. No new model calls or paid resources.

## Development correction

First pass: 312 dev queries, completeness 87/312 to 101/312; 37 gains and 23 losses. Inspection found namespace ambiguity: Tesla stock queries routed to vehicle tools and "log in" mistaken for a namespace. The corrected rule narrows only if the highest-ranked seed already agrees with an explicitly named namespace; "log" is a generic operation. This correction was informed by development results and is not a held-out claim.

Corrected development pass: 122/312 complete versus 87/312, 41 gains and 6 losses; mean bundle size 10.85 versus 11.76. Lost queries: 1080 (car ownership), 265/266 (system resources), 1329/1331 (Fidelity purchase), 485 (carbon intensity). The conservative rule can omit legitimate multi-episode dependencies. This was the intermediate namespace candidate, not the final policy. No test-set tuning or sealed-score replacement was performed.

Reproduction of the local reference: run `python scripts/evaluate_service_policy.py OUTPUT.json` from each checkout root, with the same precomputed graph. This uses an empty Hydra response to isolate local closure policy. It does not measure real-engine performance. Real-engine fixed-query replay is reported separately.

## Final policy after correctness review

Namespace narrowing was removed entirely. Even the rank-agreement version could drop a second generic task from a request such as "book with alpha and get the weather". Original BM25 and dense seeds are preserved. This is a correctness-driven removal, not a tuned namespace exception.

The final policy only withholds schema-overlap candidates and prevents switching trace provenance along a path. Raw Hydra paths must also use currently active local edges, start at an allowed expansion seed, end at their claimed tool, and be nonempty, acyclic and depth-bounded. Direct ranking seeds remain eligible.

Final exploratory development result: **117/312 complete vs 87/312**, with **33 gains and 3 losses**. Mean bundle size is **11.70 vs 11.76**. Lost queries are 1080 (car ownership), 265 (system resources), and 485 (carbon intensity). Current full paired ranks are in `results/evidence_scope_dev.json`; earlier numbers above document discarded development variants, not this result.

Trace provenance stores the latest edge update, not every supporting episode. Continuity is conservative evidence hygiene, not proof of a causal dependency. Legitimate dependencies can be missed. No general superiority or live-engine speedup is established by this local reference.
