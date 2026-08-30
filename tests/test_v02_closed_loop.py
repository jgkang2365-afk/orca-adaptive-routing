from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

from adaptive_coordinator.models import (
    AdaptiveDecision, Authority, FailureClass, LogicalGateState, Phase, Route,
)
from adaptive_coordinator.orca import CoordinatorError, OrcaAdapter, WorkerHandle
from adaptive_coordinator.routing import (
    CAPABILITY_LADDER, LUNA, SOL, TERRA, Router, capability_at, capability_rank,
    next_capability,
)
from adaptive_coordinator.runner import (
    DecisionEngine, FailureClassification, FailureClassifier, NormalizedWorkerResult,
    PhaseStatus, ProductionRunner, ResultNormalizer, SuccessEvidenceGate,
)


def route(rank: int, authority: Authority = Authority.READ_ONLY,
          phase: Phase = Phase.INVESTIGATION) -> Route:
    model, effort = capability_at(rank)
    return Route(phase, "role", model, effort, authority, "SAFE")


class CapabilityPolicyTests(unittest.TestCase):
    def test_exact_ladder_and_plus_one(self):
        self.assertEqual(CAPABILITY_LADDER, (
            (LUNA, "low"), (TERRA, "medium"), (TERRA, "high"),
            (SOL, "medium"), (SOL, "high"), (SOL, "xhigh"),
        ))
        for rank in range(5):
            advanced = next_capability(route(rank))
            self.assertEqual(capability_rank(advanced), rank + 1)
        self.assertIsNone(next_capability(route(5)))

    def test_escalation_preserves_authority_and_never_downgrades(self):
        current = route(0, Authority.READ_ONLY)
        ranks = []
        while current:
            ranks.append(capability_rank(current))
            self.assertIs(current.authority, Authority.READ_ONLY)
            current = next_capability(current)
        self.assertEqual(ranks, sorted(ranks))

    def test_xhigh_budget_and_ceiling(self):
        engine = DecisionEngine(max_xhigh_attempts_per_run=1)
        gate = LogicalGateState("g", "verification", "read-only")
        failure = FailureClassification(FailureClass.CAPABILITY_FAILURE, "high",
                                        "verified_capability_limit", ("root cause unresolved",))
        decision, _ = engine.decide(gate, route(4), failure, material_new_evidence=True, run_xhigh_count=0)
        self.assertIs(decision, AdaptiveDecision.ESCALATE_CAPABILITY)
        decision, _ = engine.decide(gate, route(4), failure, material_new_evidence=True, run_xhigh_count=1)
        self.assertIs(decision, AdaptiveDecision.TERMINAL)
        decision, _ = engine.decide(gate, route(5), failure, material_new_evidence=True, run_xhigh_count=0)
        self.assertIs(decision, AdaptiveDecision.TERMINAL)

    def test_external_and_orchestration_failures_never_escalate(self):
        engine = DecisionEngine()
        gate = LogicalGateState("g", "investigation", "read-only")
        for kind in (FailureClass.EXTERNAL_BLOCKER, FailureClass.USER_ACTION_REQUIRED):
            decision, _ = engine.decide(gate, route(0), FailureClassification(kind, "high", "x"),
                                        material_new_evidence=True, run_xhigh_count=0)
            self.assertIs(decision, AdaptiveDecision.BLOCKED)
        decision, _ = engine.decide(gate, route(0),
            FailureClassification(FailureClass.ORCHESTRATION_FAILURE, "high", "x"),
            material_new_evidence=True, run_xhigh_count=0)
        self.assertIs(decision, AdaptiveDecision.TERMINAL)

    def test_ambiguous_focused_retry_then_plus_one(self):
        engine = DecisionEngine()
        gate = LogicalGateState("g", "verification", "read-only")
        failure = FailureClassification(FailureClass.AMBIGUOUS_FAILURE, "medium", "ambiguous")
        decision, _ = engine.decide(gate, route(2), failure, material_new_evidence=True, run_xhigh_count=0)
        self.assertIs(decision, AdaptiveDecision.RETRY_SAME_CAPABILITY)
        gate.same_level_retries[2] = 1
        decision, _ = engine.decide(gate, route(2), failure, material_new_evidence=True, run_xhigh_count=0)
        self.assertIs(decision, AdaptiveDecision.ESCALATE_CAPABILITY)

    def test_identical_ambiguous_retry_collects_evidence_not_capability(self):
        engine = DecisionEngine()
        gate = LogicalGateState("g", "verification", "read-only", no_progress_count=1)
        gate.same_level_retries[2] = 1
        failure = FailureClassification(FailureClass.AMBIGUOUS_FAILURE, "low", "free_text")
        decision, _ = engine.decide(gate, route(2), failure, material_new_evidence=False, run_xhigh_count=0)
        self.assertIs(decision, AdaptiveDecision.COLLECT_EVIDENCE)
        gate.no_progress_count = 2
        decision, _ = engine.decide(gate, route(2), failure, material_new_evidence=False, run_xhigh_count=0)
        self.assertIs(decision, AdaptiveDecision.TERMINAL)

    def test_write_nontrivial_failure_inserts_read_only_diagnosis(self):
        engine = DecisionEngine()
        gate = LogicalGateState("g", "implementation", "workspace-write")
        decision, _ = engine.decide(gate, route(2, Authority.WORKSPACE_WRITE, Phase.IMPLEMENTATION),
            FailureClassification(FailureClass.CAPABILITY_FAILURE, "high", "reason"),
            material_new_evidence=True, run_xhigh_count=0)
        self.assertIs(decision, AdaptiveDecision.INSERT_READ_ONLY_DIAGNOSIS)

    def test_syntax_hint_cannot_authorize_xhigh(self):
        normalized = ResultNormalizer.normalize({"status": "failed", "summary": "SyntaxError in helper",
                                                  "failure_class_hint": "CAPABILITY_FAILURE"})
        selected = route(4, Authority.WORKSPACE_WRITE, Phase.IMPLEMENTATION)
        classified = FailureClassifier.classify(selected, {"mode": "worker_done"}, normalized, "failed")
        self.assertIs(classified.failure_class, FailureClass.RECOVERABLE_IMPLEMENTATION_FAILURE)
        gate = LogicalGateState("g", "implementation", "workspace-write")
        gate.same_level_retries[4] = 1
        decision, _ = DecisionEngine().decide(gate, selected, classified,
                                               material_new_evidence=True, run_xhigh_count=0)
        self.assertIsNot(decision, AdaptiveDecision.ESCALATE_CAPABILITY)

    def test_worker_capability_hint_requires_focused_retry_before_escalation(self):
        normalized = ResultNormalizer.normalize({
            "status": "failed", "summary": "I need a stronger model",
            "failure_class_hint": "CAPABILITY_FAILURE",
        })
        selected = route(1, Authority.READ_ONLY, Phase.INVESTIGATION)
        classified = FailureClassifier.classify(selected, {"mode": "worker_done"}, normalized, "failed")
        self.assertIs(classified.failure_class, FailureClass.AMBIGUOUS_FAILURE)
        self.assertEqual(classified.confidence, "medium")
        decision, _ = DecisionEngine().decide(
            LogicalGateState("g", "investigation", "read-only"), selected, classified,
            material_new_evidence=True, run_xhigh_count=0)
        self.assertIs(decision, AdaptiveDecision.RETRY_SAME_CAPABILITY)


