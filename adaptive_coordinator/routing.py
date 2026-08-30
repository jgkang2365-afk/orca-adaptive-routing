from __future__ import annotations

import re
from dataclasses import replace

from .models import Authority, Phase, Route, RoutingPlan, TaskBrief

LUNA = "gpt-5.6-luna"
TERRA = "gpt-5.6-terra"
SOL = "gpt-5.6-sol"

# Policy ordering, deliberately separate from measured cost metrics.
CAPABILITY_LADDER: tuple[tuple[str, str], ...] = (
    (LUNA, "low"),
    (TERRA, "medium"),
    (TERRA, "high"),
    (SOL, "medium"),
    (SOL, "high"),
    (SOL, "xhigh"),
)


def capability_rank(route: Route | tuple[str, str]) -> int:
    pair = route if isinstance(route, tuple) else (route.model, route.effort)
    try:
        return CAPABILITY_LADDER.index(pair)
    except ValueError as exc:
        raise ValueError(f"unsupported adaptive capability: {pair!r}") from exc


def capability_at(rank: int) -> tuple[str, str]:
    if not 0 <= rank < len(CAPABILITY_LADDER):
        raise IndexError(f"capability rank outside automatic ladder: {rank}")
    return CAPABILITY_LADDER[rank]


def next_capability(route: Route) -> Route | None:
    rank = capability_rank(route)
    if rank + 1 >= len(CAPABILITY_LADDER):
        return None
    model, effort = capability_at(rank + 1)
    return replace(route, model=model, effort=effort)


def apply_risk_floor(route: Route, floor_rank: int) -> Route:
    model, effort = capability_at(max(capability_rank(route), floor_rank))
    return replace(route, model=model, effort=effort)


def _route(phase: Phase, role: str, model: str, effort: str, authority: Authority,
           *, requires_assessment: bool = False) -> Route:
    write = authority is Authority.WORKSPACE_WRITE
    return Route(phase, role, model, effort, authority, "REVIEW" if write else "SAFE",
                 False, requires_assessment)


