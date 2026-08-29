from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum


class Authority(StrEnum):
    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"


class Phase(StrEnum):
    INVESTIGATION = "investigation"
    IMPLEMENTATION = "implementation"
    ASSESSMENT = "assessment"
    VERIFICATION = "verification"


@dataclass(frozen=True)
class Route:
    phase: Phase
    role: str
    model: str
    effort: str
    authority: Authority
    approval_grade: str
    automatic_review: bool = False
    requires_assessment: bool = False

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["phase"] = self.phase.value
        data["authority"] = self.authority.value
        return data


@dataclass(frozen=True)
class RoutingPlan:
    level: str
    reason: str
    routes: tuple[Route, ...]
    parallelism: str = "sequential"
    verifier: str = "no"
    escalation_triggers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "level": self.level,
            "reason": self.reason,
            "parallelism": self.parallelism,
            "verifier": self.verifier,
            "escalation_triggers": list(self.escalation_triggers),
            "routes": [route.to_dict() for route in self.routes],
        }

