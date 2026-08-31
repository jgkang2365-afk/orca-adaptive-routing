# Orca 1.4.192 Runtime Compatibility Patch

This repository carries a narrow, reversible patch for the unpacked Orca CLI
terminal handler. It does not alter Adaptive Coordinator routing or sandbox
policy.

Each create gets one `clientMutationId`. Paired remote clients have stable
runtime identity, so only they may use Orca's existing
`terminal.create-idempotency.v2` contract: `runtime_timeout` or the exact
terminal handle-publication timeout permits two bounded calls with
`reconcileExisting: true`. Local CLI calls never retry because their runtime
client identity changes across RPC calls. The patch never selects a terminal
by list order, display recency, or worktree.

Interactive Codex commands also send the runtime's official
`launchAgent: "codex"` hint so the created terminal receives authoritative
agent identity. The existing classifier excludes `codex exec`, version, help,
and other noninteractive commands. Worker-start identity and process checks
remain unchanged.

Coordinator-owned terminals carry the explicit title namespace
`orca-adaptive:<phase>`. The guarded handler uses Orca's existing
direct/headless PTY path only when that exact namespace and the local
interactive Codex classifier both match. Ordinary titles, remote calls,
one-shot Codex commands, and other interactive agents keep the stock behavior.

The installer accepts only the exact Orca 1.4.192 handler SHA-256
`b6b08954c7c2c7dc1e36a90eeb8da390b31cf0e00c5229327d006aff57bb96b4`.
It creates an exact backup before an atomic replacement and refuses unknown
source, backup, or post-install content.

The rollback path recognizes only the exact earlier candidate SHA-256 values
`18e1c85f023212dac77191d9916c724b451dd49b146610413fbdae6809173c2d`
and `53efc0eb4c7ff1ae5d09e605b7d982e7556efd77cd1169dc3123ffe16032b513`,
and only when the saved backup matches the trusted original hash. Upgrade a
candidate by rolling it back first and then installing the current patch;
arbitrary patched or modified handlers remain rejected.

```bash
python3 scripts/orca_runtime_compat.py status --target /absolute/path/to/terminal.js
python3 scripts/orca_runtime_compat.py install --target /absolute/path/to/terminal.js
python3 scripts/orca_runtime_compat.py rollback --target /absolute/path/to/terminal.js
```

Close or restart Orca only during the coordinated live rollout. Verify the
exact created handle and `agentIdentity=codex` before worker-start. Roll back
with the saved original if live registration fails; never bypass identity or
attach to a guessed terminal.
