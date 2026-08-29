# Adaptive Model Routing Policy

Version: 0.1

## 1. Purpose

This policy defines how an Orca Coordinator dynamically assigns Codex
workers based on task complexity, risk, dependencies, permissions,
and expected reasoning requirements.

The goal is not to maximize model capability.

The goal is to use the least expensive sufficient worker while
preserving correctness, operational safety, and independent verification
where justified.

---

## 2. Responsibility Boundary

### Coordinator

The Coordinator decides:

- task boundaries
- dependencies
- concurrency
- worker role
- model
- reasoning effort
- filesystem authority
- worktree placement
- retry versus escalation
- Fresh Verifier requirement

### Orca

Orca provides:

- Runs
- Tasks
- Dispatches
- supervised workers
- messaging
- worker lifecycle
- decision gates
- worktrees
- model/effort launch parameters

Orca must not be assumed to infer task conflicts or schedule workers
on behalf of the Coordinator.

### Worker

A worker:

- performs only its assigned task
- stays within its assigned authority
- reports blockers or uncertainty
- does not independently expand scope
- does not autonomously escalate to a higher model
- sends worker_done exactly once for a supervised Dispatch

---

## 3. Routing Levels

### Level 1 — Routine

Model:
`gpt-5.6-luna`

Effort:
`low`

Typical work:

- file discovery
- search
- inventory
- simple comparisons
- repetitive inspection
- test-result collection
- metadata extraction

Default authority:
READ-ONLY

---

### Level 2 — Standard

Model:
`gpt-5.6-terra`

Effort:
`medium`

Typical work:

- ordinary implementation
- code review
- moderate debugging
- isolated feature changes
- refactoring with clear boundaries
- test creation
- implementation planning from established requirements

Authority:

- analysis/review: READ-ONLY
- implementation: workspace-write

---

### Level 3 — Complex

Model:
`gpt-5.6-terra`

Effort:
`high`

Typical work:

- async behavior
- external service integration
- multi-module bugs
- difficult regressions
- state-management complexity
- complicated data transformations
- substantial refactoring

Default authority:

- investigation: READ-ONLY
- Lead implementation: workspace-write

---

### Level 4 — Critical

Model:
`gpt-5.6-sol`

Effort:
`high`

Typical work:

- authentication
- authorization
- security-sensitive changes
- database schema/migrations
- destructive data operations
- architecture decisions
- high ambiguity
- high regression cost
- high-confidence root-cause analysis after failed lower-tier attempts

Authority is task-specific.

Prefer READ-ONLY for architectural assessment and verification.
Grant WRITE only when implementation ownership explicitly requires it.

---

## 4. Escalation Policy

Escalation is task-scoped.

Do not escalate the entire project because one task becomes difficult.

Preferred sequence:

1. Re-evaluate the failure.
2. Determine whether the issue is:
   - implementation mistake
   - missing context
   - insufficient reasoning depth
   - incorrect task decomposition
   - actual model capability limit
3. Increase effort when appropriate.
4. Increase model class only when justified.

Typical progression:

Luna / low
→ Terra / medium
→ Terra / high
→ Sol / high

Do not automatically advance one level after every error.

A syntax error, failing assertion, dependency install failure,
or ordinary implementation bug is not by itself a model-escalation event.

---

## 5. Mandatory Risk Floors

Certain task classes have minimum routing requirements.

### Database schema or migration

Minimum:
Terra / high

Use Sol / high when:

- migration is destructive
- rollback is difficult
- production data integrity is uncertain
- multiple schemas or services are affected

### Authentication / Authorization / Security

Default:
Sol / high

Independent verification:
recommended

### External integrations

Minimum:
Terra / high when behavior is stateful, asynchronous,
or failure handling is non-trivial.

### Destructive operations

Use:
Sol / high assessment before execution when meaningful data loss is possible.

---

## 6. Permission Policy

Permission is independent from model strength.

A Sol worker does not automatically receive WRITE authority.

### READ-ONLY

Use for:

- research
- inspection
- review
- impact analysis
- dependency analysis
- Fresh Verification

READ-ONLY should be technically enforced using the Codex sandbox
when available.

### WRITE

Use for:

- implementation
- tests requiring source modification
- approved configuration changes

Default:
workspace-write

Avoid unrestricted filesystem authority unless a concrete task
requires it.

---

## 7. Parallelism Policy

Parallelize only genuinely independent work.

Good candidates:

- separate investigation questions
- independent code-path inspection
- documentation versus implementation research
- independent verification
- separate test analysis

Poor candidates:

- tightly coupled sequential implementation
- multiple workers editing the same files
- tasks where worker B depends materially on worker A findings
- artificial fragmentation of a small change

Default same-worktree policy:

READ + READ:
allowed in parallel

READ + WRITE:
allowed when investigation will not interfere with implementation

WRITE + WRITE:
normally prohibited when files or behavior overlap

One Lead worker should own the main write path.

---

## 8. Worktree Policy

A fresh worker does not imply a fresh Git worktree.

Prefer the required existing/current worktree unless:

- isolation is explicitly required
- filesystem conflicts make sharing unsafe
- independent implementation branches are intentional
- the user explicitly requests a separate worktree

Do not create worktrees merely because several workers exist.

---

## 9. Fresh Verifier

Fresh Verifier must be independent from the implementation reasoning path.

Default:

Model:
gpt-5.6-sol

Effort:
high

Authority:
READ-ONLY

Verifier responsibilities:

- inspect final changes
- test assumptions
- search for regressions
- verify requirements
- identify unsafe side effects
- report findings without modifying files

Do not automatically use Fresh Verifier for every trivial change.

---

## 10. Retry Policy

Retry and escalation are different.

Retry when:

- transient runtime failure occurred
- worker process failed
- setup failed
- task input was malformed
- implementation error is clearly recoverable at the same capability level

Escalate when:

- reasoning is inadequate
- complexity was misclassified
- critical risk was discovered
- repeated failures indicate capability mismatch

Respect Orca's task/dispatch failure handling and circuit breaker.
Do not construct uncontrolled retry loops.

---

## 11. Worker Completion

A supervised worker must end by reporting worker_done through Orca.

Coordinator then:

- accepts and synthesizes the result
- resolves follow-up ownership
- reuses, retains, or releases the worker
- dispatches dependent tasks only when their dependencies are satisfied

A completed worker must not silently continue into unrelated work.

---

## 12. Initial Routing Decision

Before dispatching, the Coordinator should answer:

1. Is the task READ or WRITE?
2. Is it independent?
3. Does it touch DB, auth, security, permissions, or destructive data?
4. Does it involve async or external integration?
5. Is the required change localized or architectural?
6. What is the lowest capable model?
7. What reasoning effort is sufficient?
8. What would trigger escalation?
9. Does independent verification materially reduce risk?

Only then should the worker be launched.

---

## 13. Guiding Rule

Use intelligence economically.

Do not route easy work to Sol merely because Sol is available.

Do not route high-risk work to Luna merely because Luna is cheaper.

Optimize total project cost and reliability, not the cost of one worker.
