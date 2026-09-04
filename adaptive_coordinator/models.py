from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum


class Authority(StrEnum):
    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"


class Phase(StrEnum):
    INVESTIGATION = "investigation"
    IMPLEMENTATION = "implementation"
    ASSESSMENT = "assessment"
    VERIFICATION = "verification"


class FailureClass(StrEnum):
    INSUFFICIENT_SUCCESS_EVIDENCE = "INSUFFICIENT_SUCCESS_EVIDENCE"
    EVIDENCE_GAP = "EVIDENCE_GAP"
    STALE_EVIDENCE = "STALE_EVIDENCE"
    ENVIRONMENT_MISMATCH = "ENVIRONMENT_MISMATCH"
    TARGET_IDENTITY_MISMATCH = "TARGET_IDENTITY_MISMATCH"
    TRANSIENT_FAILURE = "TRANSIENT_FAILURE"
    RECOVERABLE_IMPLEMENTATION_FAILURE = "RECOVERABLE_IMPLEMENTATION_FAILURE"
    CAPABILITY_FAILURE = "CAPABILITY_FAILURE"
    AMBIGUOUS_FAILURE = "AMBIGUOUS_FAILURE"
    DECOMPOSITION_FAILURE = "DECOMPOSITION_FAILURE"
    MISSING_CONTEXT = "MISSING_CONTEXT"
    EXTERNAL_BLOCKER = "EXTERNAL_BLOCKER"
    USER_ACTION_REQUIRED = "USER_ACTION_REQUIRED"
    ORCHESTRATION_FAILURE = "ORCHESTRATION_FAILURE"
    TERMINAL_FAILURE = "TERMINAL_FAILURE"


class AdaptiveDecision(StrEnum):
    SUCCESS = "SUCCESS"
    RESULT_REPAIR = "RESULT_REPAIR"
    COLLECT_EVIDENCE = "COLLECT_EVIDENCE"
    RETRY_SAME_CAPABILITY = "RETRY_SAME_CAPABILITY"
    INSERT_READ_ONLY_DIAGNOSIS = "INSERT_READ_ONLY_DIAGNOSIS"
    REPLAN = "REPLAN"
    ESCALATE_CAPABILITY = "ESCALATE_CAPABILITY"
    APPLY_RISK_FLOOR = "APPLY_RISK_FLOOR"
    REOPEN_IMPLEMENTATION = "REOPEN_IMPLEMENTATION"
    BLOCKED = "BLOCKED"
    TERMINAL = "TERMINAL"


class VerificationOutcome(StrEnum):
    VERIFIED = "VERIFIED"
    NOT_VERIFIED = "NOT_VERIFIED"
    INCONCLUSIVE = "INCONCLUSIVE"
    TARGET_FAILED = "TARGET_FAILED"


class VerificationMode(StrEnum):
    DETERMINISTIC_ONLY = "DETERMINISTIC_ONLY"
    MODEL_REVIEW = "MODEL_REVIEW"
    HYBRID = "HYBRID"


class InteractionMode(StrEnum):
    STANDARD = "standard"
    NO_INTERVENTION = "no-intervention"


@dataclass(frozen=True)
class RunMetadata:
    """Structured delegation state supplied by an Orca Parent."""

    delegated_by_parent: bool = False
    preapproved: bool = False
    interaction_mode: InteractionMode = InteractionMode.STANDARD

    def __post_init__(self) -> None:
        if self.interaction_mode is InteractionMode.NO_INTERVENTION and not self.preapproved:
            raise ValueError("no-intervention requires preapproved=true")

    def to_dict(self) -> dict[str, object]:
        return {
            "delegated_by_parent": self.delegated_by_parent,
            "preapproved": self.preapproved,
            "interaction_mode": self.interaction_mode.value,
        }


@dataclass(frozen=True)
class RunRequest:
    task: str
    workspace: str
    metadata: RunMetadata = field(default_factory=RunMetadata)


