# Orca Adaptive Routing

Adaptive Coordinator v0.3.0 turns a task brief into a closed-loop routing plan
and executes its logical Gates through Orca-supervised Codex workers.

```bash
python3 -m adaptive_coordinator route "Inspect the repository and list Markdown files."
python3 -m adaptive_coordinator launch "Inspect the repository and list Markdown files."
orca-adaptive run "Inspect the repository and list Markdown files." --workspace "$PWD"
orca-adaptive run "Implement the requested fix." --workspace "$PWD" \
  --delegated-by-parent --preapproved --interaction-mode no-intervention
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

The packaged implicit skill is the supported Orca Parent entry point. It tells a
Parent to delegate mutations and multi-file work, records preapproval as typed
run metadata, and leaves implementation to supervised workers. For bounded
multi-domain work the runner can launch up to three independent READ workers
before joining their evidence into a single Lead WRITE Gate. This repository
cannot install a native hard interceptor inside Orca; a host that does not load
or honor the discoverable skill cannot be forced to delegate by package code.
The production installer links the same commit-object Skill snapshot into the
shared agent directory, the regular CODEX_HOME directory, and—when its runtime
home exists—the Orca-managed Codex Skill directory. Unmanaged existing content
is never overwritten.

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
