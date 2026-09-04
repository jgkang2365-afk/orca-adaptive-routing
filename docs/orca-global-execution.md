# Orca Global Execution Policy

Version: 1.0
Date: 2026-09-04

This policy applies to all Orca Coordinator development work unless an explicit
user instruction or a stricter safety rule overrides it. It complements the
adaptive model-routing policy; it does not weaken sandbox, authorization,
production, or destructive-operation safeguards.

## 1. Throughput and parallel execution

The Coordinator should build a dependency graph before dispatching work and
parallelize independent work by default when the coordination overhead is lower
than the expected savings.

- One worktree normally has one WRITE Lead at a time.
- Multiple independent READ_ONLY investigation, review, and verification workers
  may run in parallel.
- After a coherent implementation patch exists, independent verification lanes
  should run concurrently where practical, for example focused tests,
  type/lint checks, policy/diff inspection, documentation/SHA verification, and
  isolated browser verification.
- Do not serialize independent read or verification work merely because one
  Coordinator can perform it sequentially.
- Do not parallelize overlapping file edits, dependent implementation steps,
  database writes against the same mutable fixture or namespace, migration
  application, or other operations whose side effects can race.
- Create additional worktrees only when isolation or filesystem-conflict risk
  justifies them. Parallel READ_ONLY verification does not by itself require a
  new worktree.

A typical development flow is:

1. implementation or focused diagnosis,
2. focused checks for the changed behavior,
3. parallel independent verification,
4. staging or external-system verification when required,
5. one final full deterministic quality gate,
6. fresh verification when policy requires it,
7. cleanup and final report.

## 2. Test cadence and duplicate-work avoidance

During active implementation, prefer the smallest focused checks that can prove
or falsify the current change. Do not rerun a full regression, lint, typecheck,
and production build after every small edit.

- Run focused tests after each meaningful implementation increment.
- Run broader affected-area tests at a milestone when appropriate.
- Run the required full regression, typecheck, lint, and build at the final
  quality gate, normally once per stable candidate.
- If the final gate finds a defect and code changes again, rerun the affected
  focused checks first, then run one fresh final full gate.
- Reuse fresh deterministic evidence when its target fingerprint still matches;
  do not repeat an unchanged expensive check merely to produce another copy of
  the same evidence.

This rule optimizes latency without reducing required final verification.

## 3. Permission and approval handling

For ordinary local development, prefer the Codex permission mode equivalent to
`Approve for me` when available. Never automatically select `Full Access`,
`danger-full-access`, administrator elevation, or an equivalent unrestricted
mode.

An explicit user-approved work order authorizes the ordinary non-production
operations clearly contained in that scope. Do not ask the user to approve each
command separately when the intended action was already approved at the task
level.

The Coordinator should handle routine safe approvals internally, where the
runtime permits, including:

- read-only workspace inspection;
- workspace edits by the assigned WRITE Lead within the approved task scope;
- local focused tests, typecheck, lint, build, and other deterministic checks;
- ordinary Git reads such as status, diff, log, show, and rev-parse;
- read-only GitHub or remote status inspection;
- branch-scoped push and PR metadata/body updates when the approved task already
  requires them and protected/default branches are not being merged or rewritten;
- isolated non-production staging fixture creation, verification, rollback, and
  cleanup when the approved work order explicitly includes that staging work.

When a tool requires escalation metadata for network, staging, or GitHub access,
the Coordinator should supply the escalation reason as internal execution
metadata rather than phrasing it as a new question to the user.

If the runtime offers a persistent approval for a safe command prefix, reuse a
narrow prefix only when it cannot unintentionally authorize destructive or
out-of-scope operations. Never persist a broad prefix such as unrestricted shell,
filesystem deletion, force-push, arbitrary SQL, or deployment commands merely to
avoid prompts.

If the platform still renders an approval prompt for a routine safe action, the
Coordinator should, where technically possible, select the ordinary proceed or
narrow no-repeat option itself. If the UI requires direct user interaction,
bundle related safe commands and minimize the number of prompts rather than
asking repeatedly.

## 4. Operations that still require explicit user authorization

Unless the user has explicitly authorized the exact bounded operation, stop and
ask before:

- merging into the default or a protected branch;
- Production deployment, promotion, or traffic cutover;
- Production database/data writes, migrations, destructive repair, or backfill;
- destructive filesystem actions with meaningful loss risk, especially outside
  the assigned workspace;
- force-push, destructive reset, branch/tag deletion, or history rewrite with
  meaningful loss risk;
- secret, credential, authentication, or authorization changes;
- administrator/sudo elevation, sandbox weakening, `Full Access`, or equivalent
  unrestricted authority;
- billing, paid-resource creation, or other material cost commitments;
- irreversible or externally visible side effects outside the approved task
  scope.

A user approval for one such operation is not blanket approval for unrelated
high-risk actions. Conversely, once a bounded high-risk operation has been
explicitly approved, do not repeatedly ask for the same approval while executing
that exact approved plan unless the target, scope, or risk materially changes.

## 5. User-screen non-interference

Automated web UI verification must not interfere with the user's active screen,
mouse, keyboard, or window focus.

- Default browser verification uses a worktree-isolated headless agent-browser
  session.
- Snapshot, click, fill, screenshot, console, and network checks run inside that
  isolated session whenever technically possible.
- Do not use interactive browser automation that steals focus from the user's
  desktop merely because it is convenient.
- If a required verification cannot be performed without interacting with the
  user's visible desktop, report the limitation instead of silently disrupting
  the user's work.

## 6. Coordinator reporting

The final execution report should distinguish:

- work that ran in parallel versus work that was necessarily serialized;
- routine approvals handled automatically;
- any user-gated action that remained intentionally blocked;
- final deterministic checks and their fresh target identity;
- cleanup status.

Speed is not a reason to weaken safety gates, but safety is not a reason to
serialize independent work or repeatedly request approval for already-authorized
routine actions.
