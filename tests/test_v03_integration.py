from __future__ import annotations

import unittest

from adaptive_coordinator.models import InteractionMode, RunMetadata
from adaptive_coordinator.runner import TaskDecomposer
from adaptive_coordinator.routing import Router


class V03IntegrationContractTests(unittest.TestCase):
    def test_no_intervention_requires_explicit_preapproval(self) -> None:
        with self.assertRaises(ValueError):
            RunMetadata(interaction_mode=InteractionMode.NO_INTERVENTION)

    def test_non_preapproved_default_is_standard_interaction(self) -> None:
        metadata = RunMetadata(delegated_by_parent=True)
        self.assertFalse(metadata.preapproved)
        self.assertEqual(metadata.interaction_mode, InteractionMode.STANDARD)

    def test_bounded_decomposition_has_explicit_identity_and_dependencies(self) -> None:
        task = "업무규칙, API 코드 흐름, 테스트를 각각 조사하고 async 수정을 구현해라."
        subtasks = TaskDecomposer.decompose(task, Router().classify(task))
        self.assertEqual([item.subtask_id for item in subtasks], ["read-rules", "read-code", "read-tests"])
        self.assertTrue(all(item.dependencies == () for item in subtasks))
        self.assertTrue(all(item.can_parallelize for item in subtasks))
        self.assertLessEqual(len(subtasks), 3)

    def test_conversation_does_not_force_fanout(self) -> None:
        task = "Explain what this function does."
        self.assertEqual(TaskDecomposer.decompose(task, Router().classify(task)), ())


if __name__ == "__main__":
    unittest.main()
