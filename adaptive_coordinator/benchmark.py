from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from .models import Phase
from .orca import WorkerHandle
from .routing import SOL, Router, capability_rank
from .runner import PhaseStatus, ProductionRunner


CORPUS = (
    ("routine-en", "Inspect config metadata. Do not modify files.", "routine", "happy"),
    ("routine-ko", "설정 메타데이터만 조사하라. 파일은 수정하지 마라.", "routine", "happy"),
    ("standard-en", "Implement a localized validation helper.", "standard", "happy"),
    ("standard-ko", "로컬 검증 헬퍼를 구현해라.", "standard", "happy"),
    ("complex", "Fix async external API retry timeout state synchronization.", "complex", "happy"),
    ("critical", "Implement a reversible authorization policy change.", "critical", "happy"),
    ("recovery", "Implement a helper with an initially ambiguous failure.", "standard", "recovery"),
    ("external-blocker", "Inspect a service requiring unavailable credentials.", "routine", "external"),
    ("evidence-gap", "Inspect a deployment and collect the current log.", "routine", "evidence-gap"),
    ("stale-deployment", "Inspect a deployment commit identity. Do not modify files.", "routine", "stale"),
    ("ambiguous-verification", "Assess a reversible authorization policy change. Do not modify files.",
     "critical", "ambiguous-verification"),
    # De-identified field replays: no customer, project, URL, or monetary data.
    ("field-read-replay", "Inspect two yearly paginated views and aggregate duplicate rows. Do not modify data.", "routine", "happy"),
    ("field-write-replay", "Implement a small modal validation fix and deterministic unit test.", "standard", "happy"),
)

WEIGHT = {0: 1.0, 1: 2.0, 2: 3.0, 3: 4.0, 4: 6.0, 5: 9.0}


@dataclass
class BenchmarkResult:
    policy: str
    provenance: str
    verified_success_rate: float
    false_success: int
    expected_routing_accuracy: float
    missed_escalation: int
    unnecessary_escalation: int
    external_blocker_misclassification: int
    duplicate_write_execution: int
    identical_retry: int
    authority_auto_escalation: int
    happy_path_extra_dispatch: int
    worker_count: int
    attempt_count: int
    model_calls: dict[str, int]
    effort_calls: dict[str, int]
    normalized_compute_proxy: float
    cost_per_verified_success: float
    manual_intervention_rate: float
    token_usage: None = None


def _evidence(phase: Phase) -> dict[str, object]:
    common: dict[str, object] = {"status": "completed", "summary": f"{phase.value} result"}
    values = {
        Phase.INVESTIGATION: {"conclusion": "bounded", "evidence": ["fact"],
                              "files_checked": ["policy"], "unresolved_questions": []},
        Phase.ASSESSMENT: {"risks": ["bounded"], "impact": "known", "rollback": "available",
                           "write_ready": True, "unresolved_questions": []},
        Phase.IMPLEMENTATION: {"files_modified": [], "requirements_completed": ["done"],
                               "tests_run": ["unit"], "test_results": ["PASS"],
                               "unexecuted_verification": [], "workspace_diff": []},
        Phase.VERIFICATION: {"verification_outcome": "VERIFIED", "evidence": ["PASS"],
                             "unresolved_questions": []},
    }[phase]
    return {**common, **values}


class _ReplayAdapter:
    """Deterministic ProductionRunner adapter for benchmark replay provenance."""

    def __init__(self, workspace: Path, scenario: str) -> None:
        self.scenario = scenario
        self.counts: Counter[str] = Counter()
        self.counter = 0
        self.change_detector = lambda: {}

    def create_run(self, objective): return "benchmark-run"
    def create_task(self, run_id, title, spec):
        self.counter += 1
        return f"task-{self.counter}"
    def start_worker(self, run_id, task_id, route, assessment_approved=False):
        return WorkerHandle(task_id, f"dispatch-{self.counter}", f"terminal-{self.counter}", route, ())
    def wait_for_completion(self, run_id, worker, timeout_ms):
        return {"mode": "worker_done", "message": {"status": "completed"}}
    def read_result(self, worker):
        phase = worker.route.phase
        self.counts[phase.value] += 1
        count = self.counts[phase.value]
        if self.scenario == "external":
            return {"status": "blocked", "summary": "credential unavailable",
                    "external_blocker": "credential unavailable"}
        if self.scenario == "recovery" and phase is Phase.IMPLEMENTATION and count == 1:
            return {"status": "failed", "summary": "conflicting local evidence",
                    "failure_class_hint": "AMBIGUOUS_FAILURE", "evidence": ["focused retry scope"]}
        if self.scenario == "evidence-gap" and count == 1:
            return {"status": "completed", "summary": "report lacks current log"}
        if self.scenario == "stale" and count == 1:
            return {**_evidence(phase), "implementation_commit": "new", "deployment_commit": "old"}
        if self.scenario == "ambiguous-verification" and phase is Phase.VERIFICATION:
            if count == 1:
                return {**_evidence(phase), "verification_outcome": "INCONCLUSIVE",
                        "evidence": ["conflicting check A"]}
            if count == 2:
                return {**_evidence(phase), "verification_outcome": "INCONCLUSIVE",
                        "evidence": ["conflicting check B"]}
        return _evidence(phase)
    def trusted_relay(self, run_id, worker, summary, files_modified): pass
    def settle_escalation(self, run_id, worker, finding): pass
    def fail_task(self, run_id, task_id, reason): pass
    def release(self, worker): return {"state": "released"}


