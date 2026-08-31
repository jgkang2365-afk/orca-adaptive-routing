# Orca Handoff

## Task

Adaptive Coordinator v0.2.1 — Orca/Codex runtime compatibility recovery and Pilot resumption

## Status

PASS

## Runtime Baseline

- Orca: `1.4.192`
- Codex: `0.150.1` (unchanged)
- Adaptive Coordinator: `0.2.1`
- Base GitHub main: `d2d1a661be7c22f2415b30807712510e73ed7a36`
- Initial Production snapshot: `0.2.0 / 5d15cf58b3d9b9107a0a3a57996916ba70394b8f`
- Initial Candidate snapshot: `0.2.1 / 0a0b60e1851fc41c09a7add4dcd9cb276d865bc1`

## Root Cause

- Orca's renderer-backed local terminal path did not publish an authoritative Codex identity when its launch-authority token contract was absent. The TUI could be idle while `agentIdentity` remained unset, so supervised `worker-start` correctly failed closed.
- A local CLI retry cannot safely reconcile a timed-out create because each RPC receives a different runtime client identity. Retrying locally created multiple terminals and was removed.
- The safe compatibility path is restricted to local interactive Codex commands owned by the exact `orca-adaptive:<phase>` title namespace. It uses Orca's direct PTY path and official `launchAgent: codex` metadata. Local create timeouts never retry; only paired remote clients may use bounded reconciliation with one mutation id.
- During trusted relay cleanup, Orca may close the exact worker tab as part of fencing. A subsequent close now treats only the exact structured `runtime_error: tab_not_found` result as idempotent already-absent; all other close/ownership errors remain fail-closed.
- No Codex downgrade or update was required.

## Changes

| Area | Change | Reason |
|---|---|---|
| `adaptive_coordinator/orca.py` | Adaptive terminal namespace and exact already-absent close handling | Bind the compatibility path to Coordinator-owned terminals and keep cleanup idempotent without guessing |
| `scripts/orca_runtime_compat.py` | Exact-hash, reversible Orca 1.4.192 CLI handler patch | Restore authoritative identity while rejecting unknown source, backup, marker-preserving mutations, and unsafe local retries |
| `adaptive_coordinator/runner.py` | Explicit Implementation result skeleton | Preserve strict success evidence while preventing omission of required fields |
| `tests/` | Runtime, identity, reconciliation, mutation, settlement, and contract regressions | Lock the live failure modes and fail-closed boundaries |
| `docs/orca-runtime-compatibility.md` | Installation and rollback contract | Document the narrow runtime patch and operator safety rules |

## Runtime Verification

- Live V3 probe terminal: `term_c578e885421e662af0f21fb679fef4f3`
- Run / Task / Dispatch: `run_35949f1756d4` / `task_a41d4dcca872` / `ctx_9a3b7fa47b92`
- Exact update prompt option `2` was selected; automatic Codex update was not used.
- The same terminal reached TUI idle, published `agentIdentity=codex`, started the worker, delivered `worker_done`, settled, closed, and released.
- Exact failed-candidate terminals created during diagnosis were closed by handle. No list-order, newest-terminal, title-only, or worktree-only attachment was used.
- Installed Orca handler SHA-256: `38ad8a5fd41a3c45e3927a6ccf741aa22cf161c9671fa26cb6229cfc963adedd`.
- Trusted original SHA-256: `b6b08954c7c2c7dc1e36a90eeb8da390b31cf0e00c5229327d006aff57bb96b4`.

## Tests

- Unit/contract: 229/229 PASS.
- `compileall`: PASS.
- installer shell syntax: PASS.
- `git diff --check`: PASS.
- v0.2 benchmark zero-invariants: PASS.
- GitHub `quality-gate`: PASS for PR #28 and PR #29.
- Independent `gpt-5.6-sol / high / READ_ONLY` verification: PASS for the runtime patch, settlement follow-up, and live Candidate resources.

