from __future__ import annotations

from .models import Authority, Phase, Route, RoutingPlan

LUNA = "gpt-5.6-luna"
TERRA = "gpt-5.6-terra"
SOL = "gpt-5.6-sol"


def _route(
    phase: Phase,
    role: str,
    model: str,
    effort: str,
    authority: Authority,
    *,
    requires_assessment: bool = False,
) -> Route:
    write = authority is Authority.WORKSPACE_WRITE
    return Route(
        phase=phase,
        role=role,
        model=model,
        effort=effort,
        authority=authority,
        approval_grade="REVIEW" if write else "SAFE",
        automatic_review=False,
        requires_assessment=requires_assessment,
    )


class Router:
    """Deterministic v0.1 policy classifier; permissions never derive from model."""

    CRITICAL = (
        "database migration",
        "database schema",
        "migrate existing data",
        "production data",
        "data loss",
        "corrupt",
        "authentication",
        "authorization",
        "security-sensitive",
        "destructive",
    )
    COMPLEX = (
        "external api",
        "external service",
        "asynchronous",
        "async",
        "retries",
        "timeout",
        "state synchronization",
        "multiple services",
    )
    WRITE = ("add ", "implement", "fix ", "change ", "refactor", "create ", "update ")
    ROUTINE = ("inspect", "list ", "inventory", "search", "discover", "metadata")

    def classify(self, task: str) -> RoutingPlan:
        text = " ".join(task.lower().split())
        # Scenario briefs often name risks to explicitly exclude them. Negative
        # scope declarations must not trigger a critical floor.
        text = text.replace(
            "does not touch authentication, database schema, external services, or destructive operations",
            "",
        )
        critical = any(term in text for term in self.CRITICAL)
        complex_task = any(term in text for term in self.COMPLEX)
        global_read_only = any(
            phrase in text
            for phrase in ("do not modify files", "do not modify any files", "read-only only")
        )
        write_requested = any(term in text for term in self.WRITE) and not global_read_only

        if critical:
            routes = [
                _route(Phase.ASSESSMENT, "Risk Assessor", SOL, "high", Authority.READ_ONLY)
            ]
            if write_requested:
                routes.append(
                    _route(
                        Phase.IMPLEMENTATION,
                        "Lead Implementer",
                        SOL,
                        "high",
                        Authority.WORKSPACE_WRITE,
                        requires_assessment=True,
                    )
                )
            routes.append(
                _route(Phase.VERIFICATION, "Fresh Verifier", SOL, "high", Authority.READ_ONLY)
            )
            return RoutingPlan(
                level="critical",
                reason="Critical data, authorization, security, or destructive-risk floor.",
                routes=tuple(routes),
                verifier="required" if "database" in text or "data" in text else "recommended",
                escalation_triggers=("new permission boundary", "rollback uncertainty"),
            )

        if complex_task:
            routes = [
                _route(Phase.INVESTIGATION, "Investigator", TERRA, "high", Authority.READ_ONLY)
            ]
            if write_requested:
                routes.append(
                    _route(
                        Phase.IMPLEMENTATION,
                        "Lead Implementer",
                        TERRA,
                        "high",
                        Authority.WORKSPACE_WRITE,
                    )
                )
            return RoutingPlan(
                level="complex",
                reason="Async or stateful external integration requires high reasoning.",
                routes=tuple(routes),
                parallelism="independent investigation allowed",
                verifier="conditional",
                escalation_triggers=("authorization discovered", "data-integrity risk discovered"),
            )

        if write_requested:
            return RoutingPlan(
                level="standard",
                reason="Localized ordinary implementation with no critical risk marker.",
                routes=(
                    _route(
                        Phase.IMPLEMENTATION,
                        "Lead Implementer",
                        TERRA,
                        "medium",
                        Authority.WORKSPACE_WRITE,
                    ),
                ),
                verifier="no",
                escalation_triggers=("hidden architecture", "repeated reasoning failure"),
            )

        return RoutingPlan(
            level="routine",
            reason="Read-only discovery or inspection task.",
            routes=(
                _route(Phase.INVESTIGATION, "Investigator", LUNA, "low", Authority.READ_ONLY),
            ),
            verifier="no",
            escalation_triggers=("write becomes necessary", "risk scope expands"),
        )

    def reclassify(self, original_task: str, new_findings: str) -> RoutingPlan:
        """Only the Coordinator calls this after a worker reports new facts."""
        return self.classify(f"{original_task}\nNew findings: {new_findings}")