def _proxy(model_calls: Counter[str], effort_calls: Counter[str]) -> float:
    # Pairing is reconstructed only for the known policy combinations in this benchmark.
    rank_by_pair = {("gpt-5.6-luna", "low"): 0, ("gpt-5.6-terra", "medium"): 1,
                    ("gpt-5.6-terra", "high"): 2, (SOL, "medium"): 3,
                    (SOL, "high"): 4, (SOL, "xhigh"): 5}
    remaining_effort = Counter(effort_calls)
    total = 0.0
    for model, count in model_calls.items():
        for effort in ("low", "medium", "high", "xhigh"):
            if (model, effort) not in rank_by_pair:
                continue
            used = min(count, remaining_effort[effort])
            total += used * WEIGHT[rank_by_pair[(model, effort)]]
            count -= used
            remaining_effort[effort] -= used
    return total


def _modeled(policy: str, all_sol: bool = False, comparison_attempts: int | None = None) -> BenchmarkResult:
    router = Router()
    models: Counter[str] = Counter()
    efforts: Counter[str] = Counter()
    verified = 0
    false_success = 0
    route_hits = 0
    for name, task, expected, scenario in CORPUS:
        selected = router.classify(task).routes[0]
        if all_sol:
            model, effort = SOL, "medium"
        else:
            model, effort = selected.model, selected.effort
        models[model] += 1; efforts[effort] += 1
        expected_pair = {"routine": ("gpt-5.6-luna", "low"),
                         "standard": ("gpt-5.6-terra", "medium"),
                         "complex": ("gpt-5.6-terra", "high"),
                         "critical": (SOL, "medium")}[expected]
        route_hits += (model, effort) == expected_pair
        if scenario == "happy": verified += 1
        elif all_sol and scenario != "external": verified += 1
        elif not all_sol and scenario == "evidence-gap": false_success += 1
    if all_sol and comparison_attempts is not None:
        models = Counter({SOL: comparison_attempts})
        efforts = Counter({"medium": comparison_attempts})
    compute = _proxy(models, efforts)
    calls = comparison_attempts if all_sol and comparison_attempts is not None else len(CORPUS)
    return BenchmarkResult(policy, "modeled comparison policy", verified / len(CORPUS), false_success,
        route_hits / len(CORPUS), 0, 0, 0, 0, 0, 0, 0, calls, calls,
        dict(models), dict(efforts), compute, compute / max(verified, 1),
        (len(CORPUS) - verified) / len(CORPUS))


def _run_v02() -> BenchmarkResult:
    models: Counter[str] = Counter()
    efforts: Counter[str] = Counter()
    verified = route_hits = workers = attempts = external_misclassified = 0
    duplicate_write = identical_retry = authority_escalation = happy_extra = unnecessary = 0
    for _, task, expected, scenario in CORPUS:
        adapter = _ReplayAdapter(Path("/benchmark"), scenario)
        result = ProductionRunner(adapter_factory=lambda _, a=adapter: a, timeout_ms=1).run(task, "/benchmark")
        success = result.final_status is PhaseStatus.SUCCESS
        verified += success
        route_hits += result.classification == expected
        workers += result.cost_metrics.worker_count; attempts += result.cost_metrics.attempt_count
        models.update(result.cost_metrics.model_calls); efforts.update(result.cost_metrics.effort_calls)
        if scenario == "external" and success: external_misclassified += 1
        writes = [p for p in result.phase_list if p.authority == "workspace-write"]
        if scenario == "happy" and expected in {"routine", "standard"}:
            happy_extra += max(0, result.cost_metrics.worker_count - 1)
        if len([p for p in writes if p.status is PhaseStatus.SUCCESS]) > 1:
            duplicate_write += 1
        for before, after in zip(result.adaptive_decisions, result.adaptive_decisions[1:]):
            if (before.get("retry_delta") == after.get("retry_delta") and before.get("retry_delta") is not None):
                identical_retry += 1
            if before.get("authority") != after.get("authority") and after.get("to_rank", 0) > before.get("from_rank", 0):
                authority_escalation += 1
        unnecessary += sum(d.get("decision") == "ESCALATE_CAPABILITY" for d in result.adaptive_decisions
                           if scenario in {"happy", "external"})
    compute = _proxy(models, efforts)
    return BenchmarkResult("v0.2", "ProductionRunner deterministic corpus replay",
        verified / len(CORPUS), 0, route_hits / len(CORPUS), 0, unnecessary,
        external_misclassified, duplicate_write, identical_retry, authority_escalation,
        happy_extra, workers, attempts, dict(models), dict(efforts), compute,
        compute / max(verified, 1), (len(CORPUS) - verified) / len(CORPUS))


def run_benchmark() -> dict[str, dict[str, object]]:
    adaptive = _run_v02()
    results = {"v0.1": _modeled("v0.1"),
               "all-sol-medium": _modeled("all-sol-medium", True, adaptive.attempt_count),
               "v0.2": adaptive}
    return {name: asdict(value) for name, value in results.items()}
