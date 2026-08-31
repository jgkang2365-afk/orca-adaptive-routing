import base64
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adaptive_coordinator.models import Authority, Phase, Route
from adaptive_coordinator.orca import (
    CoordinatorError,
    LifecycleSettlementError,
    OrcaAdapter,
    SafetyGateError,
    WorkerHandle,
    _bounded_json_mapping,
    _parse_orca_output,
    _default_runner,
    _git_changes,
)
from adaptive_coordinator.runner import ProductionRunner, ResultNormalizer, SuccessEvidenceGate


def route(authority: Authority, *, critical: bool = False) -> Route:
    return Route(
        phase=Phase.IMPLEMENTATION if authority is Authority.WORKSPACE_WRITE else Phase.INVESTIGATION,
        role="Lead Implementer" if authority is Authority.WORKSPACE_WRITE else "Investigator",
        model="gpt-5.6-terra",
        effort="medium",
        authority=authority,
        approval_grade="REVIEW" if authority is Authority.WORKSPACE_WRITE else "SAFE",
        automatic_review=authority is Authority.WORKSPACE_WRITE,
        requires_assessment=critical,
    )


class FakeRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command):
        command = list(command)
        self.commands.append(command)
        if command[1:3] == ["terminal", "create"]:
            return {"terminal": {"handle": "term_test"}}
        if command[1:3] == ["terminal", "wait"]:
            return {
                "wait": {
                    "condition": "tui-idle",
                    "satisfied": True,
                    "status": "running",
                    "blockedReason": None,
                }
            }
        if command[1:3] == ["terminal", "show"]:
            return {"terminal": {"handle": "term_test", "agentIdentity": "codex"}}
        if command[1:3] == ["orchestration", "worker-start"]:
            return {"dispatchId": "ctx_test"}
        if command[1:3] == ["orchestration", "task-update"]:
            return {"task": {"status": "completed"}}
        if command[1:3] == ["orchestration", "worker-stop"]:
            return {"state": "stopped"}
        if command[1:3] == ["orchestration", "worker-release"]:
            return {"state": "released"}
        if command[1:3] == ["terminal", "close"]:
            return {"closed": True}
        raise AssertionError(command)


class OrcaAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = FakeRunner()
        patcher = patch("adaptive_coordinator.orca.platform.system", return_value="Linux")
        self.addCleanup(patcher.stop)
        patcher.start()
        self.adapter = OrcaAdapter(
            "/home/user/project",
            runner=self.runner,
            change_detector=lambda: {},
            worktree_selector=r"path:\\wsl.localhost\Ubuntu-24.04\home\user\project",
        )

    def test_read_only_command_is_enforced(self) -> None:
        command = self.adapter.codex_command(route(Authority.READ_ONLY))
        self.assertIn("read-only", command)
        self.assertIn("never", command)
        self.assertNotIn("danger-full-access", command)

    def test_keepalive_documents_do_not_hide_final_orca_response(self) -> None:
        payload = _parse_orca_output(
            '{"_keepalive":true}\n{"ok":true,"result":{"messages":[]}}\n'
        )
        self.assertTrue(payload["ok"])

    def test_deep_lifecycle_json_is_rejected_without_recursion_failure(self) -> None:
        deeply_nested = '{"x":' * 10_000 + "0" + "}" * 10_000
        self.assertLess(len(deeply_nested.encode()), 65_536)
        self.assertIsNone(_bounded_json_mapping(deeply_nested))

    def test_workspace_write_is_explicit_and_non_interactive(self) -> None:
        command = self.adapter.codex_command(route(Authority.WORKSPACE_WRITE))
        sandbox = command.index("--sandbox")
        approval = command.index("--ask-for-approval")
        self.assertEqual(command[sandbox + 1], "workspace-write")
        self.assertEqual(command[approval + 1], "never")
        self.assertNotIn("--approve-for-me", command)

    def test_git_change_detector_captures_mode_only_change_and_blocks_empty_report(self) -> None:
        with tempfile.TemporaryDirectory(prefix="orca-git-fingerprint.") as directory:
            repo = Path(directory)
            script = repo / "probe.sh"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "core.filemode", "true"], check=True)
            script.write_text("#!/bin/sh\nexit 0\n")
            script.chmod(0o644)
            subprocess.run(["git", "-C", str(repo), "add", "probe.sh"], check=True)
            before = dict(_git_changes(repo))
            script.chmod(0o600)
            permission_only = dict(_git_changes(repo))
            self.assertEqual(permission_only, before)
            script.chmod(0o755)
            after = dict(_git_changes(repo))
            changed = ProductionRunner._changed_paths(before, after)
            self.assertEqual(changed, ["probe.sh"])
            normalized = ResultNormalizer.normalize({
                "status": "completed", "summary": "done", "files_modified": [],
                "requirements_completed": ["done"], "tests_run": ["unit"],
                "test_results": ["1 passed"], "unexecuted_verification": [],
                "workspace_diff": [],
            })
            self.assertFalse(SuccessEvidenceGate.evaluate(
                route(Authority.WORKSPACE_WRITE), normalized, changed)[0])

    def test_git_change_detector_captures_index_only_transition(self) -> None:
        with tempfile.TemporaryDirectory(prefix="orca-git-index.") as directory:
            repo = Path(directory)
            target = repo / "probe.txt"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            target.write_text("base\n")
            subprocess.run(["git", "-C", str(repo), "add", "probe.txt"], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.name=Test", "-c",
                 "user.email=test@example.invalid", "commit", "-qm", "base"], check=True)
            target.write_text("changed\n")
            unstaged = dict(_git_changes(repo))
            subprocess.run(["git", "-C", str(repo), "add", "probe.txt"], check=True)
            staged = dict(_git_changes(repo))
            self.assertEqual(ProductionRunner._changed_paths(unstaged, staged), ["probe.txt"])

    def test_worker_start_uses_custom_terminal_then_supervised_dispatch(self) -> None:
        worker = self.adapter.start_worker("run_test", "task_test", route(Authority.READ_ONLY))
        self.assertEqual(worker.dispatch_id, "ctx_test")
        create = self.runner.commands[0]
        launch_command = create[create.index("--command") + 1]
        self.assertIn("codex", launch_command)
        self.assertIn("read-only", launch_command)
        self.assertEqual(
            create[create.index("--title") + 1],
            "orca-adaptive:investigation",
        )
        self.assertIn(
            r"path:\\wsl.localhost\Ubuntu-24.04\home\user\project",
            create,
        )
        worker_start = next(
            command for command in self.runner.commands
            if command[1:3] == ["orchestration", "worker-start"]
        )
        self.assertIn("term_test", worker_start)
        self.assertIn(
            r"path:\\wsl.localhost\Ubuntu-24.04\home\user\project",
            worker_start,
        )

    def test_codex_update_prompt_sends_skip_then_uses_same_terminal(self) -> None:
        original = self.adapter.runner
        waits = [
            {
                "wait": {
                    "condition": "tui-idle",
                    "satisfied": False,
                    "status": "running",
                    "blockedReason": "codex-update-prompt",
                }
            },
            {
                "wait": {
                    "condition": "tui-idle",
                    "satisfied": True,
                    "status": "running",
                    "blockedReason": None,
                }
            },
        ]

        def update_prompt(command):
            command = list(command)
            if command[1:3] == ["terminal", "wait"]:
                self.runner.commands.append(command)
                return waits.pop(0)
            if command[1:3] == ["terminal", "send"]:
                self.runner.commands.append(command)
                return {"sent": True}
            return original(command)

        self.adapter.runner = update_prompt
        worker = self.adapter.start_worker(
            "run_test", "task_test", route(Authority.READ_ONLY)
        )

        self.assertEqual(worker.dispatch_id, "ctx_test")
        sends = [command for command in self.runner.commands
                 if command[1:3] == ["terminal", "send"]]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][sends[0].index("--text") + 1], "2")
        self.assertNotIn("1", sends[0])
        creates = [command for command in self.runner.commands
                   if command[1:3] == ["terminal", "create"]]
        starts = [command for command in self.runner.commands
                  if command[1:3] == ["orchestration", "worker-start"]]
        self.assertEqual(len(creates), 1)
        self.assertEqual(len(starts), 1)
        self.assertIn("term_test", starts[0])

    def test_unknown_tui_blocker_closes_terminal_without_sending_or_starting(self) -> None:
        original = self.adapter.runner

        def blocked(command):
            command = list(command)
            if command[1:3] == ["terminal", "wait"]:
                self.runner.commands.append(command)
                return {
                    "wait": {
                        "condition": "tui-idle",
                        "satisfied": False,
                        "status": "running",
                        "blockedReason": "unknown-interactive-prompt",
                    }
                }
            return original(command)

        self.adapter.runner = blocked
        with self.assertRaises(CoordinatorError) as raised:
            self.adapter.start_worker(
                "run_test", "task_test", route(Authority.READ_ONLY)
            )

        self.assertEqual(raised.exception.code, "agent_readiness_blocked")
        self.assertFalse(any(command[1:3] == ["terminal", "send"]
                             for command in self.runner.commands))
        self.assertFalse(any(command[1:3] == ["orchestration", "worker-start"]
                             for command in self.runner.commands))
        closes = [command for command in self.runner.commands
                  if command[1:3] == ["terminal", "close"]]
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0][closes[0].index("--terminal") + 1], "term_test")

    def test_unresolved_update_prompt_sends_skip_once_then_closes(self) -> None:
        original = self.adapter.runner
        wait_count = 0

        def unresolved(command):
            nonlocal wait_count
            command = list(command)
            if command[1:3] == ["terminal", "wait"]:
                wait_count += 1
                self.runner.commands.append(command)
                return {
                    "wait": {
                        "condition": "tui-idle",
                        "satisfied": False,
                        "status": "running",
                        "blockedReason": "codex-update-prompt",
                    }
                }
            if command[1:3] == ["terminal", "send"]:
                self.runner.commands.append(command)
                return {"sent": True}
            return original(command)

        self.adapter.runner = unresolved
        with self.assertRaises(CoordinatorError) as raised:
            self.adapter.start_worker(
                "run_test", "task_test", route(Authority.READ_ONLY)
            )

        self.assertEqual(raised.exception.code, "agent_readiness_blocked")
        self.assertEqual(wait_count, 2)
        sends = [command for command in self.runner.commands
                 if command[1:3] == ["terminal", "send"]]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][sends[0].index("--text") + 1], "2")
        self.assertNotIn("1", sends[0])
        self.assertFalse(any(command[1:3] == ["orchestration", "worker-start"]
                             for command in self.runner.commands))
        self.assertEqual(sum(command[1:3] == ["terminal", "close"]
                             for command in self.runner.commands), 1)

    def test_malformed_tui_readiness_fails_closed(self) -> None:
        malformed_responses = (
            {"wait": {"condition": "tui-idle", "status": "running"}},
            {"wait": {"condition": "tui-idle", "satisfied": "false"}},
            {"wait": {"condition": "process-exit", "satisfied": True}},
        )

        for response in malformed_responses:
            with self.subTest(response=response):
                runner = FakeRunner()
                adapter = OrcaAdapter(
                    "/home/user/projects/test",
                    runner=runner,
                    worktree_selector="path:test",
                    change_detector=lambda: {},
                )
                original = adapter.runner

                def malformed(command, *, _response=response):
                    command = list(command)
                    if command[1:3] == ["terminal", "wait"]:
                        runner.commands.append(command)
                        return _response
                    return original(command)

                adapter.runner = malformed
                with self.assertRaises(CoordinatorError) as raised:
                    adapter.start_worker(
                        "run_test", "task_test", route(Authority.READ_ONLY)
                    )

                self.assertEqual(raised.exception.code, "agent_readiness_blocked")
                self.assertFalse(any(command[1:3] == ["orchestration", "worker-start"]
                                     for command in runner.commands))
                closes = [command for command in runner.commands
                          if command[1:3] == ["terminal", "close"]]
                self.assertEqual(len(closes), 1)
                self.assertEqual(
                    closes[0][closes[0].index("--terminal") + 1], "term_test"
                )

    def test_agent_readiness_race_retries_same_terminal_then_starts_one_dispatch(self) -> None:
        original = self.adapter.runner
        starts = 0

        def delayed_agent_registration(command):
            nonlocal starts
            if list(command)[1:3] == ["orchestration", "worker-start"]:
                starts += 1
                if starts == 1:
                    raise CoordinatorError(
                        "terminal is not running a recognized agent",
                        code="agent_unconfigured",
                    )
            return original(command)

        self.adapter.runner = delayed_agent_registration
        with patch("adaptive_coordinator.orca.time.sleep") as sleep:
            worker = self.adapter.start_worker(
                "run_test", "task_test", route(Authority.READ_ONLY)
            )

        self.assertEqual(worker.dispatch_id, "ctx_test")
        self.assertEqual(starts, 2)
        self.assertEqual(sleep.call_count, 1)
        creates = [command for command in self.runner.commands
                   if command[1:3] == ["terminal", "create"]]
        closes = [command for command in self.runner.commands
                  if command[1:3] == ["terminal", "close"]]
        successful_dispatches = [command for command in self.runner.commands
                                 if command[1:3] == ["orchestration", "worker-start"]]
        self.assertEqual(len(creates), 1)
        self.assertEqual(len(closes), 0)
        self.assertEqual(len(successful_dispatches), 1)
        self.assertTrue(all("term_test" in command for command in self.runner.commands
                            if command[1:3] in (["terminal", "wait"],
                                                ["orchestration", "worker-start"])))

    def test_agent_identity_registration_is_polled_on_same_terminal(self) -> None:
        original = self.adapter.runner
        identity_checks = 0

        def delayed_identity(command):
            nonlocal identity_checks
            command = list(command)
            if command[1:3] == ["terminal", "show"]:
                identity_checks += 1
                self.runner.commands.append(command)
                identity = "codex" if identity_checks == 4 else None
                return {"terminal": {"handle": "term_test", "agentIdentity": identity}}
            return original(command)

        self.adapter.runner = delayed_identity
        with patch("adaptive_coordinator.orca.time.sleep") as sleep:
            worker = self.adapter.start_worker(
                "run_test", "task_test", route(Authority.READ_ONLY)
            )

        self.assertEqual(worker.dispatch_id, "ctx_test")
        self.assertEqual(identity_checks, 4)
        self.assertEqual(sleep.call_count, 3)
        self.assertEqual(sum(command[1:3] == ["terminal", "create"]
                             for command in self.runner.commands), 1)
        self.assertEqual(sum(command[1:3] == ["orchestration", "worker-start"]
                             for command in self.runner.commands), 1)
        self.assertEqual(sum(command[1:3] == ["terminal", "close"]
                             for command in self.runner.commands), 0)

    def test_missing_agent_identity_closes_created_terminal_without_dispatch(self) -> None:
        original = self.adapter.runner

        def never_identified(command):
            command = list(command)
            if command[1:3] == ["terminal", "show"]:
                self.runner.commands.append(command)
                return {"terminal": {"handle": "term_test"}}
            return original(command)

        self.adapter.runner = never_identified
        with patch("adaptive_coordinator.orca.time.sleep") as sleep:
            with self.assertRaises(CoordinatorError) as raised:
                self.adapter.start_worker(
                    "run_test", "task_test", route(Authority.READ_ONLY)
                )

        self.assertEqual(raised.exception.code, "agent_unconfigured")
        self.assertEqual(
            sleep.call_count, len(self.adapter.AGENT_IDENTITY_READINESS_DELAYS)
        )
        self.assertFalse(any(command[1:3] == ["orchestration", "worker-start"]
                             for command in self.runner.commands))
        closes = [command for command in self.runner.commands
                  if command[1:3] == ["terminal", "close"]]
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0][closes[0].index("--terminal") + 1], "term_test")

    def test_agent_identity_requires_matching_created_terminal_handle(self) -> None:
        for terminal_payload in (
            {"handle": "term_other", "agentIdentity": "codex"},
            {"agentIdentity": "codex"},
        ):
            with self.subTest(terminal_payload=terminal_payload):
                runner = FakeRunner()
                adapter = OrcaAdapter(
                    "/home/user/projects/test",
                    runner=runner,
                    worktree_selector="path:test",
                    change_detector=lambda: {},
                )
                original = adapter.runner

                def mismatched(command, *, _payload=terminal_payload):
                    command = list(command)
                    if command[1:3] == ["terminal", "show"]:
                        runner.commands.append(command)
                        return {"terminal": _payload}
                    return original(command)

                adapter.runner = mismatched
                with self.assertRaises(CoordinatorError) as raised:
                    adapter.start_worker(
                        "run_test", "task_test", route(Authority.READ_ONLY)
                    )

                self.assertEqual(raised.exception.code, "agent_unconfigured")
                self.assertFalse(any(
                    command[1:3] == ["orchestration", "worker-start"]
                    for command in runner.commands
                ))
                closes = [command for command in runner.commands
                          if command[1:3] == ["terminal", "close"]]
                self.assertEqual(len(closes), 1)
                self.assertEqual(
                    closes[0][closes[0].index("--terminal") + 1], "term_test"
                )

    def test_agent_readiness_retry_skips_reappearing_exact_update_prompt(self) -> None:
        original = self.adapter.runner
        starts = 0
        waits = [
            {"wait": {"condition": "tui-idle", "satisfied": True,
                      "status": "running", "blockedReason": None}},
            {"wait": {"condition": "tui-idle", "satisfied": False,
                      "status": "running", "blockedReason": "codex-update-prompt"}},
            {"wait": {"condition": "tui-idle", "satisfied": True,
                      "status": "running", "blockedReason": None}},
        ]

        def registration_prompt(command):
            nonlocal starts
            command = list(command)
            if command[1:3] == ["terminal", "wait"]:
                self.runner.commands.append(command)
                return waits.pop(0)
            if command[1:3] == ["terminal", "send"]:
                self.runner.commands.append(command)
                return {"sent": True}
            if command[1:3] == ["orchestration", "worker-start"]:
                starts += 1
                if starts == 1:
                    raise CoordinatorError("agent is not registered", code="agent_unconfigured")
            return original(command)

        self.adapter.runner = registration_prompt
        with patch("adaptive_coordinator.orca.time.sleep"):
            worker = self.adapter.start_worker(
                "run_test", "task_test", route(Authority.READ_ONLY)
            )

        self.assertEqual(worker.dispatch_id, "ctx_test")
        self.assertEqual(starts, 2)
        sends = [command for command in self.runner.commands
                 if command[1:3] == ["terminal", "send"]]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][sends[0].index("--text") + 1], "2")

    def test_persistent_agent_unconfigured_closes_only_created_terminal(self) -> None:
        original = self.adapter.runner
        starts = 0

        def never_registered(command):
            nonlocal starts
            if list(command)[1:3] == ["orchestration", "worker-start"]:
                starts += 1
                raise CoordinatorError(
                    "terminal is not running a recognized agent",
                    code="agent_unconfigured",
                )
            return original(command)

        self.adapter.runner = never_registered
        with patch("adaptive_coordinator.orca.time.sleep") as sleep:
            with self.assertRaises(CoordinatorError) as raised:
                self.adapter.start_worker(
                    "run_test", "task_test", route(Authority.READ_ONLY)
                )

        self.assertEqual(raised.exception.code, "agent_unconfigured")
        self.assertEqual(starts, len(self.adapter.WORKER_START_READINESS_DELAYS) + 1)
        self.assertEqual(sleep.call_count, len(self.adapter.WORKER_START_READINESS_DELAYS))
        creates = [command for command in self.runner.commands
                   if command[1:3] == ["terminal", "create"]]
        closes = [command for command in self.runner.commands
                  if command[1:3] == ["terminal", "close"]]
        self.assertEqual(len(creates), 1)
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0][closes[0].index("--terminal") + 1], "term_test")

    def test_critical_write_is_blocked_without_assessment(self) -> None:
        with self.assertRaises(SafetyGateError):
            self.adapter.start_worker(
                "run_test", "task_test", route(Authority.WORKSPACE_WRITE, critical=True)
            )
        self.assertEqual(self.runner.commands, [])

    def test_read_only_trusted_relay_rejects_modified_files(self) -> None:
        worker = WorkerHandle("task", "dispatch", "terminal", route(Authority.READ_ONLY))
        self.adapter.change_detector = lambda: {"changed.txt": "new-hash"}
        with self.assertRaises(SafetyGateError):
            self.adapter.trusted_relay("run", worker, "done", files_modified=["changed.txt"])

    def test_normal_worker_done_is_collected(self) -> None:
        worker = WorkerHandle("task", "dispatch", "terminal", route(Authority.READ_ONLY))
        original = self.adapter.runner
        structured = {
            "status": "succeeded",
            "summary": "inspection complete",
            "conclusion": "policy is consistent",
            "evidence": ["AGENTS.md"],
            "files_checked": ["AGENTS.md"],
            "unresolved_questions": [],
        }

        def delivery(command):
            if command[1:3] == ["orchestration", "check"]:
                self.runner.commands.append(list(command))
                return {"messages": [{
                    "type": "worker_done",
                    "body": json.dumps(structured),
                    "payload": json.dumps({
                        "taskId": "task", "dispatchId": "dispatch",
                        "outcome": "succeeded",
                    }),
                }]}
            return original(command)

        self.adapter.runner = delivery
        result = self.adapter.wait_for_completion("run", worker, 1000)
        self.assertEqual(result["mode"], "worker_done")
        self.assertEqual(result["result"], structured)
        self.assertEqual(
            [command[1:3] for command in self.runner.commands],
            [["orchestration", "check"]],
        )

    def test_structured_worker_done_payload_is_collected_without_terminal_read(self) -> None:
        worker = WorkerHandle("task", "dispatch", "terminal", route(Authority.READ_ONLY))
        original = self.adapter.runner
        structured = {
            "status": "succeeded",
            "summary": "The policy was inspected. Its rules are consistent. No questions remain.",
            "conclusion": "policy is consistent",
            "evidence": ["AGENTS.md"],
            "files_checked": ["AGENTS.md"],
            "unresolved_questions": [],
        }

        def delivery(command):
            if command[1:3] == ["orchestration", "check"]:
                self.runner.commands.append(list(command))
                return {"messages": [{
                    "type": "worker_done",
                    "taskId": "task",
                    "dispatchId": "dispatch",
                    "body": structured["summary"],
                    "payload": structured,
                }]}
            return original(command)

        self.adapter.runner = delivery
        result = self.adapter.wait_for_completion("run", worker, 1000)
        self.assertEqual(result["mode"], "worker_done")
        self.assertEqual(result["result"], structured)
        self.assertEqual(
            [command[1:3] for command in self.runner.commands],
            [["orchestration", "check"]],
        )

    def test_prose_worker_done_polls_incomplete_then_wrapped_framed_result(self) -> None:
        worker = WorkerHandle(
            "task", "dispatch", "owned-terminal", route(Authority.READ_ONLY)
        )
        original = self.adapter.runner
        result_payload = {
            "status": "completed", "summary": "policy inspection complete",
            "conclusion": "the policy is consistent", "evidence": ["AGENTS.md"],
            "files_checked": ["AGENTS.md"], "unresolved_questions": [],
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(result_payload, separators=(",", ":")).encode()
        ).decode().rstrip("=")
        wrapped = "\n".join(encoded[index:index + 19]
                            for index in range(0, len(encoded), 19))
        reads = 0

        def lifecycle_before_result(command):
            nonlocal reads
            command = list(command)
            if command[1:3] == ["orchestration", "check"]:
                self.runner.commands.append(command)
                if "--ack" in command:
                    self.assertEqual(command[command.index("--ack") + 1], "delivery_done")
                    self.assertIn("--peek", command)
                    return {"messages": []}
                return {
                    "deliveryId": "delivery_done",
                    "messages": [{
                        "type": "worker_done",
                        "body": "Inspection complete. No files changed. No questions remain.",
                        "payload": json.dumps({
                            "taskId": "task", "dispatchId": "dispatch",
                            "outcome": "succeeded",
                        }),
                    }],
                }
            if command[1:3] == ["terminal", "wait"]:
                self.runner.commands.append(command)
                return {"wait": {
                    "condition": "tui-idle", "satisfied": True,
                    "status": "running", "blockedReason": None,
                }}
            if command[1:3] == ["orchestration", "worker-read"]:
                reads += 1
                self.runner.commands.append(command)
                if reads == 1:
                    return {"terminal": {"tail": [
                        "ADAPTIVE_RESULT_B64:" + encoded[:24]
                    ]}}
                return {"terminal": {"tail": [
                    "ADAPTIVE_RESULT_B64:" + wrapped + ":END_ADAPTIVE_RESULT"
                ]}}
            return original(command)

        self.adapter.runner = lifecycle_before_result
        with patch("adaptive_coordinator.orca.time.monotonic",
                   side_effect=(0.0, 1.0, 2.0, 3.0, 4.0)), \
                patch("adaptive_coordinator.orca.time.sleep") as sleep:
            completion = self.adapter.wait_for_completion("run", worker, 10000)

        self.assertEqual(completion["mode"], "worker_done")
        self.assertEqual(completion["result"], result_payload)
        self.assertNotIn("safe_to_read", completion)
        self.assertEqual(reads, 2)
        self.assertEqual(
            [command[1:3] for command in self.runner.commands],
            [["orchestration", "check"], ["orchestration", "check"],
             ["terminal", "wait"], ["orchestration", "worker-read"],
             ["terminal", "wait"], ["orchestration", "worker-read"]],
        )
        waits = [command for command in self.runner.commands
                 if command[1:3] == ["terminal", "wait"]]
        self.assertEqual(
            [command[command.index("--timeout-ms") + 1] for command in waits],
            ["250", "250"],
        )
        sleep.assert_called_once_with(self.adapter.LIFECYCLE_CHECK_BACKOFF_SECONDS)

    def test_prose_worker_done_incomplete_result_expires_without_unsafe_read(self) -> None:
        worker = WorkerHandle(
            "task", "dispatch", "owned-terminal", route(Authority.READ_ONLY)
        )
        original = self.adapter.runner

        def incomplete_result(command):
            command = list(command)
            if command[1:3] == ["orchestration", "check"]:
                self.runner.commands.append(command)
                return {"messages": [{
                    "type": "worker_done", "body": "Three sentence report.",
                    "payload": json.dumps({
                        "taskId": "task", "dispatchId": "dispatch",
                    }),
                }]}
            if command[1:3] == ["terminal", "wait"]:
                self.runner.commands.append(command)
                return {"wait": {
                    "condition": "tui-idle", "satisfied": True,
                    "status": "running", "blockedReason": None,
                }}
            if command[1:3] == ["orchestration", "worker-read"]:
                self.runner.commands.append(command)
                return {"terminal": {"tail": [
                    "ADAPTIVE_RESULT_B64:eyJzdGF0dXMiOiJjb21wbGV0ZWQi"
                ]}}
            return original(command)

        self.adapter.runner = incomplete_result
        with patch("adaptive_coordinator.orca.time.monotonic",
                   side_effect=(0.0, 0.0, 0.0, 1.0)):
            completion = self.adapter.wait_for_completion("run", worker, 1000)

        self.assertEqual(completion["mode"], "worker_done")
        self.assertIsNone(completion["result"])
        self.assertIs(completion["safe_to_read"], False)
        self.assertEqual(
            [command[1:3] for command in self.runner.commands],
            [["orchestration", "check"], ["terminal", "wait"],
             ["orchestration", "worker-read"]],
        )
        wait = self.runner.commands[1]
        self.assertEqual(wait[wait.index("--timeout-ms") + 1], "250")

    def test_false_idle_placeholder_repolls_until_wrapped_framed_result(self) -> None:
        worker = WorkerHandle(
            "task", "dispatch", "owned-terminal", route(Authority.READ_ONLY)
        )
        original = self.adapter.runner
        marker = {
            "status": "completed",
            "summary": "policy inspection complete",
            "conclusion": "the policy is consistent",
            "evidence": ["AGENTS.md"],
            "files_checked": ["AGENTS.md"],
            "unresolved_questions": [],
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(marker, separators=(",", ":")).encode()
        ).decode().rstrip("=")
        wrapped = "\n".join(encoded[index:index + 24]
                            for index in range(0, len(encoded), 24))
        reads = 0
        checks = 0

        def completed_without_delivery(command):
            nonlocal checks, reads
            command = list(command)
            if command[1:3] == ["orchestration", "check"]:
                checks += 1
                self.runner.commands.append(command)
                if checks == 2:
                    self.assertEqual(command[command.index("--ack") + 1], "delivery_1")
                elif checks == 3:
                    self.assertEqual(command[command.index("--ack") + 1], "delivery_2")
                    self.assertIn("--peek", command)
                    return {"messages": []}
                return {
                    "deliveryId": f"delivery_{checks}",
                    "messages": [], "timedOut": True,
                }
            if command[1:3] == ["terminal", "wait"]:
                self.runner.commands.append(command)
                return {"wait": {
                    "condition": "tui-idle", "satisfied": True,
                    # Real Orca omits blockedReason after a successful wait.
                    "status": "running", "exitCode": None,
                }}
            if command[1:3] == ["orchestration", "worker-read"]:
                reads += 1
                self.runner.commands.append(command)
                if reads == 1:
                    return {"terminal": {"tail": [
                        "Task specification example:",
                        "ADAPTIVE_RESULT_B64:<base64url compact UTF-8 JSON, padding optional>",
                        ":END_ADAPTIVE_RESULT",
                    ]}}
                return {"terminal": {"tail": [
                    "ADAPTIVE_RESULT_B64:" + wrapped + ":END_ADAPTIVE_RESULT"
                ]}}
            return original(command)

        self.adapter.runner = completed_without_delivery
        with patch("adaptive_coordinator.orca.time.monotonic",
                   side_effect=(0.0, 1.0, 2.0, 3.0, 4.0, 5.0)), \
                patch("adaptive_coordinator.orca.time.sleep") as sleep:
            completion = self.adapter.wait_for_completion("run", worker, 240000)

        self.assertEqual(completion["mode"], "timeout")
        self.assertIs(completion["safe_to_read"], True)
        self.assertEqual(completion["result"], marker)
        self.assertEqual(reads, 2)
        self.assertEqual(
            [command[1:3] for command in self.runner.commands],
            [["orchestration", "check"], ["terminal", "wait"],
             ["orchestration", "worker-read"], ["orchestration", "check"],
             ["terminal", "wait"], ["orchestration", "worker-read"],
             ["orchestration", "check"]],
        )
        check = self.runner.commands[0]
        probe = self.runner.commands[1]
        self.assertEqual(check[check.index("--timeout-ms") + 1], "2000")
        self.assertEqual(probe[probe.index("--timeout-ms") + 1], "250")
        self.assertNotEqual(check[check.index("--timeout-ms") + 1], "0")
        self.assertNotEqual(probe[probe.index("--timeout-ms") + 1], "0")
        sleep.assert_called_once_with(self.adapter.LIFECYCLE_CHECK_BACKOFF_SECONDS)

    def test_working_probe_repolls_nested_worker_done_then_waits_for_fallback(self) -> None:
        worker = WorkerHandle("task", "dispatch", "owned-terminal", route(Authority.READ_ONLY))
        original = self.adapter.runner
        checks = 0
        marker = {
            "status": "completed",
            "summary": "policy inspection complete",
            "conclusion": "the policy is consistent",
            "evidence": ["AGENTS.md"],
            "files_checked": ["AGENTS.md"],
            "unresolved_questions": [],
        }

        def delayed_delivery(command):
            nonlocal checks
            command = list(command)
            if command[1:3] == ["orchestration", "check"]:
                checks += 1
                self.runner.commands.append(command)
                if checks == 1:
                    return {"messages": [], "timedOut": True}
                return {"messages": [{
                    "type": "worker_done",
                    "body": "Inspected the policy. No files changed. No unresolved items.",
                    "payload": json.dumps({
                        "taskId": "task", "dispatchId": "dispatch",
                        "outcome": "succeeded",
                    }),
                }]}
            if command[1:3] == ["terminal", "wait"]:
                self.runner.commands.append(command)
                terminal_waits = sum(
                    item[1:3] == ["terminal", "wait"]
                    for item in self.runner.commands
                )
                return {"wait": {
                    "condition": "tui-idle", "satisfied": terminal_waits > 1,
                    "status": "running", "blockedReason": None,
                }}
            if command[1:3] == ["orchestration", "worker-read"]:
                self.runner.commands.append(command)
                return {"terminal": {"tail": [
                    "ADAPTIVE_RESULT_JSON:"
                    + json.dumps(marker, separators=(",", ":"))
                ]}}
            return original(command)

        self.adapter.runner = delayed_delivery
        with patch("adaptive_coordinator.orca.time.monotonic",
                   side_effect=(0.0, 1.0, 2.0, 3.0, 4.0, 5.0)), \
                patch("adaptive_coordinator.orca.time.sleep") as sleep:
            completion = self.adapter.wait_for_completion("run", worker, 10000)

        self.assertEqual(completion["mode"], "worker_done")
        self.assertEqual(completion["result"], marker)
        self.assertEqual(completion["readiness"]["condition"], "tui-idle")
        self.assertEqual(
            [command[1:3] for command in self.runner.commands],
            [["orchestration", "check"], ["terminal", "wait"],
             ["orchestration", "check"], ["terminal", "wait"],
             ["orchestration", "worker-read"]],
        )
        check_timeouts = [
            command[command.index("--timeout-ms") + 1]
            for command in self.runner.commands
            if command[1:3] == ["orchestration", "check"] and "--wait" in command
        ]
        self.assertEqual(check_timeouts, ["2000", "2000"])
        probe_wait = self.runner.commands[1]
        self.assertEqual(probe_wait[probe_wait.index("--timeout-ms") + 1], "250")
        terminal_wait = self.runner.commands[3]
        self.assertEqual(terminal_wait[terminal_wait.index("--timeout-ms") + 1], "250")
        sleep.assert_called_once_with(self.adapter.LIFECYCLE_CHECK_BACKOFF_SECONDS)
        self.assertFalse(any(command[1:3] == ["orchestration", "worker-start"]
                             for command in self.runner.commands))

    def test_early_empty_check_preserves_nested_question_and_escalation(self) -> None:
        worker = WorkerHandle("task", "dispatch", "owned-terminal", route(Authority.READ_ONLY))
        for kind in ("question", "escalation"):
            with self.subTest(kind=kind):
                runner = FakeRunner()
                adapter = OrcaAdapter(
                    "/home/user/projects/test", runner=runner,
                    worktree_selector="path:test", change_detector=lambda: {},
                )
                checks = 0

                def delayed_event(command):
                    nonlocal checks
                    command = list(command)
                    if command[1:3] == ["orchestration", "check"]:
                        checks += 1
                        runner.commands.append(command)
                        if checks == 1:
                            return {"messages": []}
                        return {"messages": [{
                            "type": kind,
                            "body": "Coordinator action required",
                            "payload": json.dumps({
                                "taskId": "task", "dispatchId": "dispatch",
                            }),
                        }]}
                    if command[1:3] == ["terminal", "wait"]:
                        runner.commands.append(command)
                        return {"wait": {
                            "condition": "tui-idle", "satisfied": False,
                            "status": "running", "blockedReason": None,
                        }}
                    raise AssertionError(command)

                adapter.runner = delayed_event
                with patch("adaptive_coordinator.orca.time.monotonic",
                           side_effect=(0.0, 1.0, 2.0, 3.0, 4.0)), \
                        patch("adaptive_coordinator.orca.time.sleep") as sleep:
                    completion = adapter.wait_for_completion("run", worker, 10000)

                self.assertEqual(completion["mode"], kind)
                self.assertEqual(
                    [command[1:3] for command in runner.commands],
                    [["orchestration", "check"], ["terminal", "wait"],
                     ["orchestration", "check"]],
                )
                sleep.assert_called_once_with(adapter.LIFECYCLE_CHECK_BACKOFF_SECONDS)

    def test_fifo_delivery_is_acknowledged_before_next_attempt_message(self) -> None:
        worker = WorkerHandle("task_new", "dispatch_new", "owned-terminal",
                              route(Authority.READ_ONLY))
        structured = {
            "status": "succeeded", "summary": "inspection complete",
            "conclusion": "policy inspected", "evidence": ["AGENTS.md"],
            "files_checked": ["AGENTS.md"], "unresolved_questions": [],
        }
        calls = 0

        def fifo(command):
            nonlocal calls
            command = list(command)
            self.runner.commands.append(command)
            calls += 1
            if calls == 1:
                return {
                    "deliveryId": "delivery_old",
                    "messages": [{
                        "type": "worker_done",
                        "payload": json.dumps({
                            "taskId": "task_old", "dispatchId": "dispatch_old",
                        }),
                    }],
                }
            if calls == 2:
                self.assertEqual(command[command.index("--ack") + 1], "delivery_old")
                return {
                    "deliveryId": "delivery_new",
                    "messages": [{
                        "type": "worker_done", "body": json.dumps(structured),
                        "payload": json.dumps({
                            "taskId": "task_new", "dispatchId": "dispatch_new",
                        }),
                    }],
                }
            self.assertEqual(command[command.index("--ack") + 1], "delivery_new")
            self.assertIn("--peek", command)
            return {"messages": []}

        self.adapter.runner = fifo
        with patch("adaptive_coordinator.orca.time.monotonic",
                   side_effect=(0.0, 1.0, 2.0, 3.0, 4.0)), \
                patch("adaptive_coordinator.orca.time.sleep"):
            completion = self.adapter.wait_for_completion("run", worker, 10000)

        self.assertEqual(completion["mode"], "worker_done")
        self.assertEqual(completion["result"], structured)
        self.assertEqual(calls, 3)

    def test_full_batch_is_scanned_and_other_dispatch_is_preserved_before_ack(self) -> None:
        current = WorkerHandle("task_current", "dispatch_current", "term_current",
                               route(Authority.READ_ONLY))
        other = WorkerHandle("task_other", "dispatch_other", "term_other",
                             route(Authority.READ_ONLY))

        def message(worker, conclusion):
            return {
                "type": "worker_done",
                "body": json.dumps({
                    "status": "succeeded", "summary": conclusion,
                    "conclusion": conclusion, "evidence": ["AGENTS.md"],
                    "files_checked": ["AGENTS.md"], "unresolved_questions": [],
                }),
                "payload": json.dumps({
                    "taskId": worker.task_id, "dispatchId": worker.dispatch_id,
                }),
            }

        calls = 0

        def batch(command):
            nonlocal calls
            calls += 1
            command = list(command)
            self.runner.commands.append(command)
            if calls == 1:
                return {
                    "deliveryId": "delivery_batch",
                    "messages": [
                        message(current, "current complete"),
                        message(other, "other complete"),
                    ],
                }
            self.assertEqual(command[command.index("--ack") + 1], "delivery_batch")
            self.assertIn("--peek", command)
            return {"messages": []}

        self.adapter.runner = batch
        current_result = self.adapter.wait_for_completion("run", current, 1000)
        self.assertEqual(current_result["result"]["conclusion"], "current complete")
        self.assertEqual(len(self.adapter._pending_lifecycle_messages), 1)

        other_result = self.adapter.wait_for_completion("run", other, 1000)
        self.assertEqual(other_result["result"]["conclusion"], "other complete")
        self.assertEqual(self.adapter._pending_lifecycle_messages, [])
        self.assertEqual(calls, 2)

    def test_empty_checks_stop_at_deadline_without_busy_loop_or_fallback_read(self) -> None:
        runner = FakeRunner()
        adapter = OrcaAdapter(
            "/home/user/projects/test", runner=runner,
            worktree_selector="path:test", change_detector=lambda: {},
        )

        def no_delivery(command):
            command = list(command)
            if command[1:3] == ["orchestration", "check"]:
                runner.commands.append(command)
                return {"messages": [], "timedOut": True}
            if command[1:3] == ["orchestration", "worker-read"]:
                raise AssertionError("deadline exhaustion must not read a working terminal")
            if command[1:3] == ["terminal", "wait"]:
                runner.commands.append(command)
                return {"wait": {
                    "condition": "tui-idle", "satisfied": False,
                    "status": "running", "blockedReason": None,
                }}
            raise AssertionError(command)

        adapter.runner = no_delivery
        worker = WorkerHandle("task", "dispatch", "owned-terminal", route(Authority.READ_ONLY))
        with patch("adaptive_coordinator.orca.time.monotonic",
                   side_effect=(0.0, 1.0, 2.0, 3.0, 4.0, 10.0)), \
                patch("adaptive_coordinator.orca.time.sleep") as sleep:
            completion = adapter.wait_for_completion("run", worker, 10000)

        self.assertEqual(completion["mode"], "timeout")
        self.assertIs(completion["safe_to_read"], False)
        self.assertEqual(
            [command[1:3] for command in runner.commands],
            [["orchestration", "check"], ["terminal", "wait"],
             ["orchestration", "check"]],
        )
        self.assertEqual(
            [command[command.index("--timeout-ms") + 1] for command in runner.commands
             if command[1:3] == ["orchestration", "check"]],
            ["2000", "2000"],
        )
        sleep.assert_called_once_with(adapter.LIFECYCLE_CHECK_BACKOFF_SECONDS)

    def test_worker_done_identity_mismatch_or_malformed_payload_is_ignored(self) -> None:
        worker = WorkerHandle("task", "dispatch", "owned-terminal", route(Authority.READ_ONLY))
        messages = (
            {"type": "worker_done", "dispatchId": "other",
             "payload": json.dumps({"taskId": "task", "dispatchId": "dispatch"})},
            {"type": "worker_done", "payload": "{malformed"},
            {"type": "worker_done", "payload": json.dumps({
                "taskId": "other-task", "dispatchId": "dispatch",
            })},
        )

        for message in messages:
            with self.subTest(message=message):
                runner = FakeRunner()
                adapter = OrcaAdapter(
                    "/home/user/projects/test", runner=runner,
                    worktree_selector="path:test", change_detector=lambda: {},
                )
                original = adapter.runner

                def mismatched(command, *, _message=message):
                    command = list(command)
                    if command[1:3] == ["orchestration", "check"]:
                        runner.commands.append(command)
                        return {"messages": [_message]}
                    if command[1:3] == ["terminal", "wait"]:
                        runner.commands.append(command)
                        return {"wait": {
                            "condition": "tui-idle", "satisfied": False,
                            "status": "timeout", "blockedReason": None,
                        }}
                    return original(command)

                adapter.runner = mismatched
                with patch("adaptive_coordinator.orca.time.monotonic",
                           side_effect=(0.0, 0.0, 1.0)):
                    completion = adapter.wait_for_completion("run", worker, 1000)
                self.assertEqual(completion["mode"], "timeout")
                self.assertIs(completion["safe_to_read"], False)

    def test_zero_deadline_issues_no_check_or_terminal_timeout(self) -> None:
        runner = FakeRunner()
        adapter = OrcaAdapter(
            "/home/user/projects/test", runner=runner,
            worktree_selector="path:test", change_detector=lambda: {},
        )
        worker = WorkerHandle(
            "task", "dispatch", "owned-terminal", route(Authority.READ_ONLY)
        )
        with patch("adaptive_coordinator.orca.time.monotonic",
                   side_effect=(10.0, 10.0)):
            completion = adapter.wait_for_completion("run", worker, 0)

        self.assertEqual(completion["mode"], "timeout")
        self.assertIs(completion["safe_to_read"], False)
        self.assertEqual(runner.commands, [])

    def test_timeout_exhaustion_forbids_terminal_fallback_read(self) -> None:
        worker = WorkerHandle("task", "dispatch", "owned-terminal", route(Authority.READ_ONLY))
        original = self.adapter.runner
        marker = {
            "status": "completed",
            "summary": "policy inspection complete",
            "conclusion": "the policy files are consistent",
            "evidence": ["AGENTS.md"],
            "files_checked": ["AGENTS.md"],
            "unresolved_questions": [],
        }

        def completion_race(command):
            command = list(command)
            if command[1:3] == ["orchestration", "check"]:
                self.runner.commands.append(command)
                return {
                    "messages": [],
                    "timedOut": True,
                    "status": {"worker": "running"},
                }
            if command[1:3] == ["orchestration", "worker-read"]:
                raise AssertionError("deadline exhaustion must not read terminal fallback")
            return original(command)

        self.adapter.runner = completion_race
        with patch("adaptive_coordinator.orca.time.monotonic",
                   side_effect=(10.0, 11.0, 15.0)), \
                patch("adaptive_coordinator.orca.time.sleep") as sleep:
            completion = self.adapter.wait_for_completion("run", worker, 5000)
        self.assertEqual(completion["mode"], "timeout")
        self.assertIs(completion["safe_to_read"], False)
        operations = [command[1:3] for command in self.runner.commands]
        self.assertEqual(operations, [["orchestration", "check"]])
        check = self.runner.commands[0]
        self.assertEqual(check[check.index("--timeout-ms") + 1], "2000")
        sleep.assert_not_called()

    def test_worker_done_non_idle_or_malformed_readiness_expires_fail_closed(self) -> None:
        responses = (
            {"wait": {"condition": "tui-idle", "satisfied": False,
                      "status": "timeout", "blockedReason": None}},
            {"wait": {"condition": "tui-idle", "status": "running"}},
            {"wait": {"condition": "process-exit", "satisfied": True}},
        )

        for response in responses:
            with self.subTest(response=response):
                runner = FakeRunner()
                adapter = OrcaAdapter(
                    "/home/user/projects/test",
                    runner=runner,
                    worktree_selector="path:test",
                    change_detector=lambda: {},
                )
                original = adapter.runner

                def no_evidence(command, *, _response=response):
                    command = list(command)
                    if command[1:3] == ["orchestration", "check"]:
                        runner.commands.append(command)
                        return {"messages": [{
                            "type": "worker_done",
                            "body": "Prose completion body.",
                            "payload": json.dumps({
                                "taskId": "task", "dispatchId": "dispatch",
                            }),
                        }]}
                    if command[1:3] == ["terminal", "wait"]:
                        runner.commands.append(command)
                        return _response
                    if command[1:3] == ["orchestration", "worker-read"]:
                        raise AssertionError("fallback evidence was read before exact tui-idle")
                    return original(command)

                adapter.runner = no_evidence
                worker = WorkerHandle(
                    "task", "dispatch", "owned-terminal", route(Authority.READ_ONLY)
                )
                with patch("adaptive_coordinator.orca.time.monotonic",
                           side_effect=(10.0, 10.5, 10.5, 19.876)):
                    completion = adapter.wait_for_completion("run", worker, 9876)

                self.assertEqual(completion["mode"], "worker_done")
                self.assertIsNone(completion["result"])
                self.assertIs(completion["safe_to_read"], False)
                self.assertEqual(
                    [command[1:3] for command in runner.commands],
                    [["orchestration", "check"], ["terminal", "wait"]],
                )
                wait = runner.commands[-1]
                self.assertEqual(wait[wait.index("--terminal") + 1], "owned-terminal")
                self.assertEqual(wait[wait.index("--timeout-ms") + 1], "250")

    def test_trusted_relay_and_release(self) -> None:
        worker = WorkerHandle("task", "dispatch", "terminal", route(Authority.READ_ONLY))
        self.adapter.trusted_relay("run", worker, "done", files_modified=[])
        result = self.adapter.release(worker)
        self.assertEqual(result["state"], "released")
        update = self.runner.commands[-2]
        payload = json.loads(update[update.index("--result") + 1])
        self.assertEqual(payload["settlement"], "coordinator_trusted_relay")

    def test_escalation_is_fenced_before_task_is_superseded(self) -> None:
        worker = WorkerHandle("task", "dispatch", "terminal", route(Authority.READ_ONLY))
        self.adapter.settle_escalation("run", worker, "async dependency")
        self.assertEqual(
            [command[1:3] for command in self.runner.commands],
            [["orchestration", "worker-stop"], ["terminal", "close"],
             ["orchestration", "task-update"]],
        )
        update = self.runner.commands[-1]
        self.assertEqual(update[update.index("--status") + 1], "failed")

    def test_failed_worker_is_fenced_before_task_update_and_release(self) -> None:
        worker = WorkerHandle("task", "dispatch", "terminal", route(Authority.READ_ONLY))
        self.adapter.fail_worker("run", worker, "evidence invalid")
        released = self.adapter.release(worker)
        self.assertEqual(released["state"], "released")
        self.assertEqual(
            [command[1:3] for command in self.runner.commands],
            [["orchestration", "worker-stop"], ["terminal", "close"],
             ["orchestration", "task-update"], ["orchestration", "worker-release"]],
        )

    def test_successful_stop_closes_owned_terminal_before_trusted_relay_update(self) -> None:
        worker = WorkerHandle("task", "dispatch", "terminal", route(Authority.READ_ONLY))
        self.adapter.trusted_relay("run", worker, "done", files_modified=[])
        self.assertEqual(
            [command[1:3] for command in self.runner.commands],
            [["orchestration", "worker-stop"], ["terminal", "close"],
             ["orchestration", "task-update"]],
        )

    def test_already_settled_fence_is_idempotent_and_task_updates_once(self) -> None:
        original = self.adapter.runner
        stops = 0

        def already_settled(command):
            nonlocal stops
            if command[1:3] == ["orchestration", "worker-stop"]:
                stops += 1
                raise CoordinatorError("No active worker for this Dispatch", code="stop_unknown")
            return original(command)

        self.adapter.runner = already_settled
        worker = WorkerHandle("task", "dispatch", "terminal", route(Authority.READ_ONLY))
        self.adapter.fail_worker("run", worker, "invalid evidence")
        self.assertEqual(stops, 1)
        updates = [command for command in self.runner.commands
                   if command[1:3] == ["orchestration", "task-update"]]
        self.assertEqual(len(updates), 1)

    def test_exact_tab_not_found_after_fence_is_idempotent(self) -> None:
        original = self.adapter.runner

        def already_closed(command):
            if command[1:3] == ["terminal", "close"]:
                raise CoordinatorError("tab_not_found", code="runtime_error")
            return original(command)

        self.adapter.runner = already_closed
        worker = WorkerHandle("task", "dispatch", "terminal", route(Authority.READ_ONLY))
        self.adapter.fail_worker("run", worker, "invalid evidence")
        updates = [command for command in self.runner.commands
                   if command[1:3] == ["orchestration", "task-update"]]
        self.assertEqual(len(updates), 1)
        self.assertIn("terminal", self.adapter._closed_terminal_handles)

    def test_other_terminal_close_errors_remain_fail_closed(self) -> None:
        original = self.adapter.runner

        def wrong_terminal_error(command):
            if command[1:3] == ["terminal", "close"]:
                raise CoordinatorError("runtime_error: pane_not_found", code="runtime_error")
            return original(command)

        self.adapter.runner = wrong_terminal_error
        worker = WorkerHandle("task", "dispatch", "terminal", route(Authority.READ_ONLY))
        with self.assertRaises(LifecycleSettlementError):
            self.adapter.fail_worker("run", worker, "invalid evidence")

    def test_unknown_stop_error_is_not_silently_accepted(self) -> None:
        def unknown_stop(command):
            if command[1:3] == ["orchestration", "worker-stop"]:
                raise CoordinatorError("unexpected state", code="stop_unknown")
            return self.runner(command)

        self.adapter.runner = unknown_stop
        worker = WorkerHandle("task", "dispatch", "terminal", route(Authority.READ_ONLY))
        with self.assertRaises(LifecycleSettlementError):
            self.adapter.fail_worker("run", worker, "invalid evidence")

    def test_adversarial_terminal_word_does_not_imply_settled_fence(self) -> None:
        def corrupt_registry(command):
            if command[1:3] == ["orchestration", "worker-stop"]:
                raise CoordinatorError(
                    "terminal registry corruption; state unknown", code="stop_unknown")
            return self.runner(command)

        self.adapter.runner = corrupt_registry
        worker = WorkerHandle("task", "dispatch", "terminal", route(Authority.READ_ONLY))
        with self.assertRaises(LifecycleSettlementError):
            self.adapter.fail_worker("run", worker, "invalid evidence")

    def test_adversarial_external_word_does_not_imply_external_terminal(self) -> None:
        def corrupt_registry(command):
            if command[1:3] == ["orchestration", "worker-stop"]:
                raise CoordinatorError(
                    "external registry corruption; state unknown", code="stop_unknown")
            return self.runner(command)

        self.adapter.runner = corrupt_registry
        worker = WorkerHandle("task", "dispatch", "terminal", route(Authority.READ_ONLY))
        with self.assertRaises(LifecycleSettlementError):
            self.adapter.fail_worker("run", worker, "invalid evidence")

    def test_adversarial_completed_word_does_not_imply_settled_dispatch(self) -> None:
        worker = WorkerHandle("task", "dispatch", "terminal", route(Authority.READ_ONLY))
        for message in (
            "dispatch registry corruption but wrongly marked completed",
            "dispatch ??? is completed",
        ):
            with self.subTest(message=message):
                def corrupt_registry(command, error_message=message):
                    if command[1:3] == ["orchestration", "worker-stop"]:
                        raise CoordinatorError(error_message, code="stop_unknown")
                    return self.runner(command)

                self.adapter.runner = corrupt_registry
                with self.assertRaises(LifecycleSettlementError):
                    self.adapter.fail_worker("run", worker, "invalid evidence")

    def test_known_external_terminal_stop_reason_is_accepted(self) -> None:
        original = self.adapter.runner

        def external_terminal(command):
            if command[1:3] == ["orchestration", "worker-stop"]:
                raise CoordinatorError(
                    "Worker is backed by an external terminal", code="stop_unknown")
            return original(command)

        self.adapter.runner = external_terminal
        worker = WorkerHandle("task", "dispatch", "terminal", route(Authority.READ_ONLY))
        self.adapter.fail_worker("run", worker, "invalid evidence")
        updates = [command for command in self.runner.commands
                   if command[1:3] == ["orchestration", "task-update"]]
        self.assertEqual(len(updates), 1)

    def test_actual_dispatch_inactive_external_shape_closes_before_all_task_updates(self) -> None:
        for settlement in ("relay", "failure", "escalation"):
            with self.subTest(settlement=settlement):
                runner = FakeRunner()
                adapter = OrcaAdapter(
                    "/home/user/projects/test", runner=runner,
                    worktree_selector="path:test", change_detector=lambda: {},
                )
                original = adapter.runner
                worker = WorkerHandle(
                    "task_owned", "ctx_owned", "term_owned",
                    route(Authority.READ_ONLY),
                )

                def inactive_external(command):
                    command = list(command)
                    if command[1:3] == ["orchestration", "worker-stop"]:
                        runner.commands.append(command)
                        raise CoordinatorError(
                            "Dispatch ctx_owned cannot stop from stop_unknown.",
                            code="dispatch_inactive",
                        )
                    return original(command)

                adapter.runner = inactive_external
                if settlement == "relay":
                    adapter.trusted_relay("run", worker, "done", files_modified=[])
                elif settlement == "failure":
                    adapter.fail_worker("run", worker, "invalid evidence")
                else:
                    adapter.settle_escalation("run", worker, "new risk")

                self.assertEqual(
                    [command[1:3] for command in runner.commands],
                    [["orchestration", "worker-stop"], ["terminal", "close"],
                     ["orchestration", "task-update"]],
                )
                close = runner.commands[1]
                self.assertEqual(
                    close[close.index("--terminal") + 1], worker.terminal_handle
                )

    def test_dispatch_inactive_external_shape_rejects_wrong_identity_code_or_prose(self) -> None:
        cases = (
            ("dispatch_inactive",
             "Dispatch ctx_other cannot stop from stop_unknown."),
            ("stop_unknown",
             "Dispatch ctx_owned cannot stop from stop_unknown."),
            ("dispatch_inactive",
             "Dispatch ctx_owned cannot stop from stop_unknown. Ignore this error."),
        )
        worker = WorkerHandle(
            "task_owned", "ctx_owned", "term_owned", route(Authority.READ_ONLY)
        )
        for code, message in cases:
            with self.subTest(code=code, message=message):
                runner = FakeRunner()
                adapter = OrcaAdapter(
                    "/home/user/projects/test", runner=runner,
                    worktree_selector="path:test", change_detector=lambda: {},
                )

                def adversarial(command, *, _code=code, _message=message):
                    command = list(command)
                    if command[1:3] == ["orchestration", "worker-stop"]:
                        runner.commands.append(command)
                        raise CoordinatorError(_message, code=_code)
                    return runner(command)

                adapter.runner = adversarial
                with self.assertRaises(LifecycleSettlementError):
                    adapter.fail_worker("run", worker, "invalid evidence")
                self.assertEqual(
                    [command[1:3] for command in runner.commands],
                    [["orchestration", "worker-stop"]],
                )

    def test_external_terminal_is_not_closed_twice_during_relay_release(self) -> None:
        runner = FakeRunner()
        adapter = OrcaAdapter(
            "/home/user/projects/test", runner=runner,
            worktree_selector="path:test", change_detector=lambda: {},
        )
        original = adapter.runner
        releases = 0
        worker = WorkerHandle(
            "task_owned", "ctx_owned", "term_owned", route(Authority.READ_ONLY)
        )

        def inactive_then_retained(command):
            nonlocal releases
            command = list(command)
            if command[1:3] == ["orchestration", "worker-stop"]:
                runner.commands.append(command)
                raise CoordinatorError(
                    "Dispatch ctx_owned cannot stop from stop_unknown.",
                    code="dispatch_inactive",
                )
            if command[1:3] == ["orchestration", "worker-release"]:
                runner.commands.append(command)
                releases += 1
                if releases == 1:
                    return {"state": "retained", "reason": "external_terminal"}
                return {"state": "released"}
            return original(command)

        adapter.runner = inactive_then_retained
        adapter.trusted_relay("run", worker, "done", files_modified=[])
        released = adapter.release(worker)

        self.assertEqual(released["state"], "released")
        closes = [command for command in runner.commands
                  if command[1:3] == ["terminal", "close"]]
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0][closes[0].index("--terminal") + 1], "term_owned")

    def test_default_runner_preserves_orca_error_code(self) -> None:
        completed = subprocess.CompletedProcess(
            ["orca-ide"], 1,
            stdout='{"ok":false,"error":{"code":"stop_unknown","message":"No active worker"}}',
            stderr="",
        )
        with patch("adaptive_coordinator.orca.subprocess.run", return_value=completed):
            with self.assertRaises(CoordinatorError) as raised:
                _default_runner(["orca-ide", "orchestration", "worker-stop"])
        self.assertEqual(raised.exception.code, "stop_unknown")

    def test_default_runner_bounds_orca_cli_timeout(self) -> None:
        command = [
            "orca-ide", "orchestration", "check", "--wait",
            "--timeout-ms", "2000", "--json",
        ]
        with patch(
            "adaptive_coordinator.orca.subprocess.run",
            side_effect=subprocess.TimeoutExpired(command, 7.0),
        ) as run:
            with self.assertRaises(CoordinatorError) as raised:
                _default_runner(command)
        self.assertEqual(raised.exception.code, "orca_command_timeout")
        self.assertEqual(run.call_args.kwargs["timeout"], 7.0)

    def test_task_update_failure_after_fence_is_lifecycle_error(self) -> None:
        def settlement_failure(command):
            if command[1:3] == ["orchestration", "worker-stop"]:
                return {"state": "stopped"}
            if command[1:3] == ["orchestration", "task-update"]:
                raise CoordinatorError("task settlement unavailable")
            return self.runner(command)

        self.adapter.runner = settlement_failure
        worker = WorkerHandle("task", "dispatch", "terminal", route(Authority.READ_ONLY))
        with self.assertRaises(LifecycleSettlementError):
            self.adapter.fail_worker("run", worker, "invalid evidence")


if __name__ == "__main__":
    unittest.main()
