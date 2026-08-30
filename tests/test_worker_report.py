from __future__ import annotations

import base64
import gzip
import json
import subprocess
import unittest
from contextlib import redirect_stdout
from io import StringIO

from adaptive_coordinator.result_sentinel import final_marked_structured_result
from adaptive_coordinator.worker_report import report_worker_result


def investigation_result(**overrides):
    result = {
        "status": "succeeded",
        "summary": "The file was inspected. Its routing rules were identified. No questions remain.",
        "conclusion": "Coordinator-owned routing.",
        "evidence": ["AGENTS.md"],
        "files_checked": ["AGENTS.md"],
        "unresolved_questions": [],
    }
    result.update(overrides)
    return result


class WorkerReportTests(unittest.TestCase):
    def invoke(self, result, runner):
        output = StringIO()
        with redirect_stdout(output):
            code = report_worker_result(
                result_json=json.dumps(result),
                from_handle="term_exact",
                dispatch_capability="secret-capability",
                task_id="task_exact",
                dispatch_id="ctx_exact",
                runner=runner,
            )
        marker = output.getvalue().strip()
        decoded, error = final_marked_structured_result({"terminal": {"tail": [marker]}})
        return code, marker, decoded, error

    def test_sends_one_structured_lifecycle_and_always_prints_marker(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, "{}", "")

        expected = investigation_result()
        code, marker, decoded, error = self.invoke(expected, runner)
        self.assertEqual(code, 0)
        self.assertIsNone(error)
        self.assertEqual(decoded, expected)
        self.assertEqual(len(calls), 1)
        command, kwargs = calls[0]
        self.assertEqual(command[command.index("--body") + 1], expected["summary"])
        self.assertEqual(json.loads(command[command.index("--payload") + 1]), expected)
        self.assertEqual(command[command.index("--outcome") + 1], "succeeded")
        self.assertEqual(kwargs["timeout"], 15.0)
        self.assertTrue(marker.startswith("ADAPTIVE_RESULT_GZ64:"))

    def test_dispatch_capability_is_removed_from_payload_body_and_marker(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "{}", "")

        leaked = investigation_result(
            summary="The secret-capability appeared. It must be rejected. No leak is allowed.",
            evidence=["secret-capability"],
        )
        _, _, decoded, error = self.invoke(leaked, runner)
        self.assertIsNone(error)
        self.assertEqual(decoded["status"], "failed")
        self.assertNotIn("secret-capability", json.dumps(decoded))
        command = calls[0]
        self.assertNotIn("secret-capability", command[command.index("--body") + 1])
        self.assertNotIn("secret-capability", command[command.index("--payload") + 1])
        self.assertEqual(command[command.index("--outcome") + 1], "failed")

    def test_transport_timeout_still_leaves_valid_terminal_evidence(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        expected = investigation_result()
        code, _, decoded, error = self.invoke(expected, runner)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(error)
        self.assertEqual(decoded, expected)

    def test_oversized_or_invalid_success_fails_closed(self):
        commands = []

        def runner(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 1, "", "transport failed")

        noisy = investigation_result(evidence=[
            base64.urlsafe_b64encode(bytes(range(256))).decode() + str(index)
            for index in range(20)
        ])
        _, marker, decoded, error = self.invoke(noisy, runner)
        self.assertIsNone(error)
        self.assertEqual(decoded["status"], "failed")
        self.assertEqual(decoded["failure_class_hint"], "INSUFFICIENT_SUCCESS_EVIDENCE")
        self.assertEqual(commands[0][commands[0].index("--outcome") + 1], "failed")
        encoded = marker.removeprefix("ADAPTIVE_RESULT_GZ64:").removesuffix(":END_ADAPTIVE_RESULT")
        self.assertLessEqual(len(encoded), 512)

    def test_highly_compressible_result_over_decoded_limit_fails_closed(self):
        commands = []

        def runner(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "{}", "")

        oversized = investigation_result(evidence=["x" * 70_000])
        _, _, decoded, error = self.invoke(oversized, runner)
        self.assertIsNone(error)
        self.assertEqual(decoded["status"], "failed")
        self.assertEqual(decoded["failure_class_hint"], "INSUFFICIENT_SUCCESS_EVIDENCE")
        self.assertIn("64 KiB", decoded["reason"])
        self.assertEqual(commands[0][commands[0].index("--outcome") + 1], "failed")

    def test_unpaired_surrogate_fails_closed_and_still_reports(self):
        commands = []

        def runner(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "{}", "")

        invalid_utf8 = investigation_result(evidence=["\ud800"])
        _, _, decoded, error = self.invoke(invalid_utf8, runner)
        self.assertIsNone(error)
        self.assertEqual(len(commands), 1)
        self.assertEqual(decoded["status"], "failed")
        self.assertEqual(decoded["failure_class_hint"], "INSUFFICIENT_SUCCESS_EVIDENCE")
        self.assertIn("UTF-8", decoded["reason"])
        self.assertEqual(commands[0][commands[0].index("--outcome") + 1], "failed")

    def test_invalid_subprocess_argument_still_leaves_terminal_evidence(self):
        def runner(command, **kwargs):
            raise ValueError("embedded null byte")

        expected = investigation_result(conclusion="policy\x00is consistent")
        code, _, decoded, error = self.invoke(expected, runner)
        self.assertEqual(code, 0)
        self.assertIsNone(error)
        self.assertEqual(decoded, expected)


if __name__ == "__main__":
    unittest.main()
