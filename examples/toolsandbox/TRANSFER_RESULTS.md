# One repair episode transferred to a different recipient

The frozen query asks to send a new synthetic contact a message. It contains no phone number or hint about cellular state. Retrieval uses 15 actual ToolSandbox tools. Training uses only the successful calls from the earlier repair episode. The upstream functions, not mocks, execute the selected bundle under the fixed scripted policy.

| Bundle | Budget | Cellular off | Cellular on | Unused loaded tools |
| --- | ---: | --- | --- | --- |
| BM25 before learning | 3 | Blocked: no contact lookup | Blocked: no contact lookup | 3 |
| BM25 before learning | 5 | Actual send raises ConnectionError | Message created correctly | 3 |
| Hydra-backed hybrid after one episode | 3 | Message created correctly | Message created correctly, setter raises already-enabled error first | 0 |
| Hydra-backed hybrid after one episode | 5 | Message created correctly | Message created correctly, setter raises already-enabled error first | 2 |
| Local closure with the same evidence | 3 | Message created correctly | Message created correctly, setter raises already-enabled error first | 0 |
| Local closure with the same evidence | 5 | Message created correctly | Message created correctly, setter raises already-enabled error first | 2 |

All 12 sandbox cases were evaluated once under the frozen protocol. The independent verifier checks original retrieval-file identity, exact selected bundles, permitted calls, lookup-derived destination, content, state continuity, absence of side effects on errors, and final database rows. It verified nine successful outcomes. No new real SMS was sent; these are sandbox database writes.

## What changed

BM25's first three tools were `send_message_with_phone_number`, `search_messages`, and `add_contact`. After the observed repair episode, the hybrid's first three were `send_message_with_phone_number`, `search_contacts`, and `set_cellular_service_status`. At budget 5, BM25 included the contact lookup but still omitted cellular repair, producing a real upstream exception when cellular was off.

## What did not change

Hydra-backed hybrid and equal-evidence local closure produced identical rankings and outcomes. This demonstrates the usefulness of recorded experience in this one task, not a graph-over-local quality advantage. Their single timing observations are not a latency benchmark.

The learned edges are co-occurrence associations, including links between the two helpers. They are not a verified causal plan or an AND-prerequisite model. The cellular-on control exposes the limitation: the learned bundle still includes a redundant setter, which raises an error. The fixed executor continues to the send, exactly as specified before scoring.

This is a prospectively authored continuation of a previously inspected fixture, with two budget settings and two device states. It is not four independent held-out tasks, an autonomous-agent evaluation, or the full ToolSandbox benchmark.

## Receipt identities

- Frozen manifest SHA-256: `1e507f0a6d627c0810c2ffa9ecac3d72e0c447eaa78fe1a06ce0ffb2a719d1d4`
- Training receipt SHA-256: `1c6dbde9d65e71f256d6141f73565d6fac5043712680e17d3f04bd82fcafba17`
- Actual Hydra retrieval receipt SHA-256: `5cca358ea3e01dd72153f991706550fcc8996ddcbc684eaed3471c5aad14d735`
- Upstream execution receipt SHA-256: `23f8f0fc156feafbb0f692570e3125e26f5c463f80bb459cb72cf517ee6dc429`

Verification command:

```bash
python examples/toolsandbox/transfer_verify.py toolsandbox-transfer-execution.json --retrieval toolsandbox-transfer-retrieval.json
```

## Budget and token tradeoffs

At budget 3, the learned bundle uses 554 schema tokens versus BM25's 834. At budget 5, it uses 1,240 versus 1,198, so learning did not always save tokens. The two extra learned-arm tools at budget 5 were unused by this executor.

`transfer_results.json` is a compact, verified public summary. It excludes phone numbers, recipient names, message contents and raw database rows. Full raw receipts are not included in this summary; `transfer_verify.py` requires the originals, and `transfer_summarize.py` refuses to summarize them unless verification passes. The scripts reproduce both the raw run and the public summary. Original artifact hashes identify this captured run, not a promise that timestamps will be byte-identical in a rerun.

```bash
python examples/toolsandbox/transfer_summarize.py toolsandbox-transfer-execution.json \
  --retrieval toolsandbox-transfer-retrieval.json \
  --output toolsandbox-transfer-summary.json
```
