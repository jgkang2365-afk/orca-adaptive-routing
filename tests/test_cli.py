from __future__ import annotations

import json
import unittest
from contextlib import redirect_stdout
from io import StringIO

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


if __name__ == "__main__":
    unittest.main()
