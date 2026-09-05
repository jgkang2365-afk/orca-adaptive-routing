# Orca Handoff

## Task

Adaptive Coordinator serial retirement — final closure

## Status

FINAL RETIREMENT: PASS

The Adaptive Coordinator is retired and is not an active Production execution
path. The repository and installed snapshots remain only as historical/reference
material.

## Retirement Date

2026-09-05 (Asia/Seoul)

## Runtime Retirement

- Current Orca version: `1.4.197`.
- Exact handler: `/mnt/c/Users/USER/AppData/Local/Programs/orca/resources/app.asar.unpacked/out/cli/handlers/terminal.js`.
- Current/final SHA-256: `980c4a931ffb0d99a65b8ac30875a6472acd8cd678544c92d5109723ae5e7d7f`.
- Known historical v0.2.1 patched SHA-256: `38ad8a5fd41a3c45e3927a6ccf741aa22cf161c9671fa26cb6229cfc963adedd`.
- Trusted historical Orca 1.4.192 original SHA-256: `b6b08954c7c2c7dc1e36a90eeb8da390b31cf0e00c5229327d006aff57bb96b4`.
- `ORCA_ADAPTIVE_TERMINAL_CREATE_COMPAT_V3`: absent (`0` matches).
- `createTerminalWithExactReconciliation`: absent (`0` matches).
- Retirement determination: PASS. Under the approved version-aware rule, both
  Adaptive-only identifiers being absent from the exact Orca 1.4.197 handler
  proves that the runtime compatibility patch was removed by the Orca upgrade.
- No Orca runtime file was modified and no 1.4.192 file was copied into 1.4.197.

## Production Withdrawal

- Last Production version: `0.2.1`.
- Last installed commit: `d4dfb5473f53efbd7c49d6997370b09645bcffd9`.
- Launcher `/home/user/.local/bin/orca-adaptive`: removed.
- Launcher removal was limited to the exact symbolic link after the runtime
  retirement Gate passed.
- Production snapshots: preserved at `/home/user/.local/lib/orca-adaptive-routing/`.
- No snapshot, Codex state, or session history was deleted.

## General Environment

- WSL Codex path: `/home/user/.nvm/versions/node/v24.20.0/bin/codex`.
- Codex version: `codex-cli 0.150.1`.
- Codex smoke: PASS.
- Orca runtime: `1.4.197`, ready and reachable.
- General Orca terminal smoke: PASS. A non-Adaptive terminal emitted
  `ORCA_RETIREMENT_FINAL_SMOKE_OK` and was closed by its exact handle.

## Resource Cleanup

- Dedicated live `orca-adaptive:*` terminals: `0`.
- Live Adaptive worker terminals: `0`.
- Running `orca-adaptive` / `adaptive_coordinator` processes: `0`.
- Additional Adaptive repository worktrees: `0`.
- The disposable measurement-policy worktree created under the superseded first
  retirement instruction was verified clean and removed before final closure.
- Historical Orca worker records and user-owned general shell/Codex terminals
  were not altered; they are not live Adaptive execution resources.

## Repository

- Working branch: `chore/retire-adaptive-coordinator`.
- Baseline before retirement: `749bbf6bba2059d2cb59fe20ca979d25aec70065`.
- Initial retirement documentation commit: `6fc33732c98e2d42231dbae6bd42c9ccb42cbc33`.
- Initial retirement PR: `#33`, merged as
  `d3d241cc753d024ae9f8fd2bb6965925265d7258`; `quality-gate` PASS.
- GitHub repository is retained as `RETIRED/FROZEN` reference material.
- Historical PASS results describe component validation only, not an active
  Production deployment.

## Safety

- `danger-full-access`: not used.
- Sandbox weakening: none.
- Cross-version runtime overwrite: none.
- Other project files or worktrees: not modified by final closure.
- Production snapshots: preserved.

## Remaining Issues

None for Adaptive Coordinator Production retirement. Adaptive-only Skill-link
removal was not part of the resumed bounded instruction and should be handled
separately only after verifying each link is not shared by another Orca/Codex
workflow.

## Decision Required

None.

## Next Start Point

If desired, perform the separately scoped measurement-project `AGENTS.md` policy
migration without restoring or invoking the retired Adaptive Coordinator.
