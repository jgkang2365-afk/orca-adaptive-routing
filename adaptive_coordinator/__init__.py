"""Adaptive Coordinator v0.3.0 public API."""

from .models import (
    AdaptiveDecision, Authority, FailureClass, InteractionMode, Phase, Route,
    RoutingPlan, RunMetadata, RunRequest, SubtaskSpec, VerificationMode,
    VerificationOutcome,
)
from .routing import CAPABILITY_LADDER, Router, capability_at, capability_rank, next_capability
from .runner import PhaseResult, PhaseStatus, ProductionRunner, RunResult

__all__ = [
    "Authority",
    "AdaptiveDecision",
    "CAPABILITY_LADDER",
    "FailureClass",
    "InteractionMode",
    "Phase",
    "PhaseResult",
    "PhaseStatus",
    "ProductionRunner",
    "Route",
    "Router",
    "RoutingPlan",
    "RunMetadata",
    "RunRequest",
    "RunResult",
    "SubtaskSpec",
    "VerificationMode",
    "VerificationOutcome",
    "capability_at",
    "capability_rank",
    "next_capability",
]
