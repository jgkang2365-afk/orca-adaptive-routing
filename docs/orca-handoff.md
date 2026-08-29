# Orca Handoff

## Task

Classify the Windows canonical repository's 13 dirty files, preserve commit
`c58e8a6`, synchronize the Windows and WSL repositories, and establish the
standard Orca handoff file.

## Status

PASS

## Completed

- Classified all 13 Windows dirty files as EOL-only changes.
- Restored only the verified files and refreshed stale DrvFS index metadata.
- Fast-forwarded the Windows canonical `main` from `7f0652a` to `c58e8a6`.
- Added this handoff as the single current Orca completion record.

## Changes

- `AGENTS.md`: preserved the mandatory WSL worker runtime policy reference from
  `c58e8a6`.
- `docs/wsl-worker-runtime.md`: preserved the accepted WSL sandbox and trusted
  Coordinator lifecycle relay policy from `c58e8a6`.
- `docs/orca-handoff.md`: recorded the latest completed Orca task and next start
  point.
- No EOL-only change was committed as a functional change.

## Git

- Branch: `main`
- Final HEAD: the handoff commit containing this file
- Baseline before synchronization: `7f0652a`
- Required preserved commit: `c58e8a6`
- Working tree: clean after synchronization and verification
- Remote: WSL `origin` is the Windows canonical local repository; no external
  remote exists

## Verification

- Compared every dirty file with its HEAD blob after removing carriage returns;
  all 13 were byte-for-byte identical.
- Confirmed `git diff --quiet` and porcelain status were clean after targeted
  restoration and index refresh.
- Confirmed the update from `7f0652a` to `c58e8a6` was fast-forward-only.
- Confirmed `c58e8a6` contains the required policy files and remains an ancestor
  of the final history.
- Confirmed Windows and WSL finish on the same history.

## Safety

- No reset, repository-wide restore, stash, rebase, force push, history rewrite,
  remote change, or external publication was performed.
- No meaningful user change was deleted or mixed with EOL-only changes.
- WSL, Codex, Orca, sandbox settings, and other repositories were not changed.

## Remaining Issues

None.

## Decision Required

None.

## Next Start Point

Begin the separate Adaptive Coordinator v0.1 implementation from the committed
routing and WSL runtime policies. Start with Task classification into model,
reasoning effort, filesystem authority, and WSL placement, then implement Codex
worker launch, Orca Dispatch, result collection, normal `worker_done` or trusted
Coordinator lifecycle settlement, and worker release. Do not repeat the completed
environment, sandbox, or lifecycle relay validation.
