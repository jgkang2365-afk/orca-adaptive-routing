from __future__ import annotations

import unittest
from pathlib import Path

from adaptive_coordinator.models import (
    AdaptiveDecision, Authority, FailureClass, LogicalGateState, Phase,
)
from adaptive_coordinator.routing import CAPABILITY_LADDER, SOL, TERRA, Router, capability_at
from adaptive_coordinator.runner import (
    DecisionEngine, FailureClassification, FailureClassifier, NormalizedWorkerResult,
    ProductionRunner, ResultNormalizer, SuccessEvidenceGate,
)


def selected(rank: int, phase: Phase = Phase.VERIFICATION,
             authority: Authority = Authority.READ_ONLY):
    from adaptive_coordinator.models import Route
    model, effort = capability_at(rank)
    return Route(phase, "test", model, effort, authority, "SAFE")


class VerificationEvidenceTests(unittest.TestCase):
    def evaluate(self, **payload):
        raw = {"status": "completed", "verification_outcome": "VERIFIED", **payload}
        return SuccessEvidenceGate.evaluate(
            selected(3), ResultNormalizer.normalize(raw), ())[0]

    def test_verified_requires_evidence_and_explicit_empty_questions(self):
        self.assertFalse(self.evaluate())
        self.assertFalse(self.evaluate(evidence=["test passed"]))
        self.assertFalse(self.evaluate(evidence=["test passed"], unresolved_questions=["target unknown"]))
        self.assertTrue(self.evaluate(evidence=["test passed"], unresolved_questions=[]))

    def test_target_identity_mismatch_remains_distinct(self):
        normalized = ResultNormalizer.normalize({
            "status": "completed", "verification_outcome": "VERIFIED",
            "evidence": ["test passed"], "unresolved_questions": [],
            "implementation_commit": "new", "deployment_commit": "old",
        })
        failure = FailureClassifier.classify(
            selected(3), {"mode": "worker_done"}, normalized, "ok", "new != old")
        self.assertIs(failure.failure_class, FailureClass.TARGET_IDENTITY_MISMATCH)


class UnexecutedVerificationTests(unittest.TestCase):
    def test_structured_policy(self):
        check = SuccessEvidenceGate._unexecuted_verification
        self.assertTrue(check([])[0])
        self.assertTrue(check([{"check": "visual", "blocking": False, "reason": "no UI changed"}])[0])
        self.assertFalse(check([{"check": "integration", "blocking": True, "reason": "offline"}])[0])
        self.assertFalse(check([{"check": "visual", "blocking": False}])[0])
        self.assertFalse(check(["integration test not run"])[0])
        self.assertFalse(check([{"check": "unit", "blocking": False, "reason": "review instead",
                                 "deterministic_required": True}])[0])


class TransientAndCapabilityTests(unittest.TestCase):
    def test_repeated_transient_never_escalates(self):
        gate = LogicalGateState("g", "investigation", "read-only")
        failure = FailureClassification(FailureClass.TRANSIENT_FAILURE, "high", "transient_signal", ("rate limit",))
        engine = DecisionEngine()
        first, _ = engine.decide(gate, selected(1, Phase.INVESTIGATION), failure,
                                 material_new_evidence=True, run_xhigh_count=0)
        self.assertIs(first, AdaptiveDecision.RETRY_SAME_CAPABILITY)
        gate.same_level_retries[1] = 1
        repeated, _ = engine.decide(gate, selected(1, Phase.INVESTIGATION), failure,
                                    material_new_evidence=True, run_xhigh_count=0)
        self.assertIs(repeated, AdaptiveDecision.TERMINAL)

    def test_full_legal_ladder_is_reachable_and_fuse_is_derived(self):
        engine = DecisionEngine()
        gate = LogicalGateState("g", "verification", "read-only")
        failure = FailureClassification(FailureClass.CAPABILITY_FAILURE, "high",
                                        "verified_capability_limit", ("conflict A", "conflict B"))
        ranks = []
        for rank in range(len(CAPABILITY_LADDER) - 1):
            decision, _ = engine.decide(gate, selected(rank), failure,
                                        material_new_evidence=True, run_xhigh_count=0)
            self.assertIs(decision, AdaptiveDecision.ESCALATE_CAPABILITY)
            ranks.append(rank + 1)
        self.assertEqual(ranks, [1, 2, 3, 4, 5])
        self.assertGreaterEqual(ProductionRunner().max_attempts_per_gate, len(CAPABILITY_LADDER))

    def test_normalizer_classifier_decision_reaches_xhigh_read_only(self):
        route = selected(4)
        normalized = ResultNormalizer.normalize({
            "status": "failed", "summary": "verification remains inconclusive",
            "verification_outcome": "INCONCLUSIVE",
            "evidence": ["hypothesis A contradicted", "hypothesis B contradicted"],
            "unresolved_questions": ["which invariant owns state"],
        })
        failure = FailureClassifier.classify(route, {"mode": "worker_done"}, normalized, "failed")
        gate = LogicalGateState("g", "verification", "read-only")
        gate.same_level_retries[4] = 1
        decision, _ = DecisionEngine().decide(gate, route, failure,
                                              material_new_evidence=True, run_xhigh_count=0)
        advanced = __import__("adaptive_coordinator.routing", fromlist=["next_capability"]).next_capability(route)
        self.assertIs(decision, AdaptiveDecision.ESCALATE_CAPABILITY)
        self.assertEqual((advanced.model, advanced.effort, advanced.authority),
                         (SOL, "xhigh", Authority.READ_ONLY))

    def test_xhigh_write_is_replaced_by_read_only_diagnosis(self):
        route = selected(4, Phase.IMPLEMENTATION, Authority.WORKSPACE_WRITE)
        failure = FailureClassification(FailureClass.CAPABILITY_FAILURE, "high",
                                        "verified_capability_limit", ("conflict A", "conflict B"))
        decision, _ = DecisionEngine().decide(LogicalGateState("g", "implementation", "workspace-write"),
                                              route, failure, material_new_evidence=True,
                                              run_xhigh_count=0)
        self.assertIs(decision, AdaptiveDecision.INSERT_READ_ONLY_DIAGNOSIS)


