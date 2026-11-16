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
        client._post.return_value = {
            "columns": ["n.id"],
            "rows": [[{"value": 1, "type": "Integer"}]],
        }
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
                with self.assertRaisesRegex(RuntimeError, "catalog mutation footprint"):
                    hydra_replay.main()
                graph.assert_not_called()
                self.assertTrue(client._post.called)
                self.assertTrue(
                    all(
                        ":Tool" not in call.args[0]["query"]
                        for call in client._post.call_args_list
                    )
                )

    def test_reset_and_parameter_allocations_are_all_checked(self):
        from toolgraph.data import Tool
        from toolgraph.graph import catalog_reset_ids

        client = Mock()
        client._post.return_value = {"columns": ["n.id"], "rows": []}
        tools = [
            Tool(
                "target",
                "target",
                "external",
                tuple({"name": f"p{i}"} for i in range(2005)),
                (),
            )
        ]
        count = hydra_replay.assert_catalog_footprint_clear(client, tools)
        checked = {
            int(call.args[0]["query"].split("id: ")[1].split("}")[0])
            for call in client._post.call_args_list
        }
        self.assertTrue(set(catalog_reset_ids(1)).issubset(checked))
        self.assertIn(12005, checked)
        self.assertEqual(count, len(checked))
        self.assertTrue(
            all(
                call.args[0]["cell_id"] == "cell-0"
                for call in client._post.call_args_list
            )
        )

    def test_unverifiable_query_response_prevents_setup(self):
        from toolgraph.data import Tool

        from toolgraph.hydra import HydraClient

        client = HydraClient("http://127.0.0.1:18445", "isolated-test")
        for response in (
            {},
            {"rows": [], "columns": [], "error": "failed"},
            {"rows": [], "columns": [], "next_cursor": "more"},
        ):
            with (
                self.subTest(response=response),
                patch.object(client, "_post", return_value=response),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "verifiable complete node lookup"
                ):
                    hydra_replay.assert_catalog_footprint_clear(
                        client, [Tool("target", "target", "external", (), ())]
                    )