@dataclass(frozen=True)
class SubtaskSpec:
    subtask_id: str
    objective: str
    dependencies: tuple[str, ...]
    route: "Route"
    affected_scope: tuple[str, ...] = ()
    can_parallelize: bool = False
    parallel_group: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "subtask_id": self.subtask_id,
            "objective": self.objective,
            "dependencies": list(self.dependencies),
            "route": self.route.to_dict(),
            "affected_scope": list(self.affected_scope),
            "can_parallelize": self.can_parallelize,
            "parallel_group": self.parallel_group,
        }


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


@dataclass(frozen=True)
class TaskBrief:
    objective: str
    requested_actions: tuple[str, ...]
    forbidden_scope: tuple[str, ...]
    read_only_constraint: bool
    positive_risk_signals: tuple[str, ...]
    language: str


@dataclass
class AttemptMetadata:
    logical_gate_id: str
    attempt_id: str
    attempt_no: int
    parent_attempt_id: str | None
    phase: str
    model: str
    effort: str
    capability_rank: int
    authority: str
    failure_class: str | None = None
    classification_confidence: str | None = None
    decision: str | None = None
    decision_reason: str | None = None
    retry_delta: str | None = None
    material_new_evidence: bool = False
    workspace_fingerprint: dict[str, object] = field(default_factory=dict)
    target_fingerprint: dict[str, object] = field(default_factory=dict)
    phase_spec_size: int = 0
    evidence_packet_size: int = 0
    verification_mode: str | None = None
    blocker_kind: str | None = None
    terminal_reason: str | None = None
    files_changed: tuple[str, ...] = ()
    attempted_actions: tuple[str, ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    test_results: tuple[str, ...] = ()
    relevant_evidence_refs: tuple[str, ...] = ()
    elapsed_time: float = 0.0
    prior_gate_invalidated: bool = False
    invalidated_gate_id: str | None = None
    invalidation_reason: str | None = None
    invalidation_evidence: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class LogicalGateState:
    logical_gate_id: str
    phase: str
    authority: str
    attempts: list[AttemptMetadata] = field(default_factory=list)
    status: str = "PENDING"
    same_level_retries: dict[int, int] = field(default_factory=dict)
    no_progress_count: int = 0
    active_mutation_attempt: str | None = None
    baseline_changes: dict[str, str] = field(default_factory=dict, repr=False)
    parent_gate_id: str | None = None
    evidence_source_gate_id: str | None = None
    target_source_gate_id: str | None = None
    root_gate_id: str | None = None
    verified_facts: list[str] = field(default_factory=list)
    target_fingerprint: dict[str, object] = field(default_factory=dict)
    evidence_repairs: int = 0
    diagnosis_count: int = 0
    capability_ranks_used: set[int] = field(default_factory=set)
    applied_risk_signature: str | None = None
    applied_floor_rank: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "logical_gate_id": self.logical_gate_id,
            "phase": self.phase,
            "authority": self.authority,
            "status": self.status,
            "parent_gate_id": self.parent_gate_id,
            "evidence_source_gate_id": self.evidence_source_gate_id,
            "target_source_gate_id": self.target_source_gate_id,
            "root_gate_id": self.root_gate_id,
            "verified_facts": list(self.verified_facts),
            "target_fingerprint": dict(self.target_fingerprint),
            "evidence_repairs": self.evidence_repairs,
            "diagnosis_count": self.diagnosis_count,
            "capability_ranks_used": sorted(self.capability_ranks_used),
            "applied_risk_signature": self.applied_risk_signature,
            "applied_floor_rank": self.applied_floor_rank,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
        }


@dataclass(frozen=True)
class EvidencePacket:
    logical_gate_id: str
    previous_attempts_summary: tuple[str, ...] = ()
    verified_facts: tuple[str, ...] = ()
    attempted_actions: tuple[str, ...] = ()
    failure_class: str | None = None
    failure_reason: str | None = None
    unresolved_questions: tuple[str, ...] = ()
    files_changed: tuple[str, ...] = ()
    test_results: tuple[str, ...] = ()
    target_fingerprint: tuple[tuple[str, str], ...] = ()
    relevant_evidence_refs: tuple[str, ...] = ()
    escalation_reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
