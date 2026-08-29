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
        self.assertTrue(route.automatic_review)

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
        self.assertNotIn("danger-full-access", [r.authority.value for r in plan.routes])

    def test_05_escalation_is_coordinator_reclassification(self) -> None:
        text = scenario("05-escalation.md")
        initial, findings = text.split("## New Finding", 1)
        first = self.router.classify(initial)
        second = self.router.reclassify(initial, findings)
        self.assertIn(first.routes[0].model, (LUNA, TERRA))
        self.assertEqual(second.level, "critical")
        self.assertEqual(second.routes[0].model, SOL)
        self.assertIs(second.routes[0].authority, Authority.READ_ONLY)


if __name__ == "__main__":
    unittest.main()
