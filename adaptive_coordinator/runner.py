from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .models import Authority, Phase, Route, RoutingPlan
from .orca import CoordinatorError, OrcaAdapter, WorkerHandle
from .routing import SOL, Router


class PhaseStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    ESCALATION_REQUESTED = "ESCALATION_REQUESTED"


@dataclass
class PhaseResult:
    phase: str
    role: str
    model: str
    effort: str
    authority: str
    status: PhaseStatus
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workspace": self.workspace,
            "classification": self.classification,
            "phase_list": [phase.to_dict() for phase in self.phase_list],
            "models": [
                {
                    "model": phase.model,
                    "effort": phase.effort,
                    "authority": phase.authority,
                }
                for phase in self.phase_list
            ],
            "escalation": self.escalation,
            "verifier_result": self.verifier_result,
            "final_status": self.final_status.value,
            "cleanup_result": self.cleanup_result,
            "routing_plan": self.routing_plan,
        }


AdapterFactory = Callable[[Path], OrcaAdapter]


def _fresh_verifier(effort: str = "medium") -> Route:
    return Route(
        phase=Phase.VERIFICATION,
        role="Fresh Verifier",
        model=SOL,
        effort=effort,
        authority=Authority.READ_ONLY,
        approval_grade="SAFE",
    )