class BilingualRoutingTests(unittest.TestCase):
    def setUp(self): self.router = Router()

    def assert_same(self, left, right):
        a, b = self.router.classify(left), self.router.classify(right)
        self.assertEqual([(r.model, r.effort, r.authority, r.phase) for r in a.routes],
                         [(r.model, r.effort, r.authority, r.phase) for r in b.routes])
        self.assertEqual((a.level, a.verifier), (b.level, b.verifier))

    def test_bilingual_routine_standard_complex_critical(self):
        pairs = (
            ("Inspect config key locations. Do not modify files.", "설정 키 사용 위치만 조사하라. 파일은 수정하지 마라."),
            ("Implement a localized validation helper.", "로컬 검증 헬퍼를 구현해라."),
            ("Write a validation helper.", "검증 헬퍼를 작성해라."),
            ("Modify the validation helper.", "검증 헬퍼를 수정해라."),
            ("Fix async external service retry and state synchronization.", "비동기 외부 서비스 재시도와 상태 동기화를 수정해라."),
            ("Assess an authorization rule. Do not modify files.", "권한 규칙을 검토하라. 파일은 수정하지 마라."),
        )
        for left, right in pairs:
            with self.subTest(left=left): self.assert_same(left, right)

    def test_korean_negation_boundary_preserves_following_critical_scope(self):
        self.assert_same(
            "Do not modify files. Investigate authentication behavior.",
            "파일은 수정하지 말고 인증 동작을 조사해라.",
        )
        plan = self.router.classify("파일은 수정하지 말고 인증 동작을 조사해라.")
        self.assertEqual(plan.level, "critical")
        self.assertTrue(all(route.authority is Authority.READ_ONLY for route in plan.routes))

    def test_negative_scope_does_not_trigger_write_or_risk(self):
        cases = (
            "데이터베이스는 변경하지 않는다. 파일은 수정하지 말고 조사만 수행해라.",
            "인증과 권한은 작업 범위가 아니다. 설정 위치만 조사해라.",
            "외부 서비스 연동은 이번 작업에서 제외한다. 메타데이터만 조회해라.",
        )
        for task in cases:
            plan = self.router.classify(task)
            self.assertEqual(plan.level, "routine")
            self.assertEqual(plan.routes[0].authority, Authority.READ_ONLY)

    def test_forbidden_standard_scope_is_routine_in_both_languages(self):
        for task in (
            "Code review is out of scope. Inspect metadata only.",
            "코드 리뷰는 작업 범위가 아니다. 메타데이터만 조사해라.",
        ):
            plan = self.router.classify(task)
            self.assertEqual(plan.level, "routine")
            self.assertEqual((plan.routes[0].model, plan.routes[0].effort), (LUNA, "low"))

    def test_forbidden_modify_scope_remains_read_only(self):
        for task in (
            "Do not modify files. Inspect metadata only.",
            "파일은 수정하지 말고 메타데이터만 조사해라.",
        ):
            plan = self.router.classify(task)
            self.assertEqual(plan.level, "routine")
            self.assertEqual(plan.routes[0].authority, Authority.READ_ONLY)

    def test_initial_routing_never_uses_xhigh(self):
        for task in ("Inspect files", "Implement helper", "Fix async retry", "Implement destructive migration"):
            self.assertNotIn("xhigh", {r.effort for r in self.router.classify(task).routes})


