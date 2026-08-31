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
    ("repeated-transient", "Inspect metadata through a temporarily rate-limited service. Do not modify files.",
     "routine", "transient-repeat"),
    ("repeated-risk-floor", "Implement a localized helper while monitoring newly discovered risks.",
     "standard", "risk-repeat"),
    ("invalid-verified-evidence", "Assess an authorization rule without modifying files.",
     "critical", "invalid-verified"),
)

WEIGHT = {0: 1.0, 1: 2.0, 2: 3.0, 3: 4.0, 4: 6.0, 5: 9.0}

SCENARIO_EXPECTED_SUCCESS = {
    "happy": True,
    "recovery": True,
    "evidence-gap": True,
    "stale": True,
    "ambiguous-verification": True,
    "external": False,
    "transient-repeat": False,
    "risk-repeat": False,
    "invalid-verified": False,
}


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
    transient_capability_escalation: int
    initial_xhigh: int
    xhigh_write: int
    repeated_risk_floor_loop: int
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
        self.wait_counts: Counter[str] = Counter()
        self.change_detector = lambda: {}

    def create_run(self, objective): return "benchmark-run"
    def create_task(self, run_id, title, spec):
        self.counter += 1
        return f"task-{self.counter}"
    def start_worker(self, run_id, task_id, route, assessment_approved=False):
        return WorkerHandle(task_id, f"dispatch-{self.counter}", f"terminal-{self.counter}", route, ())
    def wait_for_completion(self, run_id, worker, timeout_ms):
        self.wait_counts[worker.route.phase.value] += 1
        if self.scenario == "risk-repeat" and worker.route.phase is Phase.IMPLEMENTATION:
            return {"mode": "escalation", "message": {
                "type": "escalation", "body": "authorization risk discovered",
                "dispatchId": worker.dispatch_id,
            }}
        return {"mode": "worker_done", "message": {"status": "completed"}}
    def read_result(self, worker):
        phase = worker.route.phase
        self.counts[phase.value] += 1
        count = self.counts[phase.value]
        if self.scenario == "external":
            return {"status": "blocked", "summary": "credential unavailable",
                    "external_blocker": "credential unavailable"}
        if self.scenario == "transient-repeat":
            return {"status": "failed", "summary": "rate limit temporarily exceeded",
                    "evidence": [f"runtime retry {count}"]}
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
        if self.scenario == "invalid-verified" and phase is Phase.VERIFICATION:
            return {"status": "completed", "summary": "verification completed",
                    "verification_outcome": "VERIFIED"}
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
        elif all_sol and scenario not in {"external", "transient-repeat", "risk-repeat", "invalid-verified"}: verified += 1
        elif not all_sol and scenario == "evidence-gap": false_success += 1
    if all_sol and comparison_attempts is not None:
        models = Counter({SOL: comparison_attempts})
        efforts = Counter({"medium": comparison_attempts})
    compute = _proxy(models, efforts)
    calls = comparison_attempts if all_sol and comparison_attempts is not None else len(CORPUS)
    return BenchmarkResult(policy, "modeled comparison policy", verified / len(CORPUS), false_success,
        route_hits / len(CORPUS), 0, 0, 0, 0, 0, 0, 0, calls, calls,
        dict(models), dict(efforts), compute, compute / max(verified, 1),
        (len(CORPUS) - verified) / len(CORPUS), 0, 0, 0, 0)


def _trace_invariant_counts(result, scenario: str) -> dict[str, int]:
    decisions = result.adaptive_decisions
    assessment_count = sum(item.phase == Phase.ASSESSMENT.value for item in result.phase_list)
    risk_terminal = any("identical risk floor already applied" in str(item.get("decision_reason", ""))
                        for item in decisions)
    return {
        "transient_capability_escalation": sum(
            item.get("failure_class") == "TRANSIENT_FAILURE"
            and item.get("decision") == "ESCALATE_CAPABILITY" for item in decisions),
        "initial_xhigh": sum(route.get("effort") == "xhigh"
                             for route in result.routing_plan.get("routes", [])),
        "xhigh_write": sum(item.effort == "xhigh" and item.authority == "workspace-write"
                           for item in result.phase_list),
        "repeated_risk_floor_loop": int(
            scenario == "risk-repeat" and (assessment_count > 1 or not risk_terminal)),
    }


