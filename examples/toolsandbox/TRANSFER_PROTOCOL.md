# Prospective task transfer protocol

Frozen manifest SHA-256: `1e507f0a6d627c0810c2ffa9ecac3d72e0c447eaa78fe1a06ce0ffb2a719d1d4`.

This manifest was generated before retrieval scoring. A pre-scoring revision added budget 5 and the cellular-on control; the prior manifest hash is retained in the manifest. No outcomes were used to select the query, recipient or catalog.

The new recipient is selected deterministically from the upstream fixture, excluding Fredrik and John, who were already replayed. The catalog contains all 15 registered agent-visible tools defined in the upstream contact, messaging and setting modules. Descriptions and signatures come from those functions, not authored hints. The manifest exports upstream documentation; its license is retained in `LICENSE.upstream`.

The task is an **authored prospective continuation**, not a held-out benchmark task. The upstream contact fixture and tool source were already inspected. The query mentions the chosen recipient and message, but does not supply their phone number or say which device setting is wrong.

## Compare before learning, after learning, and an equally informed control

Use the same query, catalog and ordinary BM25 seeding for every arm. No forced target seed. Record all rankings and failures at budgets 3 and 5:

- BM25 without the observed repair episode.
- Genuine Hydra retrieval after the single observed repair episode.
- Local closure with exactly the same observed evidence.

An equal Hydra/local result is a tie, not evidence of a graph quality advantage. No model calls or other learning episodes are permitted. The milestone DAG is not training data.

## Execute only what the bundle contains

For each arm and budget, create a fresh actual upstream ExecutionContext with the upstream contacts and no messages. Run once with cellular off, once with it on.

The executor reuses the operation order from the earlier observed successful repair: look up the recipient, attempt to enable cellular, send. Each operation is allowed only when its tool is in the returned bundle. A missing lookup does not get a hidden phone number from the fixture. If the setter is missing but lookup and send are available, call the actual send function and record its result or exception. Other returned tools are not invoked by this restricted executor and are counted as unused.

When cellular is already on, the upstream setter can raise an already-enabled error. Preserve it in the receipt, then continue to the send according to the same fixed policy. Do not erase inconvenient exceptions.

The independent evaluator checks exactly one actual message row, exact requested destination and exact content. State snapshots are measurement, not agent-selected tool calls. This is a scripted executor, not evidence that Hydra inferred a plan or satisfied an autonomous-agent benchmark.

`transfer_execute.py` refuses a different manifest hash, altered upstream source, missing arm/budget pairs, unknown tool names, duplicated tools or over-budget bundles. It blocks Python network connections before importing upstream tools.
