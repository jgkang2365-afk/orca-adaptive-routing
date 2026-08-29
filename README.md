# Orca Adaptive Routing

Adaptive Coordinator v0.1 converts a task brief into an explicit routing plan
and can attach a policy-configured Codex terminal to an Orca supervised
Task/Dispatch.

```bash
python3 -m adaptive_coordinator route "Inspect the repository and list Markdown files."
python3 -m adaptive_coordinator launch "Inspect the repository and list Markdown files."
```

The router selects model, reasoning effort, authority, phase ordering, and
verification requirements independently. Worker launches are restricted to a
WSL/Linux workspace under `/home`: read tasks use `read-only` with approvals
disabled, ordinary implementation uses explicit `workspace-write` with approvals
disabled so outside-workspace requests fail rather than entering review,
and `danger-full-access` is never generated. Critical WRITE phases cannot launch
until their separate READ-ONLY assessment is explicitly recorded as complete.

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
