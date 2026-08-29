from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

from adaptive_coordinator.models import Authority, Phase
from adaptive_coordinator.orca import WorkerHandle
from adaptive_coordinator.runner import PhaseStatus, ProductionRunner
from adaptive_coordinator.routing import LUNA, SOL, TERRA, Router


class FakeAdapter:
    def __init__(
        self,
        workspace: Path,
        *,
        modes: list[str] | None = None,
        failures: dict[str, str] | None = None,
        changes: dict[str, str] | None = None,
    ) -> None:
        self.workspace = workspace
        self.modes = list(modes or [])
        self.failures = failures or {}
        self.changes = changes or {}
        self.routes = []
        self.relayed = []
        self.failed = []
        self.released = []
        self._counter = 0
        self.change_detector = lambda: self.changes

    def create_run(self, objective):
        return "run_test"

    def create_task(self, run_id, title, spec):
        self._counter += 1
        return f"task_{self._counter}"

    def start_worker(self, run_id, task_id, route, assessment_approved=False):
        if route.requires_assessment and not assessment_approved:
            raise AssertionError("assessment gate bypassed")
        self.routes.append(route)
        return WorkerHandle(
            task_id,
            f"dispatch_{self._counter}",
            f"term_{self._counter}",
            route,
            (),
        )

    def wait_for_completion(self, run_id, worker, timeout_ms):
        mode = self.modes.pop(0) if self.modes else "worker_done"
        if mode == "escalation":
            return {
                "mode": mode,
                "message": {
                    "body": "authorization and production data integrity risk discovered"
                },
            }
        return {"mode": mode, "message": {"status": "completed"}}

    def read_result(self, worker):
        failure = self.failures.get(worker.route.phase.value)
        if failure:
            return {"status": "failed", "reason": failure}
        return {"status": "completed", "summary": f"{worker.route.phase.value} complete"}

    def trusted_relay(self, run_id, worker, summary, files_modified):
        self.relayed.append((worker.route.phase, tuple(files_modified)))

    def fail_task(self, run_id, task_id, reason):
        self.failed.append((task_id, reason))

    def release(self, worker):
        self.released.append(worker.dispatch_id)
        return {"state": "released"}


class RunnerContractTests(unittest.TestCase):
    def run_task(self, task, **adapter_kwargs):
        adapter = FakeAdapter(Path("/home/user/project"), **adapter_kwargs)
        runner = ProductionRunner(
            adapter_factory=lambda _: adapter,
            timeout_ms=1,
        )
        return runner.run(task, "/home/user/project"), adapter

    def test_a_routine_read(self):
        result, adapter = self.run_task("Inspect Markdown files. Do not modify files.")
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual([(r.model, r.effort, r.authority) for r in adapter.routes],
                         [(LUNA, "low", Authority.READ_ONLY)])

    def test_b_standard_write(self):
        result, adapter = self.run_task("Implement a small validation helper and unit test.")
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual([r.phase for r in adapter.routes], [Phase.IMPLEMENTATION])
        self.assertIs(adapter.routes[0].authority, Authority.WORKSPACE_WRITE)

    def test_c_complex_write_adds_conditional_verifier(self):
        result, adapter = self.run_task("Fix async external API retry timeout state sync.")
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual(
            [r.phase for r in adapter.routes],
            [Phase.INVESTIGATION, Phase.IMPLEMENTATION, Phase.VERIFICATION],
        )
        self.assertEqual(adapter.routes[-1].model, SOL)
        self.assertEqual(adapter.routes[-1].effort, "medium")
        self.assertIs(adapter.routes[-1].authority, Authority.READ_ONLY)

    def test_d_critical_write_order_and_gate(self):
        result, adapter = self.run_task("Implement a reversible database migration.")
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual(
            [r.phase for r in adapter.routes],
            [Phase.ASSESSMENT, Phase.IMPLEMENTATION, Phase.VERIFICATION],
        )
        self.assertTrue(adapter.routes[1].requires_assessment)

    def test_e_critical_high_records_high_effort(self):
        result, adapter = self.run_task(
            "Implement a destructive migration; production data loss and rollback is uncertain."
        )
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual({r.effort for r in adapter.routes}, {"high"})
        self.assertEqual({r.model for r in adapter.routes}, {SOL})

    def test_f_assessment_failure_prevents_write(self):
        result, adapter = self.run_task(
            "Implement a reversible database migration.",
            failures={"assessment": "rollback plan missing"},
        )
        self.assertIs(result.final_status, PhaseStatus.FAILED)
        self.assertEqual([r.phase for r in adapter.routes], [Phase.ASSESSMENT])

    def test_g_escalation_is_reclassified_by_coordinator(self):
        result, adapter = self.run_task(
            "Inspect a display bug.",
            modes=["escalation", "worker_done", "worker_done"],
        )
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertIs(result.phase_list[0].status, PhaseStatus.ESCALATION_REQUESTED)
        self.assertEqual(adapter.routes[1].model, SOL)
        self.assertIs(adapter.routes[1].phase, Phase.ASSESSMENT)

    def test_h_verifier_failure_propagates(self):
        result, _ = self.run_task(
            "Implement a reversible database migration.",
            failures={"verification": "regression found"},
        )
        self.assertIs(result.final_status, PhaseStatus.FAILED)

    def test_i_trusted_relay_settlement(self):
        result, adapter = self.run_task(
            "Inspect Markdown files. Do not modify files.",
            modes=["timeout"],
        )
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual(adapter.relayed, [(Phase.INVESTIGATION, ())])
        self.assertEqual(result.phase_list[0].settlement, "coordinator_trusted_relay")

    def test_timeout_without_evidence_fails(self):
        adapter = FakeAdapter(Path("/home/user/project"), modes=["timeout"])
        adapter.read_result = lambda worker: {}
        result = ProductionRunner(adapter_factory=lambda _: adapter, timeout_ms=1).run(
            "Inspect Markdown files.", "/home/user/project"
        )
        self.assertIs(result.final_status, PhaseStatus.FAILED)

    def test_j_every_started_worker_is_released(self):
        result, adapter = self.run_task("Implement a reversible database migration.")
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual(len(adapter.released), len(adapter.routes))
        self.assertTrue(all(item["state"] == "released" for item in result.cleanup_result))

    def test_release_failure_prevents_pass(self):
        adapter = FakeAdapter(Path("/home/user/project"))
        adapter.release = lambda worker: {"state": "retained"}
        result = ProductionRunner(adapter_factory=lambda _: adapter).run(
            "Inspect Markdown files.", "/home/user/project"
        )
        self.assertIs(result.final_status, PhaseStatus.FAILED)

    def test_route_authority_never_derives_from_model(self):
        result, adapter = self.run_task("Review an authorization rule without changing it.")
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual({r.model for r in adapter.routes}, {SOL})
        self.assertEqual({r.authority for r in adapter.routes}, {Authority.READ_ONLY})
        self.assertNotIn("danger-full-access", result.to_dict().__repr__())


if __name__ == "__main__":
    unittest.main()
