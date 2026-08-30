from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .models import (
    AdaptiveDecision, AttemptMetadata, Authority, EvidencePacket, FailureClass,
    LogicalGateState, Phase, Route, RoutingPlan, VerificationMode, VerificationOutcome,
)
from .orca import CoordinatorError, LifecycleSettlementError, OrcaAdapter, WorkerHandle
from .result_sentinel import explicit_orca_failure_status, final_marked_structured_result
from .routing import SOL, Router, apply_risk_floor, capability_at, capability_rank, next_capability


class PhaseStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    ESCALATION_REQUESTED = "ESCALATION_REQUESTED"


@dataclass
class NormalizedWorkerResult:
    status: str
    summary: str
    failure_class_hint: str | None = None
    reason: str | None = None
    evidence: tuple[str, ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    files_modified: tuple[str, ...] = ()
    tests_run: tuple[str, ...] = ()
    test_results: tuple[str, ...] = ()
    needs_user_input: bool = False
    external_blocker: str | None = None
    verification_outcome: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {
            "status": self.status, "summary": self.summary,
            "failure_class_hint": self.failure_class_hint, "reason": self.reason,
            "evidence": list(self.evidence), "unresolved_questions": list(self.unresolved_questions),
            "files_modified": list(self.files_modified), "tests_run": list(self.tests_run),
            "test_results": list(self.test_results), "needs_user_input": self.needs_user_input,
            "external_blocker": self.external_blocker,
            "verification_outcome": self.verification_outcome,
        }


@dataclass(frozen=True)
class FailureClassification:
    failure_class: FailureClass
    confidence: str
    reason_code: str
    evidence: tuple[str, ...] = ()


@dataclass
class PhaseResult:
    phase: str
    role: str
    model: str
    effort: str
    authority: str
    status: PhaseStatus
    logical_gate_id: str | None = None
    attempt_id: str | None = None
    attempt_no: int = 0
    settlement: str | None = None
    task_id: str | None = None
    dispatch_id: str | None = None
    worker_result: Any = None
    escalation: str | None = None
    cleanup: Any = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["status"] = self.status.value
        return result


@dataclass
class CostMetrics:
    worker_count: int = 0
    attempt_count: int = 0
    elapsed_time: float = 0.0
    model_calls: dict[str, int] = field(default_factory=dict)
    effort_calls: dict[str, int] = field(default_factory=dict)
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    output_tokens: int | None = None

    def record(self, route: Route, elapsed: float) -> None:
        self.worker_count += 1
        self.attempt_count += 1
        self.elapsed_time += elapsed
        self.model_calls[route.model] = self.model_calls.get(route.model, 0) + 1
        self.effort_calls[route.effort] = self.effort_calls.get(route.effort, 0) + 1


@dataclass
class RunResult:
    run_id: str | None
    workspace: str
    classification: str
    phase_list: list[PhaseResult] = field(default_factory=list)
    escalation: list[dict[str, Any]] = field(default_factory=list)
    verifier_result: Any = None
    final_status: PhaseStatus = PhaseStatus.BLOCKED
    cleanup_result: list[Any] = field(default_factory=list)
    routing_plan: dict[str, Any] = field(default_factory=dict)
    logical_gates: dict[str, LogicalGateState] = field(default_factory=dict)
    adaptive_decisions: list[dict[str, Any]] = field(default_factory=list)
    cost_metrics: CostMetrics = field(default_factory=CostMetrics)
    verification_decision: str = "not_applicable"
    verification_mode: str = VerificationMode.DETERMINISTIC_ONLY.value
    verifier_required_reason: str | None = None
    deterministic_coverage: list[str] = field(default_factory=list)
    remaining_risk: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        # v1 fields stay present; v2 fields are additive.
        return {
            "result_schema_version": 2,
            "run_id": self.run_id, "workspace": self.workspace,
            "classification": self.classification,
            "phase_list": [phase.to_dict() for phase in self.phase_list],
            "models": [{"model": phase.model, "effort": phase.effort, "authority": phase.authority}
                       for phase in self.phase_list],
            "escalation": self.escalation, "verifier_result": self.verifier_result,
            "final_status": self.final_status.value, "cleanup_result": self.cleanup_result,
            "routing_plan": self.routing_plan,
            "logical_gates": [gate.to_dict() for gate in self.logical_gates.values()],
            "attempt_history": [attempt.to_dict() for gate in self.logical_gates.values() for attempt in gate.attempts],
            "adaptive_decisions": self.adaptive_decisions,
            "cost_metrics": asdict(self.cost_metrics),
            "verification_decision": self.verification_decision,
            "verification_mode": self.verification_mode,
            "verifier_required_reason": self.verifier_required_reason,
            "deterministic_coverage": self.deterministic_coverage,
            "remaining_risk": self.remaining_risk,
        }


AdapterFactory = Callable[[Path], OrcaAdapter]


def _fresh_verifier(effort: str = "medium") -> Route:
    return Route(Phase.VERIFICATION, "Fresh Verifier", SOL, effort, Authority.READ_ONLY, "SAFE")


def _messages(payload: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    for key in ("result", "worker_result", "message", "worker", "delivery", "payload"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            yield from _messages(value)
    yield payload


def _summary(payload: Any) -> str:
    limit = 1_000
    if isinstance(payload, str):
        return payload.strip()[:limit]
    if isinstance(payload, Mapping):
        for item in _messages(payload):
            for key in ("summary", "finalOutput", "output", "body", "text"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:limit]
        terminal = payload.get("terminal")
        if isinstance(terminal, Mapping) and isinstance(terminal.get("tail"), Sequence):
            return "\n".join(str(line) for line in terminal["tail"][-12:])[-limit:]
    return ""


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(str(item).strip() for item in value if str(item).strip())[:50]
    if isinstance(value, Mapping):
        return tuple(f"{key}: {item}" for key, item in list(value.items())[:50])
    return ()


class ResultNormalizer:
    """Accept structured v2, legacy Orca envelopes, then bounded text fallback."""

    @staticmethod
    def normalize(payload: Any) -> NormalizedWorkerResult:
        if not isinstance(payload, Mapping):
            return NormalizedWorkerResult("unknown", _summary(payload))
        marked_result, marker_error = final_marked_structured_result(payload)
        marked_results = [marked_result] if marked_result is not None else []
        candidates = [*list(_messages(payload)), *marked_results]
        selected = max(candidates, key=lambda item: sum(key in item for key in (
            "status", "summary", "evidence", "files_modified", "tests_run", "verification_outcome")))
        selected_status = selected.get("status")
        if isinstance(selected_status, Mapping):
            selected_status = selected_status.get("worker") or selected_status.get("terminal")
        selected_failed = str(selected_status).lower() in {
            "failed", "failure", "error", "blocked", "cancelled", "canceled", "crashed",
        }
        explicit_failure = explicit_orca_failure_status(payload)
        if marker_error or (explicit_failure and not selected_failed):
            failure = marker_error or f"Orca status {explicit_failure}"
            return NormalizedWorkerResult(
                "FAILED", _summary(payload) or failure,
                reason=f"visible worker failure: {failure}",
                fields={"terminal_failure": failure},
            )
        textual = _summary(selected) or _summary(payload)
        status_value = selected.get("status", payload.get("status", "unknown"))
        if isinstance(status_value, Mapping):
            status_value = status_value.get("worker") or status_value.get("terminal") or "unknown"
        fields = dict(selected)
        return NormalizedWorkerResult(
            status=str(status_value).upper(), summary=_summary(selected) or textual,
            failure_class_hint=(str(selected.get("failure_class_hint")) if selected.get("failure_class_hint") else None),
            reason=(str(selected.get("reason") or selected.get("error")) if selected.get("reason") or selected.get("error") else None),
            evidence=_strings(selected.get("evidence")),
            unresolved_questions=_strings(selected.get("unresolved_questions")),
            files_modified=_strings(selected.get("files_modified")),
            tests_run=_strings(selected.get("tests_run")), test_results=_strings(selected.get("test_results")),
            needs_user_input=bool(selected.get("needs_user_input", False)),
            external_blocker=(str(selected.get("external_blocker")) if selected.get("external_blocker") else None),
            verification_outcome=(str(selected.get("verification_outcome")).upper()
                                  if selected.get("verification_outcome") else None),
            fields=fields,
        )


class SuccessEvidenceGate:
    @staticmethod
    def _material_conclusion(value: Any) -> bool:
        conclusions = [item.strip().lower() for item in _strings(value) if item.strip()]
        generic = {"complete", "completed", "done", "success", "successful",
                   "investigation complete", "investigation completed"}
        return bool(conclusions) and any(item not in generic for item in conclusions)

    @staticmethod
    def _reported_paths(value: Any) -> set[str] | None:
        if isinstance(value, Mapping):
            paths = {str(key).strip() for key in value if str(key).strip()}
            return paths
        if isinstance(value, str):
            # Only accept an unambiguous path list; prose is not diff evidence.
            if any(marker in value.lower() for marker in ("changed ", "modified ", "diff ")):
                return None
            return {part.strip() for part in re.split(r"[,\n]", value) if part.strip()}
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            if not all(isinstance(item, str) and item.strip() for item in value):
                return None
            return {str(item).strip() for item in value}
        return None

    @staticmethod
    def _tests_passed(tests_run: Sequence[str], test_results: Sequence[str]) -> bool:
        if not tests_run or not test_results:
            return False
        text = " ; ".join(test_results).strip().lower()
        if any(marker in text for marker in ("error", "crash", "not run", "not-run",
                                             "not executed", "execution skipped")):
            return False
        if re.search(r"\b[1-9][0-9]*\s+failed\b", text):
            return False
        without_zero_failures = re.sub(r"\b0\s+failed\b", "", text)
        if re.search(r"\b(fail|failed|failure)\b", without_zero_failures):
            return False
        explicit_pass = bool(
            re.search(r"\b[1-9][0-9]*\s+passed\b", text)
            or re.search(r"\b(status|result|outcome)\s*[:=]\s*(pass|passed|ok|success)\b", text)
            or re.search(r"\ball\s+(?:tests?\s+)?passed\b", text)
            or text in {"pass", "passed", "ok", "success"}
        )
        return explicit_pass

    @staticmethod
    def evaluate(route: Route, result: NormalizedWorkerResult, actual_changes: Sequence[str]) -> tuple[bool, str]:
        fields = result.fields
        if result.status not in {"SUCCESS", "SUCCEEDED", "COMPLETED", "DONE"}:
            return False, "worker status is not successful"
        if route.phase is Phase.INVESTIGATION:
            required = (SuccessEvidenceGate._material_conclusion(fields.get("conclusion")), bool(result.evidence),
                        bool(fields.get("files_checked") or fields.get("tools_used")),
                        "unresolved_questions" in fields)
            return (all(required), "investigation requires conclusion, evidence, tools/files, and unresolved questions")
        if route.phase is Phase.ASSESSMENT:
            rollback = fields.get("rollback") or fields.get("recovery")
            unresolved = _strings(fields.get("unresolved_questions"))
            non_evidence = {"unknown", "none", "n/a", "tbd", "uncertain", "unavailable"}
            impact_ok = bool(fields.get("impact")) and str(fields.get("impact")).strip().lower() not in non_evidence
            rollback_ok = bool(rollback) and str(rollback).strip().lower() not in non_evidence
            required = ("risks" in fields, impact_ok, rollback_ok,
                        fields.get("write_ready") is True, "unresolved_questions" in fields,
                        not unresolved)
            return (all(required),
                    "assessment requires material impact/rollback evidence, explicit write_ready=true, and no blocking uncertainty")
        if route.phase is Phase.IMPLEMENTATION:
            reported = set(result.files_modified)
            actual = set(actual_changes)
            if reported != actual:
                return False, "reported files_modified does not match actual workspace diff"
            workspace_diff = SuccessEvidenceGate._reported_paths(fields.get("workspace_diff"))
            if workspace_diff is None or workspace_diff != actual:
                return False, "reported workspace_diff does not match actual workspace diff"
            required = (bool(_strings(fields.get("requirements_completed"))), bool(result.tests_run),
                        bool(result.test_results), "workspace_diff" in fields,
                        "unexecuted_verification" in fields)
            if not all(required):
                return False, "implementation evidence fields are incomplete"
            if not SuccessEvidenceGate._tests_passed(result.tests_run, result.test_results):
                return False, "deterministic test evidence is absent, non-passing, skipped, or failed"
            return True, "implementation evidence verified"
        if route.phase is Phase.VERIFICATION:
            return (result.verification_outcome == VerificationOutcome.VERIFIED.value,
                    "verification requires explicit VERIFIED outcome")
        return False, "unsupported phase"


class FailureClassifier:
    EXTERNAL = ("permission denied", "credential", "plan limitation", "quota", "service outage", "access denied")
    ORCHESTRATION = ("agent_unconfigured", "terminal is not running a recognized agent",
                     "selector_not_found", "worker placement", "placement failure", "placement rejected", "vsock",
                     "adapter error", "adapter failure", "terminal launch failure",
                     "orca runtime error", "orca lifecycle error")
    TRANSIENT = ("rate limit", "temporarily", "network timeout", "connection reset", "process interrupted")
    RECOVERABLE = ("syntax error", "syntaxerror", "assertion failed", "assertion failure",
                   "assertionerror", "type error", "typeerror", "lint failure")
    ORCHESTRATION_ERROR_CODES = {
        "agent_unconfigured",
        "agent_readiness_blocked",
        "orca_command_timeout",
        "selector_not_found",
        "worker_placement_failure",
    }

    @classmethod
    def classify(cls, route: Route, completion: Mapping[str, Any], result: NormalizedWorkerResult,
                 evidence_reason: str, target_mismatch: str | None = None) -> FailureClassification:
        mode = str(completion.get("mode", ""))
        error_code = str(completion.get("error_code") or "").lower()
        text = " ".join(filter(None, (result.reason, result.summary, evidence_reason))).lower()
        if mode == "question" or result.needs_user_input:
            return FailureClassification(FailureClass.USER_ACTION_REQUIRED, "high", "question_event", (text[:500],))
        if (result.fields.get("non_idempotent_operation")
                and not any(result.fields.get(key) for key in (
                    "idempotent", "operation_not_executed", "safe_rollback_confirmed"))):
            return FailureClassification(FailureClass.USER_ACTION_REQUIRED, "high",
                                         "non_idempotent_retry_unproven", (text[:500],))
        if route.phase is Phase.ASSESSMENT and result.fields.get("write_ready") is False:
            return FailureClassification(FailureClass.TERMINAL_FAILURE, "high",
                                         "assessment_write_not_approved",
                                         tuple(result.evidence) or (text[:500],))
        if target_mismatch:
            return FailureClassification(FailureClass.TARGET_IDENTITY_MISMATCH, "high", "target_fingerprint", (target_mismatch,))
        if result.external_blocker or any(marker in text for marker in cls.EXTERNAL):
            return FailureClassification(FailureClass.EXTERNAL_BLOCKER, "high", "external_limit", (text[:500],))
        if result.verification_outcome == VerificationOutcome.TARGET_FAILED.value:
            return FailureClassification(FailureClass.RECOVERABLE_IMPLEMENTATION_FAILURE, "high", "verified_target_defect", result.evidence)
        if result.verification_outcome in {VerificationOutcome.INCONCLUSIVE.value, VerificationOutcome.NOT_VERIFIED.value}:
            return FailureClassification(FailureClass.AMBIGUOUS_FAILURE, "high", "verification_inconclusive", result.evidence)
        if error_code in cls.ORCHESTRATION_ERROR_CODES or any(marker in text for marker in cls.ORCHESTRATION):
            return FailureClassification(FailureClass.ORCHESTRATION_FAILURE, "high", "runtime_error", (text[:500],))
        if any(marker in text for marker in cls.TRANSIENT):
            return FailureClassification(FailureClass.TRANSIENT_FAILURE, "high", "transient_signal", (text[:500],))
        if mode == "adapter_error":
            # An adapter/Orca command failure is infrastructure evidence, not
            # proof that the assigned model lacked reasoning capability. Keep
            # explicit external/transient mappings above, then fail closed for
            # every unknown structured adapter error.
            return FailureClassification(
                FailureClass.ORCHESTRATION_FAILURE,
                "high",
                "adapter_error",
                (text[:500],),
            )
        if any(marker in text for marker in cls.RECOVERABLE):
            return FailureClassification(FailureClass.RECOVERABLE_IMPLEMENTATION_FAILURE, "high",
                                         "local_implementation_error", (text[:500],))
        if result.failure_class_hint:
            try:
                hint = FailureClass(result.failure_class_hint.upper())
            except ValueError:
                hint = FailureClass.AMBIGUOUS_FAILURE
            if hint is FailureClass.CAPABILITY_FAILURE:
                # Worker self-assessment is evidence to investigate, not proof
                # that its assigned capability is exhausted.
                hint = FailureClass.AMBIGUOUS_FAILURE
            # A worker hint is medium confidence and cannot override concrete evidence above.
            return FailureClassification(hint, "medium", "worker_hint", result.evidence)
        if result.status in {"FAILED", "FAILURE", "ERROR"}:
            kind = (FailureClass.RECOVERABLE_IMPLEMENTATION_FAILURE
                    if route.phase is Phase.IMPLEMENTATION else FailureClass.AMBIGUOUS_FAILURE)
            return FailureClassification(kind, "medium", "structured_failure_status", (text[:500],))
        if mode == "timeout" and not result.summary:
            return FailureClassification(FailureClass.EVIDENCE_GAP, "high", "timeout_without_evidence")
        return FailureClassification(FailureClass.INSUFFICIENT_SUCCESS_EVIDENCE, "high", "success_evidence_gate", (evidence_reason,))


class DecisionEngine:
    def __init__(self, *, same_level_retry_limit: int = 1, max_xhigh_attempts_per_gate: int = 1,
                 max_xhigh_attempts_per_run: int = 1) -> None:
        self.same_level_retry_limit = same_level_retry_limit
        self.max_xhigh_attempts_per_gate = max_xhigh_attempts_per_gate
        self.max_xhigh_attempts_per_run = max_xhigh_attempts_per_run

    def decide(self, gate: LogicalGateState, route: Route, failure: FailureClassification,
               *, material_new_evidence: bool, run_xhigh_count: int) -> tuple[AdaptiveDecision, str]:
        kind = failure.failure_class
        if kind in {FailureClass.EXTERNAL_BLOCKER, FailureClass.USER_ACTION_REQUIRED, FailureClass.MISSING_CONTEXT}:
            return AdaptiveDecision.BLOCKED, f"{kind.value} cannot be solved by more reasoning"
        if kind in {FailureClass.ORCHESTRATION_FAILURE, FailureClass.TERMINAL_FAILURE}:
            return AdaptiveDecision.TERMINAL, f"{kind.value} is not a model failure"
        if kind in {FailureClass.INSUFFICIENT_SUCCESS_EVIDENCE, FailureClass.EVIDENCE_GAP,
                    FailureClass.STALE_EVIDENCE, FailureClass.ENVIRONMENT_MISMATCH,
                    FailureClass.TARGET_IDENTITY_MISMATCH}:
            rank = capability_rank(route)
            if gate.same_level_retries.get(rank, 0) >= self.same_level_retry_limit or gate.no_progress_count >= 1:
                return AdaptiveDecision.TERMINAL, "evidence repair budget exhausted without verified success"
            return AdaptiveDecision.COLLECT_EVIDENCE, "obtain correct evidence before escalation"
        if kind is FailureClass.DECOMPOSITION_FAILURE:
            return AdaptiveDecision.REPLAN, "task decomposition must change before capability"
        if route.phase is Phase.VERIFICATION and failure.reason_code == "verified_target_defect":
            return AdaptiveDecision.REOPEN_IMPLEMENTATION, "verifier confirmed an implementation defect"
        if route.authority is Authority.WORKSPACE_WRITE and kind not in {
            FailureClass.RECOVERABLE_IMPLEMENTATION_FAILURE, FailureClass.TRANSIENT_FAILURE,
        }:
            return AdaptiveDecision.INSERT_READ_ONLY_DIAGNOSIS, "non-trivial WRITE failure requires READ_ONLY diagnosis"
        rank = capability_rank(route)
        retries = gate.same_level_retries.get(rank, 0)
        retryable = kind in {FailureClass.TRANSIENT_FAILURE, FailureClass.RECOVERABLE_IMPLEMENTATION_FAILURE,
                             FailureClass.AMBIGUOUS_FAILURE}
        if retryable and not material_new_evidence:
            if gate.no_progress_count >= 2:
                return AdaptiveDecision.TERMINAL, "no-progress circuit breaker: two attempts without new evidence"
            return AdaptiveDecision.COLLECT_EVIDENCE, "identical retry forbidden; change evidence or strategy"
        if retryable and retries < self.same_level_retry_limit and material_new_evidence:
            return AdaptiveDecision.RETRY_SAME_CAPABILITY, "focused retry with material delta"
        if kind in {FailureClass.CAPABILITY_FAILURE, FailureClass.AMBIGUOUS_FAILURE} or (retryable and retries >= self.same_level_retry_limit):
            candidate = next_capability(route)
            if candidate is None:
                return AdaptiveDecision.TERMINAL, "Sol/xhigh is the automatic capability ceiling"
            if candidate.effort == "xhigh":
                gate_xhigh = sum(a.effort == "xhigh" for a in gate.attempts)
                allowed_reason = (
                    failure.confidence == "high"
                    and failure.reason_code in {
                        "verification_inconclusive", "verified_capability_limit",
                        "conflicting_evidence", "production_root_cause_unresolved",
                    }
                    and bool(failure.evidence)
                )
                if not allowed_reason or gate_xhigh >= self.max_xhigh_attempts_per_gate or run_xhigh_count >= self.max_xhigh_attempts_per_run:
                    return AdaptiveDecision.TERMINAL, "xhigh conditions or budget not satisfied"
            return AdaptiveDecision.ESCALATE_CAPABILITY, "current capability exhausted; advance exactly one rank"
        return AdaptiveDecision.TERMINAL, f"no safe automatic recovery for {kind.value}"


class ProductionRunner:
    """Closed-loop v0.2 Coordinator. No classifier worker is dispatched."""

    def __init__(self, *, router: Router | None = None, adapter_factory: AdapterFactory = OrcaAdapter,
                 timeout_ms: int = 300_000, same_level_retry_limit: int = 1,
                 capability_escalation_limit_per_gate: int = 5, max_attempts_per_gate: int = 8,
                 max_attempts_per_run: int = 24, max_xhigh_attempts_per_gate: int = 1,
                 max_xhigh_attempts_per_run: int = 1, max_escalations: int | None = None) -> None:
        self.router = router or Router()
        self.adapter_factory = adapter_factory
        self.timeout_ms = timeout_ms
        self.capability_escalation_limit_per_gate = capability_escalation_limit_per_gate
        self.max_attempts_per_gate = max_attempts_per_gate
        self.max_attempts_per_run = max_attempts_per_run
        self.engine = DecisionEngine(same_level_retry_limit=same_level_retry_limit,
            max_xhigh_attempts_per_gate=max_xhigh_attempts_per_gate,
            max_xhigh_attempts_per_run=max_xhigh_attempts_per_run)

    def run(self, task: str, workspace: str | Path) -> RunResult:
        started = time.monotonic()
        root = Path(workspace).resolve()
        plan = self.router.classify(task)
        result = RunResult(None, str(root), plan.level, routing_plan=plan.to_dict())
        if plan.verifier == "required":
            result.verification_decision = "required"
            result.verification_mode = VerificationMode.HYBRID.value
            result.verifier_required_reason = "Critical safety policy"
        elif plan.verifier == "conditional":
            result.verification_decision = "conditional-deterministic-first"
        try:
            adapter = self.adapter_factory(root)
            run_id = adapter.create_run(task)
            result.run_id = run_id
        except Exception as exc:
            result.phase_list.append(PhaseResult("startup", "Coordinator", "", "", "", PhaseStatus.BLOCKED, error=str(exc)))
            return result

        assessment_approved = False
        completed_phases: set[Phase] = set()
        queue: list[tuple[Route, str]] = [(route, f"{route.phase.value}-{index + 1}") for index, route in enumerate(plan.routes)]
        run_xhigh_count = 0
        while queue and result.cost_metrics.attempt_count < self.max_attempts_per_run:
            route, gate_id = queue.pop(0)
            gate = result.logical_gates.setdefault(gate_id, LogicalGateState(gate_id, route.phase.value, route.authority.value))
            if gate.status == "SUCCESS":
                continue
            if len(gate.attempts) >= self.max_attempts_per_gate:
                gate.status = "FAILED"; result.final_status = PhaseStatus.FAILED; break
            if route.requires_assessment and not assessment_approved:
                result.phase_list.append(PhaseResult(route.phase.value, route.role, route.model, route.effort,
                    route.authority.value, PhaseStatus.BLOCKED, gate_id, error="Critical WRITE blocked until assessment succeeds"))
                result.final_status = PhaseStatus.BLOCKED; break
            if route.authority is Authority.WORKSPACE_WRITE and gate.active_mutation_attempt:
                result.final_status = PhaseStatus.BLOCKED; break

            attempt_no = len(gate.attempts) + 1
            attempt_id = f"{gate_id}-attempt-{attempt_no}"
            parent = gate.attempts[-1].attempt_id if gate.attempts else None
            before = dict(adapter.change_detector())
            if not gate.attempts:
                gate.baseline_changes = dict(before)
            fingerprint = self._workspace_fingerprint(root, before)
            attempt = AttemptMetadata(gate_id, attempt_id, attempt_no, parent, route.phase.value,
                route.model, route.effort, capability_rank(route), route.authority.value,
                workspace_fingerprint=fingerprint)
            # Capture only settled attempts. Appending the current pending
            # attempt first would hide the prior failure/diff at attempts[-1].
            prior_packet = self._evidence_packet(gate)
            gate.attempts.append(attempt)
            if route.authority is Authority.WORKSPACE_WRITE:
                gate.active_mutation_attempt = attempt_id
            phase = PhaseResult(route.phase.value, route.role, route.model, route.effort, route.authority.value,
                                PhaseStatus.BLOCKED, gate_id, attempt_id, attempt_no)
            worker: WorkerHandle | None = None
            attempt_started = time.monotonic()
            completion: Mapping[str, Any] = {}
            normalized = NormalizedWorkerResult("unknown", "")
            decision = AdaptiveDecision.TERMINAL
            diagnosis_no_progress = False
            lifecycle_settled = False
            try:
                evidence_source = gate.parent_gate_id or gate.evidence_source_gate_id
                packet = (self._evidence_packet(result.logical_gates[evidence_source])
                          if evidence_source else prior_packet)
                phase_spec = self._phase_spec(task, route, gate, packet)
                attempt.phase_spec_size = len(phase_spec.encode())
                attempt.evidence_packet_size = len(json.dumps(packet.to_dict(), ensure_ascii=False).encode())
                task_id = adapter.create_task(run_id, f"{route.role}: {task[:80]}", phase_spec)
                phase.task_id = task_id
                worker = adapter.start_worker(run_id, task_id, route, assessment_approved=assessment_approved)
                phase.dispatch_id = worker.dispatch_id
                completion = adapter.wait_for_completion(run_id, worker, self.timeout_ms)
                mode = completion.get("mode")
                lifecycle_settled = mode == "worker_done"
                explicit_failure: FailureClassification | None = None
                if mode == "escalation":
                    finding = _summary(completion.get("message", completion))
                    adapter.settle_escalation(run_id, worker, finding)
                    lifecycle_settled = True
                    if finding and finding not in gate.verified_facts:
                        gate.verified_facts.append(finding)
                    new_brief = self.router.normalize(finding)
                    if new_brief.positive_risk_signals:
                        explicit_failure = FailureClassification(
                            FailureClass.CAPABILITY_FAILURE, "high",
                            "confirmed_risk_floor_high" if "SOL_HIGH_RISK" in new_brief.requested_actions else "confirmed_risk_floor",
                            (finding,))
                    else:
                        explicit_failure = FailureClassification(
                            FailureClass.CAPABILITY_FAILURE, "medium", "worker_escalation", (finding,))
                    normalized = NormalizedWorkerResult(
                        "FAILED", finding, failure_class_hint="CAPABILITY_FAILURE",
                        reason="worker requested Coordinator reclassification", evidence=(finding,))
                elif mode == "question":
                    question = _summary(completion.get("message", completion))
                    explicit_failure = FailureClassification(
                        FailureClass.USER_ACTION_REQUIRED, "high", "question_event",
                        (question,))
                    normalized = NormalizedWorkerResult(
                        "BLOCKED", question, reason="worker question requires resolution",
                        needs_user_input=True, evidence=(question,))
                else:
                    safe_to_read = completion.get("safe_to_read", True) is not False
                    if mode == "timeout" and not safe_to_read:
                        explicit_failure = FailureClassification(
                            FailureClass.ORCHESTRATION_FAILURE, "high",
                            "lifecycle_deadline_exhausted",
                            ("No matching lifecycle message or safe terminal evidence before deadline",),
                        )
                        normalized = NormalizedWorkerResult(
                            "FAILED", "lifecycle deadline exhausted",
                            reason="no safe completion evidence before lifecycle deadline",
                        )
                    elif (mode == "worker_done" and not safe_to_read
                          and not isinstance(completion.get("result"), Mapping)):
                        explicit_failure = FailureClassification(
                            FailureClass.ORCHESTRATION_FAILURE, "high",
                            "lifecycle_result_deadline_exhausted",
                            ("Worker lifecycle settled without safe result evidence before deadline",),
                        )
                        normalized = NormalizedWorkerResult(
                            "FAILED", "lifecycle result deadline exhausted",
                            reason="no safe worker result evidence before lifecycle deadline",
                        )
                    else:
                        raw = completion.get("result") if mode in {"worker_done", "timeout"} else None
                        if not isinstance(raw, Mapping) and safe_to_read:
                            try:
                                raw = adapter.read_result(worker)
                            except Exception as exc:
                                if lifecycle_settled:
                                    raise LifecycleSettlementError(
                                        f"result read failed after lifecycle settlement: {exc}") from exc
                                raise
                        normalized = ResultNormalizer.normalize(raw or {})
                if (route.authority is Authority.WORKSPACE_WRITE
                        and self._non_idempotent_intent(task)
                        and not any(normalized.fields.get(key) for key in (
                            "idempotent", "operation_not_executed", "safe_rollback_confirmed"))):
                    normalized.fields["non_idempotent_operation"] = True
                attempt.attempted_actions = tuple(dict.fromkeys((
                    *_strings(normalized.fields.get("attempted_actions")),
                    *_strings(normalized.fields.get("requirements_completed")),
                    *normalized.tests_run,
                )))[:20]
                attempt.unresolved_questions = tuple(normalized.unresolved_questions[:20])
                attempt.test_results = tuple(normalized.test_results[:20])
                attempt.relevant_evidence_refs = tuple(dict.fromkeys((
                    *_strings(normalized.fields.get("evidence_refs")),
                    *normalized.evidence,
                )))[:20]
                attempt.target_fingerprint = {key: normalized.fields[key] for key in (
                    "git_head", "target_id", "deployment_id", "target_url", "implementation_commit",
                    "deployment_commit", "verification_timestamp") if normalized.fields.get(key) is not None}
                attempt.verification_mode = (str(normalized.fields.get("verification_mode"))
                                             if normalized.fields.get("verification_mode") else None)
                phase.worker_result = normalized.public()
                after = dict(adapter.change_detector())
                actual_changes = self._changed_paths(before, after)
                gate_changes = self._changed_paths(gate.baseline_changes, after)
                attempt.files_changed = tuple(gate_changes)
                evidence_ok, evidence_reason = SuccessEvidenceGate.evaluate(route, normalized, gate_changes)
                if (evidence_ok and route.phase is Phase.IMPLEMENTATION
                        and route.authority is Authority.READ_ONLY):
                    prior_write = any(
                        prior.authority == Authority.WORKSPACE_WRITE.value
                        for prior in gate.attempts[:-1]
                    )
                    objective_proof = (normalized.fields.get("prior_mutation_succeeded") is True
                                       and bool(normalized.evidence))
                    if not prior_write or not objective_proof:
                        evidence_ok = False
                        evidence_reason = ("READ_ONLY evidence repair cannot substitute for a WRITE success; "
                                           "objective prior_mutation_succeeded evidence is required")
                target_mismatch = self._target_mismatch(normalized.fields)
                completion_result = ResultNormalizer.normalize(completion)
                reports_failure = (normalized.status in {"FAILED", "FAILURE", "ERROR", "BLOCKED"}
                                   or completion_result.status in {"FAILED", "FAILURE", "ERROR", "BLOCKED"}
                                   or normalized.external_blocker is not None or normalized.needs_user_input)
                classified_result = completion_result if completion_result.status in {
                    "FAILED", "FAILURE", "ERROR", "BLOCKED"} else normalized
                failure = explicit_failure or (
                    FailureClassifier.classify(route, completion, classified_result, evidence_reason, target_mismatch)
                    if target_mismatch or reports_failure or not evidence_ok else None)
                if evidence_ok and explicit_failure is None:
                    if completion.get("mode") == "worker_done":
                        phase.settlement = "worker_done"
                    elif completion.get("mode") == "timeout":
                        adapter.trusted_relay(run_id, worker, normalized.summary, files_modified=actual_changes)
                        phase.settlement = "coordinator_trusted_relay"
                    else:
                        failure = FailureClassification(FailureClass.ORCHESTRATION_FAILURE, "high", "missing_lifecycle")
                if failure is None:
                    decision = AdaptiveDecision.SUCCESS
                    phase.status = PhaseStatus.SUCCESS
                    gate.status = "SUCCESS"
                    completed_phases.add(route.phase)
                    if gate.parent_gate_id:
                        parent_gate = result.logical_gates[gate.parent_gate_id]
                        diagnostic_facts = tuple(dict.fromkeys((
                            *normalized.evidence,
                            *_strings(normalized.fields.get("conclusion")),
                            *_strings(normalized.fields.get("files_checked")),
                            *_strings(normalized.fields.get("tools_used")),
                        )))
                        new_facts = [fact for fact in diagnostic_facts if fact not in parent_gate.verified_facts]
                        if new_facts:
                            parent_gate.verified_facts.extend(new_facts)
                        else:
                            diagnosis_no_progress = True
                    if route.phase is Phase.ASSESSMENT:
                        assessment_approved = True
                    if (route.phase is Phase.IMPLEMENTATION and plan.verifier == "conditional"
                            and (normalized.fields.get("verification_mode") in {
                                VerificationMode.MODEL_REVIEW.value, VerificationMode.HYBRID.value}
                                 or bool(normalized.fields.get("remaining_risk")))):
                        queue.insert(0, (_fresh_verifier(), "verification-conditional"))
                        result.verification_decision = "model-review-selected"
                        result.verification_mode = str(normalized.fields.get("verification_mode") or VerificationMode.MODEL_REVIEW.value)
                        result.verifier_required_reason = str(normalized.fields.get("remaining_risk") or "remaining semantic regression risk")
                        result.remaining_risk = list(_strings(normalized.fields.get("remaining_risk")))
                    elif route.phase is Phase.IMPLEMENTATION and plan.verifier == "conditional":
                        result.verification_decision = "deterministic-evidence-sufficient"
                        result.deterministic_coverage = list(normalized.tests_run)
                    if route.phase is Phase.VERIFICATION:
                        result.verifier_result = normalized.public()
                else:
                    material = self._material_delta(gate, failure, normalized)
                    if failure.evidence:
                        attempt.relevant_evidence_refs = tuple(dict.fromkeys((
                            *attempt.relevant_evidence_refs, *failure.evidence,
                        )))[:20]
                    if failure.reason_code in {"confirmed_risk_floor", "confirmed_risk_floor_high"}:
                        floor_name = "Sol/high" if failure.reason_code.endswith("_high") else "Sol/medium"
                        decision, reason = AdaptiveDecision.APPLY_RISK_FLOOR, f"confirmed risk applies {floor_name} floor"
                    else:
                        decision, reason = self.engine.decide(gate, route, failure,
                            material_new_evidence=material, run_xhigh_count=run_xhigh_count)
                    prior_escalations = sum(
                        prior.decision == AdaptiveDecision.ESCALATE_CAPABILITY.value
                        for prior in gate.attempts[:-1]
                    )
                    if (decision is AdaptiveDecision.ESCALATE_CAPABILITY
                            and prior_escalations >= self.capability_escalation_limit_per_gate):
                        decision, reason = AdaptiveDecision.TERMINAL, "capability escalation budget exhausted"
                    attempt.failure_class = failure.failure_class.value
                    attempt.classification_confidence = failure.confidence
                    attempt.decision_reason = reason
                    if decision is AdaptiveDecision.BLOCKED:
                        attempt.blocker_kind = failure.failure_class.value
                    if decision is AdaptiveDecision.TERMINAL:
                        attempt.terminal_reason = reason
                    attempt.material_new_evidence = material
                    attempt.retry_delta = self._retry_delta(failure, normalized) if material else None
                    phase.error = reason
                    phase.status = (PhaseStatus.ESCALATION_REQUESTED if completion.get("mode") == "escalation"
                                    else PhaseStatus.BLOCKED if decision is AdaptiveDecision.BLOCKED
                                    else PhaseStatus.FAILED)
                    if mode != "escalation":
                        try:
                            if lifecycle_settled:
                                adapter.fail_task(run_id, task_id, reason)
                            elif hasattr(adapter, "fail_worker"):
                                adapter.fail_worker(run_id, worker, reason)
                            else:
                                adapter.fail_task(run_id, task_id, reason)
                        except CoordinatorError as exc:
                            raise LifecycleSettlementError(
                                f"failure task settlement failed: {exc}") from exc
                    if decision is AdaptiveDecision.REOPEN_IMPLEMENTATION:
                        completed_phases.discard(Phase.IMPLEMENTATION)
                        attempt.prior_gate_invalidated = True
                        invalidated = next((candidate.logical_gate_id for candidate in result.logical_gates.values()
                                           if candidate.phase == Phase.IMPLEMENTATION.value), None)
                        attempt.invalidated_gate_id = invalidated
                        attempt.invalidation_reason = "Fresh Verifier confirmed TARGET_FAILED"
                        attempt.invalidation_evidence = "; ".join(normalized.evidence[:3])
                    if decision is AdaptiveDecision.APPLY_RISK_FLOOR:
                        floor = 4 if failure.reason_code == "confirmed_risk_floor_high" else 3
                        if route.authority is Authority.WORKSPACE_WRITE:
                            assessment_approved = False
                            model, effort = capability_at(floor)
                            assessment = Route(Phase.ASSESSMENT, "Risk Assessor", model, effort,
                                               Authority.READ_ONLY, "SAFE")
                            implementation = replace(apply_risk_floor(route, floor),
                                                     requires_assessment=True)
                            verifier = Route(Phase.VERIFICATION, "Fresh Verifier", model, effort,
                                             Authority.READ_ONLY, "SAFE")
                            assessment_id = f"{gate_id}-risk-assessment"
                            verification_id = f"{gate_id}-risk-verification"
                            result.logical_gates.setdefault(assessment_id, LogicalGateState(
                                assessment_id, Phase.ASSESSMENT.value, Authority.READ_ONLY.value,
                                evidence_source_gate_id=gate_id))
                            result.logical_gates.setdefault(verification_id, LogicalGateState(
                                verification_id, Phase.VERIFICATION.value, Authority.READ_ONLY.value,
                                evidence_source_gate_id=gate_id))
                            queue.insert(0, (verifier, verification_id))
                            queue.insert(0, (implementation, gate_id))
                            queue.insert(0, (assessment, assessment_id))
                            result.classification = "critical"
                            result.verification_decision = "required"
                            result.verification_mode = VerificationMode.HYBRID.value
                            result.verifier_required_reason = "new Critical risk discovered during WRITE"
                        else:
                            queue.insert(0, (apply_risk_floor(route, floor), gate_id))
                    else:
                        self._schedule_decision(queue, route, gate_id, gate, decision, result, normalized)
                    if decision is AdaptiveDecision.ESCALATE_CAPABILITY and next_capability(route).effort == "xhigh":
                        run_xhigh_count += 1
                    if decision in {AdaptiveDecision.BLOCKED, AdaptiveDecision.TERMINAL}:
                        gate.status = decision.value
                        result.final_status = PhaseStatus.BLOCKED if decision is AdaptiveDecision.BLOCKED else PhaseStatus.FAILED
                attempt.decision = decision.value
                self._record_decision(result, attempt, decision)
            except LifecycleSettlementError as exc:
                decision = AdaptiveDecision.TERMINAL
                reason = f"lifecycle settlement failed; no worker retry: {exc}"
                phase.status = PhaseStatus.FAILED
                phase.error = reason
                attempt.failure_class = FailureClass.ORCHESTRATION_FAILURE.value
                attempt.classification_confidence = "high"
                attempt.decision = decision.value
                attempt.decision_reason = reason
                attempt.terminal_reason = reason
                gate.status = "TERMINAL"
                result.final_status = PhaseStatus.FAILED
                queue.clear()
                self._record_decision(result, attempt, decision)
            except CoordinatorError as exc:
                error_result = NormalizedWorkerResult("FAILED", "", reason=str(exc))
                failure = FailureClassifier.classify(
                    route,
                    {"mode": "adapter_error", "error_code": exc.code},
                    error_result,
                    str(exc),
                )
                material = self._material_delta(gate, failure, error_result)
                if route.model == SOL and "unavailable" in str(exc).lower():
                    if route.effort == "xhigh":
                        decision, reason = AdaptiveDecision.TERMINAL, "Sol/xhigh unavailable; no automatic max fallback"
                    else:
                        decision, reason = AdaptiveDecision.BLOCKED, "MODEL_UNAVAILABLE; no Terra downgrade"
                else:
                    decision, reason = self.engine.decide(gate, route, failure,
                        material_new_evidence=material, run_xhigh_count=run_xhigh_count)
                phase.status = PhaseStatus.BLOCKED if decision is AdaptiveDecision.BLOCKED else PhaseStatus.FAILED
                phase.error = reason
                attempt.failure_class = failure.failure_class.value
                attempt.classification_confidence = failure.confidence
                attempt.decision = decision.value
                attempt.decision_reason = reason
                attempt.material_new_evidence = material
                attempt.retry_delta = self._retry_delta(failure, error_result) if material else None
                attempt.blocker_kind = "MODEL_UNAVAILABLE" if decision is AdaptiveDecision.BLOCKED else None
                attempt.terminal_reason = reason if decision is AdaptiveDecision.TERMINAL else None
                if phase.task_id:
                    try:
                        if worker is not None and hasattr(adapter, "fail_worker"):
                            adapter.fail_worker(run_id, worker, reason)
                        else:
                            adapter.fail_task(run_id, phase.task_id, reason)
                    except Exception: pass
                self._schedule_decision(queue, route, gate_id, gate, decision, result, error_result)
                self._record_decision(result, attempt, decision)
                result.final_status = PhaseStatus.BLOCKED if decision is AdaptiveDecision.BLOCKED else PhaseStatus.FAILED
            except Exception as exc:
                phase.status = PhaseStatus.BLOCKED; phase.error = str(exc)
                attempt.failure_class = FailureClass.ORCHESTRATION_FAILURE.value
                attempt.classification_confidence = "high"; attempt.decision = AdaptiveDecision.TERMINAL.value
                if phase.task_id:
                    try:
                        if worker is not None and hasattr(adapter, "fail_worker"):
                            adapter.fail_worker(run_id, worker, str(exc))
                        else:
                            adapter.fail_task(run_id, phase.task_id, str(exc))
                    except Exception: pass
                result.final_status = PhaseStatus.BLOCKED
            finally:
                elapsed = time.monotonic() - attempt_started
                attempt.elapsed_time = elapsed; result.cost_metrics.record(route, elapsed)
                try:
                    current_changes = dict(adapter.change_detector())
                    attempt.files_changed = tuple(self._changed_paths(gate.baseline_changes, current_changes))
                except Exception:
                    pass
                if result.adaptive_decisions and result.adaptive_decisions[-1].get("attempt_id") == attempt.attempt_id:
                    result.adaptive_decisions[-1]["elapsed_time"] = elapsed
                cleanup_ok_for_attempt = worker is None
                if worker is not None:
                    try: cleanup = adapter.release(worker)
                    except Exception as exc: cleanup = {"state": "release_failed", "error": str(exc)}
                    phase.cleanup = cleanup; result.cleanup_result.append(cleanup)
                    cleanup_ok_for_attempt = isinstance(cleanup, Mapping) and cleanup.get("state") in {"released", "closed"}
                if cleanup_ok_for_attempt:
                    gate.active_mutation_attempt = None
                result.phase_list.append(phase)
            if not cleanup_ok_for_attempt:
                gate.status = "TERMINAL"
                result.final_status = PhaseStatus.FAILED
                queue.clear()
                break
            if diagnosis_no_progress and gate.parent_gate_id:
                parent_gate = result.logical_gates[gate.parent_gate_id]
                parent_gate.status = "TERMINAL"
                queue[:] = [(queued_route, queued_gate) for queued_route, queued_gate in queue
                            if queued_gate != gate.parent_gate_id]
                result.adaptive_decisions.append({
                    "logical_gate_id": gate.parent_gate_id,
                    "decision": AdaptiveDecision.TERMINAL.value,
                    "decision_reason": "READ_ONLY diagnosis produced no material new evidence; next WRITE fenced",
                    "material_new_evidence": False,
                    "terminal_reason": "no-progress diagnosis circuit breaker",
                })
                result.final_status = PhaseStatus.FAILED
                queue.clear()
                break
            if decision in {AdaptiveDecision.BLOCKED, AdaptiveDecision.TERMINAL} or attempt.failure_class == FailureClass.ORCHESTRATION_FAILURE.value:
                break

        cleanup_ok = all(isinstance(item, Mapping) and item.get("state") in {"released", "closed"}
                         for item in result.cleanup_result)
        if queue and result.cost_metrics.attempt_count >= self.max_attempts_per_run:
            result.final_status = PhaseStatus.FAILED
            result.adaptive_decisions.append({
                "decision": AdaptiveDecision.TERMINAL.value,
                "decision_reason": "max_attempts_per_run exhausted",
                "terminal_reason": "finite-run circuit breaker",
            })
        all_required = all(route.phase in completed_phases for route in plan.routes)
        if cleanup_ok and not queue and all_required and result.final_status not in {PhaseStatus.FAILED, PhaseStatus.BLOCKED}:
            result.final_status = PhaseStatus.SUCCESS
        elif cleanup_ok and not queue and all_required and all(g.status == "SUCCESS" for g in result.logical_gates.values()):
            result.final_status = PhaseStatus.SUCCESS
        elif not cleanup_ok:
            result.final_status = PhaseStatus.FAILED
        result.cost_metrics.elapsed_time = time.monotonic() - started
        return result

    @staticmethod
    def _phase_spec(task: str, route: Route, gate: LogicalGateState | None = None,
                    evidence: EvidencePacket | None = None) -> str:
        contract = {
            Phase.INVESTIGATION: "conclusion, evidence, files_checked or tools_used, unresolved_questions",
            Phase.ASSESSMENT: "risks, impact, rollback, write_ready, unresolved_questions",
            Phase.IMPLEMENTATION: "files_modified, requirements_completed, tests_run, test_results, unexecuted_verification, workspace_diff",
            Phase.VERIFICATION: "verification_outcome (VERIFIED, NOT_VERIFIED, INCONCLUSIVE, TARGET_FAILED), evidence, unresolved_questions",
        }[route.phase]
        packet = json.dumps(evidence.to_dict(), ensure_ascii=False, separators=(",", ":")) if evidence else "{}"
        return (f"User task:\n{task}\n\nAssigned logical gate: {gate.logical_gate_id if gate else route.phase.value}; "
                f"phase: {route.phase.value}; role: {route.role}; authority: {route.authority.value}. "
                "Read AGENTS.md. Do only this gate; do not spawn workers. Construct one complete result object first, "
                f"including status, summary and: {contract}. The summary string itself must be exactly three sentences. "
                f"Evidence packet (bounded input; do not echo it as an output field): {packet}. Report escalation "
                "findings to the Coordinator. Include the required result fields and concise evidence values. "
                "Serialize the complete object as compact UTF-8 JSON and keep it in memory (for example, a shell "
                "variable). Do not create a temporary file or write anywhere to deliver it: READ-ONLY workers cannot "
                "write to /tmp or the workspace. Your last tool call must invoke the installed `orca-adaptive "
                "worker-report` helper exactly once with --result-json equal to that in-memory JSON and the exact --from, "
                "--dispatch-capability, --task-id, and --dispatch-id values from the injected worker_done command. Do not "
                "invoke `orca-ide orchestration send` yourself. The helper validates the result, attempts worker_done "
                "exactly once with the three-sentence summary and matching outcome, compresses the full result, and "
                "always prints exactly one framed marker. Never call worker-report or worker_done twice. "
                "The marker is "
                "durable terminal evidence for the Coordinator when a successful CLI response is not delivered through "
                "the Orca inbox; it is not a second lifecycle message. Use this marker: "
                "ADAPTIVE_RESULT_GZ64:<base64url gzip-compressed compact UTF-8 JSON, padding optional>:END_ADAPTIVE_RESULT "
                "encoding the same compact JSON object. Whitespace inside the base64url payload is allowed for terminal "
                "wrapping. After that tool call, copy its complete marker exactly once as your entire final assistant "
                "response so it remains visible outside collapsed TUI tool output. Do not regenerate or alter the "
                "marker, add other prose, or call another tool.")

    @staticmethod
    def _changed_paths(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
        return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))

    @staticmethod
    def _workspace_fingerprint(root: Path, changes: Mapping[str, str]) -> dict[str, object]:
        digest = hashlib.sha256(json.dumps(sorted(changes.items())).encode()).hexdigest()
        completed = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                   check=False, capture_output=True, text=True)
        head = completed.stdout.strip() if completed.returncode == 0 else None
        return {"workspace": str(root), "git_head": head, "workspace_dirty_fingerprint": digest}

    @staticmethod
    def _target_mismatch(fields: Mapping[str, Any]) -> str | None:
        expected = fields.get("implementation_commit")
        actual = fields.get("deployment_commit")
        if expected and actual and expected != actual:
            return f"implementation_commit={expected} deployment_commit={actual}"
        return None

    @staticmethod
    def _non_idempotent_intent(task: str) -> bool:
        text = " ".join(task.lower().split())
        signals = (
            "trigger a production deployment", "trigger production deployment",
            "production deployment", "deployment trigger", "external write api",
            "production mutation", "billing action", "charge customer",
            "운영 배포 실행", "프로덕션 배포 실행", "외부 쓰기 api",
            "운영 데이터 변경", "결제 실행", "과금 실행",
        )
        return any(signal in text for signal in signals)

    @staticmethod
    def _material_delta(gate: LogicalGateState, failure: FailureClassification,
                        result: NormalizedWorkerResult) -> bool:
        signature = (gate.attempts[-1].capability_rank, tuple(gate.verified_facts), failure.failure_class.value,
                     tuple(result.evidence), result.reason, result.summary[:200])
        if not gate.attempts[:-1]:
            setattr(gate.attempts[-1], "_failure_signature", signature)
            return True
        previous = gate.attempts[-2]
        previous_signature = getattr(previous, "_failure_signature", None)
        setattr(gate.attempts[-1], "_failure_signature", signature)
        material = signature != previous_signature
        gate.no_progress_count = 0 if material else gate.no_progress_count + 1
        return material and gate.no_progress_count < 2

    @staticmethod
    def _retry_delta(failure: FailureClassification, result: NormalizedWorkerResult) -> str:
        return f"focused {failure.reason_code}: {', '.join(result.evidence[:3]) or result.reason or 'clarify evidence contract'}"[:500]

    @staticmethod
    def _evidence_packet(gate: LogicalGateState) -> EvidencePacket:
        attempts = gate.attempts[-3:]
        latest = attempts[-1] if attempts else None
        return EvidencePacket(gate.logical_gate_id,
            tuple(f"{a.attempt_id}:{a.failure_class or 'pending'}:{a.decision or 'pending'}" for a in attempts),
            verified_facts=tuple(gate.verified_facts[-20:]),
            attempted_actions=latest.attempted_actions if latest else (),
            failure_class=latest.failure_class if latest else None,
            failure_reason=latest.decision_reason if latest else None,
            unresolved_questions=latest.unresolved_questions if latest else (),
            files_changed=latest.files_changed if latest else (),
            test_results=latest.test_results if latest else (),
            target_fingerprint=tuple(sorted((str(key), str(value))
                                            for key, value in (latest.target_fingerprint if latest else {}).items())),
            relevant_evidence_refs=latest.relevant_evidence_refs if latest else (),
            escalation_reason=latest.decision_reason if latest else None)

    def _schedule_decision(self, queue: list[tuple[Route, str]], route: Route, gate_id: str,
                           gate: LogicalGateState, decision: AdaptiveDecision, result: RunResult,
                           normalized: NormalizedWorkerResult) -> None:
        if decision in {AdaptiveDecision.COLLECT_EVIDENCE, AdaptiveDecision.RESULT_REPAIR,
                        AdaptiveDecision.RETRY_SAME_CAPABILITY, AdaptiveDecision.REPLAN}:
            rank = capability_rank(route)
            gate.same_level_retries[rank] = gate.same_level_retries.get(rank, 0) + 1
            repair_route = route
            if (decision in {AdaptiveDecision.COLLECT_EVIDENCE, AdaptiveDecision.RESULT_REPAIR}
                    and route.authority is Authority.WORKSPACE_WRITE):
                repair_route = replace(route, role="Result Evidence Collector", authority=Authority.READ_ONLY,
                                       approval_grade="SAFE", requires_assessment=False)
            queue.insert(0, (repair_route, gate_id))
        elif decision is AdaptiveDecision.ESCALATE_CAPABILITY:
            advanced = next_capability(route)
            if advanced:
                queue.insert(0, (advanced, gate_id))
                result.escalation.append({"logical_gate_id": gate_id, "from": route.to_dict(), "to": advanced.to_dict()})
        elif decision is AdaptiveDecision.APPLY_RISK_FLOOR:
            queue.insert(0, (apply_risk_floor(route, 3), gate_id))
        elif decision is AdaptiveDecision.INSERT_READ_ONLY_DIAGNOSIS:
            diagnostic = replace(route, phase=Phase.INVESTIGATION, role="Failure Diagnostician", authority=Authority.READ_ONLY,
                                 approval_grade="SAFE", requires_assessment=False)
            diagnosis_no = 1 + sum(key.startswith(f"{gate_id}-diagnosis-") for key in result.logical_gates)
            diagnosis_id = f"{gate_id}-diagnosis-{diagnosis_no}"
            result.logical_gates[diagnosis_id] = LogicalGateState(
                diagnosis_id, Phase.INVESTIGATION.value, Authority.READ_ONLY.value,
                parent_gate_id=gate_id)
            queue.insert(0, (route, gate_id))
            queue.insert(0, (diagnostic, diagnosis_id))
        elif decision is AdaptiveDecision.REOPEN_IMPLEMENTATION:
            implementation_state = next((candidate for candidate in result.logical_gates.values()
                                         if candidate.phase == Phase.IMPLEMENTATION.value), None)
            implementation_gate = (implementation_state.logical_gate_id
                                   if implementation_state else "implementation-reopened")
            if implementation_state and implementation_state.attempts:
                previous = implementation_state.attempts[-1]
                implementation = Route(
                    Phase.IMPLEMENTATION, "Lead Implementer", previous.model, previous.effort,
                    Authority.WORKSPACE_WRITE, "REVIEW", requires_assessment=any(
                        candidate.phase == Phase.ASSESSMENT.value
                        for candidate in result.logical_gates.values()))
            else:
                implementation = Route(Phase.IMPLEMENTATION, "Lead Implementer", route.model, route.effort,
                                       Authority.WORKSPACE_WRITE, "REVIEW", requires_assessment=False)
            if implementation_gate in result.logical_gates:
                result.logical_gates[implementation_gate].status = "PENDING"
            queue.insert(0, (route, gate_id))
            queue.insert(0, (implementation, implementation_gate))

    @staticmethod
    def _record_decision(result: RunResult, attempt: AttemptMetadata, decision: AdaptiveDecision) -> None:
        next_route = None
        if decision is AdaptiveDecision.ESCALATE_CAPABILITY:
            model, effort = capability_at(min(attempt.capability_rank + 1, 5))
            next_route = (model, effort, min(attempt.capability_rank + 1, 5))
        result.adaptive_decisions.append({
            "logical_gate_id": attempt.logical_gate_id, "attempt_id": attempt.attempt_id,
            "from_model": attempt.model, "from_effort": attempt.effort,
            "from_rank": attempt.capability_rank, "authority": attempt.authority,
            "to_model": next_route[0] if next_route else attempt.model,
            "to_effort": next_route[1] if next_route else attempt.effort,
            "to_rank": next_route[2] if next_route else attempt.capability_rank,
            "failure_class": attempt.failure_class, "classification_confidence": attempt.classification_confidence,
            "decision": decision.value, "decision_reason": attempt.decision_reason,
            "retry_delta": attempt.retry_delta, "material_new_evidence": attempt.material_new_evidence,
            "files_changed": list(attempt.files_changed), "workspace_fingerprint": attempt.workspace_fingerprint,
            "target_fingerprint": attempt.target_fingerprint,
            "verification_mode": attempt.verification_mode,
            "blocker_kind": attempt.blocker_kind, "terminal_reason": attempt.terminal_reason,
            "elapsed_time": attempt.elapsed_time,
        })