def _false_success_count(result, scenario: str) -> int:
    expected_success = SCENARIO_EXPECTED_SUCCESS[scenario]
    if result.final_status is not PhaseStatus.SUCCESS:
        return 0
    invariant_breach = any(_trace_invariant_counts(result, scenario).values())
    cleanup_breach = any(
        not isinstance(item, dict) or item.get("state") not in {"released", "closed"}
        for item in result.cleanup_result)
    terminal_gate = any(gate.status in {"FAILED", "BLOCKED", "TERMINAL"}
                        for gate in result.logical_gates.values())
    return int(not expected_success or invariant_breach or cleanup_breach or terminal_gate)


def _run_v02() -> BenchmarkResult:
    models: Counter[str] = Counter()
    efforts: Counter[str] = Counter()
    verified = route_hits = workers = attempts = external_misclassified = false_success = 0
    duplicate_write = identical_retry = authority_escalation = happy_extra = unnecessary = 0
    trace_invariants: Counter[str] = Counter()
    for _, task, expected, scenario in CORPUS:
        adapter = _ReplayAdapter(Path("/benchmark"), scenario)
        result = ProductionRunner(adapter_factory=lambda _, a=adapter: a, timeout_ms=1).run(task, "/benchmark")
        success = result.final_status is PhaseStatus.SUCCESS
        verified += success and SCENARIO_EXPECTED_SUCCESS[scenario]
        route_hits += result.classification == expected
        workers += result.cost_metrics.worker_count; attempts += result.cost_metrics.attempt_count
        models.update(result.cost_metrics.model_calls); efforts.update(result.cost_metrics.effort_calls)
        trace_invariants.update(_trace_invariant_counts(result, scenario))
        false_success += _false_success_count(result, scenario)
        if scenario == "external" and success: external_misclassified += 1
        writes = [p for p in result.phase_list if p.authority == "workspace-write"]
        if scenario == "happy" and expected in {"routine", "standard"}:
            happy_extra += max(0, result.cost_metrics.worker_count - 1)
        if len([p for p in writes if p.status is PhaseStatus.SUCCESS]) > 1:
            duplicate_write += 1
        for before, after in zip(result.adaptive_decisions, result.adaptive_decisions[1:]):
            if (before.get("retry_delta") == after.get("retry_delta") and before.get("retry_delta") is not None):
                identical_retry += 1
            if (before.get("logical_gate_id") == after.get("logical_gate_id")
                    and before.get("authority") != after.get("authority")
                    and after.get("to_rank", 0) > before.get("from_rank", 0)):
                authority_escalation += 1
        unnecessary += sum(d.get("decision") == "ESCALATE_CAPABILITY" for d in result.adaptive_decisions
                           if scenario in {"happy", "external"})
    compute = _proxy(models, efforts)
    return BenchmarkResult("v0.2", "ProductionRunner deterministic corpus replay",
        verified / len(CORPUS), false_success, route_hits / len(CORPUS), 0, unnecessary,
        external_misclassified, duplicate_write, identical_retry, authority_escalation,
        happy_extra, workers, attempts, dict(models), dict(efforts), compute,
        compute / max(verified, 1), (len(CORPUS) - verified) / len(CORPUS),
        trace_invariants["transient_capability_escalation"],
        trace_invariants["initial_xhigh"], trace_invariants["xhigh_write"],
        trace_invariants["repeated_risk_floor_loop"])


def run_benchmark() -> dict[str, dict[str, object]]:
    adaptive = _run_v02()
    results = {"v0.1": _modeled("v0.1"),
               "all-sol-medium": _modeled("all-sol-medium", True, adaptive.attempt_count),
               "v0.2": adaptive}
    return {name: asdict(value) for name, value in results.items()}


def benchmark_violations(results: dict[str, dict[str, object]]) -> list[str]:
    adaptive = results["v0.2"]
    zero_invariants = (
        "false_success", "external_blocker_misclassification", "duplicate_write_execution",
        "identical_retry", "authority_auto_escalation", "happy_path_extra_dispatch",
        "transient_capability_escalation", "initial_xhigh", "xhigh_write",
        "repeated_risk_floor_loop",
    )
    violations = [name for name in zero_invariants if adaptive[name] != 0]
    if adaptive["verified_success_rate"] < results["v0.1"]["verified_success_rate"]:
        violations.append("verified_quality_regression")
    if adaptive["normalized_compute_proxy"] >= results["all-sol-medium"]["normalized_compute_proxy"]:
        violations.append("normalized_compute_regression")
    return violations
