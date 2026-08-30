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
        self.assertIn(
            r"path:\\wsl.localhost\Ubuntu-24.04\home\user\project",
            create,
        )
        worker_start = self.runner.commands[2]
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

    def test_early_empty_check_repolls_nested_worker_done_then_waits_for_fallback(self) -> None:
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
                return {"wait": {
                    "condition": "tui-idle", "satisfied": True,
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
                   side_effect=(0.0, 1.0, 2.0, 3.0)), \
                patch("adaptive_coordinator.orca.time.sleep") as sleep:
            completion = self.adapter.wait_for_completion("run", worker, 10000)
        raw = self.adapter.read_result(worker)

        self.assertEqual(completion["mode"], "worker_done")
        self.assertIsNone(completion["result"])
        self.assertEqual(completion["readiness"]["condition"], "tui-idle")
        self.assertEqual(ResultNormalizer.normalize(raw).status, "COMPLETED")
        self.assertEqual(
            [command[1:3] for command in self.runner.commands],
            [["orchestration", "check"], ["orchestration", "check"],
             ["terminal", "wait"], ["orchestration", "worker-read"]],
        )
        check_timeouts = [
            command[command.index("--timeout-ms") + 1]
            for command in self.runner.commands[:2]
        ]
        self.assertEqual(check_timeouts, ["10000", "8000"])
        terminal_wait = self.runner.commands[2]
        self.assertEqual(terminal_wait[terminal_wait.index("--timeout-ms") + 1], "7000")
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
                    raise AssertionError(command)

                adapter.runner = delayed_event
                with patch("adaptive_coordinator.orca.time.monotonic",
                           side_effect=(0.0, 1.0, 2.0)), \
                        patch("adaptive_coordinator.orca.time.sleep") as sleep:
                    completion = adapter.wait_for_completion("run", worker, 10000)

                self.assertEqual(completion["mode"], kind)
                self.assertEqual(
                    [command[1:3] for command in runner.commands],
                    [["orchestration", "check"], ["orchestration", "check"]],
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
                   side_effect=(0.0, 1.0, 2.0)), \
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
            raise AssertionError(command)

        adapter.runner = no_delivery
        worker = WorkerHandle("task", "dispatch", "owned-terminal", route(Authority.READ_ONLY))
        with patch("adaptive_coordinator.orca.time.monotonic",
                   side_effect=(0.0, 1.0, 2.0, 10.0)), \
                patch("adaptive_coordinator.orca.time.sleep") as sleep:
            completion = adapter.wait_for_completion("run", worker, 10000)

        self.assertEqual(completion["mode"], "timeout")
        self.assertIs(completion["safe_to_read"], False)
        self.assertEqual(
            [command[1:3] for command in runner.commands],
            [["orchestration", "check"], ["orchestration", "check"]],
        )
        self.assertEqual(
            [command[command.index("--timeout-ms") + 1] for command in runner.commands],
            ["10000", "8000"],
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
                completion = adapter.wait_for_completion("run", worker, 0)
                self.assertEqual(completion["mode"], "timeout")
                self.assertIs(completion["safe_to_read"], False)

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
        self.assertEqual(check[check.index("--timeout-ms") + 1], "5000")
        sleep.assert_called_once_with(self.adapter.LIFECYCLE_CHECK_BACKOFF_SECONDS)

    def test_completion_true_timeout_or_malformed_readiness_fails_closed(self) -> None:
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
                           side_effect=(10.0, 10.5)):
                    with self.assertRaises(CoordinatorError) as raised:
                        adapter.wait_for_completion("run", worker, 9876)

                self.assertEqual(raised.exception.code, "agent_readiness_blocked")
                self.assertEqual(
                    [command[1:3] for command in runner.commands],
                    [["orchestration", "check"], ["terminal", "wait"]],
                )
                wait = runner.commands[-1]
                self.assertEqual(wait[wait.index("--terminal") + 1], "owned-terminal")
                self.assertEqual(wait[wait.index("--timeout-ms") + 1], "9375")

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
            [["orchestration", "worker-stop"], ["orchestration", "task-update"]],
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
            [["orchestration", "worker-stop"], ["orchestration", "task-update"],
             ["orchestration", "worker-release"]],
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
