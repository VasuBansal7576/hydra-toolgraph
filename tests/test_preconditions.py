import unittest

from toolgraph.preconditions import Precondition, PreconditionError, execute_guarded


class PreconditionsTest(unittest.TestCase):
    def test_refreshes_state_and_never_reuses_prior_success(self):
        state = {"ready": False}
        calls = []

        def check():
            calls.append("check")
            return state["ready"]

        def repair():
            calls.append("repair")
            state["ready"] = True

        def action():
            calls.append("action")
            return "done"

        guard = Precondition("ready", check, repair)
        for ready in [False, True, False]:
            state["ready"] = ready
            self.assertEqual(execute_guarded(action, [guard]), "done")
        self.assertEqual(
            calls,
            [
                "check",
                "repair",
                "check",
                "action",
                "check",
                "action",
                "check",
                "repair",
                "check",
                "action",
            ],
        )

    def test_failed_or_invalid_checks_block_action(self):
        calls = []
        for value in [False, None, 1, "true"]:
            with self.subTest(value=value), self.assertRaises(PreconditionError):
                execute_guarded(
                    lambda: calls.append("action"),
                    [Precondition("ready", lambda: value)],
                )
        self.assertEqual(calls, [])

    def test_unsuccessful_repair_blocks_action(self):
        calls = []
        with self.assertRaises(PreconditionError):
            execute_guarded(
                lambda: calls.append("action"),
                [Precondition("ready", lambda: False, lambda: calls.append("repair"))],
            )
        self.assertEqual(calls, ["repair"])

    def test_later_repair_cannot_silently_invalidate_prior_guard(self):
        state = {"a": True, "b": False}
        calls = []

        def repair_b():
            state.update(a=False, b=True)

        with self.assertRaises(PreconditionError):
            execute_guarded(
                lambda: calls.append("action"),
                [
                    Precondition("a", lambda: state["a"]),
                    Precondition("b", lambda: state["b"], repair_b),
                ],
            )
        self.assertEqual(calls, [])

    def test_repair_exception_blocks_action(self):
        calls = []

        def repair():
            calls.append("repair")
            raise ConnectionError("repair failed")

        with self.assertRaises(ConnectionError):
            execute_guarded(
                lambda: calls.append("action"),
                [Precondition("ready", lambda: False, repair)],
            )
        self.assertEqual(calls, ["repair"])

    def test_invalid_guard_names_fail_before_observing_or_mutating(self):
        calls = []

        def check():
            calls.append("check")

        for guards in [
            [Precondition("", check)],
            [Precondition("ready", check), Precondition("ready", check)],
        ]:
            with self.assertRaises(ValueError):
                execute_guarded(lambda: calls.append("action"), guards)
        self.assertEqual(calls, [])

    def test_probe_and_action_errors_propagate_without_retry(self):
        calls = []

        def fail():
            calls.append("fail")
            raise ConnectionError("offline")

        with self.assertRaises(ConnectionError):
            execute_guarded(
                lambda: calls.append("action"), [Precondition("ready", fail)]
            )
        with self.assertRaises(ConnectionError):
            execute_guarded(fail, [Precondition("ready", lambda: True)])
        self.assertEqual(calls, ["fail", "fail"])


if __name__ == "__main__":
    unittest.main()
