# Positive observations and later updates

An edge can remain active after negative feedback. Previously, its `prov` field was overwritten with `neg:<trace>` and displayed as "Evidence". That is the latest update, not the positive observation that originally supported the association.

The edge now retains `positive_prov` separately. Negative feedback changes the weight and latest update without inventing positive support. Undo restores the previous positive observation and labels the latest update as an undo. Snapshots, imports, and source-count updates preserve this distinction. Legacy negative-only records have unknown positive support rather than an invented origin.

The API and demo display:

- Latest recorded update: the latest provenance-bearing positive, negative, or undo update.
- Latest positive observation: the most recent known positive co-occurrence observation.
- Weight and observation count: aggregate model statistics, not dependency proof.

This is not a complete history of every supporting trace, nor proof that one tool requires another. Evidence-scoped path continuity uses the positive observation field, not a negative-feedback identifier.

## Regression proof

Three positive traces using both tools followed by a trace that loads both but uses only the first leave the edge active at weight 2/3. Its latest update starts with `neg:`, while its latest positive observation stays unchanged. Undo records an `undo:` update and restores the positive evidence. The test also round-trips the snapshot and verifies that a legacy negative-only edge reports no known positive observation.

Run `python -m unittest discover -s tests -p test_service_consistency.py -v`. These are real state transitions in GraphStore with a fake transport; live Hydra replay is a separate check, not implied by these tests.