class EvidenceContractTests(unittest.TestCase):
    def test_worker_done_without_success_evidence_is_not_success(self):
        normalized = ResultNormalizer.normalize({"status": "completed", "summary": "investigation complete"})
        ok, reason = SuccessEvidenceGate.evaluate(route(0), normalized, [])
        self.assertFalse(ok)
        classified = FailureClassifier.classify(route(0), {"mode": "worker_done"}, normalized, reason)
        self.assertIs(classified.failure_class, FailureClass.INSUFFICIENT_SUCCESS_EVIDENCE)

    def test_product_name_in_incomplete_report_is_not_orchestration_failure(self):
        selected = route(0)
        normalized = ResultNormalizer.normalize({
            "status": "completed", "summary": "Inspected Orca policy, report incomplete",
        })
        ok, reason = SuccessEvidenceGate.evaluate(selected, normalized, [])
        self.assertFalse(ok)
        classified = FailureClassifier.classify(selected, {"mode": "worker_done"}, normalized, reason)
        self.assertIs(classified.failure_class, FailureClass.INSUFFICIENT_SUCCESS_EVIDENCE)

    def test_generic_summary_cannot_replace_investigation_conclusion(self):
        normalized = ResultNormalizer.normalize({
            "status": "completed", "summary": "complete", "evidence": ["file exists"],
            "files_checked": ["a.py"], "unresolved_questions": [],
        })
        self.assertFalse(SuccessEvidenceGate.evaluate(route(0), normalized, [])[0])

    def test_failing_test_blocks_false_success(self):
        r = route(1, Authority.WORKSPACE_WRITE, Phase.IMPLEMENTATION)
        normalized = ResultNormalizer.normalize({"status": "completed", "summary": "completed",
            "files_modified": ["a.py"], "requirements_completed": ["x"], "tests_run": ["unit"],
            "test_results": ["FAIL: assertion"], "unexecuted_verification": [], "workspace_diff": ["a.py"]})
        self.assertFalse(SuccessEvidenceGate.evaluate(r, normalized, ["a.py"])[0])

    def test_implementation_test_results_require_explicit_deterministic_pass(self):
        implementation = route(1, Authority.WORKSPACE_WRITE, Phase.IMPLEMENTATION)
        base = {"status": "completed", "summary": "implemented", "files_modified": [],
                "requirements_completed": ["done"], "tests_run": ["unit"],
                "unexecuted_verification": [], "workspace_diff": []}
        accepted = (
            ["PASS"], ["0 failed, 1 passed"], ["status: passed"],
            {"status": "passed"}, ["all tests passed"],
        )
        for test_results in accepted:
            with self.subTest(accepted=test_results):
                normalized = ResultNormalizer.normalize({**base, "test_results": test_results})
                self.assertTrue(SuccessEvidenceGate.evaluate(implementation, normalized, [])[0])
        rejected = (
            [], ["ERROR during collection"], ["worker crash"], ["NOT RUN"],
            ["SKIPPED"], ["2 skipped"], ["1 failed, 3 passed"], ["FAILED"],
            ["0 failed, 0 passed"],
        )
        for test_results in rejected:
            with self.subTest(rejected=test_results):
                normalized = ResultNormalizer.normalize({**base, "test_results": test_results})
                self.assertFalse(SuccessEvidenceGate.evaluate(implementation, normalized, [])[0])

    def test_empty_tests_run_cannot_succeed(self):
        implementation = route(1, Authority.WORKSPACE_WRITE, Phase.IMPLEMENTATION)
        normalized = ResultNormalizer.normalize({
            "status": "completed", "summary": "implemented", "files_modified": [],
            "requirements_completed": ["done"], "tests_run": [], "test_results": ["PASS"],
            "unexecuted_verification": [], "workspace_diff": []})
        self.assertFalse(SuccessEvidenceGate.evaluate(implementation, normalized, [])[0])

    def test_implementation_requires_completed_requirement_and_truthful_workspace_diff(self):
        implementation = route(1, Authority.WORKSPACE_WRITE, Phase.IMPLEMENTATION)
        base = {
            "status": "completed", "summary": "implemented", "files_modified": ["a.py"],
            "requirements_completed": ["helper implemented"], "tests_run": ["unit"],
            "test_results": ["1 passed"], "unexecuted_verification": [],
            "workspace_diff": ["a.py"],
        }
        self.assertTrue(SuccessEvidenceGate.evaluate(
            implementation, ResultNormalizer.normalize(base), ["a.py"])[0])
        for override in ({"requirements_completed": []}, {"workspace_diff": ["wrong.py"]}):
            self.assertFalse(SuccessEvidenceGate.evaluate(
                implementation, ResultNormalizer.normalize({**base, **override}), ["a.py"])[0])

    def test_target_identity_mismatch_precedes_capability(self):
        normalized = ResultNormalizer.normalize({"status": "completed", "summary": "checked",
            "verification_outcome": "INCONCLUSIVE", "implementation_commit": "aaa", "deployment_commit": "bbb"})
        classification = FailureClassifier.classify(route(3, phase=Phase.VERIFICATION), {"mode": "worker_done"},
                                                      normalized, "not verified", "aaa != bbb")
        self.assertIs(classification.failure_class, FailureClass.TARGET_IDENTITY_MISMATCH)

    def test_non_idempotent_retry_without_safety_proof_blocks(self):
        normalized = ResultNormalizer.normalize({"status": "failed", "summary": "deployment uncertain",
                                                  "non_idempotent_operation": True})
        classification = FailureClassifier.classify(
            route(2, Authority.WORKSPACE_WRITE, Phase.IMPLEMENTATION), {"mode": "worker_done"},
            normalized, "failed")
        self.assertIs(classification.failure_class, FailureClass.USER_ACTION_REQUIRED)

    def test_verification_outcomes_are_distinct(self):
        verifier = route(3, phase=Phase.VERIFICATION)
        verified = ResultNormalizer.normalize({"status": "completed", "summary": "ok",
            "verification_outcome": "VERIFIED", "evidence": ["test"], "unresolved_questions": []})
        self.assertTrue(SuccessEvidenceGate.evaluate(verifier, verified, [])[0])
        for outcome in ("INCONCLUSIVE", "NOT_VERIFIED", "TARGET_FAILED"):
            value = replace(verified, verification_outcome=outcome)
            self.assertFalse(SuccessEvidenceGate.evaluate(verifier, value, [])[0])

    def test_target_failed_precedes_assertion_keyword(self):
        verifier = route(3, phase=Phase.VERIFICATION)
        normalized = ResultNormalizer.normalize({
            "status": "failed", "summary": "assertion failed: regression reproduced",
            "verification_outcome": "TARGET_FAILED", "evidence": ["reproduced"]})
        classified = FailureClassifier.classify(verifier, {"mode": "worker_done"}, normalized, "failed")
        self.assertEqual(classified.reason_code, "verified_target_defect")
        decision, _ = DecisionEngine().decide(LogicalGateState("v", "verification", "read-only"),
                                               verifier, classified, material_new_evidence=True,
                                               run_xhigh_count=0)
        self.assertIs(decision, AdaptiveDecision.REOPEN_IMPLEMENTATION)

    def test_assessment_requires_explicit_approval_recovery_and_no_blocking_questions(self):
        assessment = route(3, phase=Phase.ASSESSMENT)
        base = {"status": "completed", "summary": "assessment", "risks": ["bounded"],
                "impact": "known", "rollback": "available", "write_ready": True,
                "unresolved_questions": []}
        self.assertTrue(SuccessEvidenceGate.evaluate(assessment, ResultNormalizer.normalize(base), [])[0])
        for override in ({"write_ready": False}, {"rollback": ""}, {"rollback": "unknown"},
                         {"unresolved_questions": ["approval missing"]}):
            self.assertFalse(SuccessEvidenceGate.evaluate(
                assessment, ResultNormalizer.normalize({**base, **override}), [])[0])

    def test_evidence_packet_is_bounded(self):
        from adaptive_coordinator.models import AttemptMetadata
        gate = LogicalGateState("g", "verification", "read-only")
        for index in range(10):
            gate.attempts.append(AttemptMetadata("g", f"a{index}", index + 1, None, "verification",
                SOL, "medium", 3, "read-only", failure_class="AMBIGUOUS_FAILURE", decision="COLLECT_EVIDENCE"))
        from adaptive_coordinator.runner import ProductionRunner
        packet = ProductionRunner._evidence_packet(gate)
        self.assertLessEqual(len(packet.previous_attempts_summary), 3)
        self.assertLess(len(json.dumps(packet.to_dict())), 2000)


class OrcaGoldenPayloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = Path(__file__).parent / "fixtures" / "orca_payloads_v02.json"
        cls.payloads = json.loads(fixture.read_text())

    def test_sanitized_fixture_contract_is_complete(self):
        self.assertEqual(set(self.payloads) - {"_provenance", "actual_worker_show", "actual_dispatch_not_found"}, {"worker_done", "escalation", "question",
            "timeout_valid_evidence", "timeout_no_evidence", "permission_denied", "plan_limitation",
            "worker_placement_failure", "cleanup_failure", "synthetic_worker_read_vsock_marker"})
        provenance = self.payloads["_provenance"]
        self.assertIn("actual_worker_show", provenance["actual_sanitized_captures"])
        self.assertIn("synthetic_worker_read_vsock_marker", provenance["deterministic_synthetic_edge_cases"])
        self.assertNotIn("synthetic_worker_read_vsock_marker", provenance["actual_sanitized_captures"])
        self.assertIn("question", provenance["deterministic_synthetic_edge_cases"])
        self.assertEqual(self.payloads["actual_worker_show"]["result"]["worker"]["residualResources"], [])
        self.assertEqual(self.payloads["actual_dispatch_not_found"]["error"]["code"], "dispatch_not_found")

    def test_marked_worker_read_tail_recovers_explicit_structured_result(self):
        normalized = ResultNormalizer.normalize(self.payloads["synthetic_worker_read_vsock_marker"])
        self.assertEqual(normalized.status, "COMPLETED")
        self.assertEqual(normalized.evidence,
                         ("AGENTS.md", "docs/wsl-worker-runtime.md"))
        self.assertTrue(SuccessEvidenceGate.evaluate(route(0), normalized, [])[0])

    def test_unmarked_documentation_json_and_visible_crash_cannot_succeed(self):
        payload = {
            "source": "terminal", "status": {"worker": "running"},
            "terminal": {"tail": [
                'Documentation example: {"status":"completed","conclusion":"looks good",'
                '"evidence":["doc"],"files_checked":["doc"],"unresolved_questions":[]}',
                "worker crashed before final response",
            ]},
        }
        normalized = ResultNormalizer.normalize(payload)
        self.assertEqual(normalized.status, "RUNNING")
        self.assertFalse(SuccessEvidenceGate.evaluate(route(0), normalized, [])[0])

    def test_unmarked_documentation_json_without_crash_cannot_succeed(self):
        payload = {
            "source": "terminal", "status": {"worker": "running"},
            "terminal": {"tail": [
                'Example only: {"status":"completed","conclusion":"looks good",'
                '"evidence":["doc"],"files_checked":["doc"],"unresolved_questions":[]}',
            ]},
        }
        normalized = ResultNormalizer.normalize(payload)
        self.assertFalse(SuccessEvidenceGate.evaluate(route(0), normalized, [])[0])

    def test_prompt_failure_words_do_not_override_valid_marked_result(self):
        payload = json.loads(json.dumps(self.payloads["synthetic_worker_read_vsock_marker"]))
        payload["command"] = "Investigate this traceback and explain why worker crashed"
        payload["task"] = "Explain why worker crashed"
        normalized = ResultNormalizer.normalize(payload)
        self.assertEqual(normalized.status, "COMPLETED")
        self.assertTrue(SuccessEvidenceGate.evaluate(route(0), normalized, [])[0])

    def test_explicit_failed_orca_status_overrides_marked_success(self):
        payload = json.loads(json.dumps(self.payloads["synthetic_worker_read_vsock_marker"]))
        payload["status"]["worker"] = "failed"
        normalized = ResultNormalizer.normalize(payload)
        self.assertEqual(normalized.status, "FAILED")
        self.assertFalse(SuccessEvidenceGate.evaluate(route(0), normalized, [])[0])

    def test_failed_worker_envelope_overrides_rich_legacy_success_result(self):
        payload = {
            "result": {
                "status": "completed", "summary": "inspection complete",
                "conclusion": "policy is consistent", "evidence": ["AGENTS.md"],
                "files_checked": ["AGENTS.md"], "unresolved_questions": [],
            },
            "worker": {"status": "failed"},
        }
        normalized = ResultNormalizer.normalize(payload)
        self.assertEqual(normalized.status, "FAILED")
        self.assertFalse(SuccessEvidenceGate.evaluate(route(0), normalized, [])[0])

    def test_later_malformed_marker_invalidates_earlier_success_marker(self):
        payload = json.loads(json.dumps(self.payloads["synthetic_worker_read_vsock_marker"]))
        payload["terminal"]["tail"].extend([
            'ADAPTIVE_RESULT_JSON:{"status":"completed"',
            "fatal crash",
        ])
        normalized = ResultNormalizer.normalize(payload)
        self.assertEqual(normalized.status, "FAILED")
        self.assertIn("malformed", normalized.reason)
        self.assertFalse(SuccessEvidenceGate.evaluate(route(0), normalized, [])[0])

    def test_test_failure_after_valid_implementation_marker_invalidates_success(self):
        contract = {
            "status": "completed", "summary": "implemented", "files_modified": [],
            "requirements_completed": ["done"], "tests_run": ["unit"],
            "test_results": ["1 passed"], "unexecuted_verification": [],
            "workspace_diff": [],
        }
        payload = {"terminal": {"tail": [
            "ADAPTIVE_RESULT_JSON:" + json.dumps(contract, separators=(",", ":")),
            "1 failed, 1 passed",
        ]}}
        normalized = ResultNormalizer.normalize(payload)
        self.assertEqual(normalized.status, "FAILED")
        self.assertIn("substantive output", normalized.reason)
        self.assertFalse(SuccessEvidenceGate.evaluate(
            route(1, Authority.WORKSPACE_WRITE, Phase.IMPLEMENTATION), normalized, [])[0])

    def test_transport_prefix_cannot_hide_semantic_test_failure(self):
        payload = json.loads(json.dumps(self.payloads["synthetic_worker_read_vsock_marker"]))
        payload["terminal"]["tail"][-1] = "worker_done delivery failed: 5 tests failed"
        normalized = ResultNormalizer.normalize(payload)
        self.assertEqual(normalized.status, "FAILED")
        self.assertIn("substantive output", normalized.reason)

    def test_question_is_not_lost_as_timeout(self):
        adapter = object.__new__(OrcaAdapter)
        adapter.executable = "orca-ide"
        adapter.runner = lambda command: self.payloads["question"]
        worker = WorkerHandle("task", "dispatch_fixture", "terminal", route(0))
        self.assertEqual(adapter.wait_for_completion("run", worker, 1)["mode"], "question")

    def test_worker_done_and_escalation_fixture_transitions(self):
        worker = WorkerHandle("task", "dispatch_fixture", "terminal", route(0))
        for key, expected in (("worker_done", "worker_done"), ("escalation", "escalation")):
            adapter = object.__new__(OrcaAdapter)
            adapter.executable = "orca-ide"
            adapter.runner = lambda command, payload=self.payloads[key]: payload
            self.assertEqual(adapter.wait_for_completion("run", worker, 1)["mode"], expected)

    def test_timeout_fixture_evidence_transitions(self):
        valid = ResultNormalizer.normalize(self.payloads["timeout_valid_evidence"]["result"])
        self.assertTrue(SuccessEvidenceGate.evaluate(route(0), valid, [])[0])
        missing = ResultNormalizer.normalize(self.payloads["timeout_no_evidence"]["result"])
        classified = FailureClassifier.classify(route(0), {"mode": "timeout"}, missing,
                                                  "missing completion evidence")
        self.assertIs(classified.failure_class, FailureClass.EVIDENCE_GAP)

    def test_cleanup_fixture_is_non_success_state(self):
        self.assertNotIn(self.payloads["cleanup_failure"]["state"], {"released", "closed"})

    def test_external_and_placement_payload_classification(self):
        for key, expected in (("permission_denied", FailureClass.EXTERNAL_BLOCKER),
                              ("plan_limitation", FailureClass.EXTERNAL_BLOCKER),
                              ("worker_placement_failure", FailureClass.ORCHESTRATION_FAILURE)):
            normalized = ResultNormalizer.normalize(self.payloads[key])
            failure = FailureClassifier.classify(route(0), {"mode": "worker_done"}, normalized, "failed")
            self.assertIs(failure.failure_class, expected)


