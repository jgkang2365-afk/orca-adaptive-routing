from pathlib import Path
import unittest

from adaptive_coordinator.models import Authority, Phase
from adaptive_coordinator.routing import LUNA, SOL, TERRA, Router


SCENARIOS = Path(__file__).parent / "scenarios"


def scenario(name: str) -> str:
    return (SCENARIOS / name).read_text()


class RoutingScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = Router()

    def test_01_simple_inventory(self) -> None:
        plan = self.router.classify(scenario("01-simple-inventory.md"))
        route = plan.routes[0]
        self.assertEqual((route.model, route.effort), (LUNA, "low"))
        self.assertIs(route.authority, Authority.READ_ONLY)
        self.assertEqual(plan.verifier, "no")

    def test_02_ordinary_implementation(self) -> None:
        plan = self.router.classify(scenario("02-standard-implementation.md"))
        route = plan.routes[0]
        self.assertEqual((route.model, route.effort), (TERRA, "medium"))
        self.assertIs(route.authority, Authority.WORKSPACE_WRITE)
        self.assertFalse(route.automatic_review)

    def test_scoped_no_existing_file_change_does_not_cancel_fixture_write(self) -> None:
        plan = self.router.classify(
            "Add a temporary fixture, then delete it. Do not modify any existing project file."
        )
        route = plan.routes[0]
        self.assertEqual((route.model, route.effort), (TERRA, "medium"))
        self.assertIs(route.authority, Authority.WORKSPACE_WRITE)

    def test_03_complex_external_integration(self) -> None:
        plan = self.router.classify(scenario("03-complex-integration.md"))
        self.assertEqual(plan.level, "complex")
        self.assertEqual([route.model for route in plan.routes], [TERRA, TERRA])
        self.assertEqual([route.effort for route in plan.routes], ["high", "high"])
        self.assertEqual(
            [route.authority for route in plan.routes],
            [Authority.READ_ONLY, Authority.WORKSPACE_WRITE],
        )
        self.assertEqual(plan.verifier, "conditional")

    def test_04_database_migration(self) -> None:
        plan = self.router.classify(scenario("04-database-migration.md"))
        self.assertEqual(plan.level, "critical")
        self.assertEqual(
            [route.phase for route in plan.routes],
            [Phase.ASSESSMENT, Phase.IMPLEMENTATION, Phase.VERIFICATION],
        )
        self.assertTrue(plan.routes[1].requires_assessment)
        self.assertEqual([route.model for route in plan.routes], [SOL, SOL, SOL])
        self.assertEqual([route.effort for route in plan.routes], ["high", "high", "high"])
        self.assertNotIn("danger-full-access", [r.authority.value for r in plan.routes])

    def test_05_escalation_is_coordinator_reclassification(self) -> None:
        text = scenario("05-escalation.md")
        initial, findings = text.split("## New Finding", 1)
        first = self.router.classify(initial)
        second = self.router.reclassify(initial, findings)
        self.assertIn(first.routes[0].model, (LUNA, TERRA))
        self.assertEqual(second.level, "critical")
        self.assertEqual(second.routes[0].model, SOL)
        self.assertEqual(second.routes[0].effort, "medium")
        self.assertIs(second.routes[0].authority, Authority.READ_ONLY)

    def test_policy_floor_vocabulary_cannot_bypass_critical_gate(self) -> None:
        for task in (
            "Implement an Alembic migration that drops the users.email column.",
            "Implement a security fix for privilege escalation.",
            "Change the service architecture with a difficult rollback.",
        ):
            with self.subTest(task=task):
                plan = self.router.classify(task)
                self.assertEqual(plan.level, "critical")
                self.assertIs(plan.routes[0].phase, Phase.ASSESSMENT)
                self.assertIs(plan.routes[0].authority, Authority.READ_ONLY)
                self.assertTrue(plan.routes[1].requires_assessment)

    def test_standard_code_review_is_terra_read_only(self) -> None:
        plan = self.router.classify("Review code in the validation module for ordinary bugs.")
        route = plan.routes[0]
        self.assertEqual(plan.level, "standard")
        self.assertEqual((route.model, route.effort), (TERRA, "medium"))
        self.assertIs(route.authority, Authority.READ_ONLY)

    def test_sol_medium_is_the_default_critical_effort(self) -> None:
        for task in (
            "Assess a production authorization rule change. No destructive operation is requested.",
            "Review schema change impact and a reversible migration plan.",
            "Assess authorization impact with no destructive migration, no data loss risk, and no attack path risk.",
        ):
            with self.subTest(task=task):
                plan = self.router.classify(task)
                self.assertEqual(plan.level, "critical")
                self.assertEqual({route.effort for route in plan.routes}, {"medium"})
                self.assertIs(plan.routes[0].authority, Authority.READ_ONLY)

    def test_sol_high_requires_and_records_a_concrete_risk(self) -> None:
        plan = self.router.classify(
            "Implement a destructive migration with production data loss risk; rollback is uncertain."
        )
        self.assertEqual({route.effort for route in plan.routes}, {"high"})
        self.assertIn("destructive migration", plan.reason)
        self.assertIn("rollback uncertainty", plan.reason)

    def test_fresh_verifier_effort_tracks_critical_risk(self) -> None:
        normal = self.router.classify("Change an authorization rule and independently verify it.")
        high = self.router.classify(
            "Change authorization after a high-impact security attack path and meaningful data-loss risk, then independently verify it."
        )
        self.assertEqual(normal.routes[-1].role, "Fresh Verifier")
        self.assertEqual(normal.routes[-1].effort, "medium")
        self.assertIs(normal.routes[-1].authority, Authority.READ_ONLY)
        self.assertEqual(high.routes[-1].effort, "high")

    def test_lower_tiers_are_unchanged_by_sol_effort_policy(self) -> None:
        cases = (
            ("Inspect metadata without modifying files.", LUNA, "low"),
            ("Implement a localized validation helper.", TERRA, "medium"),
            ("Fix async retry timeout state synchronization.", TERRA, "high"),
        )
        for task, model, effort in cases:
            with self.subTest(task=task):
                route = self.router.classify(task).routes[0]
                self.assertEqual((route.model, route.effort), (model, effort))


if __name__ == "__main__":
    unittest.main()
