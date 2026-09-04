---
name: orca-adaptive-routing
description: Delegate multi-step project investigation, any workspace mutation, implementation, or review to the installed Orca Adaptive Coordinator; skip only single-answer conversation that needs no tools or file changes.
---

# Orca Adaptive Routing

Read the target workspace's `AGENTS.md` and referenced project rules first.
Those files define what must be preserved; this skill controls how work is
delegated and executed.

For any requested mutation, multi-file investigation, implementation, bug fix,
refactor, test change, or verification-bearing development task, the Orca Parent
must remain an orchestrator and invoke:

```bash
orca-adaptive run "<user task>" --workspace "<absolute WSL workspace>" --delegated-by-parent
```

When the user has clearly preapproved normal in-scope work and requested no
intermediate intervention, also pass:

```text
--preapproved --interaction-mode no-intervention
```

These flags record delegation and interaction policy; they never expand the
filesystem sandbox or authorize out-of-scope/high-risk operations. The Parent
must not use file mutation tools or implementation shell commands itself.

Let the Coordinator choose model, effort, authority, decomposition, bounded
READ fan-out, ordering, escalation, and verification. Workers must not spawn
other workers. Preserve one Lead WRITE owner unless the Coordinator has explicit
isolated worktrees and non-overlapping scopes.

For preapproved runs, ordinary reads, tests, and workspace-local writes proceed
without approval prompts. An operation outside the authorized workspace,
`danger-full-access`, destructive production mutation, secret requirement, or
new business-policy choice must fail closed or return BLOCKED; do not turn it
into an Allow request or weaken the sandbox.

Inspect the machine-readable result and require cleanup. Treat FAILED, BLOCKED,
verifier failure, nonzero `parent_mutation_count`, nonzero
`approval_prompt_count`, or cleanup failure as non-success.

This skill is the supported discoverable Parent integration point. It does not
claim to be a native hard interceptor inside Orca: hosts that do not load or
honor implicitly selected skills cannot be forced by repository code alone.
