# Orca Adaptive Routing

> **Status: RETIRED — NOT A PRODUCTION EXECUTION PATH**
>
> Further Adaptive Coordinator development has stopped. The repository is kept
> as historical/reference material. Orca 1.4.197 contains neither Adaptive
> runtime compatibility marker, so the earlier Orca 1.4.192 patch was superseded
> by the upgrade without copying cross-version runtime code. The Production
> `orca-adaptive` launcher has been withdrawn; do not start new Adaptive Runs.

## Historical reference

Adaptive Coordinator v0.2.1 turned a task brief into a closed-loop routing plan
and executed its logical Gates through Orca-supervised Codex workers. The
following commands describe the retired experiment and are not current
Production instructions.

```bash
python3 -m adaptive_coordinator route "Inspect the repository and list Markdown files."
python3 -m adaptive_coordinator launch "Inspect the repository and list Markdown files."
orca-adaptive run "Inspect the repository and list Markdown files." --workspace "$PWD"
orca-adaptive --version
```

The router selects model, reasoning effort, authority, phase ordering, and
verification requirements independently. Worker launches are restricted to a
WSL/Linux workspace under `/home`: read tasks use `read-only` with approvals
disabled, ordinary implementation uses explicit `workspace-write` with approvals
disabled so outside-workspace requests fail rather than entering review,
and `danger-full-access` is never generated. Critical WRITE phases cannot launch
until their separate READ-ONLY assessment is explicitly recorded as complete.

The capability ladder is Luna/low, Terra/medium, Terra/high, Sol/medium,
Sol/high, then Sol/xhigh. Automatic Sol/xhigh is a bounded READ-ONLY diagnosis
only; capability never raises filesystem authority. Lifecycle completion is not
task success: phase-specific evidence, unresolved-question, deterministic-test,
target-identity, fencing, and cleanup Gates must all pass. Pull requests and
main pushes run the `Adaptive Coordinator Quality / quality-gate` GitHub check.

The Orca adapter uses the supported custom-argv path:

1. create a Codex terminal with the selected sandbox/model/effort;
2. wait for TUI readiness;
3. attach it through `orchestration worker-start --terminal`;
4. read the result and accept normal `worker_done`, or use the trusted
   Coordinator relay after evidence validation;
5. release the settled worker.

Run the deterministic routing and lifecycle contract tests with:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```
