# Support workflow: do not issue a second refund because a request was repeated

**This is an authored, deterministic local simulation.** It uses ToolGraph's actual `execute_guarded` implementation, but it is not an autonomous support agent and has no helpdesk, shop, customer or payment-provider connection. Amounts and order IDs are invented fixtures. No money moves.

The concrete buyer is the engineer building a support agent that can initiate consequential actions. Retrieving a refund tool is insufficient: the current order must be eligible, approval must still hold, and a refund must not already exist.

## Three executable cases

| Request state | Verified local outcome |
| --- | --- |
| Eligible order, current approval, no prior refund | One simulated refund is added to the sandbox ledger |
| The same request is repeated | Guard blocks before calling the refund endpoint; no second row |
| Approval was true earlier but is revoked before execution | Fresh guard blocks before calling the endpoint; no row |

After all three cases, the endpoint call count and ledger row count are both exactly one. The sandbox endpoint also rejects ineligible or duplicate requests; the guard is not the only line of defense.

```bash
python examples/support/refund_demo.py
```

`refund_receipt.json` contains the actual local run, including every guard observation and before/after ledger and call-count values. The code verifies those values and fails if a blocked case changes either the endpoint-call count or ledger.

## Plain demo narration

“This is a simulated support workflow, not a connected payment system. The first approved refund produces one ledger entry. Repeating the request does not call the refund endpoint again. In the third case, approval changed after it was first read. The guard reads the current value and stops the action. These are explicit developer-defined conditions; the graph did not learn what counts as customer consent.”

## Production boundary

A production integration would need authenticated and appropriately scoped approval, authoritative order and refund state, provider-side idempotency, and transactional enforcement. The helper checks state before an action but does not prevent concurrent changes. A boolean in this fixture is not proof of real customer authorization. No customer savings, adoption, real refund success rate or production-safety claim has been measured.

This example gives the project a concrete support-engineering use case while preserving the distinction between observed tool associations, manually authored execution guards, and real business authorization.
