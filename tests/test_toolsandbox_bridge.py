"""Receipt integrity and isolation checks before graph mutation."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from examples.toolsandbox import hydra_replay


RECEIPT = Path(__file__).resolve().parents[1] / "examples/toolsandbox/receipt.json"


class ToolSandboxBridgeTest(unittest.TestCase):
    def test_observed_episode_excludes_the_failed_send(self):
        receipt = json.loads(RECEIPT.read_text())
        self.assertEqual(
            hydra_replay.successful_repair_episode(receipt),
            [
                "search_contacts",
                "set_cellular_service_status",
                "send_message_with_phone_number",
            ],
        )

    def test_inconsistent_recipient_or_state_is_rejected(self):
        original = json.loads(RECEIPT.read_text())
        for mutation in (
            "recipient",
            "state",
            "failure",
            "different_retry",
            "broken_lookup_chain",
        ):
            with self.subTest(mutation=mutation):
                receipt = copy.deepcopy(original)
                if mutation == "recipient":
                    receipt["events"][3]["arguments"]["phone_number"] = (
                        "not-the-observed-contact"
                    )
                elif mutation == "state":
                    receipt["events"][3]["before"]["cellular"] = False
                elif mutation == "different_retry":
                    receipt["events"][1]["arguments"]["content"] = "different task"
                elif mutation == "broken_lookup_chain":
                    receipt["events"][0]["after"]["cellular"] = True
                else:
                    receipt["events"][1]["status"] = "ok"
                with self.assertRaises(ValueError):
                    hydra_replay.successful_repair_episode(receipt)

    def test_any_existing_node_blocks_destructive_catalog_setup(self):
        client = Mock()
        client.query.return_value = {"rows": [[{"value": 1, "type": "Integer"}]]}
        with tempfile.TemporaryDirectory() as temporary:
            argv = [
                "hydra_replay.py",
                str(RECEIPT),
                "--hydra-url",
                "http://127.0.0.1:18445",
                "--output",
                str(Path(temporary) / "result.json"),
            ]
            with (
                patch("sys.argv", argv),
                patch.dict("os.environ", {"HYDRA_TOKEN": "isolated-test"}),
                patch.object(hydra_replay, "HydraClient", return_value=client),
                patch.object(hydra_replay, "GraphStore") as graph,
            ):
                with self.assertRaisesRegex(RuntimeError, "no nodes of any label"):
                    hydra_replay.main()
                graph.assert_not_called()
                client.query.assert_called_once_with("MATCH (n) RETURN count(n)")
