# An action can be found and still be impossible to execute

This reproducible proof runs the **unmodified messaging, contact and device-setting tools from ToolSandbox** against their real in-memory databases. It uses the upstream contact fixture and the cellular-off messaging scenario as its starting point. It never sends a real SMS.

## What to watch

1. The correct recipient is found. Attempting to send still fails because cellular service is disabled.
2. Enabling cellular changes the actual sandbox state. The next send creates a message for the requested recipient with the requested content.
3. A new recipient requires a new contact lookup. Having called `search_contacts` earlier is not proof that its old result is valid for this task.
4. Turning cellular off again invalidates the earlier success. The next send fails and creates no additional message.

The output records each actual function call, its result or exception, and the database state before and after. It records source hashes and the upstream commit.

## Reproduce without model calls

```bash
git clone https://github.com/apple-aiml-research/ToolSandbox.git /tmp/toolgraph-toolsandbox
git -C /tmp/toolgraph-toolsandbox checkout c8571d7854316d2e1c5f288e59fe1e34e53f6dd1
python3 -m venv /tmp/toolsandbox-proof-venv
/tmp/toolsandbox-proof-venv/bin/pip install -r examples/toolsandbox/requirements.txt
/tmp/toolsandbox-proof-venv/bin/python examples/toolsandbox/replay.py /tmp/toolgraph-toolsandbox --output /tmp/toolsandbox-replay.json
python3 examples/toolsandbox/verify_receipt.py /tmp/toolsandbox-replay.json
```

The replay rejects a different or modified upstream checkout. Python socket connections are blocked before upstream imports. The upstream source is not patched, stubbed or replaced by mocks. Only the dependencies needed to import the upstream tools are installed, not the model-agent stack. The unavailable upstream dependency pin `ccy==1.3.1` is replaced by `ccy==1.4.0`; this replay never invokes currency functionality.

## What this does not establish

- This is a scripted integration proof, not an autonomous agent run.
- It does not run the full upstream milestone evaluator and is not a ToolSandbox benchmark score.
- The adapter creates a fresh upstream ExecutionContext with the upstream contact fixture and the documented initial cellular state. It does not import or claim to replay every field of the complete upstream Scenario object.
- The changed-recipient and later-invalidation cases are additional checks authored for this project, not held-out upstream tasks.
- This receipt contains no ToolGraph/Hydra call and proves no graph-retrieval quality or speed advantage. It is an executable foundation for a future graph-backed readiness layer, not evidence that the current retrieval prototype already implements one.
- Contact lookup is how this task obtains the destination, not a universal API prerequisite: callers who already possess a valid number can send directly when cellular is enabled.

## Provenance

Upstream: [ToolSandbox](https://github.com/apple-aiml-research/ToolSandbox/tree/c8571d7854316d2e1c5f288e59fe1e34e53f6dd1).

Scenario: [`send_message_with_contact_content_cellular_off`](https://github.com/apple-aiml-research/ToolSandbox/blob/c8571d7854316d2e1c5f288e59fe1e34e53f6dd1/tool_sandbox/scenarios/multiple_tool_call_scenarios.py#L1356).

The scenario's milestone DAG is reference ground truth, **not evidence learned by ToolGraph**. The replay does not use its edges to manufacture a learned result. See upstream [LICENSE](https://github.com/apple-aiml-research/ToolSandbox/blob/c8571d7854316d2e1c5f288e59fe1e34e53f6dd1/LICENSE). No upstream source is vendored here.

## Record the observed repair in a real graph

`hydra_replay.py` reads that receipt, validates the successful repair episode, and records only its three successful calls. The failed send is not a positive observation. No tool-loading set was measured, so the bridge does not invent negative feedback.

Use a **dedicated, exclusively owned, empty local Hydra instance**, separate from any app or benchmark graph. Catalog setup is destructive; the bridge refuses a non-loopback endpoint or an instance containing any node, regardless of label. Keep `HYDRA_TOKEN` in the environment and never commit it.

```bash
PYTHONPATH=. python examples/toolsandbox/hydra_replay.py \
  examples/toolsandbox/receipt.json \
  --hydra-url http://127.0.0.1:18445 \
  --output /tmp/toolsandbox-hydra-proof.json
```

The fixed seed is `send_message_with_phone_number` and the budget is three tools. Before learning, the fresh graph returns only that seed. The bridge then checks whether real Hydra paths retrieve the observed lookup and cellular-repair tools, and compares the result to local closure using identical evidence. The output must pass every check before it reports success.

This is same-episode trace integration, not a held-out benchmark, causal prerequisite inference, autonomous agent, or proof that Hydra outperforms local closure. The sandbox's recipient and invalidation checks remain in the source receipt; the bridge does not pretend it executed them again under graph control. A measured bridge receipt is separate from `receipt.json`.
