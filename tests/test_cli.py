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
        self.assertEqual(payload["package_version"], "0.2.0")
        self.assertIn("installed_commit", payload)
        self.assertNotIn("/home/", output.getvalue())

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
