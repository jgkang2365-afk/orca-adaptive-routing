from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

from adaptive_coordinator.models import (
    Authority, InteractionMode, Phase, Route, RoutingPlan, RunMetadata, RunRequest,
)
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
        self.timeline = []
        self.specs = {}
        self.change_detector = lambda: self.changes

    def create_run(self, objective):
        return "run_test"

    def create_task(self, run_id, title, spec):
        self._counter += 1
        self.specs[f"task_{self._counter}"] = spec
        return f"task_{self._counter}"

    def start_worker(self, run_id, task_id, route, assessment_approved=False):
        if route.requires_assessment and not assessment_approved:
            raise AssertionError("assessment gate bypassed")
        self.routes.append(route)
        self.timeline.append(("start", task_id))
        return WorkerHandle(
            task_id,
            f"dispatch_{self._counter}",
            f"term_{self._counter}",
            route,
            (),
        )

    def wait_for_completion(self, run_id, worker, timeout_ms):
        self.timeline.append(("wait", worker.task_id))
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

    def test_preapproved_parent_metadata_is_additive_and_does_not_raise_authority(self):
        adapter = FakeAdapter(Path("/home/user/project"))
        request = RunRequest(
            "Inspect Markdown files. Do not modify files.",
            "/home/user/project",
            RunMetadata(True, True, InteractionMode.NO_INTERVENTION),
        )
        result = ProductionRunner(adapter_factory=lambda _: adapter, timeout_ms=1).run(request)
        payload = result.to_dict()
        self.assertTrue(payload["delegated_by_parent"])
        self.assertTrue(payload["preapproved"])
        self.assertEqual(payload["interaction_mode"], "no-intervention")
        self.assertEqual(payload["parent_mutation_count"], 0)
        self.assertEqual(payload["approval_prompt_count"], 0)
        self.assertEqual(payload["telemetry_scope"], "coordinator-owned")
        self.assertIn("host UI must be measured separately",
                      payload["telemetry_provenance"]["approval_prompt_count"])
        self.assertEqual(adapter.routes[0].authority, Authority.READ_ONLY)

    def test_parallel_read_fanout_starts_all_before_wait_and_joins_once(self):
        task = ("프로젝트의 업무규칙, API 코드 흐름, 관련 테스트를 각각 조사하고 "
                "async 영향도를 종합한 뒤 구현해라.")
        result, adapter = self.run_task(task)
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        first_wait = next(i for i, event in enumerate(adapter.timeline) if event[0] == "wait")
        self.assertEqual(sum(event[0] == "start" for event in adapter.timeline[:first_wait]), 3)
        self.assertEqual(result.subtask_count, 3)
        self.assertEqual(result.launch_barrier_count, 3)
        self.assertEqual(result.max_observed_concurrency, 1)
        self.assertEqual(result.overlap_observation, "not-observed-by-coordinator")
        self.assertEqual(result.write_worker_count, 1)
        self.assertEqual([route.authority for route in adapter.routes[:3]], [Authority.READ_ONLY] * 3)
        self.assertEqual(adapter.routes[3].authority, Authority.WORKSPACE_WRITE)
        implementation_start = adapter.timeline.index(("start", "task_4"))
        self.assertEqual(sum(event[0] == "wait" for event in adapter.timeline[:implementation_start]), 3)
        implementation_spec = adapter.specs["task_4"]
        self.assertIn("scope inspected", implementation_spec)
        self.assertLess(len(implementation_spec.encode()), 20_000)

    def test_failed_read_sibling_only_is_retried(self):
        class OneSiblingFailure(FakeAdapter):
            def __init__(self, workspace):
                super().__init__(workspace)
                self.failed_once = False

            def read_result(self, worker):
                if worker.task_id == "task_2" and not self.failed_once:
                    self.failed_once = True
                    return {"status": "completed", "summary": "evidence report incomplete"}
                return super().read_result(worker)

        adapter = OneSiblingFailure(Path("/home/user/project"))
        runner = ProductionRunner(adapter_factory=lambda _: adapter, timeout_ms=1)
        result = runner.run(
            "업무규칙, API 코드 흐름, 관련 테스트를 각각 조사하고 async 수정해라.",
            "/home/user/project",
        )
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        read_starts = [event for event in adapter.timeline if event[0] == "start"]
        self.assertEqual(len(read_starts), 5)  # three reads, one focused retry, one Lead WRITE
        decisions = [item for item in result.adaptive_decisions
                     if item.get("decision") == "RETRY_SAME_CAPABILITY"]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(result.write_worker_count, 1)

    def test_unsettled_failed_fanout_task_is_failed_before_release(self):
        class TimeoutFailure(FakeAdapter):
            def __init__(self, workspace):
                super().__init__(workspace, modes=["timeout", "worker_done", "worker_done"])
                self.failed_once = False
                self.settlement_events = []

            def fail_worker(self, run_id, worker, reason):
                self.settlement_events.append(("fail_worker", worker.dispatch_id))
                self.fail_task(run_id, worker.task_id, reason)

            def release(self, worker):
                self.settlement_events.append(("release", worker.dispatch_id))
                return super().release(worker)

            def read_result(self, worker):
                if worker.task_id == "task_1" and not self.failed_once:
                    self.failed_once = True
                    return {"status": "completed", "summary": "runtime evidence incomplete"}
                return super().read_result(worker)

        adapter = TimeoutFailure(Path("/home/user/project"))
        result = ProductionRunner(adapter_factory=lambda _: adapter, timeout_ms=1).run(
            "업무규칙, API 코드 흐름, 관련 테스트를 각각 조사하고 파일은 수정하지 마라.",
            "/home/user/project",
        )
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertTrue(any(task_id == "task_1" for task_id, _reason in adapter.failed))
        self.assertIn("dispatch_1", adapter.released)
        self.assertLess(adapter.settlement_events.index(("fail_worker", "dispatch_1")),
                        adapter.settlement_events.index(("release", "dispatch_1")))

    def test_settled_worker_done_with_bad_evidence_is_not_settled_twice(self):
        class SettledBadEvidence(FakeAdapter):
            def __init__(self, workspace):
                super().__init__(workspace)
                self.failed_once = False

            def read_result(self, worker):
                if worker.task_id == "task_1" and not self.failed_once:
                    self.failed_once = True
                    return {"status": "completed", "summary": "done"}
                return super().read_result(worker)

        adapter = SettledBadEvidence(Path("/home/user/project"))
        result = ProductionRunner(adapter_factory=lambda _: adapter, timeout_ms=1).run(
            "업무규칙, API 코드 흐름, 관련 테스트를 각각 조사하고 파일은 수정하지 마라.",
            "/home/user/project",
        )
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual(adapter.failed, [])

    def test_fanout_launch_failure_fences_started_sibling_and_leaves_no_stale_task(self):
        class SecondLaunchFails(FakeAdapter):
            def __init__(self, workspace):
                super().__init__(workspace)
                self.settlement_events = []

            def start_worker(self, run_id, task_id, route, assessment_approved=False):
                if task_id == "task_2":
                    raise RuntimeError("fixture launch failure")
                return super().start_worker(run_id, task_id, route, assessment_approved)

            def fail_worker(self, run_id, worker, reason):
                self.settlement_events.append(("fail_worker", worker.dispatch_id))
                self.fail_task(run_id, worker.task_id, reason)

            def release(self, worker):
                self.settlement_events.append(("release", worker.dispatch_id))
                return super().release(worker)

        adapter = SecondLaunchFails(Path("/home/user/project"))
        result = ProductionRunner(adapter_factory=lambda _: adapter, timeout_ms=1).run(
            "업무규칙, API 코드 흐름, 관련 테스트를 각각 조사하고 파일은 수정하지 마라.",
            "/home/user/project",
        )
        self.assertIs(result.final_status, PhaseStatus.BLOCKED)
        self.assertTrue(any(task_id == "task_1" for task_id, _reason in adapter.failed))
        self.assertTrue(any(task_id == "task_2" for task_id, _reason in adapter.failed))
        self.assertEqual(adapter.released, ["dispatch_1"])
        self.assertEqual(result.worker_count, 1)
        self.assertEqual(adapter.settlement_events,
                         [("fail_worker", "dispatch_1"), ("release", "dispatch_1")])

    def test_early_release_failure_drains_every_started_sibling_before_terminal(self):
        class FirstReleaseFails(FakeAdapter):
            def __init__(self, workspace):
                super().__init__(workspace)
                self.release_attempts = []

            def release(self, worker):
                self.release_attempts.append(worker.dispatch_id)
                if worker.dispatch_id == "dispatch_1":
                    raise RuntimeError("fixture release failure")
                return super().release(worker)

        adapter = FirstReleaseFails(Path("/home/user/project"))
        result = ProductionRunner(adapter_factory=lambda _: adapter, timeout_ms=1).run(
            "업무규칙, API 코드 흐름, 관련 테스트를 각각 조사하고 파일은 수정하지 마라.",
            "/home/user/project",
        )
        self.assertIs(result.final_status, PhaseStatus.FAILED)
        self.assertEqual([event for event in adapter.timeline if event[0] == "wait"], [
            ("wait", "task_1"), ("wait", "task_2"), ("wait", "task_3"),
        ])
        self.assertEqual(adapter.release_attempts,
                         ["dispatch_1", "dispatch_2", "dispatch_3"])
        self.assertEqual(result.worker_count, 3)
        self.assertEqual(len([item for item in result.adaptive_decisions
                              if item.get("decision") == "RETRY_SAME_CAPABILITY"]), 0)

    def test_failed_settlement_is_terminal_even_when_release_succeeds(self):
        class SettlementFails(FakeAdapter):
            def __init__(self, workspace):
                super().__init__(workspace, modes=["timeout", "worker_done", "worker_done"])

            def read_result(self, worker):
                if worker.task_id == "task_1":
                    return {"status": "failed", "reason": "fixture evidence failure"}
                return super().read_result(worker)

            def fail_worker(self, run_id, worker, reason):
                raise RuntimeError("fixture settlement failure")

        adapter = SettlementFails(Path("/home/user/project"))
        result = ProductionRunner(adapter_factory=lambda _: adapter, timeout_ms=1).run(
            "업무규칙, API 코드 흐름, 관련 테스트를 각각 조사하고 파일은 수정하지 마라.",
            "/home/user/project",
        )
        self.assertIs(result.final_status, PhaseStatus.FAILED)
        self.assertEqual(len([event for event in adapter.timeline if event[0] == "start"]), 3)
        self.assertEqual(len([event for event in adapter.timeline if event[0] == "wait"]), 3)
        self.assertEqual(len([item for item in result.adaptive_decisions
                              if item.get("decision") == "RETRY_SAME_CAPABILITY"]), 0)

    def test_fanout_question_blocks_without_retry_after_full_drain(self):
        adapter = FakeAdapter(
            Path("/home/user/project"),
            modes=["question", "worker_done", "worker_done"],
        )
        result = ProductionRunner(adapter_factory=lambda _: adapter, timeout_ms=1).run(
            "업무규칙, API 코드 흐름, 관련 테스트를 각각 조사하고 파일은 수정하지 마라.",
            "/home/user/project",
        )
        self.assertIs(result.final_status, PhaseStatus.BLOCKED)
        self.assertEqual(len([event for event in adapter.timeline if event[0] == "start"]), 3)
        self.assertEqual(len([event for event in adapter.timeline if event[0] == "wait"]), 3)
        self.assertTrue(any(item.get("failure_class") == "USER_ACTION_REQUIRED"
                            for item in result.adaptive_decisions))
        self.assertFalse(any(item.get("decision") == "RETRY_SAME_CAPABILITY"
                             for item in result.adaptive_decisions))

    def test_fanout_capability_escalation_retries_only_failed_sibling_at_exact_next_rank(self):
        class CapabilityEscalation(FakeAdapter):
            def wait_for_completion(self, run_id, worker, timeout_ms):
                self.timeline.append(("wait", worker.task_id))
                if worker.task_id == "task_1":
                    return {"mode": "escalation", "message": {
                        "body": "current reasoning remains insufficient after multiple hypotheses"
                    }}
                return {"mode": "worker_done", "message": {"status": "completed"}}

        adapter = CapabilityEscalation(Path("/home/user/project"))
        result = ProductionRunner(adapter_factory=lambda _: adapter, timeout_ms=1).run(
            "업무규칙, API 코드 흐름, 관련 테스트를 각각 조사하고 파일은 수정하지 마라.",
            "/home/user/project",
        )
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual(len(adapter.settled_escalations), 1)
        self.assertEqual([(route.model, route.effort, route.authority) for route in adapter.routes], [
            (LUNA, "low", Authority.READ_ONLY),
            (LUNA, "low", Authority.READ_ONLY),
            (LUNA, "low", Authority.READ_ONLY),
            (TERRA, "medium", Authority.READ_ONLY),
        ])
        self.assertEqual(len(result.logical_gates["fanout-read-rules"].attempts), 2)
        self.assertEqual(len(result.logical_gates["fanout-read-code"].attempts), 1)
        self.assertEqual(len(result.logical_gates["fanout-read-tests"].attempts), 1)

    def test_fanout_risk_floor_protects_pending_write_without_sibling_replay(self):
        adapter = FakeAdapter(
            Path("/home/user/project"),
            modes=["escalation", "worker_done", "worker_done"],
        )
        result = ProductionRunner(adapter_factory=lambda _: adapter, timeout_ms=1).run(
            "업무규칙, API 코드 흐름, 관련 테스트를 각각 조사하고 async 수정을 구현해라.",
            "/home/user/project",
        )
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual(result.classification, "critical")
        self.assertEqual(len(result.logical_gates["fanout-read-rules"].attempts), 1)
        self.assertEqual(len(result.logical_gates["fanout-read-code"].attempts), 1)
        self.assertEqual(len(result.logical_gates["fanout-read-tests"].attempts), 1)
        tail = adapter.routes[3:]
        self.assertEqual([route.phase for route in tail], [
            Phase.ASSESSMENT, Phase.IMPLEMENTATION, Phase.VERIFICATION,
        ])
        self.assertTrue(tail[1].requires_assessment)
        self.assertEqual([(route.model, route.effort) for route in tail], [(SOL, "medium")] * 3)
        self.assertEqual([route.authority for route in tail], [
            Authority.READ_ONLY, Authority.WORKSPACE_WRITE, Authority.READ_ONLY,
        ])
        self.assertEqual(result.routing_plan["fanout_risk_floor"]["rank"], 3)

    def _assert_multiple_risk_findings_keep_highest_floor(self, findings):
        class MultipleRisks(FakeAdapter):
            def wait_for_completion(self, run_id, worker, timeout_ms):
                self.timeline.append(("wait", worker.task_id))
                index = int(worker.task_id.removeprefix("task_")) - 1
                if index < len(findings):
                    return {"mode": "escalation", "message": {"body": findings[index]}}
                return {"mode": "worker_done", "message": {"status": "completed"}}

        adapter = MultipleRisks(Path("/home/user/project"))
        result = ProductionRunner(adapter_factory=lambda _: adapter, timeout_ms=1).run(
            "업무규칙, API 코드 흐름, 관련 테스트를 각각 조사하고 async 수정을 구현해라.",
            "/home/user/project",
        )
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual(result.routing_plan["fanout_risk_floor"]["rank"], 4)
        finding = result.routing_plan["fanout_risk_floor"]["finding"]
        self.assertIn("destructive migration", finding)
        self.assertIn("authorization", finding)
        self.assertLessEqual(len(finding), 2_000)
        self.assertEqual([len(result.logical_gates[gate].attempts) for gate in (
            "fanout-read-rules", "fanout-read-code", "fanout-read-tests",
        )], [1, 1, 1])
        tail = adapter.routes[3:]
        self.assertEqual([route.phase for route in tail], [
            Phase.ASSESSMENT, Phase.IMPLEMENTATION, Phase.VERIFICATION,
        ])
        self.assertEqual([(route.model, route.effort) for route in tail], [(SOL, "high")] * 3)
        self.assertEqual([route.authority for route in tail], [
            Authority.READ_ONLY, Authority.WORKSPACE_WRITE, Authority.READ_ONLY,
        ])

    def test_high_then_medium_risk_never_downgrades_fanout_floor(self):
        self._assert_multiple_risk_findings_keep_highest_floor([
            "destructive migration with data loss and rollback uncertain",
            "authorization boundary change",
        ])

    def test_medium_then_high_risk_uses_same_monotonic_fanout_floor(self):
        self._assert_multiple_risk_findings_keep_highest_floor([
            "authorization boundary change",
            "destructive migration with data loss and rollback uncertain",
        ])

    def test_fanout_sol_high_requires_structured_evidence_before_xhigh(self):
        class EscalateUntilTerminal(FakeAdapter):
            def wait_for_completion(self, run_id, worker, timeout_ms):
                self.timeline.append(("wait", worker.task_id))
                if worker.task_id in {"task_2", "task_3"}:
                    return {"mode": "worker_done", "message": {"status": "completed"}}
                return {"mode": "escalation", "message": {
                    "body": "reasoning remains insufficient",
                }}

        adapter = EscalateUntilTerminal(Path("/home/user/project"))
        result = ProductionRunner(adapter_factory=lambda _: adapter, timeout_ms=1).run(
            "업무규칙, API 코드 흐름, 관련 테스트를 각각 조사하고 파일은 수정하지 마라.",
            "/home/user/project",
        )
        self.assertIs(result.final_status, PhaseStatus.FAILED)
        self.assertFalse(any(route.effort == "xhigh" for route in adapter.routes))

    def test_fanout_structured_sol_high_can_use_one_read_only_xhigh_per_run(self):
        class AllChildrenEscalate(FakeAdapter):
            def wait_for_completion(self, run_id, worker, timeout_ms):
                self.timeline.append(("wait", worker.task_id))
                if worker.route.effort == "xhigh":
                    return {"mode": "worker_done", "message": {"status": "completed"}}
                message = {"body": "reasoning remains insufficient"}
                if worker.route.model == SOL and worker.route.effort == "high":
                    message.update({
                        "evidence": ["hypothesis A contradicted trace", "hypothesis B contradicted state"],
                        "attempted_hypotheses": ["A", "B"],
                        "unresolved_questions": ["which invariant explains both traces"],
                    })
                return {"mode": "escalation", "message": message}

        adapter = AllChildrenEscalate(Path("/home/user/project"))
        result = ProductionRunner(adapter_factory=lambda _: adapter, timeout_ms=1).run(
            "업무규칙, API 코드 흐름, 관련 테스트를 각각 조사하고 파일은 수정하지 마라.",
            "/home/user/project",
        )
        self.assertIs(result.final_status, PhaseStatus.FAILED)
        xhigh = [route for route in adapter.routes if route.effort == "xhigh"]
        self.assertEqual(len(xhigh), 1)
        self.assertIs(xhigh[0].authority, Authority.READ_ONLY)
        self.assertTrue(any("xhigh" in item.get("decision_reason", "")
                            for item in result.adaptive_decisions))

    def test_fanout_global_hard_fuse_is_enforced_during_launch_barrier(self):
        adapter = FakeAdapter(Path("/home/user/project"))
        result = ProductionRunner(
            adapter_factory=lambda _: adapter, timeout_ms=1, max_attempts_per_run=2,
        ).run(
            "업무규칙, API 코드 흐름, 관련 테스트를 각각 조사하고 파일은 수정하지 마라.",
            "/home/user/project",
        )
        self.assertIs(result.final_status, PhaseStatus.BLOCKED)
        self.assertEqual(result.worker_count, 2)
        self.assertEqual(adapter.released, ["dispatch_1", "dispatch_2"])
        self.assertTrue(any("global attempt fuse exhausted" in item.get("decision_reason", "")
                            for item in result.adaptive_decisions))

    def test_risk_floor_does_not_short_circuit_another_escalated_sibling(self):
        class MixedEscalations(FakeAdapter):
            def wait_for_completion(self, run_id, worker, timeout_ms):
                self.timeline.append(("wait", worker.task_id))
                if worker.task_id == "task_1":
                    return {"mode": "escalation", "message": {
                        "body": "authorization boundary change",
                    }}
                if worker.task_id == "task_2":
                    return {"mode": "escalation", "message": {
                        "body": "current reasoning remains insufficient after multiple hypotheses",
                    }}
                return {"mode": "worker_done", "message": {"status": "completed"}}

        adapter = MixedEscalations(Path("/home/user/project"))
        result = ProductionRunner(adapter_factory=lambda _: adapter, timeout_ms=1).run(
            "업무규칙, API 코드 흐름, 관련 테스트를 각각 조사하고 async 수정을 구현해라.",
            "/home/user/project",
        )
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual(len(result.logical_gates["fanout-read-rules"].attempts), 1)
        self.assertEqual(len(result.logical_gates["fanout-read-code"].attempts), 2)
        self.assertEqual(len(result.logical_gates["fanout-read-tests"].attempts), 1)
        self.assertEqual(adapter.routes[3].phase, Phase.INVESTIGATION)
        self.assertEqual((adapter.routes[3].model, adapter.routes[3].effort), (SOL, "medium"))
        self.assertIs(adapter.routes[3].authority, Authority.READ_ONLY)
        self.assertEqual([route.phase for route in adapter.routes[4:]], [
            Phase.ASSESSMENT, Phase.IMPLEMENTATION, Phase.VERIFICATION,
        ])

    def test_fanout_external_blocker_never_retries_or_escalates(self):
        class ExternalBlocker(FakeAdapter):
            def read_result(self, worker):
                if worker.task_id == "task_1":
                    return {
                        "status": "failed", "summary": "credential unavailable",
                        "external_blocker": "credential unavailable",
                    }
                return super().read_result(worker)

        adapter = ExternalBlocker(Path("/home/user/project"))
        result = ProductionRunner(adapter_factory=lambda _: adapter, timeout_ms=1).run(
            "업무규칙, API 코드 흐름, 관련 테스트를 각각 조사하고 파일은 수정하지 마라.",
            "/home/user/project",
        )
        self.assertIs(result.final_status, PhaseStatus.BLOCKED)
        self.assertTrue(any(item.get("failure_class") == "EXTERNAL_BLOCKER"
                            for item in result.adaptive_decisions))
        self.assertFalse(any(item.get("decision") in {
            "RETRY_SAME_CAPABILITY", "ESCALATE_CAPABILITY"
        } for item in result.adaptive_decisions))

    def test_fanout_unsafe_timeout_drains_all_and_is_terminal(self):
        class UnsafeTimeout(FakeAdapter):
            def wait_for_completion(self, run_id, worker, timeout_ms):
                self.timeline.append(("wait", worker.task_id))
                if worker.task_id == "task_1":
                    return {"mode": "timeout", "safe_to_read": False}
                return {"mode": "worker_done", "message": {"status": "completed"}}

        adapter = UnsafeTimeout(Path("/home/user/project"))
        result = ProductionRunner(adapter_factory=lambda _: adapter, timeout_ms=1).run(
            "업무규칙, API 코드 흐름, 관련 테스트를 각각 조사하고 파일은 수정하지 마라.",
            "/home/user/project",
        )
        self.assertIs(result.final_status, PhaseStatus.FAILED)
        self.assertEqual(len([event for event in adapter.timeline if event[0] == "wait"]), 3)
        self.assertTrue(any(item.get("failure_class") == "ORCHESTRATION_FAILURE"
                            for item in result.adaptive_decisions))
        self.assertFalse(any(item.get("decision") == "ESCALATE_CAPABILITY"
                             for item in result.adaptive_decisions))

    def test_worker_done_result_read_exception_is_terminal_not_retry(self):
        class ResultReadFails(FakeAdapter):
            def read_result(self, worker):
                if worker.task_id == "task_1":
                    raise RuntimeError("fixture result parser failure")
                return super().read_result(worker)

        adapter = ResultReadFails(Path("/home/user/project"))
        result = ProductionRunner(adapter_factory=lambda _: adapter, timeout_ms=1).run(
            "업무규칙, API 코드 흐름, 관련 테스트를 각각 조사하고 파일은 수정하지 마라.",
            "/home/user/project",
        )
        self.assertIs(result.final_status, PhaseStatus.FAILED)
        self.assertEqual(len([event for event in adapter.timeline if event[0] == "start"]), 3)
        self.assertFalse(any(item.get("decision") == "RETRY_SAME_CAPABILITY"
                             for item in result.adaptive_decisions))
        self.assertTrue(any(item.get("failure_class") == "ORCHESTRATION_FAILURE"
                            for item in result.adaptive_decisions))

    def test_trusted_relay_exception_is_terminal_not_retry(self):
        class RelayFails(FakeAdapter):
            def __init__(self, workspace):
                super().__init__(workspace, modes=["timeout", "worker_done", "worker_done"])

            def trusted_relay(self, run_id, worker, summary, files_modified):
                if worker.task_id == "task_1":
                    raise RuntimeError("fixture relay failure")
                return super().trusted_relay(run_id, worker, summary, files_modified)

        adapter = RelayFails(Path("/home/user/project"))
        result = ProductionRunner(adapter_factory=lambda _: adapter, timeout_ms=1).run(
            "업무규칙, API 코드 흐름, 관련 테스트를 각각 조사하고 파일은 수정하지 마라.",
            "/home/user/project",
        )
        self.assertIs(result.final_status, PhaseStatus.FAILED)
        self.assertEqual(len([event for event in adapter.timeline if event[0] == "wait"]), 3)
        self.assertFalse(any(item.get("decision") == "RETRY_SAME_CAPABILITY"
                             for item in result.adaptive_decisions))

    def test_fanout_xhigh_budget_is_shared_with_later_outer_verification(self):
        class SharedXhighBudget(FakeAdapter):
            def __init__(self, workspace):
                super().__init__(workspace)
                self.verification_attempt = 0

            def wait_for_completion(self, run_id, worker, timeout_ms):
                self.timeline.append(("wait", worker.task_id))
                if worker.route.phase is Phase.INVESTIGATION:
                    if worker.task_id in {"task_2", "task_3"} or worker.route.effort == "xhigh":
                        return {"mode": "worker_done", "message": {"status": "completed"}}
                    message = {"body": "reasoning remains insufficient"}
                    if worker.route.model == SOL and worker.route.effort == "high":
                        message.update({
                            "evidence": ["trace contradicts A", "state contradicts B"],
                            "attempted_hypotheses": ["A", "B"],
                            "unresolved_questions": ["which invariant reconciles both"],
                        })
                    return {"mode": "escalation", "message": message}
                return {"mode": "worker_done", "message": {"status": "completed"}}

            def read_result(self, worker):
                if worker.route.phase is Phase.IMPLEMENTATION:
                    return {
                        "status": "completed", "summary": "implementation complete",
                        "files_modified": [], "requirements_completed": ["implemented"],
                        "tests_run": ["unit"], "test_results": ["PASS"],
                        "unexecuted_verification": [], "workspace_diff": [],
                        "verification_mode": "MODEL_REVIEW",
                        "remaining_risk": ["semantic async interaction"],
                    }
                if worker.route.phase is Phase.VERIFICATION:
                    self.verification_attempt += 1
                    return {
                        "status": "completed", "summary": f"verification inconclusive {self.verification_attempt}",
                        "verification_outcome": "INCONCLUSIVE",
                        "evidence": [
                            f"conflicting verifier trace {self.verification_attempt}",
                            f"conflicting verifier state {self.verification_attempt}",
                        ],
                        "unresolved_questions": ["semantic interaction remains unresolved"],
                    }
                return super().read_result(worker)

        adapter = SharedXhighBudget(Path("/home/user/project"))
        result = ProductionRunner(adapter_factory=lambda _: adapter, timeout_ms=1).run(
            "업무규칙, API 코드 흐름, 관련 테스트를 각각 조사하고 async 수정을 구현해라.",
            "/home/user/project",
        )
        self.assertIs(result.final_status, PhaseStatus.FAILED)
        xhigh_routes = [route for route in adapter.routes if route.effort == "xhigh"]
        self.assertEqual(len(xhigh_routes), 1)
        self.assertIs(xhigh_routes[0].phase, Phase.INVESTIGATION)
        self.assertIs(xhigh_routes[0].authority, Authority.READ_ONLY)
        verification_routes = [route for route in adapter.routes if route.phase is Phase.VERIFICATION]
        self.assertTrue(verification_routes)
        self.assertTrue(all(route.effort != "xhigh" for route in verification_routes))
        self.assertTrue(all(route.authority is Authority.READ_ONLY for route in verification_routes))
        self.assertTrue(any("xhigh conditions or budget not satisfied" in
                            (item.get("decision_reason") or "")
                            for item in result.adaptive_decisions))

    def test_two_write_routes_are_serial_and_never_overlap(self):
        write = Route(
            Phase.IMPLEMENTATION, "Lead Implementer", TERRA, "medium",
            Authority.WORKSPACE_WRITE, "REVIEW",
        )

        class TwoWriteRouter:
            def classify(self, task):
                return RoutingPlan("standard", "fixture", (write, write))

        adapter = FakeAdapter(Path("/home/user/project"))
        result = ProductionRunner(
            router=TwoWriteRouter(), adapter_factory=lambda _: adapter, timeout_ms=1
        ).run("Implement two serial fixture changes.", "/home/user/project")
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual(adapter.timeline, [
            ("start", "task_1"), ("wait", "task_1"),
            ("start", "task_2"), ("wait", "task_2"),
        ])
        self.assertEqual(result.write_worker_count, 2)
        self.assertEqual(result.max_observed_concurrency, 1)

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
