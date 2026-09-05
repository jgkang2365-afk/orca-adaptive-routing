# Orca Handoff

## Task

Adaptive Coordinator serial retirement — first pass

## Status

PARTIAL RETIREMENT

The repository is no longer approved as a Production execution path. The exact
runtime rollback Gate could not be satisfied, so launcher removal was not
performed out of sequence.

## Retirement Date

2026-09-05 (Asia/Seoul)

## Source State

- Branch: `chore/retire-adaptive-coordinator`
- Baseline source/GitHub main: `749bbf6bba2059d2cb59fe20ca979d25aec70065`
- Preserved unmerged v0.3 branch: `fix/v0.3-parent-coordinator-integration`
- Single Adaptive repository worktree: `/home/user/projects/orca-adaptive-routing`

## Resource Cleanup

- The disposable `/tmp/measurement-agents-retirement` worktree created under the superseded first instruction was verified clean, equal to its `origin/main`, free of unique commits and running processes, then removed.
- Its temporary `docs/agents-work-order-orchestration` branch was deleted after confirming it pointed to `origin/main`.
- Dedicated live `orca-adaptive:*` terminals: `0`.
- Running `orca-adaptive` / `adaptive_coordinator` processes: `0`.
- Live Adaptive worker terminals: `0`.
- Two historical external-ownership worker resource records remain in Orca metadata. Both exact terminal handles return `terminal_handle_stale`; they are not live processes and were not mutated because they are externally owned.
- The single visible `orca-adaptive-routing` terminal is the user-approved Coordinator workspace for this retirement task, not an Adaptive worker.

## Runtime

- Current Orca version: `1.4.197`.
- Exact handler: `/mnt/c/Users/USER/AppData/Local/Programs/orca/resources/app.asar.unpacked/out/cli/handlers/terminal.js`.
- Current/before SHA-256: `980c4a931ffb0d99a65b8ac30875a6472acd8cd678544c92d5109723ae5e7d7f`.
- Known v0.2.1 patched SHA-256: `38ad8a5fd41a3c45e3927a6ccf741aa22cf161c9671fa26cb6229cfc963adedd`.
- Trusted Orca 1.4.192 original SHA-256: `b6b08954c7c2c7dc1e36a90eeb8da390b31cf0e00c5229327d006aff57bb96b4`.
- `orca_runtime_compat.py status`: `state=unknown`, `trustedBackup=false`.
- The current handler contains no Adaptive patch marker or `orca-adaptive:` compatibility branch.
- Rollback: **NOT EXECUTED / FAIL-CLOSED**. Orca is not the verified 1.4.192 build, the current SHA is unknown to the rollback tool, and no trusted backup exists beside the 1.4.197 handler. Restoring the 1.4.192 JavaScript into 1.4.197 would be an unsafe cross-version overwrite forbidden by policy.
- After SHA: unchanged at `980c4a931ffb0d99a65b8ac30875a6472acd8cd678544c92d5109723ae5e7d7f`.

## Production Installation

- Last Production version: `0.2.1`.
- Last installed commit: `d4dfb5473f53efbd7c49d6997370b09645bcffd9`.
- Launcher: `/home/user/.local/bin/orca-adaptive`.
- Launcher target: `/home/user/.local/lib/orca-adaptive-routing/d4dfb5473f53efbd7c49d6997370b09645bcffd9/orca-adaptive`.
- Launcher removal: **NO**. The instruction requires exact runtime rollback and smoke PASS before removal; the rollback Gate failed.
- Snapshots preserved: **YES** at `/home/user/.local/lib/orca-adaptive-routing/`.
- No snapshot or session history was deleted.

## General Environment

- WSL Codex resolution: `/home/user/.nvm/versions/node/v24.20.0/bin/codex` (Linux path precedes the Windows fallback).
- Codex version: `codex-cli 0.150.1`.
- Codex smoke: PASS (`codex --version`; the read-only environment emitted only the expected inability to create PATH aliases).
- Orca version query: PASS (`1.4.197`).
- General Orca shell terminal smoke: PASS. A non-Adaptive terminal printed `ORCA_RETIREMENT_SMOKE_OK` and was closed by exact handle.

## Repository State

- Current status: retirement requested; not approved for Production use.
- README prominently marks the project `RETIREMENT IN PROGRESS — NOT AN APPROVED PRODUCTION PATH`.
- Historical implementation and validation records remain available for reference.
- Any old `PASS` result describes historical component validation only and does not mean the Coordinator remains an active Production path.
- GitHub repository is retained; it is not deleted.

## Scope Protection

- No new worker, sub-agent, fan-out, scheduler, runtime patch, or Adaptive feature was created.
- No runtime file was changed.
- No unrelated Orca/Codex configuration, session database, project worker, or terminal was changed.
- The dirty Windows `측정일지_html` main and its existing worktrees were not modified. Policy migration was excluded by the superseding second instruction.

## Remaining Blocker

The retirement specification requires the runtime handler to equal the Orca
1.4.192 trusted-original SHA before launcher removal. The installed Orca is
1.4.197 with a different, unrecognized handler and no trusted 1.4.192 backup.
The rollback tool correctly refuses this state. A version-aware, vendor-sourced
integrity decision for Orca 1.4.197 is required before the ordered launcher and
Adaptive-only Skill removal steps can be completed.

## Decision Required

None was requested during this run. The operation stopped fail-closed at the
explicit runtime hash Gate. Do not copy the 1.4.192 handler into Orca 1.4.197.

## Next Start Point

Obtain an authoritative Orca 1.4.197 handler hash or supported repair/reinstall
procedure from the exact installed release. Once the current handler is proven
vendor-original, record that the old patch was superseded by upgrade, rerun the
general Orca smoke, then remove only the `orca-adaptive` launcher and clearly
Adaptive-only Skill links while preserving all snapshots. Finish with an
independent READ_ONLY retirement review. The measurement-project `AGENTS.md`
policy migration remains a separate later task.
