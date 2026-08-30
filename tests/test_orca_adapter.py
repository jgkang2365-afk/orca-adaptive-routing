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
            return {"condition": "tui-idle"}
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

        def delivery(command):
            if command[1:3] == ["orchestration", "check"]:
                return {"messages": [{"type": "worker_done", "dispatchId": "dispatch"}]}
            return original(command)

        self.adapter.runner = delivery
        result = self.adapter.wait_for_completion("run", worker, 1000)
        self.assertEqual(result["mode"], "worker_done")

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