## Candidate Pilot

### READ_ONLY 3/3

| Run | Task | Dispatch | Route | Result |
|---|---|---|---|---|
| `run_3047936dd45a` | `task_22e210dff029` | `ctx_68631c9f9fb0` | Luna/low/READ_ONLY | SUCCESS, 1 worker, 1 attempt, released |
| `run_d3eb68f2d46b` | `task_10f685afd673` | `ctx_21c562c9f2aa` | Luna/low/READ_ONLY | SUCCESS, 1 worker, 1 attempt, released |
| `run_8adbc9145367` | `task_e57eb42ad1ce` | `ctx_9bfcdb8111cb` | Luna/low/READ_ONLY | SUCCESS, 1 worker, 1 attempt, released |

### workspace-write 3/3

| Run | Task | Dispatch | Route | Result |
|---|---|---|---|---|
| `run_63e71e3c5bd0` | `task_172f6764be94` | `ctx_e057d3d14a7e` | Terra/medium/workspace-write | SUCCESS, one requested file, released |
| `run_9dd135c490eb` | `task_571cf7fbbd03` | `ctx_9041608cc2c2` | Terra/medium/workspace-write | SUCCESS, one requested file, released |
| `run_c9313fe6eb74` | `task_b67510f3282b` | `ctx_83ef0739dd87` | Terra/medium/workspace-write | SUCCESS, one requested file, released |

The three disposable Pilot files were verified and removed. The dedicated Pilot worktree was clean and then removed. No retry, capability escalation, duplicate WRITE, outside-workspace write, or residual Pilot resource occurred.

The six successful Pilot Tasks used the documented Coordinator trusted relay because the sandboxed worker's direct WSL lifecycle delivery was unavailable. Orca's post-close worker metadata records operator-close, while each Task contains verified completion evidence and every terminal resource is released with zero residual resources. This is not reported as normal direct `worker_done` success.

## Production

- Runtime code merge: `48a904e8e8ac03a8f37bda9660639de66b7b5e2e` (PR #29; includes PR #28 runtime patch).
- Production version: `0.2.1`.
- Production Closure Pilot: `run_e3e4de591ede` / `task_3f759d1e47af` / `ctx_36c4d3e20083` — Luna/low/READ_ONLY, 1 worker, 1 attempt, SUCCESS, released.
- Exact Supabase title dry-run: Complex; Terra/high READ_ONLY Investigation, Terra/high workspace-write Implementation, conditional verifier; no initial Sol/high or xhigh.
- Final GitHub/local/installed equality is verified after publishing the commit containing this handoff.

## Rollback

- Adaptive launcher switched to the preserved `0.2.0 / 5d15cf58...` snapshot and reported the expected version/commit, then restored to `0.2.1`.
- Orca handler rolled back to trusted original SHA `b6b08954...`, reported `state=original`, then reinstalled V3 SHA `38ad8a5f...`, reported `state=patched`, retained the trusted backup, and passed Node syntax validation.
- No snapshot or previous binary was deleted.

## Safety

- No identity bypass, terminal guessing, sandbox weakening, `danger-full-access`, Codex auto-update, model escalation for runtime failure, force push, rebase, reset, history rewrite, or unrelated-project mutation occurred.
- READ_ONLY and workspace-write remained independent of model capability.
- Existing unrelated terminals, workers, repositories, and worktrees were preserved.

## Remaining Issues

None for this deployment. Orca's operator-close status on trusted-relay Dispatch records is an expected lifecycle artifact and must be interpreted together with the authoritative completed Task evidence and released resource state.

## Decision Required

None.

## Next Start Point

Use the Production `orca-adaptive run` entry point for the next real project task. Keep Orca at the verified 1.4.192 build unless the compatibility patch is reviewed against a new exact handler hash; after an Orca upgrade, do not reapply this patch by guesswork.
