from __future__ import annotations

import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from adaptive_coordinator.cli import main


class CliTests(unittest.TestCase):
    def test_version_is_machine_readable_without_local_path(self):
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--version"]), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["package_version"], "0.3.0")
        self.assertIn("installed_commit", payload)
        self.assertNotIn("/home/", output.getvalue())

    def test_parent_delegation_metadata_is_structured(self):
        completed = type("Completed", (), {
            "final_status": type("Status", (), {"__eq__": lambda self, other: False})(),
            "to_dict": lambda self: {
                "final_status": "SUCCESS", "delegated_by_parent": True,
                "preapproved": True, "interaction_mode": "no-intervention",
            },
        })()
        with patch("adaptive_coordinator.cli.ProductionRunner") as runner, redirect_stdout(StringIO()):
            runner.return_value.run.return_value = completed
            main([
                "run", "Implement the fix", "--workspace", "/home/user/project",
                "--delegated-by-parent", "--preapproved",
                "--interaction-mode", "no-intervention",
            ])
        request = runner.return_value.run.call_args.args[0]
        self.assertTrue(request.metadata.delegated_by_parent)
        self.assertTrue(request.metadata.preapproved)
        self.assertEqual(request.metadata.interaction_mode.value, "no-intervention")

    def test_worker_report_routes_directly_to_internal_helper(self):
        with patch("adaptive_coordinator.cli.report_worker_result", return_value=0) as report:
            code = main([
                "worker-report",
                "--result-json", '{"status":"failed"}',
                "--from", "terminal",
                "--dispatch-capability", "capability",
                "--task-id", "task",
                "--dispatch-id", "dispatch",
            ])
        self.assertEqual(code, 0)
        report.assert_called_once_with(
            result_json='{"status":"failed"}',
            from_handle="terminal",
            dispatch_capability="capability",
            task_id="task",
            dispatch_id="dispatch",
        )


if __name__ == "__main__":
    unittest.main()
