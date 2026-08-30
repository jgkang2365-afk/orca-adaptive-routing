# Adaptive Model Routing Policy

Version: 0.2

## Purpose and boundaries

The Coordinator chooses task boundaries, ordering, capability, authority,
placement, retries, escalation, verification, settlement, and cleanup. Orca
provides lifecycle primitives; it is not an automatic scheduler. A worker never
spawns a stronger worker. Linux `/home` placement, Codex sandbox enforcement,
the Critical READ-before-WRITE gate, trusted lifecycle relay, and the ban on
automatic `danger-full-access` remain mandatory.

## Initial routing and task normalization

Initial routing stays lowest-sufficient:

| Level | Model / effort | Typical authority |
|---|---|---|
| Routine | Luna / low | READ_ONLY |
| Standard | Terra / medium | role-dependent |
| Complex | Terra / high | READ diagnosis, one WRITE Lead |
| Critical | Sol / medium | role-dependent, phase-separated |

Concrete destructive migration, meaningful data-loss or rollback risk,
high-impact security, very large architecture impact, or high ambiguity may
start at Sol/high. Initial routing never uses xhigh.

The Router normalizes a `TaskBrief` with objective, requested actions,
forbidden scope, READ_ONLY constraint, positive risk signals, and language.
Only requested actions and positive risk signals influence the route. English
and Korean signals cover READ, WRITE, Standard, Complex, Critical, and high-risk
work. Negative scope such as “DB는 변경하지 않는다” or “authentication is out
of scope” is removed before risk matching. Equivalent Korean and English tasks
must produce the same model, effort, authority, phase plan, risk floor, and
verifier policy.

## Capability ladder and risk floor

The single policy ladder is:

```text
rank 0 Luna/low
rank 1 Terra/medium
rank 2 Terra/high
rank 3 Sol/medium
rank 4 Sol/high
rank 5 Sol/xhigh
```

`capability_rank`, `capability_at`, and `next_capability` implement this order.
Rank is policy ordering, not an exact price model; call counts, effort counts,
workers, attempts, elapsed time, and available token usage are separate cost
metrics.

Capability failure advances exactly one rank for the failed logical Gate.
Capability never changes authority and the same Gate never downgrades. A newly
confirmed authentication, security, DB, or data-integrity risk may instead
apply the required risk floor immediately. “Hard” and “risky” are separate
decisions.

Sol/xhigh is allowed only after the same Gate ran at Sol/high, remains unresolved,
and additional reasoning is plausibly useful for high ambiguity, conflicting
evidence, high-impact security/data/architecture, production verification, or
an untrusted root cause. Permission, credentials, plan limits, outages, quota,
user action, unsupported features, Orca/placement errors, syntax failures,
missing evidence, and wrong deployment targets never justify xhigh. Default
budgets are one xhigh attempt per Gate and one per run. xhigh is the automatic
ceiling; Sol/max is outside v0.2.

## Lifecycle, logical Gates, and attempts

`LIFECYCLE_DONE != TASK_SUCCESS`. `worker_done` only closes the worker lifecycle.
Each plan phase is a logical Gate with its own attempt history. Every attempt
records its parent, phase, model, effort, rank, authority, classification,
decision, material retry delta, file changes, workspace/target fingerprints,
elapsed time, and invalidation evidence.

Escalation and retry rerun only the failed Gate. Successful assessment or WRITE
Gates are not replayed when verification needs more reasoning. A prior Gate is
reopened only when new objective evidence invalidates it, such as a verifier's
`TARGET_FAILED`; the invalidated Gate and evidence are recorded. Only one
mutation attempt may be active per Gate, and the previous worker must be
settled, stopped, released, or fenced before another WRITE attempt. Cleanup
failure forbids final SUCCESS.

## Worker result and success evidence

Results are normalized in this order: structured v2 fields, legacy Orca
envelopes, then bounded text. Strict JSON is not required, but a lifecycle event
or the word “completed” is not success evidence.

| Phase | Minimum success evidence |
|---|---|
| Investigation | conclusion, evidence, checked files/tools, unresolved questions |
| Assessment | risks, impact, rollback/recovery, WRITE readiness, uncertainty |
| Implementation | actual/reported changed files match, requirement results, tests and results, skipped verification, current diff |
| Verification | explicit `VERIFIED`; `NOT_VERIFIED`, `INCONCLUSIVE`, and `TARGET_FAILED` remain non-success |

Test failures or a mismatch between reported and actual changed files prevent
false SUCCESS. If tool results exist but the report is incomplete, repair or
collect evidence instead of repeating implementation or escalating capability.

Evidence freshness records Git head, dirty/change fingerprints, target or
deployment identity, URL when applicable, and verification time. Implementation
commit, deployment commit, deployment, and verification target must agree.
Stale or mismatched evidence is repaired before reasoning escalation.

## Failure classification and decisions

