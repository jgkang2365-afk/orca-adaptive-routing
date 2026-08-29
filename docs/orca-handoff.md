# Orca Handoff

## Task

Adaptive Coordinator v0.1

## Status

PASS

## Completed

- Implemented deterministic task classification and autonomous first-phase selection.
- Implemented model, effort, authority, phase ordering, parallelism, verification,
  and escalation decisions.
- Connected policy-selected Codex terminals to Orca Runs, Tasks, and supervised
  Dispatches.
- Implemented normal `worker_done` collection, trusted Coordinator settlement,
  launch rollback, and worker cleanup.
- Passed all five routing scenarios and two live worker integrations.

## Changes

- `adaptive_coordinator/models.py`: routing plan and route contracts.
- `adaptive_coordinator/routing.py`: v0.1 classifier and reclassification.
- `adaptive_coordinator/orca.py`: WSL launch, Orca lifecycle, result, settlement,
  evidence, and release adapter.
- `adaptive_coordinator/cli.py`: `route` and autonomous `launch` commands.
- `tests/test_routing.py`: five scenario contracts and scoped-write regression.
- `tests/test_orca_adapter.py`: sandbox, gate, lifecycle, relay, cleanup, and JSON
  stream coverage.
- `README.md`, `pyproject.toml`, `.gitignore`: usage, packaging, and build hygiene.

## Routing Behavior

- Routine inspection: Luna / low / read-only.
- Ordinary implementation: Terra / medium / automatic-review workspace-write.
- Async or external integration: Terra / high, READ diagnosis before WRITE Lead
  when implementation is requested.
- Critical risk: Sol / high READ assessment; WRITE cannot launch without separate
  assessment approval; Fresh Verifier is independently planned.
- New risk findings are reclassified by the Coordinator.
- Model and authority are independent; `danger-full-access` is never generated.

## Integration Tests

- Test A: `run_b62fb4157f40`, `task_8f570ca6246b`, `ctx_e5b0238119cd`.
  Luna / low / read-only blocked workspace and `/tmp` writes, returned normal
  successful `worker_done`, and ended with released terminal accounting.
- Test B: `run_422738dba509`, `task_cd2b90aeaa08`, `ctx_cf77202acf73`.
  Terra / medium / `--approve-for-me` created, verified, and removed the fixture;
  an outside-workspace write was blocked. Direct `worker_done` hit the known WSL
  vsock boundary, so verified content-hash evidence was settled by trusted relay
  and the terminal was released.
- No integration probe file remains.

## Verification

- `python3 -m unittest discover -s tests -p 'test_*.py' -v`: 14 passed.
- Python compilation and `git diff --check` passed.
- Both integration Tasks are completed and both terminal resources are released.
- Independent gpt-5.6-sol / high / read-only Fresh Verifier: `VERDICT: PASS`.
- Existing routing and WSL runtime policy files were not changed.

## Git

- Branch: `main`
- Baseline: `d45dd63`
- Final HEAD: the Adaptive Coordinator v0.1 commit containing this handoff
- WSL `origin`: Windows canonical local repository
- External remote: none
- Working tree: clean after final synchronization

## Safety

- No danger-full-access, sandbox weakening, global configuration change, remote
  change, rebase, reset, force push, history rewrite, or repository-wide restore.
- No environment or verified lifecycle hook changes.
- Critical WRITE remains behind a separate READ-ONLY assessment.
- Only exact integration worker terminals were stopped or closed.

## Remaining Issues

None.

## Decision Required

None.

## Next Start Point

Use `python3 -m adaptive_coordinator route "<task>"` to inspect a decision or
`python3 -m adaptive_coordinator launch "<task>"` to create the first
policy-selected supervised worker. The next increment should add a durable loop
that advances dependent phases, replies to questions, and acknowledges mailbox
deliveries; do not repeat WSL sandbox or lifecycle relay validation.