class StateMachineInvariantTests(unittest.TestCase):
    def test_transition_matrix_is_finite_and_authority_safe(self):
        engine = DecisionEngine()
        failures = list(FailureClass)
        for rank in range(len(CAPABILITY_LADDER)):
            for authority in (Authority.READ_ONLY, Authority.WORKSPACE_WRITE):
                phase = Phase.INVESTIGATION if authority is Authority.READ_ONLY else Phase.IMPLEMENTATION
                current = route(rank, authority, phase)
                for kind in failures:
                    gate = LogicalGateState("g", phase.value, authority.value)
                    decision, _ = engine.decide(gate, current,
                        FailureClassification(kind, "high", "matrix"), material_new_evidence=True,
                        run_xhigh_count=0)
                    self.assertIn(decision, set(AdaptiveDecision))
                    advanced = next_capability(current) if decision is AdaptiveDecision.ESCALATE_CAPABILITY else None
                    if advanced:
                        self.assertEqual(advanced.authority, authority)
                        self.assertEqual(capability_rank(advanced), rank + 1)


class ClosedLoopAdapter:
    def __init__(self, results, modes=None, release_state="released"):
        self.results = {key: list(values) for key, values in results.items()}
        self.modes = list(modes or [])
        self.release_state = release_state
        self.routes = []
        self.failed = []
        self.released = []
        self._counter = 0
        self.specs = []
        self.change_detector = lambda: {}

    def create_run(self, objective): return "run_v02"
    def create_task(self, run_id, title, spec):
        self._counter += 1
        self.specs.append(spec)
        return f"task_{self._counter}"
    def start_worker(self, run_id, task_id, selected, assessment_approved=False):
        self.routes.append(selected)
        return WorkerHandle(task_id, f"dispatch_{self._counter}", f"term_{self._counter}", selected, ())
    def wait_for_completion(self, run_id, worker, timeout_ms):
        mode = self.modes.pop(0) if self.modes else "worker_done"
        return {"mode": mode, "message": {"type": mode, "dispatchId": worker.dispatch_id}}
    def read_result(self, worker):
        values = self.results.get(worker.route.phase.value, [])
        if values:
            return values.pop(0)
        return evidence(worker.route.phase)
    def trusted_relay(self, run_id, worker, summary, files_modified): pass
    def settle_escalation(self, run_id, worker, finding): pass
    def fail_task(self, run_id, task_id, reason): self.failed.append((task_id, reason))
    def release(self, worker):
        self.released.append(worker.dispatch_id)
        return {"state": self.release_state}


