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

The scenario's milestone DAG is reference ground truth, **not evidence learned by ToolGraph**. The replay does not use its edges to manufacture a learned result. See upstream [LICENSE](https://github.com/apple-aiml-research/ToolSandbox/blob/c8571d7854316d2e1c5f288e59fe1e34e53f6dd1/LICENSE). No upstream implementation source is vendored here. The transfer manifest exports upstream tool documentation; its license is retained as `LICENSE.upstream`.

A separately frozen prospective task-and-distractor comparison is described in [TRANSFER_PROTOCOL.md](TRANSFER_PROTOCOL.md). It does not change the claims made by the original replay receipt.

## Record the observed repair in a real graph

`hydra_replay.py` reads that receipt, validates the successful repair episode, and records only its three successful calls. The failed send is not a positive observation. No tool-loading set was measured, so the bridge does not invent negative feedback.

Use a **dedicated, exclusively owned, empty local Hydra instance**, separate from any app or benchmark graph. Catalog setup is destructive; the bridge refuses a non-loopback endpoint and checks every node ID its catalog setup could delete or create, across all labels. Any occupied ID or unverifiable query aborts before setup. Hydra v0.4 does not support a general all-node scan, so this proves a clear mutation footprint, not global emptiness. The instance must still be fresh and exclusively owned with no concurrent writers. Keep `HYDRA_TOKEN` in the environment and never commit it.

```bash
PYTHONPATH=. python examples/toolsandbox/hydra_replay.py \
  examples/toolsandbox/receipt.json \
  --hydra-url http://127.0.0.1:18445 \
  --output /tmp/toolsandbox-hydra-proof.json
```

The fixed seed is `send_message_with_phone_number` and the budget is three tools. Before learning, the fresh graph returns only that seed. The bridge then checks whether real Hydra paths retrieve the observed lookup and cellular-repair tools, and compares the result to local closure using identical evidence. The output must pass every check before it reports success.

This is same-episode trace integration, not a held-out benchmark, causal prerequisite inference, autonomous agent, or proof that Hydra outperforms local closure. The sandbox's recipient and invalidation checks remain in the source receipt; the bridge does not pretend it executed them again under graph control. A measured bridge receipt is separate from `receipt.json`.

## Reproduce the frozen new-recipient comparison

Run from the ToolGraph checkout root with Python 3.12, Git and Docker available. Port 18447 must be free. This creates a new, loopback-only, memory-backed Hydra instance; do not point the proof at an existing app graph. The image digest and startup flags below are the ones used in the recorded run. The mounted token is the repository's public local-development token, not a production credential.

```bash
proof_dir=$(mktemp -d)
proof_container="toolgraph-transfer-proof-$(date +%s)"
export HYDRA_TOKEN="$(cat deploy/hydra-auth-token)"

docker run -d --name "$proof_container" --memory 1g --cpus 1 \
  --tmpfs /data/store:rw,size=256m -p 127.0.0.1:18447:8443 \
  -e CLOUD_PROVIDER=local -e LOCAL_PATH=/data/store \
  -e GRAPH_NAMESPACE=default -e GRAPH_ID=default \
  -e GRAPH_ALLOW_PLAINTEXT=true -e GRAPH_USAGE_REPORT_ENABLED=false \
  -e GRAPH_AUTH_TOKEN_FILE=/var/run/secrets/slatedb-graph/auth-token \
  -v "$(pwd)/deploy/hydra-auth-token:/var/run/secrets/slatedb-graph/auth-token:ro" \
  ghcr.io/hydra-db/hydradb@sha256:ab71d651fefee13ebb23eb56d72316065bdca563ff7587442e07be663f2bf880

python3.12 -m venv "$proof_dir/toolgraph-venv"
"$proof_dir/toolgraph-venv/bin/pip" install -r requirements.txt
python3.12 -m venv "$proof_dir/toolsandbox-venv"
"$proof_dir/toolsandbox-venv/bin/pip" install -r examples/toolsandbox/requirements.txt

git clone https://github.com/apple-aiml-research/ToolSandbox.git "$proof_dir/upstream"
git -C "$proof_dir/upstream" checkout c8571d7854316d2e1c5f288e59fe1e34e53f6dd1
```

Wait until this read-only query succeeds. It checks server readiness, not graph emptiness; the retrieval runner performs the full mutation-footprint guard before setup.

```bash
curl --fail --silent --show-error http://127.0.0.1:18447/v1/graphs/default/query \
  -H "Authorization: Bearer $HYDRA_TOKEN" -H 'X-Graph-Namespace: default' \
  -H 'Content-Type: application/json' \
  --data '{"cell_id":"cell-0","query":"MATCH (n {id: 1}) RETURN n.id"}'
```

Run retrieval once, execute its exact bundles in the separate upstream environment, verify the raw states, then produce the phone-free summary. Do not rerun retrieval against the populated graph or change the frozen task after seeing results.

```bash
PYTHONPATH=. "$proof_dir/toolgraph-venv/bin/python" \
  examples/toolsandbox/transfer_retrieve.py \
  --hydra-url http://127.0.0.1:18447 \
  --output "$proof_dir/retrieval.json"

"$proof_dir/toolsandbox-venv/bin/python" examples/toolsandbox/transfer_execute.py \
  "$proof_dir/upstream" "$proof_dir/retrieval.json" \
  --output "$proof_dir/execution.json"

"$proof_dir/toolgraph-venv/bin/python" examples/toolsandbox/transfer_verify.py \
  "$proof_dir/execution.json" --retrieval "$proof_dir/retrieval.json"

"$proof_dir/toolgraph-venv/bin/python" examples/toolsandbox/transfer_summarize.py \
  "$proof_dir/execution.json" --retrieval "$proof_dir/retrieval.json" \
  --output "$proof_dir/summary.json"

docker stop "$proof_container"
```

Keep the raw receipts locally for verification; the public summary intentionally omits names, phone numbers, message contents and database rows. The source fixture contains synthetic upstream contacts, not personal user data. No model or API credit calls are needed. The recorded outcomes, including failures and the local-control tie, are in [TRANSFER_RESULTS.md](TRANSFER_RESULTS.md).