| Failure class | Default decision | Retry | Capability escalation | Terminal condition |
|---|---|---:|---:|---|
| INSUFFICIENT_SUCCESS_EVIDENCE | RESULT_REPAIR / COLLECT_EVIDENCE | bounded | no | repair budget exhausted |
| EVIDENCE_GAP | COLLECT_EVIDENCE | bounded | no | evidence unavailable |
| STALE_EVIDENCE | COLLECT_EVIDENCE | bounded | no | current target unavailable |
| ENVIRONMENT_MISMATCH | COLLECT_EVIDENCE | bounded | no | correct environment unavailable |
| TARGET_IDENTITY_MISMATCH | COLLECT_EVIDENCE | bounded | no | target cannot be reconciled |
| TRANSIENT_FAILURE | RETRY_SAME_CAPABILITY | once | not initially | retry budget exhausted |
| RECOVERABLE_IMPLEMENTATION_FAILURE | RETRY_SAME_CAPABILITY | once | not initially | retry/recovery exhausted |
| CAPABILITY_FAILURE | ESCALATE_CAPABILITY | no | exact +1 | ceiling/budget |
| AMBIGUOUS_FAILURE | focused retry | once | on repetition, +1 | ceiling/budget |
| DECOMPOSITION_FAILURE | REPLAN / READ diagnosis | bounded | not initially | no viable plan |
| MISSING_CONTEXT | acquire context / BLOCKED | no | no | required context unavailable |
| EXTERNAL_BLOCKER | BLOCKED | no | no | external condition remains |
| USER_ACTION_REQUIRED | BLOCKED | no | no | user action required |
| ORCHESTRATION_FAILURE | recover / TERMINAL | no | no | runtime cannot recover safely |
| TERMINAL_FAILURE | FAILED / BLOCKED | no | no | immediately terminal |

Classification trusts system/tool results, structured Orca status, concrete
codes, and workspace/target evidence before worker hints or free text. A worker's
capability hint is not authoritative. Low-confidence text remains ambiguous.
Classification and `AdaptiveDecision` are separate types; public phase status is
not overloaded with internal control decisions. No extra classifier worker is
used on the happy path.

Questions are first-class lifecycle events. Requests for user input, approval,
credentials, access, or policy decisions become `USER_ACTION_REQUIRED` and
BLOCKED. Coordinator-known facts may answer a question without user interaction.
A question never disappears as a timeout or triggers capability escalation.

## Retry, WRITE recovery, and side effects

Same-level retry is normally limited to one per rank for transient, recoverable,
or first ambiguous failures. Every retry records a material delta: new evidence,
corrected input or target, narrower task, restored runtime, clarified criteria,
or changed implementation state. Identical prompt, evidence, environment,
strategy, and failure signature is forbidden. Two consecutive attempts without
new evidence trip the no-progress circuit breaker. Gate/run attempt caps ensure
all runs terminate.

A failed WRITE may have changed files. The Coordinator captures actual diff and
passes it in a bounded evidence packet; it never automatically reverts. Except
for clearly recoverable local mistakes or safe transient failure, a READ_ONLY
diagnostic Gate determines root cause and scope before WRITE is reopened.
Capability and authority remain independent during diagnosis and escalation.

Deployment triggers, external write APIs, production mutation, billing, and data
changes are not automatically repeated unless idempotency, non-execution, or a
safe rollback/retry condition is objectively established. Otherwise use a
READ_ONLY assessment or BLOCKED.

## Verification policy

Verification modes are `DETERMINISTIC_ONLY`, `MODEL_REVIEW`, and `HYBRID`.
Deterministic checks (tests, type/lint/schema checks, Git diff, API status,
deployment commit and response) run first. Low-risk local work with sufficient
deterministic coverage does not receive a Sol verifier merely because
implementation succeeded.

Model review is used for semantic design, policy, UX, multi-module interaction,
or remaining regression risk. Critical DB/auth/security/data-loss and
non-idempotent operations use HYBRID. Fresh Verifier defaults to Sol/medium and
READ_ONLY, independent from the implementer. It receives requirements,
acceptance criteria, workspace state, tests, and objective facts—not an
implementer's unbounded reasoning transcript.

`INCONCLUSIVE` receives one focused verifier retry, then verifier-Gate capability
may rise. `TARGET_FAILED` reopens the Implementation Gate, repairs the confirmed
defect, and performs fresh verification. Escalating only the verifier after a
confirmed implementation defect is incorrect.

## Bounded evidence and observability

The evidence packet carries the logical Gate, up to three prior attempt
summaries, verified facts, attempted actions, failure and reason, unresolved
questions, changed files, test results, target fingerprint, evidence references,
and escalation reason. It excludes raw transcripts, secrets, tokens, and
credentials. Packet and phase-spec sizes stay bounded as attempts grow.

Machine-readable schema v2 retains v1 fields (`final_status`, `phase_list`,
`models`, `routing_plan`, `cleanup_result`) and adds `result_schema_version`,
`logical_gates`, `attempt_history`, `adaptive_decisions`, and `cost_metrics`.
Decision traces include Gate/attempt IDs, rank/capability, authority, failure,
confidence, decision/reason, retry delta, new-evidence flag, changed files,
fingerprints, verification mode, terminal/blocker cause, and elapsed time.

## Cost and quality benchmark

The representative corpus covers Routine, Standard, Complex, Critical, failure
recovery, external blockers, ambiguous verification, evidence gaps, stale
deployment, Korean/English tasks, and de-identified field replays. It compares
v0.1, an all-Sol/medium comparison policy, and v0.2.

Lowest initial model alone is not proof of savings. Routine/Standard happy paths
must preserve v0.1 route, worker count, and LLM Dispatch count. Failure recovery
is judged by verified success, false-success reduction, manual intervention, and
cost per verified success—not cheap early failure. v0.2 must have zero false
SUCCESS, duplicate successful WRITE execution, external-blocker escalation,
initial xhigh, identical retry, happy-path extra Dispatch, and authority
escalation. Mixed-workload verified quality must not fall below v0.1 and its
normalized compute proxy must remain below all-Sol/medium. Token metrics are
reported when available; deterministic model/effort/attempt proxies are used
otherwise.
