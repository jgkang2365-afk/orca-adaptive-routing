from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

from adaptive_coordinator.models import Authority, Phase
from adaptive_coordinator.orca import LifecycleSettlementError, WorkerHandle
from adaptive_coordinator.runner import PhaseStatus, ProductionRunner, _summary
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
        self.settled_escalations = []
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
        common = {"status": "completed", "summary": f"{worker.route.phase.value} complete"}
        if worker.route.phase is Phase.INVESTIGATION:
            return {**common, "conclusion": "scope inspected", "evidence": ["AGENTS.md"],
                    "files_checked": ["AGENTS.md"], "unresolved_questions": []}
        if worker.route.phase is Phase.ASSESSMENT:
            return {**common, "risks": ["reviewed"], "impact": "bounded", "rollback": "available",
                    "write_ready": True, "unresolved_questions": []}
        if worker.route.phase is Phase.IMPLEMENTATION:
            return {**common, "files_modified": [], "requirements_completed": ["implemented"],
                    "tests_run": ["unit"], "test_results": ["PASS"],
                    "unexecuted_verification": [], "workspace_diff": []}
        return {**common, "verification_outcome": "VERIFIED", "evidence": ["tests PASS"],
                "unresolved_questions": []}

    def trusted_relay(self, run_id, worker, summary, files_modified):
        self.relayed.append((worker.route.phase, tuple(files_modified)))

    def fail_task(self, run_id, task_id, reason):
        self.failed.append((task_id, reason))

    def settle_escalation(self, run_id, worker, finding):
        self.settled_escalations.append((worker.dispatch_id, finding))

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

    def test_structured_worker_done_result_avoids_terminal_fallback_read(self):
        class LifecycleResultAdapter(FakeAdapter):
            def wait_for_completion(self, run_id, worker, timeout_ms):
                return {
                    "mode": "worker_done",
                    "message": {"type": "worker_done"},
                    "result": {
                        "status": "succeeded",
                        "summary": "policy inspection complete",
                        "conclusion": "scope inspected",
                        "evidence": ["AGENTS.md"],
                        "files_checked": ["AGENTS.md"],
                        "unresolved_questions": [],
                    },
                }

            def read_result(self, worker):
                raise AssertionError("structured worker_done must precede terminal fallback")

        adapter = LifecycleResultAdapter(Path("/home/user/project"))
        result = ProductionRunner(adapter_factory=lambda _: adapter, timeout_ms=1).run(
            "Inspect Markdown files. Do not modify files.", "/home/user/project"
        )

        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual(result.phase_list[0].settlement, "worker_done")
        self.assertEqual(len(adapter.routes), 1)

    def test_prose_worker_done_fallback_keeps_settlement_and_single_dispatch(self):
        class ProseLifecycleAdapter(FakeAdapter):
            def wait_for_completion(self, run_id, worker, timeout_ms):
                return {
                    "mode": "worker_done",
                    "message": {"type": "worker_done", "body": "Three sentence report."},
                    "result": None,
                    "readiness": {"condition": "tui-idle", "satisfied": True},
                }

        adapter = ProseLifecycleAdapter(Path("/home/user/project"))
        result = ProductionRunner(adapter_factory=lambda _: adapter, timeout_ms=1).run(
            "Inspect Markdown files. Do not modify files.", "/home/user/project"
        )

        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual(result.phase_list[0].settlement, "worker_done")
        self.assertEqual(len(adapter.routes), 1)
        self.assertEqual(adapter.released, ["dispatch_1"])

    def test_polled_worker_done_result_succeeds_once_without_trusted_relay(self):
        result_payload = {
            "status": "completed", "summary": "policy inspection complete",
            "conclusion": "scope inspected", "evidence": ["AGENTS.md"],
            "files_checked": ["AGENTS.md"], "unresolved_questions": [],
        }

        class PolledLifecycleAdapter(FakeAdapter):
            def wait_for_completion(self, run_id, worker, timeout_ms):
                return {
                    "mode": "worker_done",
                    "message": {"type": "worker_done", "body": "Three sentence report."},
                    "result": result_payload,
                    "readiness": {"condition": "tui-idle", "satisfied": True},
                }

            def read_result(self, worker):
                raise AssertionError("polled worker_done result must not be read twice")

        adapter = PolledLifecycleAdapter(Path("/home/user/project"))
        result = ProductionRunner(adapter_factory=lambda _: adapter, timeout_ms=1).run(
            "Inspect Markdown files. Do not modify files.", "/home/user/project"
        )

        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual(result.phase_list[0].settlement, "worker_done")
        self.assertEqual(len(adapter.routes), 1)
        self.assertEqual(adapter.relayed, [])
        self.assertEqual(adapter.released, ["dispatch_1"])

    def test_worker_done_deadline_without_result_never_rereads_or_succeeds(self):
        class DeadlineLifecycleAdapter(FakeAdapter):
            def wait_for_completion(self, run_id, worker, timeout_ms):
                return {
                    "mode": "worker_done", "safe_to_read": False,
                    "message": {"type": "worker_done", "body": "Three sentence report."},
                    "result": None,
                }

            def read_result(self, worker):
                raise AssertionError("unsafe settled result must not be read")

        adapter = DeadlineLifecycleAdapter(Path("/home/user/project"))
        runner = ProductionRunner(adapter_factory=lambda _: adapter, timeout_ms=1)
        failures = []
        decide = runner.engine.decide

        def capture_failure(gate, route, failure, **kwargs):
            failures.append(failure)
            return decide(gate, route, failure, **kwargs)

        runner.engine.decide = capture_failure
        result = runner.run(
            "Inspect Markdown files. Do not modify files.", "/home/user/project"
        )

        self.assertIs(result.final_status, PhaseStatus.FAILED)
        self.assertEqual(len(adapter.routes), 1)
        self.assertEqual(
            [(route.model, route.effort, route.authority) for route in adapter.routes],
            [(LUNA, "low", Authority.READ_ONLY)],
        )
        self.assertEqual(adapter.relayed, [])
        self.assertEqual(adapter.released, ["dispatch_1"])
        attempt = result.logical_gates["investigation-1"].attempts[0]
        self.assertEqual(attempt.failure_class, "ORCHESTRATION_FAILURE")
        self.assertEqual(failures[0].reason_code, "lifecycle_result_deadline_exhausted")
        self.assertEqual(failures[0].confidence, "high")
        self.assertEqual(attempt.decision, "TERMINAL")
        self.assertEqual(attempt.authority, "read-only")
        self.assertEqual(
            attempt.terminal_reason,
            "ORCHESTRATION_FAILURE is not a model failure",
        )

    def test_b_standard_write(self):
        result, adapter = self.run_task("Implement a small validation helper and unit test.")
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual([r.phase for r in adapter.routes], [Phase.IMPLEMENTATION])
        self.assertIs(adapter.routes[0].authority, Authority.WORKSPACE_WRITE)

    def test_c_complex_write_uses_deterministic_only_when_evidence_is_sufficient(self):
        result, adapter = self.run_task("Fix async external API retry timeout state sync.")
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual(
            [r.phase for r in adapter.routes],
            [Phase.INVESTIGATION, Phase.IMPLEMENTATION],
        )

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
        self.assertNotIn(Phase.IMPLEMENTATION, [r.phase for r in adapter.routes])

    def test_g_escalation_is_reclassified_by_coordinator(self):
        result, adapter = self.run_task(
            "Inspect a display bug.",
            modes=["escalation", "worker_done", "worker_done"],
        )
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertIs(result.phase_list[0].status, PhaseStatus.ESCALATION_REQUESTED)
        self.assertEqual(adapter.routes[1].model, SOL)
        self.assertIs(adapter.routes[1].phase, Phase.INVESTIGATION)
        self.assertEqual(len(adapter.settled_escalations), 1)

    def test_escalated_gate_does_not_replay_completed_plan(self):
        adapter = FakeAdapter(Path("/home/user/project"), modes=["escalation"])

        def completion(run_id, worker, timeout_ms):
            if adapter.modes:
                adapter.modes.pop()
                return {
                    "mode": "escalation",
                    "message": {"body": "async external API retry and timeout discovered"},
                }
            return {"mode": "worker_done", "message": {"status": "completed"}}

        adapter.wait_for_completion = completion
        result = ProductionRunner(adapter_factory=lambda _: adapter, timeout_ms=1).run(
            "Implement a small display fix.", "/home/user/project"
        )
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual([route.phase for route in adapter.routes],
                         [Phase.IMPLEMENTATION, Phase.INVESTIGATION, Phase.IMPLEMENTATION])

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

    def test_marked_worker_read_vsock_tail_uses_trusted_relay_and_releases(self):
        fixture = (Path(__file__).parent / "fixtures" / "orca_payloads_v02.json")
        payload = json.loads(fixture.read_text())["synthetic_worker_read_vsock_marker"]

        class ActualTailAdapter(FakeAdapter):
            read_calls = 0

            def wait_for_completion(self, run_id, worker, timeout_ms):
                return {
                    "mode": "timeout", "safe_to_read": True,
                    "readiness": {
                        "condition": "tui-idle", "satisfied": True,
                        "blockedReason": None,
                    },
                    "delivery": {"messages": []},
                }

            def read_result(self, worker):
                self.read_calls += 1
                return payload

        adapter = ActualTailAdapter(Path("/home/user/project"))
        result = ProductionRunner(adapter_factory=lambda _: adapter, timeout_ms=1).run(
            "Inspect policy files. Do not modify files.", "/home/user/project")
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual(result.phase_list[0].settlement, "coordinator_trusted_relay")
        self.assertEqual(adapter.relayed, [(Phase.INVESTIGATION, ())])
        self.assertEqual(adapter.released, ["dispatch_1"])
        self.assertEqual(adapter.read_calls, 1)

    def test_timeout_result_from_adapter_avoids_duplicate_terminal_read(self):
        result_payload = {
            "status": "completed", "summary": "policy inspection complete",
            "conclusion": "scope inspected", "evidence": ["AGENTS.md"],
            "files_checked": ["AGENTS.md"], "unresolved_questions": [],
        }

        class SentinelAdapter(FakeAdapter):
            def wait_for_completion(self, run_id, worker, timeout_ms):
                return {
                    "mode": "timeout", "safe_to_read": True,
                    "result": result_payload,
                }

            def read_result(self, worker):
                raise AssertionError("validated timeout result must not be read twice")

        adapter = SentinelAdapter(Path("/home/user/project"))
        result = ProductionRunner(adapter_factory=lambda _: adapter, timeout_ms=1).run(
            "Inspect policy files. Do not modify files.", "/home/user/project"
        )

        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual(result.phase_list[0].settlement, "coordinator_trusted_relay")
        self.assertEqual(adapter.relayed, [(Phase.INVESTIGATION, ())])
        self.assertEqual(adapter.released, ["dispatch_1"])

    def test_timeout_without_evidence_fails(self):
        adapter = FakeAdapter(Path("/home/user/project"), modes=["timeout"])
        adapter.read_result = lambda worker: {}
        result = ProductionRunner(adapter_factory=lambda _: adapter, timeout_ms=1).run(
            "Inspect Markdown files.", "/home/user/project"
        )
        self.assertIs(result.final_status, PhaseStatus.FAILED)

    def test_unsafe_deadline_timeout_is_terminal_without_second_worker(self):
        class DeadlineAdapter(FakeAdapter):
            def wait_for_completion(self, run_id, worker, timeout_ms):
                return {"mode": "timeout", "safe_to_read": False, "delivery": {}}

            def read_result(self, worker):
                raise AssertionError("unsafe timeout must not read terminal evidence")

        adapter = DeadlineAdapter(Path("/home/user/project"))
        result = ProductionRunner(adapter_factory=lambda _: adapter, timeout_ms=1).run(
            "Inspect Markdown files. Do not modify files.", "/home/user/project"
        )

        self.assertIs(result.final_status, PhaseStatus.FAILED)
        self.assertEqual(len(adapter.routes), 1)
        self.assertEqual(len(adapter.released), 1)
        attempt = result.logical_gates["investigation-1"].attempts[0]
        self.assertEqual(attempt.failure_class, "ORCHESTRATION_FAILURE")
        self.assertEqual(attempt.decision, "TERMINAL")
        self.assertEqual(attempt.authority, "read-only")

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

    def test_worker_start_failure_settles_created_task(self):
        adapter = FakeAdapter(Path("/home/user/project"))

        def fail_start(*args, **kwargs):
            from adaptive_coordinator.orca import CoordinatorError

            raise CoordinatorError("placement rejected")

        adapter.start_worker = fail_start
        result = ProductionRunner(adapter_factory=lambda _: adapter).run(
            "Inspect project metadata.", "/home/user/project"
        )
        self.assertIs(result.final_status, PhaseStatus.FAILED)
        self.assertEqual(len(adapter.failed), 1)
        self.assertEqual(adapter.failed[0][0], "task_1")
        self.assertIn("ORCHESTRATION_FAILURE", adapter.failed[0][1])

    def test_persistent_agent_unconfigured_is_terminal_without_capability_escalation(self):
        adapter = FakeAdapter(Path("/home/user/project"))
        start_calls = 0

        def fail_start(*args, **kwargs):
            nonlocal start_calls
            from adaptive_coordinator.orca import CoordinatorError

            start_calls += 1
            raise CoordinatorError(
                "terminal is not running a recognized agent",
                code="agent_unconfigured",
            )

        adapter.start_worker = fail_start
        result = ProductionRunner(adapter_factory=lambda _: adapter).run(
            "Inspect project metadata.", "/home/user/project"
        )

        self.assertIs(result.final_status, PhaseStatus.FAILED)
        self.assertEqual(start_calls, 1)
        self.assertEqual(len(adapter.routes), 0)
        self.assertEqual(len(adapter.failed), 1)
        self.assertEqual(len(result.phase_list), 1)
        attempt = result.logical_gates["investigation-1"].attempts[0]
        self.assertEqual(attempt.model, "gpt-5.6-luna")
        self.assertEqual(attempt.failure_class, "ORCHESTRATION_FAILURE")
        self.assertEqual(attempt.decision, "TERMINAL")
        self.assertFalse(any(
            decision.get("decision") == "ESCALATE_CAPABILITY"
            for decision in result.adaptive_decisions
        ))

    def test_unknown_structured_adapter_error_is_terminal_without_model_retry(self):
        adapter = FakeAdapter(Path("/home/user/project"))
        start_calls = 0

        def fail_start(*args, **kwargs):
            nonlocal start_calls
            from adaptive_coordinator.orca import CoordinatorError

            start_calls += 1
            raise CoordinatorError("opaque command rejection", code="other_code")

        adapter.start_worker = fail_start
        result = ProductionRunner(adapter_factory=lambda _: adapter).run(
            "Inspect project metadata.", "/home/user/project"
        )

        self.assertIs(result.final_status, PhaseStatus.FAILED)
        self.assertEqual(start_calls, 1)
        self.assertEqual(len(adapter.failed), 1)
        self.assertEqual(len(result.phase_list), 1)
        attempt = result.logical_gates["investigation-1"].attempts[0]
        self.assertEqual(attempt.model, "gpt-5.6-luna")
        self.assertEqual(attempt.failure_class, "ORCHESTRATION_FAILURE")
        self.assertEqual(attempt.decision, "TERMINAL")
        self.assertFalse(any(
            decision.get("decision") in {
                "RETRY_SAME_CAPABILITY", "ESCALATE_CAPABILITY"
            }
            for decision in result.adaptive_decisions
        ))

    def test_active_dispatch_is_fenced_before_failure_update_and_release(self):
        class ActiveDispatchAdapter(FakeAdapter):
            def __init__(self, workspace):
                super().__init__(workspace, failures={"investigation": "permission denied"})
                self.active = set()
                self.events = []

            def start_worker(self, *args, **kwargs):
                worker = super().start_worker(*args, **kwargs)
                self.active.add(worker.dispatch_id)
                return worker

            def fail_task(self, run_id, task_id, reason):
                self.assert_no_active_dispatch()
                self.events.append("task-update")
                super().fail_task(run_id, task_id, reason)

            def assert_no_active_dispatch(self):
                if self.active:
                    raise AssertionError("task updated while Dispatch is active")

            def fail_worker(self, run_id, worker, reason):
                self.events.append("worker-stop")
                self.active.discard(worker.dispatch_id)
                self.fail_task(run_id, worker.task_id, reason)

            def release(self, worker):
                self.assert_no_active_dispatch()
                self.events.append("worker-release")
                return super().release(worker)

        adapter = ActiveDispatchAdapter(Path("/home/user/project"))
        result = ProductionRunner(adapter_factory=lambda _: adapter).run(
            "Inspect project metadata.", "/home/user/project")
        self.assertIs(result.final_status, PhaseStatus.BLOCKED)
        self.assertEqual(adapter.events, ["worker-stop", "task-update", "worker-release"])

    def test_already_fenced_failure_settlement_error_is_terminal_without_retry(self):
        class SettlementFailureAdapter(FakeAdapter):
            def __init__(self, workspace):
                super().__init__(workspace, failures={"investigation": "permission denied"})
                self.failure_settlements = 0

            def fail_task(self, run_id, task_id, reason):
                self.failure_settlements += 1
                raise LifecycleSettlementError("task update failed after already-settled worker_done")

        adapter = SettlementFailureAdapter(Path("/home/user/project"))
        result = ProductionRunner(adapter_factory=lambda _: adapter).run(
            "Inspect project metadata.", "/home/user/project")
        self.assertIs(result.final_status, PhaseStatus.FAILED)
        self.assertEqual(len(adapter.routes), 1)
        self.assertEqual(adapter.failure_settlements, 1)
        self.assertEqual(adapter.released, ["dispatch_1"])
        self.assertEqual(result.adaptive_decisions[-1]["decision"], "TERMINAL")

    def test_escalation_task_update_failure_is_terminal_without_refence_or_retry(self):
        class EscalationSettlementFailureAdapter(FakeAdapter):
            def __init__(self, workspace):
                super().__init__(workspace, modes=["escalation"])
                self.settlement_calls = 0

            def settle_escalation(self, run_id, worker, finding):
                self.settlement_calls += 1
                raise LifecycleSettlementError("task update failed after escalation fence")

        adapter = EscalationSettlementFailureAdapter(Path("/home/user/project"))
        result = ProductionRunner(adapter_factory=lambda _: adapter).run(
            "Inspect async integration metadata.", "/home/user/project")
        self.assertIs(result.final_status, PhaseStatus.FAILED)
        self.assertEqual(len(adapter.routes), 1)
        self.assertEqual(adapter.settlement_calls, 1)
        self.assertEqual(adapter.released, ["dispatch_1"])

    def test_settled_escalation_uses_captured_finding_without_reading_old_worker(self):
        class SettledEscalationAdapter(FakeAdapter):
            def __init__(self, workspace):
                super().__init__(workspace, modes=["escalation"])
                self.read_calls = 0

            def read_result(self, worker):
                self.read_calls += 1
                if worker.dispatch_id == "dispatch_1":
                    raise RuntimeError("settled worker result unavailable")
                return super().read_result(worker)

        adapter = SettledEscalationAdapter(Path("/home/user/project"))
        result = ProductionRunner(adapter_factory=lambda _: adapter).run(
            "Inspect async integration metadata.", "/home/user/project")
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual(adapter.read_calls, 1)  # only the newly routed worker is read
        self.assertEqual(len(adapter.settled_escalations), 1)

    def test_question_blocks_without_read_retry_or_dispatch_multiplication(self):
        class QuestionAdapter(FakeAdapter):
            def __init__(self, workspace):
                super().__init__(workspace, modes=["question"])
                self.read_calls = 0
                self.failure_settlements = 0

            def read_result(self, worker):
                self.read_calls += 1
                raise RuntimeError("question result unavailable")

            def fail_worker(self, run_id, worker, reason):
                self.failure_settlements += 1
                self.fail_task(run_id, worker.task_id, reason)

        adapter = QuestionAdapter(Path("/home/user/project"))
        result = ProductionRunner(adapter_factory=lambda _: adapter).run(
            "Inspect project metadata.", "/home/user/project")
        self.assertIs(result.final_status, PhaseStatus.BLOCKED)
        self.assertEqual(len(adapter.routes), 1)
        self.assertEqual(adapter.read_calls, 0)
        self.assertEqual(adapter.failure_settlements, 1)

    def test_settled_write_read_failure_preserves_partial_diff_and_never_retries(self):
        class PartialWriteAdapter(FakeAdapter):
            def __init__(self, workspace):
                super().__init__(workspace)
                self.changes = {}
                self.change_detector = lambda: dict(self.changes)

            def read_result(self, worker):
                self.changes["a.py"] = "partial"
                raise RuntimeError("settled worker result unavailable")

        adapter = PartialWriteAdapter(Path("/home/user/project"))
        result = ProductionRunner(adapter_factory=lambda _: adapter).run(
            "Implement the validation fix.", "/home/user/project")
        self.assertIs(result.final_status, PhaseStatus.FAILED)
        self.assertEqual(len(adapter.routes), 1)
        self.assertEqual(result.logical_gates["implementation-1"].attempts[0].files_changed,
                         ("a.py",))
        self.assertEqual(adapter.released, ["dispatch_1"])

    def test_worker_summary_is_bounded_and_does_not_dump_full_transcript(self):
        payload = {
            "source": "terminal",
            "status": {"worker": "succeeded"},
            "terminal": {"tail": ["secret-" + ("x" * 2_000)]},
            "unrelated": "must-not-be-serialized",
        }
        summary = _summary(payload)
        self.assertLessEqual(len(summary), 1_000)
        self.assertNotIn("unrelated", summary)

    def test_conditional_verifier_is_not_forced_for_deterministic_evidence(self):
        adapter = FakeAdapter(Path("/home/user/project"))
        original_start = adapter.start_worker

        def start(run_id, task_id, route, assessment_approved=False):
            if route.phase is Phase.VERIFICATION:
                raise RuntimeError("verifier launch unavailable")
            return original_start(run_id, task_id, route, assessment_approved)

        adapter.start_worker = start
        result = ProductionRunner(adapter_factory=lambda _: adapter).run(
            "Fix async external API retry timeout state sync.", "/home/user/project"
        )
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertNotIn(Phase.VERIFICATION, [route.phase for route in adapter.routes])

    def test_route_authority_never_derives_from_model(self):
        result, adapter = self.run_task("Review an authorization rule without changing it.")
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual({r.model for r in adapter.routes}, {SOL})
        self.assertEqual({r.authority for r in adapter.routes}, {Authority.READ_ONLY})
        self.assertNotIn("danger-full-access", result.to_dict().__repr__())


if __name__ == "__main__":
    unittest.main()
