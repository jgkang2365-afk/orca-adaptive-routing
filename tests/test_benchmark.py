import unittest

from adaptive_coordinator.benchmark import run_benchmark


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
        self.assertEqual(results["v0.2"]["provenance"], "ProductionRunner deterministic corpus replay")

    def test_absolute_zero_criteria(self):
        result = run_benchmark()["v0.2"]
        for field in ("false_success", "duplicate_write_execution",
                      "external_blocker_misclassification", "identical_retry",
                      "authority_auto_escalation", "happy_path_extra_dispatch"):
            self.assertEqual(result[field], 0, field)


if __name__ == "__main__":
    unittest.main()
