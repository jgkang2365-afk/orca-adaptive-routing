import json
import unittest
from unittest.mock import patch

from adaptive_coordinator.models import Authority, Phase, Route
from adaptive_coordinator.orca import (
    OrcaAdapter,
    SafetyGateError,
    WorkerHandle,
    _parse_orca_output,
)


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
            "/home/user/project", runner=self.runner, change_detector=lambda: {}
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

    def test_worker_start_uses_custom_terminal_then_supervised_dispatch(self) -> None:
        worker = self.adapter.start_worker("run_test", "task_test", route(Authority.READ_ONLY))
        self.assertEqual(worker.dispatch_id, "ctx_test")
        create = self.runner.commands[0]
        launch_command = create[create.index("--command") + 1]
        self.assertIn("codex", launch_command)
        self.assertIn("read-only", launch_command)
        self.assertIn("current", create)
        self.assertIn("term_test", self.runner.commands[2])

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


if __name__ == "__main__":
    unittest.main()
