# WSL Worker Runtime Policy

Status: ACCEPTED
Version: 0.1

## Runtime

Codex workers that require enforceable filesystem permissions run in:

- WSL2
- Ubuntu 24.04
- Linux filesystem workspace under `/home/...`
- Linux Codex

Native Windows Codex sandbox is not used for adaptive-routing permission enforcement.

## Permission Routing

### READ-ONLY

Use for:

- Investigator
- Reviewer
- impact analysis
- Fresh Verifier

Required runtime:

- `--sandbox read-only`
- `--ask-for-approval never`

The filesystem must technically reject writes.

### WRITE

Lead implementation uses:

- `--sandbox workspace-write`
- `--ask-for-approval never`

Writes are permitted only inside the assigned workspace.

### Full Access

`danger-full-access` must never be selected automatically by the adaptive router.

It requires an explicit exceptional decision.

## Model And Permission Independence

Model capability and filesystem authority are separate decisions.

Examples:

- Luna / low / READ-ONLY
- Terra / medium / workspace-write
- Terra / high / READ-ONLY
- Sol / high / READ-ONLY
- Sol / high / workspace-write

Selecting Sol never grants WRITE authority by itself.

## Orca Lifecycle Hooks

The trusted Orca lifecycle hook currently comes from:

`~/.local/share/orca/codex-runtime-home/home/hooks.json`

and invokes:

`~/.orca/agent-hooks/codex-hook.sh`

The verified hook only relays lifecycle metadata to Orca and does not modify
the project workspace.

If its source or implementation materially changes, it must be reviewed again
before being trusted by READ-ONLY workers.

## Lifecycle Relay Boundary

Sandbox restrictions must not be weakened merely to allow a worker to call
Windows-side Orca lifecycle commands.

WSL Codex workers may be unable to deliver `worker_done` directly through
`orca-ide` because Windows/WSL vsock transport can fail from inside the
sandbox.

The accepted v0.1 architecture therefore places lifecycle settlement outside
the Codex filesystem sandbox.

Flow:

1. Worker performs the assigned task inside its enforced sandbox.
2. Worker reports completion and findings in its final output.
3. Coordinator reads the supervised worker output through Orca.
4. Coordinator verifies the expected completion evidence.
5. For READ-ONLY work, Coordinator also verifies that no files were modified.
6. Coordinator performs lifecycle settlement in Orca.
7. Worker terminal is released or closed.

The lifecycle relay is trusted infrastructure.

It may transmit orchestration state and completion metadata, but it must not
perform project implementation or bypass the worker's filesystem authority.

## Guiding Rule

Never weaken the Linux sandbox to solve orchestration transport problems.

Worker execution security and Orca lifecycle transport are separate layers.