class Router:
    """Deterministic bilingual policy router; capability never implies authority."""

    READ_ONLY = (
        "do not modify", "do not change", "read-only", "read only", "inspect only",
        "파일을 수정하지", "파일은 수정하지", "변경하지 말", "조사만", "읽기 전용",
        "조회만", "검토만",
    )
    WRITE = (
        "implement", "write ", "modify ", "fix ", "add ", "change ", "update ", "refactor", "create ",
        "구현", "수정", "추가", "변경", "고쳐", "리팩터", "작성",
    )
    STANDARD = ("code review", "review code", "unit test", "debug",
                "코드 리뷰", "단위 테스트", "디버깅")
    COMPLEX = (
        "external api", "external service", "external integration", "asynchronous", "async",
        "retry", "timeout", "concurrency", "state synchronization", "state sync",
        "multi-module", "multiple services", "외부 api", "외부 서비스", "외부 연동",
        "비동기", "재시도", "타임아웃", "동시성", "상태 동기화", "다중 모듈",
        "여러 서비스", "복합 회귀",
    )
    CRITICAL_SIGNALS = {
        "database migration": "database migration", "database schema": "database schema",
        "migration": "migration", "production data": "production data", "data integrity": "data integrity",
        "authentication": "authentication", "authorization": "authorization", "security": "security",
        "architecture": "architecture", "destructive": "destructive operation",
        "데이터베이스 마이그레이션": "database migration", "db 마이그레이션": "database migration",
        "스키마": "database schema", "운영 데이터": "production data", "데이터 무결성": "data integrity",
        "인증": "authentication", "권한": "authorization", "보안": "security",
        "아키텍처": "architecture", "파괴적": "destructive operation",
    }
    SOL_HIGH_RISKS = {
        "destructive migration": "destructive migration", "destroy production": "production data destruction",
        "data loss": "meaningful data-loss risk", "data-loss": "meaningful data-loss risk",
        "corrupt": "data corruption risk", "rollback is uncertain": "rollback uncertainty",
        "rollback uncertain": "rollback uncertainty", "rollback impossible": "rollback impossible",
        "high-impact security": "high-impact security risk", "attack path": "security attack-path analysis",
        "security vulnerability": "security vulnerability analysis", "very large architecture": "very large architecture impact",
        "high ambiguity": "high ambiguity", "highly ambiguous": "high ambiguity",
        "파괴적 마이그레이션": "destructive migration", "데이터 손실": "meaningful data-loss risk",
        "데이터 파괴": "production data destruction", "롤백 불확실": "rollback uncertainty",
        "롤백 불가능": "rollback impossible", "공격 경로": "security attack-path analysis",
        "보안 취약점": "security vulnerability analysis", "고도의 모호성": "high ambiguity",
        "매우 큰 아키텍처": "very large architecture impact",
    }
    FORBIDDEN_PATTERNS = (
        r"[^.\n;]*(?:is|are)\s+(?:out of scope|not in scope)[^.\n;]*",
        r"(?:do not|don't|does not|doesn't|exclude|out of scope|not in scope)[^.\n;]*?(?=\bbut\b|[.\n;]|$)",
        r"(?:no|without)\s+(?:destructive migration|data loss(?: risk)?|attack path(?: risk)?|database changes?|authentication changes?|external services?)[^.\n;,]*",
        r"[^.\n;,]*?(?:수정하지|변경하지|제외|범위가 아니다|범위 밖|하지 말)[^.\n;,]*?(?=\s*(?:지만|말고)|[.\n;,]|$)",
    )

    def normalize(self, task: str) -> TaskBrief:
        normalized = " ".join(task.lower().split())
        forbidden: list[str] = []
        positive = normalized
        for pattern in self.FORBIDDEN_PATTERNS:
            matches = re.findall(pattern, positive, flags=re.IGNORECASE)
            forbidden.extend(match.strip() for match in matches if match.strip())
            positive = re.sub(pattern, " ", positive, flags=re.IGNORECASE)
        positive = " ".join(positive.split())
        positive_write = any(signal in positive for signal in self.WRITE)
        read_only = any(signal in normalized for signal in self.READ_ONLY) and not positive_write
        requested = ["WRITE" if positive_write and not read_only else "READ_ONLY"]
        if any(signal in positive for signal in self.COMPLEX):
            requested.append("COMPLEX")
        if any(signal in positive for signal in self.STANDARD):
            requested.append("STANDARD")
        risks = tuple(dict.fromkeys(label for signal, label in self.CRITICAL_SIGNALS.items() if signal in positive))
        high = tuple(dict.fromkeys(reason for signal, reason in self.SOL_HIGH_RISKS.items() if signal in positive))
        if high:
            requested.append("SOL_HIGH_RISK")
        return TaskBrief(task.strip(), tuple(requested), tuple(forbidden), read_only,
                         tuple(dict.fromkeys((*risks, *high))),
                         "ko" if re.search(r"[가-힣]", task) else "en")

    def classify(self, task: str) -> RoutingPlan:
        brief = self.normalize(task)
        write_requested = "WRITE" in brief.requested_actions
        high_reasons = tuple(r for r in brief.positive_risk_signals if r in self.SOL_HIGH_RISKS.values())
        critical = any(r in self.CRITICAL_SIGNALS.values() for r in brief.positive_risk_signals)
        complex_task = "COMPLEX" in brief.requested_actions
        if critical:
            effort = "high" if high_reasons else "medium"
            routes = [_route(Phase.ASSESSMENT, "Risk Assessor", SOL, effort, Authority.READ_ONLY)]
            if write_requested:
                routes.append(_route(Phase.IMPLEMENTATION, "Lead Implementer", SOL, effort,
                                     Authority.WORKSPACE_WRITE, requires_assessment=True))
            routes.append(_route(Phase.VERIFICATION, "Fresh Verifier", SOL, effort, Authority.READ_ONLY))
            return RoutingPlan("critical",
                f"Sol/high risk floor: {', '.join(high_reasons)}." if high_reasons else "Critical risk floor with Sol/medium default.",
                tuple(routes), verifier="required",
                escalation_triggers=("new permission boundary", "rollback uncertainty"))
        if complex_task:
            routes = [_route(Phase.INVESTIGATION, "Investigator", TERRA, "high", Authority.READ_ONLY)]
            if write_requested:
                routes.append(_route(Phase.IMPLEMENTATION, "Lead Implementer", TERRA, "high", Authority.WORKSPACE_WRITE))
            return RoutingPlan("complex", "Async, external, or multi-module complexity.", tuple(routes),
                               "independent investigation allowed", "conditional",
                               ("authorization discovered", "data-integrity risk discovered"))
        if write_requested:
            return RoutingPlan("standard", "Localized ordinary implementation.",
                (_route(Phase.IMPLEMENTATION, "Lead Implementer", TERRA, "medium", Authority.WORKSPACE_WRITE),),
                verifier="no", escalation_triggers=("hidden architecture", "repeated reasoning failure"))
        if "STANDARD" in brief.requested_actions:
            return RoutingPlan("standard", "General review or debugging analysis.",
                (_route(Phase.INVESTIGATION, "Reviewer", TERRA, "medium", Authority.READ_ONLY),), verifier="no")
        return RoutingPlan("routine", "Read-only discovery or inspection task.",
            (_route(Phase.INVESTIGATION, "Investigator", LUNA, "low", Authority.READ_ONLY),),
            verifier="no", escalation_triggers=("write becomes necessary", "risk scope expands"))

    def reclassify(self, original_task: str, new_findings: str) -> RoutingPlan:
        return self.classify(f"{original_task}\nConfirmed findings: {new_findings}")