class PollingRoutingTests(unittest.TestCase):
    def test_exact_korean_polling_write(self):
        plan = Router().classify("Supabase 사용량 초과 대응 및 상시 Polling 1차 최적화")
        self.assertEqual(plan.level, "complex")
        self.assertEqual([(r.phase, r.model, r.effort, r.authority) for r in plan.routes], [
            (Phase.INVESTIGATION, TERRA, "high", Authority.READ_ONLY),
            (Phase.IMPLEMENTATION, TERRA, "high", Authority.WORKSPACE_WRITE),
        ])
        self.assertEqual(plan.verifier, "conditional")

    def test_exact_korean_polling_investigation_only(self):
        plan = Router().classify("Supabase 상시 Polling 사용량 증가 원인만 조사하고 파일은 수정하지 마라.")
        self.assertEqual(plan.level, "complex")
        self.assertEqual(len(plan.routes), 1)
        self.assertEqual((plan.routes[0].model, plan.routes[0].effort, plan.routes[0].authority),
                         (TERRA, "high", Authority.READ_ONLY))

    def test_generic_polling_optimization(self):
        plan = Router().classify("Worker polling 호출 주기를 최적화해라.")
        self.assertEqual(plan.level, "complex")
        self.assertTrue(any(r.authority is Authority.WORKSPACE_WRITE for r in plan.routes))

    def test_polling_signals_are_bilingual(self):
        english = Router().classify("Optimize the worker polling interval to reduce API usage.")
        korean = Router().classify("API 사용량 절감을 위해 worker 폴링 호출 주기를 최적화해라.")
        self.assertEqual(
            [(r.model, r.effort, r.authority, r.phase) for r in english.routes],
            [(r.model, r.effort, r.authority, r.phase) for r in korean.routes],
        )


class ReplanAndCiContractTests(unittest.TestCase):
    def test_replan_creates_narrow_read_only_child(self):
        runner = ProductionRunner()
        gate = LogicalGateState("implementation-1", "implementation", "workspace-write")
        from adaptive_coordinator.runner import RunResult
        result = RunResult("run", "/workspace", "standard", logical_gates={gate.logical_gate_id: gate})
        queue = []
        runner._schedule_decision(queue, selected(1, Phase.IMPLEMENTATION, Authority.WORKSPACE_WRITE),
                                  gate.logical_gate_id, gate, AdaptiveDecision.REPLAN, result,
                                  NormalizedWorkerResult("failed", "broad task"))
        focused, child_id = queue[0]
        self.assertIs(focused.authority, Authority.READ_ONLY)
        self.assertEqual(focused.phase, Phase.INVESTIGATION)
        self.assertNotEqual(child_id, gate.logical_gate_id)
        self.assertIn("narrowed_question: resolve the latest unresolved question only",
                      result.logical_gates[child_id].verified_facts)

    def test_quality_workflow_contract(self):
        text = Path(".github/workflows/quality.yml").read_text()
        for required in ("name: Adaptive Coordinator Quality", "quality-gate:",
                         "python -m unittest discover", "python -m compileall",
                         "bash -n scripts/install-production.sh", "git diff --check",
                         "python scripts/run-v02-benchmark.py"):
            self.assertIn(required, text)


if __name__ == "__main__":
    unittest.main()
