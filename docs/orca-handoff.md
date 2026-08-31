# Orca Handoff

## Task

Adaptive Coordinator v0.2.1 stabilization, automated verification, and deployment

## Status

BLOCKED — source, CI, review, candidate installation, routing dry-run, and xhigh READ_ONLY smoke passed, but mandatory end-to-end pilots and Production promotion are blocked by the live Orca terminal/agent registration contract.

## A. Baseline

- Baseline main commit: `5d15cf58b3d9b9107a0a3a57996916ba70394b8f`.
- Production installed commit: `5d15cf58b3d9b9107a0a3a57996916ba70394b8f` (`0.2.0`).
- Initial work branch: `fix/v0.2.1-stabilization`.
- Working tree: clean.
- Baseline tests: 177/177 PASS.

## B. Changes

| Files | Purpose | Core change |
|---|---|---|
| `adaptive_coordinator/runner.py`, `models.py`, `routing.py` | v0.2.1 state-machine hardening | Strong verification evidence, structured unexecuted checks, transient no-escalation, full capability budget, xhigh READ_ONLY diagnosis, focused REPLAN, and risk-floor loop fencing |
| `adaptive_coordinator/orca.py` | Live Orca readiness | Bind Codex agent registration to the exact created terminal with bounded same-terminal polling and fail-closed cleanup |
| `adaptive_coordinator/benchmark.py`, `scripts/run-v02-benchmark.py` | Quality/cost guard | Compute real zero-invariants and detect forced false-success perturbations |
| `tests/` | Regression coverage | Target identity, false success, transient, Polling, budgets, xhigh, REPLAN, risk floor, readiness, cleanup, and CI contracts |
| `.github/workflows/quality.yml` | Automated CI | `Adaptive Coordinator Quality / quality-gate` for tests, compile, shell syntax, diff check, and benchmark |
| `README.md`, `AGENTS.md`, `docs/adaptive-model-routing.md`, `pyproject.toml` | Public contract | v0.2.1 policy/version and operating constraints |

## C. Stabilization Result

- Verification requires successful status, explicit `VERIFIED`, concrete evidence, an empty `unresolved_questions` list, and target identity consistency.
- Blocking or unstructured unexecuted verification cannot become success. Executable deterministic checks cannot be replaced by model review.
- Repeated transient failures terminate without capability escalation.
- The legal ladder remains reachable and bounded; automatic xhigh is READ_ONLY only.
- WRITE requiring xhigh reasoning inserts READ_ONLY diagnosis before reopening a Sol/high-or-lower WRITE Gate.
- REPLAN creates a narrower child investigation rather than replaying the same prompt.
- Repeated identical risk floors cannot recreate the same critical cycle.
- Matching fabricated commit aliases and worker-provided external-target flags cannot bypass the Coordinator-observed workspace HEAD.
- `worker_done` remains lifecycle evidence only, never sufficient task-success evidence.

## D. Routing Verification

Exact task:

`Supabase 사용량 초과 대응 및 상시 Polling 1차 최적화`

Candidate route result:

- Classification: Complex.
- Investigation: `gpt-5.6-terra / high / READ_ONLY`.
- Implementation: `gpt-5.6-terra / high / workspace-write`.
- Verifier: conditional.
- Initial Sol/high: no.
- Initial Sol/xhigh: no.

## E. Tests and Benchmark

- Final unit/contract suite: 217/217 PASS.
- `compileall`: PASS.
- Installer shell syntax: PASS.
- `git diff --check`: PASS.
- Benchmark: PASS; false success, duplicate successful WRITE, external/transient escalation, identical retry, authority auto-escalation, initial xhigh, xhigh WRITE, and repeated risk-floor loop are all zero.
- Failure injection contracts: repeated transient, question, permission/plan limitation, insufficient evidence, and cleanup failure all fail closed with no capability escalation.
- PR #25 and PR #26 each passed GitHub `quality-gate` before merge.

## F. Independent Verification

- Independent model: `gpt-5.6-sol / high / READ_ONLY`.
- Final code verdict: PASS.
- Adversarial checks covered fabricated Git identities, external-target flags, alias contradictions, repair carry-forward, blocking verification, transient handling, full ladder, xhigh READ_ONLY, REPLAN, risk-floor lineage, authority, worker fencing, and cleanup.
- Live readiness follow-up verified exact terminal-handle binding; mismatched or missing handles fail closed, do not dispatch, and close only the created terminal.

## G. xhigh Smoke