def evidence(phase: Phase, **overrides):
    common = {"status": "completed", "summary": f"{phase.value} result"}
    values = {
        Phase.INVESTIGATION: {"conclusion": "diagnosed", "evidence": ["fact"],
                              "files_checked": ["a"], "unresolved_questions": []},
        Phase.ASSESSMENT: {"risks": ["bounded"], "impact": "known", "rollback": "available",
                           "write_ready": True, "unresolved_questions": []},
        Phase.IMPLEMENTATION: {"files_modified": [], "requirements_completed": ["done"],
                               "tests_run": ["unit"], "test_results": ["PASS"],
                               "unexecuted_verification": [], "workspace_diff": []},
        Phase.VERIFICATION: {"verification_outcome": "VERIFIED", "evidence": ["unit PASS"],
                             "unresolved_questions": []},
    }[phase]
    return {**common, **values, **overrides}


class ProductionClosedLoopTests(unittest.TestCase):
    def run_with(self, task, adapter):
        return ProductionRunner(adapter_factory=lambda _: adapter, timeout_ms=1).run(task, "/home/user/project")

    def test_happy_paths_have_no_additional_dispatch(self):
        for task, expected in (("Inspect metadata. Do not modify files.", 1),
                               ("Implement a validation helper.", 1)):
            adapter = ClosedLoopAdapter({})
            result = self.run_with(task, adapter)
            self.assertIs(result.final_status, PhaseStatus.SUCCESS)
            self.assertEqual(len(adapter.routes), expected)
            self.assertIn("ADAPTIVE_RESULT_JSON:<compact JSON object>", adapter.specs[0])
            self.assertLess(adapter.specs[0].index("First attempt worker_done exactly once"),
                            adapter.specs[0].index("ADAPTIVE_RESULT_JSON:<compact JSON object>"))
            self.assertIn("Do not call any tool after printing that final marker", adapter.specs[0])

    def test_repeated_inconclusive_retries_only_verification_gate(self):
        adapter = ClosedLoopAdapter({"verification": [
            evidence(Phase.VERIFICATION, verification_outcome="INCONCLUSIVE"),
            evidence(Phase.VERIFICATION, verification_outcome="INCONCLUSIVE", evidence=["new log"]),
            evidence(Phase.VERIFICATION),
        ]})
        result = self.run_with("Implement a reversible database migration.", adapter)
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual([r.phase for r in adapter.routes], [Phase.ASSESSMENT, Phase.IMPLEMENTATION,
                         Phase.VERIFICATION, Phase.VERIFICATION, Phase.VERIFICATION])
        self.assertEqual([r.effort for r in adapter.routes[-2:]], ["medium", "high"])

    def test_target_failure_reopens_implementation_then_verifies(self):
        adapter = ClosedLoopAdapter({"verification": [
            evidence(Phase.VERIFICATION, verification_outcome="TARGET_FAILED", evidence=["regression reproduced"]),
            evidence(Phase.VERIFICATION),
        ]})
        result = self.run_with("Implement a reversible database migration.", adapter)
        phases = [r.phase for r in adapter.routes]
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual(phases.count(Phase.ASSESSMENT), 1)
        self.assertEqual(phases.count(Phase.IMPLEMENTATION), 2)
        self.assertEqual(phases.count(Phase.VERIFICATION), 2)
        invalidation = [a for g in result.logical_gates.values() for a in g.attempts if a.prior_gate_invalidated]
        self.assertEqual(len(invalidation), 1)

    def test_external_blocker_blocks_without_capability_escalation(self):
        adapter = ClosedLoopAdapter({"assessment": [{"status": "blocked", "summary": "blocked",
            "external_blocker": "account plan limitation"}]})
        result = self.run_with("Assess a database migration. Do not modify files.", adapter)
        self.assertIs(result.final_status, PhaseStatus.BLOCKED)
        self.assertEqual(len(adapter.routes), 1)
        self.assertEqual(result.adaptive_decisions[0]["failure_class"], "EXTERNAL_BLOCKER")

    def test_unapproved_assessment_never_launches_write_and_terminates(self):
        denied = evidence(Phase.ASSESSMENT, write_ready=False)
        adapter = ClosedLoopAdapter({"assessment": [denied, denied]})
        result = self.run_with("Implement a reversible database migration.", adapter)
        self.assertIs(result.final_status, PhaseStatus.FAILED)
        self.assertEqual([route.phase for route in adapter.routes], [Phase.ASSESSMENT])
        self.assertNotIn(Phase.IMPLEMENTATION, [route.phase for route in adapter.routes])
        self.assertEqual(result.adaptive_decisions[0]["decision"], "TERMINAL")
        self.assertEqual(result.adaptive_decisions[0]["failure_class"], "TERMINAL_FAILURE")

    def test_question_event_blocks_without_escalation(self):
        adapter = ClosedLoopAdapter({}, modes=["question"])
        result = self.run_with("Inspect metadata. Do not modify files.", adapter)
        self.assertIs(result.final_status, PhaseStatus.BLOCKED)
        self.assertEqual(result.adaptive_decisions[0]["failure_class"], "USER_ACTION_REQUIRED")

    def test_success_evidence_gap_repairs_once_then_stops(self):
        incomplete = {"status": "completed", "summary": "complete"}
        adapter = ClosedLoopAdapter({"investigation": [incomplete, incomplete]})
        result = self.run_with("Inspect metadata. Do not modify files.", adapter)
        self.assertIs(result.final_status, PhaseStatus.FAILED)
        self.assertEqual(len(adapter.routes), 2)
        self.assertNotIn("ESCALATE_CAPABILITY", [d["decision"] for d in result.adaptive_decisions])

    def test_write_evidence_repair_does_not_repeat_mutation_authority(self):
        adapter = ClosedLoopAdapter({"implementation": [
            {"status": "completed", "summary": "report incomplete"},
            evidence(Phase.IMPLEMENTATION, prior_mutation_succeeded=True,
                     evidence=["workspace diff and passing test prove prior mutation succeeded"])]})
        result = self.run_with("Implement a validation helper.", adapter)
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual([r.authority for r in adapter.routes],
                         [Authority.WORKSPACE_WRITE, Authority.READ_ONLY])

    def test_read_only_repair_cannot_turn_failed_write_into_success(self):
        failure = {"status": "failed", "summary": "reasoning insufficient",
                   "failure_class_hint": "CAPABILITY_FAILURE"}
        adapter = ClosedLoopAdapter({"implementation": [failure, failure, evidence(Phase.IMPLEMENTATION)]})
        result = self.run_with("Implement the fix.", adapter)
        self.assertIsNot(result.final_status, PhaseStatus.SUCCESS)
        successful_write_attempts = [phase for phase in result.phase_list
                                     if phase.phase == Phase.IMPLEMENTATION.value
                                     and phase.authority == Authority.WORKSPACE_WRITE.value
                                     and phase.status is PhaseStatus.SUCCESS]
        self.assertEqual(successful_write_attempts, [])

    def test_non_idempotent_deployment_failure_does_not_auto_retry(self):
        failure = {"status": "failed", "summary": "production deployment network timeout"}
        adapter = ClosedLoopAdapter({"implementation": [failure, failure]})
        result = self.run_with("Implement the fix and trigger a production deployment.", adapter)
        self.assertIs(result.final_status, PhaseStatus.BLOCKED)
        self.assertEqual(len(adapter.routes), 1)
        self.assertEqual(result.adaptive_decisions[0]["failure_class"], "USER_ACTION_REQUIRED")
        self.assertNotIn("ESCALATE_CAPABILITY", [item["decision"] for item in result.adaptive_decisions])

    def test_nontrivial_write_failure_diagnoses_before_next_write(self):
        adapter = ClosedLoopAdapter({"implementation": [
            {"status": "failed", "summary": "reasoning insufficient",
             "failure_class_hint": "CAPABILITY_FAILURE"}, evidence(Phase.IMPLEMENTATION)]})
        result = self.run_with("Implement a validation helper.", adapter)
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual([r.authority for r in adapter.routes],
                         [Authority.WORKSPACE_WRITE, Authority.READ_ONLY, Authority.WORKSPACE_WRITE])

    def test_complex_prior_investigation_does_not_suppress_write_diagnosis(self):
        adapter = ClosedLoopAdapter({"implementation": [
            {"status": "failed", "summary": "reasoning insufficient",
             "failure_class_hint": "CAPABILITY_FAILURE"}, evidence(Phase.IMPLEMENTATION)]})
        result = self.run_with("Fix async external API retry timeout state sync.", adapter)
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual([(r.phase, r.authority) for r in adapter.routes], [
            (Phase.INVESTIGATION, Authority.READ_ONLY),
            (Phase.IMPLEMENTATION, Authority.WORKSPACE_WRITE),
            (Phase.INVESTIGATION, Authority.READ_ONLY),
            (Phase.IMPLEMENTATION, Authority.WORKSPACE_WRITE),
        ])

    def test_repeated_write_failure_runs_fresh_diagnosis_and_fences_no_progress_write(self):
        failure = {"status": "failed", "summary": "reasoning insufficient",
                   "failure_class_hint": "CAPABILITY_FAILURE"}
        adapter = ClosedLoopAdapter({"implementation": [failure, failure, failure]})
        result = self.run_with("Implement a validation helper.", adapter)
        self.assertIs(result.final_status, PhaseStatus.FAILED)
        self.assertEqual([(r.phase, r.authority) for r in adapter.routes], [
            (Phase.IMPLEMENTATION, Authority.WORKSPACE_WRITE),
            (Phase.INVESTIGATION, Authority.READ_ONLY),
            (Phase.IMPLEMENTATION, Authority.WORKSPACE_WRITE),
            (Phase.INVESTIGATION, Authority.READ_ONLY),
        ])
        diagnosis_gates = [gate for gate in result.logical_gates.values() if gate.parent_gate_id]
        self.assertEqual(len(diagnosis_gates), 2)
        self.assertIn('"verified_facts":["fact","diagnosed","a"]', adapter.specs[2])
        self.assertIn('"failure_class":"AMBIGUOUS_FAILURE"', adapter.specs[2])
        self.assertIn("READ_ONLY diagnosis", adapter.specs[2])
        self.assertIn("next WRITE fenced", result.adaptive_decisions[-1]["decision_reason"])

    def test_partial_write_diff_cannot_be_hidden_by_read_only_repair(self):
        adapter = ClosedLoopAdapter({"implementation": [
            {"status": "completed", "summary": "incomplete report",
             "attempted_actions": ["edited validation branch"],
             "unresolved_questions": ["which edge case is canonical"],
             "tests_run": ["unit validation"], "test_results": ["1 failed, 2 passed"],
             "target_id": "workspace-under-test", "evidence_refs": ["unit-log-1"]},
            evidence(Phase.IMPLEMENTATION)]})
        state = {}
        adapter.change_detector = lambda: dict(state)
        original_read = adapter.read_result
        calls = 0
        def read(worker):
            nonlocal calls
            calls += 1
            if calls == 1:
                state["a.py"] = "modified"
            return original_read(worker)
        adapter.read_result = read
        result = self.run_with("Implement a validation helper.", adapter)
        self.assertIs(result.final_status, PhaseStatus.FAILED)
        self.assertEqual([r.authority for r in adapter.routes],
                         [Authority.WORKSPACE_WRITE, Authority.READ_ONLY])
        self.assertEqual(result.logical_gates["implementation-1"].attempts[-1].files_changed,
                         ("a.py",))
        repair_spec = adapter.specs[1]
        self.assertIn('"files_changed":["a.py"]', repair_spec)
        self.assertIn('"failure_class":"INSUFFICIENT_SUCCESS_EVIDENCE"', repair_spec)
        self.assertIn('"attempted_actions":["edited validation branch","unit validation"]', repair_spec)
        self.assertIn('"unresolved_questions":["which edge case is canonical"]', repair_spec)
        self.assertIn('"test_results":["1 failed, 2 passed"]', repair_spec)
        self.assertIn('"target_fingerprint":[["target_id","workspace-under-test"]]', repair_spec)
        self.assertIn('"relevant_evidence_refs":["unit-log-1"', repair_spec)
        self.assertIn("obtain correct evidence before escalation", repair_spec)
        self.assertNotIn("User task:\\nUser task:", repair_spec)

    def test_new_auth_risk_during_write_inserts_assessment_and_verifier(self):
        adapter = ClosedLoopAdapter({}, modes=["escalation", "worker_done", "worker_done", "worker_done"])
        original_wait = adapter.wait_for_completion
        def wait(run_id, worker, timeout_ms):
            value = original_wait(run_id, worker, timeout_ms)
            if value["mode"] == "escalation":
                value["message"]["body"] = "authorization and data integrity risk discovered"
            return value
        adapter.wait_for_completion = wait
        result = self.run_with("Implement a validation helper.", adapter)
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual([r.phase for r in adapter.routes], [
            Phase.IMPLEMENTATION, Phase.ASSESSMENT, Phase.IMPLEMENTATION, Phase.VERIFICATION])
        self.assertIs(adapter.routes[1].authority, Authority.READ_ONLY)
        self.assertTrue(adapter.routes[2].requires_assessment)
        self.assertIs(adapter.routes[3].authority, Authority.READ_ONLY)
        for index in (1, 2, 3):
            self.assertIn("authorization and data integrity risk discovered", adapter.specs[index])
        self.assertIn('"verified_facts":["authorization and data integrity risk discovered"]',
                      adapter.specs[1])

    def test_complex_target_failure_reopens_without_invented_assessment(self):
        implementation = evidence(Phase.IMPLEMENTATION, verification_mode="MODEL_REVIEW")
        adapter = ClosedLoopAdapter({
            "implementation": [implementation, evidence(Phase.IMPLEMENTATION)],
            "verification": [
                evidence(Phase.VERIFICATION, verification_outcome="TARGET_FAILED", evidence=["defect"]),
                evidence(Phase.VERIFICATION),
            ],
        })
        result = self.run_with("Fix async external API retry timeout state sync.", adapter)
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertNotIn(Phase.ASSESSMENT, [r.phase for r in adapter.routes])
        self.assertEqual([r.phase for r in adapter.routes], [
            Phase.INVESTIGATION, Phase.IMPLEMENTATION, Phase.VERIFICATION,
            Phase.IMPLEMENTATION, Phase.VERIFICATION])

    def test_cleanup_failure_forbids_success(self):
        adapter = ClosedLoopAdapter({}, release_state="release_failed")
        result = self.run_with("Inspect metadata. Do not modify files.", adapter)
        self.assertIs(result.final_status, PhaseStatus.FAILED)

    def test_schema_v2_is_additive_and_keeps_v1_fields(self):
        result = self.run_with("Inspect metadata. Do not modify files.", ClosedLoopAdapter({})).to_dict()
        self.assertEqual(result["result_schema_version"], 2)
        for legacy in ("final_status", "phase_list", "models", "routing_plan", "cleanup_result"):
            self.assertIn(legacy, result)
        for additive in ("logical_gates", "attempt_history", "adaptive_decisions", "cost_metrics"):
            self.assertIn(additive, result)

    def test_deterministic_only_complex_write_skips_model_verifier(self):
        result = self.run_with("Fix async external API retry timeout state sync.", ClosedLoopAdapter({}))
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual(result.verification_mode, "DETERMINISTIC_ONLY")
        self.assertEqual(result.verification_decision, "deterministic-evidence-sufficient")

    def test_rate_limit_retries_same_capability_once(self):
        adapter = ClosedLoopAdapter({})
        original = adapter.start_worker
        calls = 0
        def transient(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise CoordinatorError("rate limit temporarily exceeded")
            return original(*args, **kwargs)
        adapter.start_worker = transient
        result = self.run_with("Inspect metadata. Do not modify files.", adapter)
        self.assertIs(result.final_status, PhaseStatus.SUCCESS)
        self.assertEqual(result.cost_metrics.attempt_count, 2)
        self.assertEqual(result.adaptive_decisions[0]["decision"], "RETRY_SAME_CAPABILITY")

    def test_sol_unavailable_blocks_without_terra_downgrade(self):
        adapter = ClosedLoopAdapter({})
        adapter.start_worker = lambda *args, **kwargs: (_ for _ in ()).throw(CoordinatorError("model unavailable"))
        result = self.run_with("Assess an authorization rule. Do not modify files.", adapter)
        self.assertIs(result.final_status, PhaseStatus.BLOCKED)
        self.assertEqual(result.adaptive_decisions[0]["blocker_kind"], "MODEL_UNAVAILABLE")
        self.assertEqual(result.phase_list[0].model, SOL)


if __name__ == "__main__":
    unittest.main()
