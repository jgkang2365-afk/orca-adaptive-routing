import unittest

from adaptive_coordinator.benchmark import (
    _false_success_count, _trace_invariant_counts, benchmark_violations, run_benchmark,
)
from adaptive_coordinator.runner import PhaseResult, PhaseStatus, RunResult


class BenchmarkTests(unittest.TestCase):
    def test_quality_and_compute_acceptance(self):
        results = run_benchmark()
        old, blanket, adaptive = results["v0.1"], results["all-sol-medium"], results["v0.2"]
        self.assertEqual(adaptive["false_success"], 0)
        self.assertGreaterEqual(adaptive["verified_success_rate"], old["verified_success_rate"])
        self.assertLess(adaptive["normalized_compute_proxy"], blanket["normalized_compute_proxy"])
        self.assertLess(adaptive["manual_intervention_rate"], old["manual_intervention_rate"])

    def test_happy_path_initial_routes_are_not_inflated(self):
        results = run_benchmark()
        self.assertNotIn("xhigh", results["v0.2"]["effort_calls"])
        self.assertEqual(results["v0.2"]["happy_path_extra_dispatch"], 0)
        for invariant in ("transient_capability_escalation", "initial_xhigh",
                          "xhigh_write", "repeated_risk_floor_loop"):
            self.assertEqual(results["v0.2"][invariant], 0)
        self.assertEqual(results["v0.2"]["provenance"], "ProductionRunner deterministic corpus replay")

    def test_absolute_zero_criteria(self):
        result = run_benchmark()["v0.2"]
        for field in ("false_success", "duplicate_write_execution",
                      "external_blocker_misclassification", "identical_retry",
                      "authority_auto_escalation", "happy_path_extra_dispatch"):
            self.assertEqual(result[field], 0, field)

    def test_trace_invariants_detect_perturbed_run_results(self):
        result = RunResult("run", "/workspace", "standard", routing_plan={
            "routes": [{"effort": "xhigh", "authority": "read-only"}],
        })
        result.phase_list.extend([
            PhaseResult("implementation", "Lead", "gpt-5.6-sol", "xhigh",
                        "workspace-write", PhaseStatus.FAILED),
            PhaseResult("assessment", "Assessor", "gpt-5.6-sol", "medium",
                        "read-only", PhaseStatus.SUCCESS),
            PhaseResult("assessment", "Assessor", "gpt-5.6-sol", "medium",
                        "read-only", PhaseStatus.SUCCESS),
        ])
        result.adaptive_decisions.append({
            "failure_class": "TRANSIENT_FAILURE", "decision": "ESCALATE_CAPABILITY",
            "decision_reason": "incorrectly escalated",
        })
        counts = _trace_invariant_counts(result, "risk-repeat")
        self.assertEqual(counts, {
            "transient_capability_escalation": 1,
            "initial_xhigh": 1,
            "xhigh_write": 1,
            "repeated_risk_floor_loop": 1,
        })

    def test_false_success_is_computed_from_expected_scenario_outcome(self):
        invalid = RunResult("run", "/workspace", "critical",
                            final_status=PhaseStatus.SUCCESS)
        self.assertEqual(_false_success_count(invalid, "invalid-verified"), 1)
        invalid.final_status = PhaseStatus.FAILED
        self.assertEqual(_false_success_count(invalid, "invalid-verified"), 0)
        results = run_benchmark()
        results["v0.2"]["false_success"] = 1
        self.assertIn("false_success", benchmark_violations(results))

    def test_forced_repeated_transient_success_is_false_success(self):
        forced = RunResult("run", "/workspace", "routine",
                           final_status=PhaseStatus.SUCCESS)
        forced.cleanup_result = [{"state": "released"}]
        self.assertEqual(_false_success_count(forced, "transient-repeat"), 1)
        ordinary = RunResult("run", "/workspace", "routine",
                             final_status=PhaseStatus.SUCCESS)
        ordinary.cleanup_result = [{"state": "released"}]
        self.assertEqual(_false_success_count(ordinary, "happy"), 0)


if __name__ == "__main__":
    unittest.main()