- One actual `gpt-5.6-sol / xhigh / READ_ONLY` Codex smoke was executed.
- Result: PASS; branch `main` and HEAD `0a0b60e1851fc41c09a7add4dcd9cb276d865bc1` were reported without modification.
- Usage: 30,802 input tokens, 25,088 cached input tokens, 124 output tokens.
- No WRITE authority, max fallback, or external mutation was used.

## H. GitHub

- Repository: `jgkang2365-afk/orca-adaptive-routing` (PRIVATE).
- PR #25 merge: `20bb5ade7ead75fe52473e37f242a2c6b4fdaa25`.
- PR #26 merge: `0a0b60e1851fc41c09a7add4dcd9cb276d865bc1`.
- Both PRs used normal merge commits after independent review and successful CI.
- Branch protection API returned `403`: Private-repository protection requires GitHub Pro or public visibility for this account. The repository was not made public and the restriction was not bypassed.

## I. Candidate Installation

- Candidate version: `0.2.1`.
- Candidate installed commit: `0a0b60e1851fc41c09a7add4dcd9cb276d865bc1`.
- Candidate path: `/home/user/.local/lib/orca-adaptive-routing-candidate/0a0b60e1851fc41c09a7add4dcd9cb276d865bc1`.
- Source main, GitHub main, and candidate snapshot were equal before the handoff-only commit.
- Production launcher was intentionally left at version `0.2.0`, commit `5d15cf58b3d9b9107a0a3a57996916ba70394b8f`.

## J. Candidate Pilot and Blocker

- Routing was correct on every attempted READ pilot: Luna/low/READ_ONLY, one logical Gate, no escalation.
- The Runner correctly returned `ORCHESTRATION_FAILURE`; it did not report false success or escalate model capability.
- Live failure forms:
  - `terminal create` can return `Timed out waiting for terminal handle after creation` even when the runtime later creates a terminal.
  - After the exact Codex update prompt is skipped, the TUI reaches idle but `terminal show` does not publish `agentIdentity`, so `worker-start` returns or is preempted by `agent_unconfigured`.
- Manual structured evidence confirmed `blockedReason=codex-update-prompt`, successful Skip option `2`, TUI idle, and missing `agentIdentity` after boot.
- The adapter cannot safely guess an unknown handle or treat an unbound terminal as a supervised agent.
- Candidate READ 3/3, WRITE 3/3, Production closure pilot, and Production promotion therefore were not performed.

## K. Lifecycle / Cleanup

- Failed Pilot Runs: `run_539de18355fa`, `run_6f5648cd8c91`, and `run_0bf644dce097`.
- Each run had zero supervised worker resources after termination.
- Handle-known candidate terminals were closed by the adapter.
- Two terminals created despite handle-less diagnostic errors and the final controlled diagnostic terminal were identified by before/after inventory and closed by exact handle.
- Final probe worker count: zero.
- The three terminals that existed before this task were preserved; unrelated project workers and terminals were not touched.

## L. Safety

- No Codex update, sandbox weakening, danger-full-access, model-to-authority escalation, force push, rebase, reset, history rewrite, public exposure, or other-project change occurred.
- Production was not promoted after a failed candidate Gate.
- The previous production exact snapshot and launcher remain available, so runtime rollback was not needed and no snapshot was deleted.
- Source and GitHub code are v0.2.1; the intentionally older Production launcher is explicitly reported rather than concealed.

## Remaining Issues

1. Orca 1.4.192 does not reliably return the created terminal handle and does not register the freshly launched Codex 0.150.1 TUI as `agentIdentity=codex` after the update prompt is skipped.
2. GitHub branch protection cannot be enabled for this Private repository under the current account plan.
3. Candidate READ/WRITE repetition, Production exact installation, closure pilot, and rollback switch test remain pending behind issue 1.

## Decision Required

Resolve the Orca/Codex agent-registration compatibility boundary without weakening sandboxing. Acceptable next work is an Orca runtime fix or an explicitly approved Codex runtime compatibility update followed by the withheld candidate pilots. Do not bypass `agentIdentity`, guess terminal handles, or promote Production from the current failed candidate evidence.

## Next Start Point

Reproduce the fresh-terminal `agentIdentity` failure against the supported Orca/Codex version pair, fix that runtime boundary, then resume at candidate READ_ONLY Pilot 1/3. Do not repeat v0.2.1 code stabilization, CI, benchmark, xhigh smoke, or exact Polling route validation.
