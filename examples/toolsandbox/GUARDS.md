# Check current state instead of repeating yesterday's repair

The original recorded retrieval proof exposed a real error: when cellular was already on, the fixed executor still tried to enable it. We implemented a separate reusable execution guard in `toolgraph/preconditions.py` rather than hiding that negative result.

The guard checks a caller-supplied predicate, performs an authorized repair only when needed, checks again after repair, and invokes the action once. Checks must return strict booleans. Exceptions, invalid results, or a repair that leaves the condition false prevent the action. State is not cached across calls. Multiple guards are rechecked before the final action because later repairs may invalidate earlier conditions. Checks and observers should not mutate state. This is not a transaction; concurrent changes remain possible.

## Actual upstream verification

A new protocol was frozen before execution. Each arm has the same four tools: contact lookup, cellular getter, cellular setter and send. The getter is explicitly loaded and charged. The actual upstream sandbox keeps its state across three tasks with different recipients: initially off, already on, and externally turned off again.

| Arm | Correct sandbox messages | Tool errors | Actual selected tool calls | Loaded schema tokens |
| --- | ---: | ---: | ---: | ---: |
| Original unconditional repair | 3 | 1 | 9 | 587 |
| New live-state guard | 3 | 0 | 13 | 587 |

The new guard avoids the already-enabled setter call. It refreshes the contact lookup for each recipient and repairs cellular again after the environment disables it. Four additional getter calls are the explicit cost. Counts cover selected execution calls; the separate before/after snapshot probes are measurement instrumentation, not adapter decisions. Both arms already sent all three messages correctly; this improves error behavior, not measured task completion or speed.

All six cases were independently verified against the raw returned values and database states. The verifier checks the source and protocol identities, both policy orders, environment changes, actual getter results, fresh lookup-derived destinations, exact message contents and state continuity. No real SMS, LLM call or paid service was used.

This guard is manually authored by the developer. Hydra did not learn these preconditions, and this experiment proves no graph necessity. The earlier frozen retrieval results, including their errors, remain unchanged.

## Reproduce

Use the pinned upstream checkout and environment described in [README.md](README.md), then install the tokenizer used by the project:

```bash
/tmp/toolsandbox-proof-venv/bin/pip install tiktoken==0.14.0
PYTHONPATH=. /tmp/toolsandbox-proof-venv/bin/python examples/toolsandbox/guard_execute.py \
  /tmp/toolgraph-toolsandbox --output /tmp/toolsandbox-guard-execution.json
/tmp/toolsandbox-proof-venv/bin/python examples/toolsandbox/guard_verify.py \
  /tmp/toolsandbox-guard-execution.json --summary /tmp/toolsandbox-guard-summary.json
```

`guard_manifest.json` is the frozen protocol. `guard_results.json` is a verified, phone-free public summary. It omits recipient names, numbers, message content and raw database rows. The original raw receipt SHA-256 is `8200072e28f92476a63b764c0d32fa70809f1434e89edb40258fb723f143390d`; reruns can differ in generated IDs and timestamps.
