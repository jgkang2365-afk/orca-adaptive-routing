# Orca Handoff

## Task

Adaptive Coordinator v0.3.0-1 — Default Orca Entry, No-Intervention Runs, and Parallel Fan-out

## Status

PARTIAL — implementation and static verification are complete, but Production acceptance is NOT COMPLETE. Work was stopped at a safe boundary for the day.

## Baseline

- Base `main` / GitHub / Production commit: `d4dfb5473f53efbd7c49d6997370b09645bcffd9`
- Working branch: `fix/v0.3-parent-coordinator-integration`
- Implementation commit: `c25dbb6be33c5c92a19381ed9f9735bc090e3d10`
- Package version on the branch: `0.3.0`
- Baseline tests: 229/229 PASS

## Completed

- Added typed Parent delegation and preapproval metadata.
- Added bounded task decomposition, up to three READ_ONLY fan-out workers, dependency join, bounded evidence handoff, one-Lead WRITE fencing, verifier separation, and Coordinator-owned telemetry.
- Added fail-closed handling for sibling launch, settlement, question, cleanup, escalation, risk-floor, and shared xhigh-budget paths.
- Added exact-snapshot installation of the common Adaptive Routing Skill into shared, Codex, and Orca-managed WSL Skill roots.
- Expanded the Skill so mutation, multi-file investigation, implementation, testing, and refactoring requests enter Adaptive Coordinator while simple explanatory conversation may remain with Parent.
- Candidate snapshot installed at commit `c25dbb6be33c5c92a19381ed9f9735bc090e3d10`; production launcher was not promoted.

## Verification

- Unit/contract tests: 267/267 PASS.
- `compileall`: PASS.
- installer shell syntax: PASS.
- `git diff --check`: PASS.
- v0.2 benchmark zero-invariants: PASS.
- Skill validation: PASS.
- Independent `gpt-5.6-sol / high / READ_ONLY` review: PASS for the implementation and adversarial state-machine cases; no P0/P1 finding remained.

## Actual Orca Parent E2E

- A visible Orca Parent Codex terminal was created through the official terminal API and reached `tui-idle`.
- The normal user-style mutation prompt automatically selected the installed `orca-adaptive-routing` Skill. Parent did not call `apply_patch` or directly mutate the repository.
- The first delegated CLI attempt used the existing Production `0.2.1` launcher because Codex prepended `~/.local/bin` ahead of the candidate PATH. That launcher rejected the new v0.3 metadata flags. Parent then retried using its supported form.
- The Coordinator invocation failed before worker creation with `UtilBindVsockAnyPort:309: socket failed 1`.
- A separate READ_ONLY probe confirmed that the Windows Orca CLI cannot be invoked from the sandboxed Parent either; it fails with the same WSL vsock error.
- The repository remained clean and the temporary E2E file was never created.
- The E2E Parent terminal created by this task was closed successfully. No E2E worker was created.

## Root Cause / Integration Boundary

The Skill-based Parent delegation decision works, but the tested READ_ONLY WSL Parent cannot invoke the current `orca-ide` bridge. The bridge launches Windows PowerShell/Orca through WSL interop, and that interop requires a vsock operation denied inside the Codex sandbox. No workspace-write or sandbox-weakening workaround was attempted.

The current Orca hook endpoint accepts lifecycle/telemetry events; no verified official contract was found that turns a Parent prompt into an out-of-sandbox Coordinator Run. Removing the sandbox, requesting an Allow escalation, attaching an unknown terminal, or letting Parent mutate directly would violate the acceptance criteria and was not attempted.

## Acceptance State

- General Parent selects the Adaptive Skill: PROVEN.
- Parent direct mutation count in E2E A: 0.
- Approval prompt count in E2E A: 0.
- Fail-closed behavior and clean workspace: PROVEN.
- Coordinator worker launch from sandboxed Parent: BLOCKED by WSL vsock boundary.
- Actual parallel overlap, E2E A completion, E2E B/C/D, PR, merge, Production installation, Production smoke, and rollback: NOT RUN because the first mandatory E2E Gate failed.

## Git / Deployment

- Development remains on `fix/v0.3-parent-coordinator-integration`; `main` was not modified.
- No PR, merge, GitHub main push, or Production promotion was performed.
- Existing Production `0.2.1` remains unchanged.
- Candidate `0.3.0` snapshot is preserved for continuation.

## Safety

- No sandbox weakening, `danger-full-access`, unknown-terminal attachment, force push, rebase, history rewrite, unrelated-project mutation, or direct Parent WRITE occurred.
- Existing unrelated Orca terminals, workers, Runs, repositories, and worktrees were not changed.
- Only the E2E Parent terminal created by this task was closed.

## Remaining Issues

1. Establish an official trusted Parent-to-Coordinator relay that executes outside the sandbox while preserving structured preapproval and exact workspace identity.
2. Ensure candidate E2E resolves the candidate executable explicitly instead of the older Production launcher.
3. After the relay works, restart at E2E A and then run B/C/D, independent runtime verification, PR/CI/merge, exact Production installation, Production E2E smoke, and rollback.

## Decision Required

None for today's close. Do not weaken the sandbox as a workaround. If Orca exposes no supported trusted delegation primitive, the task remains BLOCKED pending an Orca runtime capability rather than a repository-only fix.

## Next Start Point

Resume from the sandbox-to-Orca transport boundary. First determine whether Orca 1.4.197 exposes a supported out-of-sandbox prompt/delegation relay or host-side coordinator callback. If it does, integrate that exact contract and rerun E2E A from the beginning. If it does not, record the product-runtime blocker and do not proceed to Production promotion.