def _messages(payload: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    for key in ("message", "result", "worker", "delivery"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            yield value
    yield payload


def _failure_reason(payload: Any) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    for item in _messages(payload):
        status = str(item.get("status") or item.get("outcome") or "").lower()
        if status in {"failed", "failure", "blocked", "error"}:
            return str(item.get("reason") or item.get("error") or status)
        if item.get("success") is False:
            return str(item.get("reason") or item.get("error") or "worker reported failure")
    return None


def _summary(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, Mapping):
        for key in ("summary", "finalOutput", "output", "body", "text"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for item in _messages(payload):
            for key in ("summary", "finalOutput", "output", "body", "text"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return json.dumps(payload, ensure_ascii=False, default=str)


def _public_worker_result(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {"summary": _summary(payload)}
    status = payload.get("status")
    if isinstance(status, Mapping):
        status = status.get("worker") or status.get("terminal")
    return {
        "summary": _summary(payload),
        "status": status,
        "source": payload.get("source"),
    }


class ProductionRunner:
    """Execute a complete RoutingPlan while keeping orchestration in the Coordinator."""

    def __init__(
        self,
        *,
        router: Router | None = None,
        adapter_factory: AdapterFactory = OrcaAdapter,
        timeout_ms: int = 300_000,
        max_escalations: int = 2,
    ) -> None:
        self.router = router or Router()
        self.adapter_factory = adapter_factory
        self.timeout_ms = timeout_ms
        self.max_escalations = max_escalations

    def run(self, task: str, workspace: str | Path) -> RunResult:
        root = Path(workspace).resolve()
        plan = self.router.classify(task)
        result = RunResult(
            run_id=None,
            workspace=str(root),
            classification=plan.level,
            routing_plan=plan.to_dict(),
        )
        try:
            adapter = self.adapter_factory(root)
            run_id = adapter.create_run(task)
            result.run_id = run_id
        except Exception as exc:
            result.final_status = PhaseStatus.BLOCKED
            result.phase_list.append(
                PhaseResult(
                    phase="startup",
                    role="Coordinator",
                    model="",
                    effort="",
                    authority="",
                    status=PhaseStatus.BLOCKED,
                    error=str(exc),
                )
            )
            return result

        queue = list(plan.routes)
        active_plan = plan
        assessment_approved = False
        implementation_succeeded = False
        verifier_seen = False
        escalation_count = 0

        while queue:
            route = queue.pop(0)
            phase_result = PhaseResult(
                phase=route.phase.value,
                role=route.role,
                model=route.model,
                effort=route.effort,
                authority=route.authority.value,
                status=PhaseStatus.BLOCKED,
            )
            worker: WorkerHandle | None = None
            try:
                if route.requires_assessment and not assessment_approved:
                    phase_result.error = "Critical WRITE blocked until READ-ONLY assessment succeeds"
                    result.phase_list.append(phase_result)
                    result.final_status = PhaseStatus.BLOCKED
                    break

                task_id = adapter.create_task(
                    run_id,
                    f"{route.role}: {task[:80]}",
                    self._phase_spec(task, route),
                )
                phase_result.task_id = task_id
                worker = adapter.start_worker(
                    run_id,
                    task_id,
                    route,
                    assessment_approved=assessment_approved,
                )
                phase_result.dispatch_id = worker.dispatch_id
                completion = adapter.wait_for_completion(run_id, worker, self.timeout_ms)
                mode = completion.get("mode")

                if mode == "escalation":
                    finding = _summary(completion.get("message", completion))
                    adapter.settle_escalation(run_id, worker, finding)
                    phase_result.status = PhaseStatus.ESCALATION_REQUESTED
                    phase_result.escalation = finding
                    result.phase_list.append(phase_result)
                    escalation_count += 1
                    if escalation_count > self.max_escalations:
                        result.final_status = PhaseStatus.BLOCKED
                        phase_result.error = "maximum Coordinator escalation count exceeded"
                        break
                    new_plan = self.router.reclassify(task, finding)
                    active_plan = new_plan
                    result.routing_plan = new_plan.to_dict()
                    result.escalation.append(
                        {
                            "finding": finding,
                            "from": route.to_dict(),
                            "to": new_plan.to_dict(),
                        }
                    )
                    result.classification = new_plan.level
                    queue = list(new_plan.routes)
                    assessment_approved = False
                    implementation_succeeded = False
                    continue

                worker_result = adapter.read_result(worker)
                phase_result.worker_result = _public_worker_result(worker_result)
                failure = _failure_reason(completion) or _failure_reason(worker_result)
                if failure:
                    phase_result.status = PhaseStatus.FAILED
                    phase_result.error = failure
                    adapter.fail_task(run_id, task_id, failure)
                    result.phase_list.append(phase_result)
                    result.final_status = PhaseStatus.FAILED
                    break

                if mode == "worker_done":
                    phase_result.settlement = "worker_done"
                elif mode == "timeout" and self._has_completion_evidence(worker_result):
                    files = self._actual_changes(worker, adapter)
                    adapter.trusted_relay(
                        run_id,
                        worker,
                        _summary(worker_result),
                        files_modified=files,
                    )
                    phase_result.settlement = "coordinator_trusted_relay"
                else:
                    phase_result.status = PhaseStatus.FAILED
                    phase_result.error = "worker completion evidence was not received"
                    adapter.fail_task(run_id, task_id, phase_result.error)
                    result.phase_list.append(phase_result)
                    result.final_status = PhaseStatus.FAILED
                    break

                phase_result.status = PhaseStatus.SUCCESS
                if route.phase is Phase.ASSESSMENT:
                    assessment_approved = True
                elif route.phase is Phase.IMPLEMENTATION:
                    implementation_succeeded = True
                elif route.phase is Phase.VERIFICATION:
                    verifier_seen = True
                    result.verifier_result = _public_worker_result(worker_result)
                result.phase_list.append(phase_result)
            except CoordinatorError as exc:
                phase_result.status = PhaseStatus.FAILED
                phase_result.error = str(exc)
                result.phase_list.append(phase_result)
                result.final_status = PhaseStatus.FAILED
                break
            except Exception as exc:
                phase_result.status = PhaseStatus.BLOCKED
                phase_result.error = str(exc)
                result.phase_list.append(phase_result)
                result.final_status = PhaseStatus.BLOCKED
                break
            finally:
                if worker is not None:
                    try:
                        cleanup = adapter.release(worker)
                    except Exception as exc:
                        cleanup = {"state": "release_failed", "error": str(exc)}
                    phase_result.cleanup = cleanup
                    result.cleanup_result.append(cleanup)

            if phase_result.status is not PhaseStatus.SUCCESS:
                break

        if (
            result.phase_list
            and all(p.status in {PhaseStatus.SUCCESS, PhaseStatus.ESCALATION_REQUESTED} for p in result.phase_list)
            and not queue
        ):
            if active_plan.verifier == "required" and not verifier_seen:
                result.final_status = PhaseStatus.FAILED
            else:
                result.final_status = PhaseStatus.SUCCESS
        elif not result.phase_list:
            result.final_status = PhaseStatus.BLOCKED

        if result.final_status is PhaseStatus.SUCCESS and active_plan.verifier == "conditional":
            # Complex implementation gets independent verification only after an
            # actual successful WRITE phase; diagnosis-only runs stay lean.
            if implementation_succeeded and not verifier_seen:
                verifier_result = self._run_appended_verifier(adapter, run_id, task, result)
                result.final_status = verifier_result

        if any(
            not isinstance(item, Mapping) or item.get("state") not in {"released", "closed"}
            for item in result.cleanup_result
        ):
            result.final_status = PhaseStatus.FAILED
        return result

    @staticmethod
    def _phase_spec(task: str, route: Route) -> str:
        return (
            f"User task:\n{task}\n\n"
            f"Assigned phase: {route.phase.value}; role: {route.role}; "
            f"authority: {route.authority.value}. "
            "Read the workspace AGENTS.md and applicable project rules before acting. "
            "Do only this phase. Report SUCCESS, FAILED, BLOCKED, or ESCALATION_REQUESTED "
            "with concrete evidence and files modified. Do not spawn another worker. "
            "Use the Orca worker_done lifecycle contract exactly once when complete."
        )

    @staticmethod
    def _has_completion_evidence(payload: Any) -> bool:
        if not isinstance(payload, Mapping):
            return bool(str(payload).strip())
        text = _summary(payload)
        state = str(payload.get("state") or payload.get("status") or "").lower()
        return bool(text and text not in {"{}", "null"}) and state not in {
            "running",
            "active",
            "starting",
        }

    @staticmethod
    def _actual_changes(worker: WorkerHandle, adapter: OrcaAdapter) -> list[str]:
        baseline = dict(worker.baseline_changes)
        current = dict(adapter.change_detector())
        return sorted(
            path for path in set(baseline) | set(current) if baseline.get(path) != current.get(path)
        )

    def _run_appended_verifier(
        self,
        adapter: OrcaAdapter,
        run_id: str,
        task: str,
        result: RunResult,
    ) -> PhaseStatus:
        route = _fresh_verifier()
        phase = PhaseResult(
            phase=route.phase.value,
            role=route.role,
            model=route.model,
            effort=route.effort,
            authority=route.authority.value,
            status=PhaseStatus.BLOCKED,
        )
        worker: WorkerHandle | None = None
        try:
            task_id = adapter.create_task(run_id, f"Fresh Verifier: {task[:80]}", self._phase_spec(task, route))
            phase.task_id = task_id
            worker = adapter.start_worker(run_id, task_id, route)
            phase.dispatch_id = worker.dispatch_id
            completion = adapter.wait_for_completion(run_id, worker, self.timeout_ms)
            worker_result = adapter.read_result(worker)
            phase.worker_result = _public_worker_result(worker_result)
            failure = _failure_reason(completion) or _failure_reason(worker_result)
            if failure:
                phase.status = PhaseStatus.FAILED
                phase.error = failure
                adapter.fail_task(run_id, task_id, failure)
                return PhaseStatus.FAILED
            if completion.get("mode") == "worker_done":
                phase.settlement = "worker_done"
            elif completion.get("mode") == "timeout" and self._has_completion_evidence(worker_result):
                adapter.trusted_relay(run_id, worker, _summary(worker_result), files_modified=[])
                phase.settlement = "coordinator_trusted_relay"
            else:
                phase.status = PhaseStatus.FAILED
                phase.error = "verifier completion evidence was not received"
                return PhaseStatus.FAILED
            phase.status = PhaseStatus.SUCCESS
            result.verifier_result = _public_worker_result(worker_result)
            return PhaseStatus.SUCCESS
        finally:
            if worker is not None:
                try:
                    cleanup = adapter.release(worker)
                except Exception as exc:
                    cleanup = {"state": "release_failed", "error": str(exc)}
                phase.cleanup = cleanup
                result.cleanup_result.append(cleanup)
            result.phase_list.append(phase)
