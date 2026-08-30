# Orca Adaptive Routing

This repository defines the adaptive routing policy used by an Orca
Coordinator to select Codex workers, models, reasoning effort, permissions,
placement, concurrency, and escalation behavior.

## Core Principle

The Coordinator owns orchestration decisions.

Orca provides task, dispatch, worker, message, lifecycle, and worktree
primitives. The Coordinator must determine:

- task decomposition
- dependency ordering
- parallelism
- worker role
- Codex model
- reasoning effort
- READ/WRITE authority
- worktree placement
- escalation conditions
- verifier requirements

Do not treat Orca itself as an automatic scheduler or conflict resolver.

## Default Model Routing

Use the lowest-cost model reasonably capable of completing the task.

Default routing:

- Simple search, inventory, repetitive inspection:
  gpt-5.6-luna / low

- General analysis, code review, ordinary implementation:
  gpt-5.6-terra / medium

- Complex implementation, async behavior, external integration,
  difficult debugging:
  gpt-5.6-terra / high

- Security, authorization, architecture, or database migration:
  gpt-5.6-sol / medium by default

- Destructive migration, meaningful data-loss or rollback risk, high-impact
  security, very large architecture impact, or high ambiguity:
  gpt-5.6-sol / high with the concrete escalation reason recorded

- Independent fresh verification when justified:
  gpt-5.6-sol / medium / READ-ONLY by default

Use Sol / high only when a concrete condition such as destructive migration,
meaningful data-loss risk, rollback uncertainty, high-impact security analysis,
very large architecture impact, high ambiguity, or insufficient Sol / medium
confidence is present. Record the escalation reason in the routing decision.

Do not start with a higher model merely because it is available.

## Escalation

Escalate a task only when one or more of the following occurs:

- the assigned worker reports insufficient confidence
- task complexity materially exceeds the initial classification
- repeated failure indicates reasoning limitations rather than an ordinary bug
- hidden dependencies or architectural implications are discovered
- security, authorization, database integrity, or data-loss risk emerges

Prefer increasing reasoning effort before increasing model class when appropriate.

The normal ladder is Luna / low, Terra / medium, Terra / high, Sol / medium,
Sol / high, then Sol / xhigh. Sol / xhigh is an automatic ceiling used only
after an unresolved Sol / high logical Gate with a concrete reasoning need;
Sol / max is not an automatic fallback.

Workers must not autonomously spawn higher-tier workers.
They report an escalation condition to the Coordinator.
The Coordinator decides whether and how to re-dispatch.

## Parallelism

Parallelize independent investigation and verification work when useful.

Do not parallelize tasks merely to increase worker count.

For the same worktree:

- multiple READ-ONLY workers may inspect in parallel
- WRITE ownership should normally belong to one Lead worker
- avoid multiple workers editing overlapping files concurrently

Create separate worktrees only when there is a concrete isolation or
filesystem-conflict reason, or when explicitly required.

## Permissions

READ-ONLY workers must be technically constrained where possible,
not merely instructed by prompt.

Typical READ-ONLY roles:

- investigation
- dependency analysis
- code review
- impact analysis
- fresh verification

WRITE workers must receive only the minimum filesystem authority needed
for implementation.

The default implementation role uses workspace-write rather than
unrestricted filesystem access whenever practical.

## Worker Lifecycle

Every supervised worker must belong to an Orca Run, Task, and Dispatch.

A supervised worker must report `worker_done` exactly once using the
Orca orchestration lifecycle contract.

After an accepted `worker_done`, the Coordinator must either:

1. immediately reuse the same worker for a new Dispatch,
2. explicitly retain it for debugging when required, or
3. release it using Orca worker-release.

Do not leave completed supervised workers running without ownership.

## Verification

Use a Fresh Verifier only when independent verification provides
meaningful risk reduction.

Fresh verification is recommended for:

- database schema or migration changes
- authentication or authorization changes
- destructive or irreversible operations
- architectural changes
- complicated concurrency or async behavior
- changes with meaningful regression risk

Fresh Verifier is READ-ONLY unless a separate implementation task is
explicitly dispatched.

## Closed-Loop Invariants

`worker_done` settles lifecycle; it does not prove task success. The
Coordinator must validate phase-specific evidence before accepting SUCCESS.
Evidence gaps, stale or mismatched targets, external blockers, questions, and
orchestration failures do not justify capability escalation.

Capability escalation advances one rank for the failed logical Gate and never
raises filesystem authority. It must not replay a successful WRITE Gate.
Retries require a material delta and are bounded; identical prompt, evidence,
environment, and strategy must not be repeated. Prefer deterministic
verification, and invoke an independent READ-ONLY verifier only when remaining
risk makes model review useful or mandatory. Cleanup failure forbids final
SUCCESS.

## User Overrides

Explicit user instructions override automatic routing policy.

If a user specifies a model, effort, worker count, permission boundary,
or worktree constraint, follow it unless doing so would be unsafe or
technically impossible.

## Detailed Policy

See:

`docs/adaptive-model-routing.md`

## Mandatory Safety Gate

Critical WRITE tasks must not begin solely because they were routed to Sol.

Database migration, destructive data operations, authentication,
authorization, and security-sensitive work require a separate READ-ONLY
assessment before WRITE implementation unless the user explicitly provides
an already-approved and independently verified execution plan.

Model capability never implies filesystem authority.

## WSL Worker Runtime

Codex worker permission enforcement follows:

`docs/wsl-worker-runtime.md`

This runtime policy is mandatory for adaptive-routed Codex workers.
