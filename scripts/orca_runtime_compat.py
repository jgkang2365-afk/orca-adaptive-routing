#!/usr/bin/env python3
"""Install or roll back the Orca 1.4.192 terminal-create compatibility patch.

This utility intentionally patches one compiled, unpacked CLI file.  It is
fail-closed: the production command accepts only the exact Orca 1.4.192 source
hash or a file carrying this patch's marker, and rollback accepts only the
exact saved original.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Final


ORIGINAL_SHA256: Final = (
    "b6b08954c7c2c7dc1e36a90eeb8da390b31cf0e00c5229327d006aff57bb96b4"
)
PATCH_MARKER: Final = "ORCA_ADAPTIVE_TERMINAL_CREATE_COMPAT_V3"
LEGACY_PATCHED_SHA256S: Final = frozenset(
    {
        "18e1c85f023212dac77191d9916c724b451dd49b146610413fbdae6809173c2d",
        "53efc0eb4c7ff1ae5d09e605b7d982e7556efd77cd1169dc3123ffe16032b513",
    }
)
BACKUP_SUFFIX: Final = ".orca-adaptive-v021.original"
MANIFEST_SUFFIX: Final = ".orca-adaptive-v021.json"

IMPORT_ANCHOR: Final = (
    'const codex_command_classification_1 = require("../codex-command-classification");\n'
)
IMPORT_REPLACEMENT: Final = (
    'const crypto_1 = require("node:crypto");\n' + IMPORT_ANCHOR
)
HELPER_ANCHOR: Final = "const DEFAULT_TERMINAL_WAIT_RPC_TIMEOUT_MS = 5 * 60 * 1000;\n"
HELPER_REPLACEMENT: Final = HELPER_ANCHOR + f"""// {PATCH_MARKER}
// Orca 1.4.192 already implements terminal.create-idempotency.v2.  Keep one
// mutation id across bounded reconciliation calls so a delayed response can
// recover only the terminal created by this operation.
const TERMINAL_CREATE_RECONCILE_LIMIT = 2;
const TERMINAL_HANDLE_PUBLICATION_TIMEOUT = 'Timed out waiting for terminal handle after creation';
function isExactTerminalCreateRecoveryError(error) {{
    return (error instanceof runtime_client_1.RuntimeClientError &&
        (error.code === 'runtime_timeout' ||
            error.message === TERMINAL_HANDLE_PUBLICATION_TIMEOUT));
}}
async function createTerminalWithExactReconciliation(client, payload) {{
    const mutationPayload = {{
        ...payload,
        clientMutationId: (0, crypto_1.randomUUID)()
    }};
    try {{
        return await client.call('terminal.create', mutationPayload);
    }}
    catch (error) {{
        // Local CLI calls receive a new runtime client identity on every RPC,
        // so the server cannot reconcile them by mutation id. Reconciliation
        // is safe only on a paired remote client with stable identity.
        if (!client.isRemote || !isExactTerminalCreateRecoveryError(error)) {{
            throw error;
        }}
        let lastError = error;
        for (let attempt = 0; attempt < TERMINAL_CREATE_RECONCILE_LIMIT; attempt += 1) {{
            try {{
                return await client.call('terminal.create', {{
                    ...mutationPayload,
                    reconcileExisting: true
                }});
            }}
            catch (reconcileError) {{
                if (!isExactTerminalCreateRecoveryError(reconcileError)) {{
                    throw reconcileError;
                }}
                lastError = reconcileError;
            }}
        }}
        throw lastError;
    }}
}}
"""

CREATE_ANCHOR: Final = """        const command = (0, flags_1.getOptionalStringFlag)(flags, 'command');
        const useRendererBackedInteractiveTerminal = !client.isRemote && (0, codex_command_classification_1.shouldUseRendererBackedInteractiveTerminal)(command);
        const focus = flags.get('focus') === true;
        const result = await client.call('terminal.create', {
            worktree: await (0, selectors_1.getBrowserWorktreeSelector)(flags, cwd, client),
            command,
            title: (0, flags_1.getOptionalStringFlag)(flags, 'title'),
            // Why: interactive local agent TUIs need the renderer-backed terminal
            // path for browser-side features, but CLI creates must stay backgrounded
            // unless the caller explicitly asks for focus.
            focus,
            ...(focus ? { presentation: 'focused' } : {}),
            ...(useRendererBackedInteractiveTerminal ? { rendererBacked: true, activate: focus } : {})
        });
"""

CREATE_REPLACEMENT: Final = """        const command = (0, flags_1.getOptionalStringFlag)(flags, 'command');
        const title = (0, flags_1.getOptionalStringFlag)(flags, 'title');
        const launchCodexAgent = !client.isRemote && (0, codex_command_classification_1.shouldUseRendererBackedCodexTerminal)(command);
        const useDirectCodexTerminal = launchCodexAgent && title?.startsWith('orca-adaptive:');
        const useRendererBackedInteractiveTerminal = !client.isRemote && !useDirectCodexTerminal && (0, codex_command_classification_1.shouldUseRendererBackedInteractiveTerminal)(command);
        const focus = flags.get('focus') === true;
        const result = await createTerminalWithExactReconciliation(client, {
            worktree: await (0, selectors_1.getBrowserWorktreeSelector)(flags, cwd, client),
            command,
            title,
            // Why: interactive local agent TUIs need the renderer-backed terminal
            // path for browser-side features, but CLI creates must stay backgrounded
            // unless the caller explicitly asks for focus.
            focus,
            ...(focus ? { presentation: 'focused' } : {}),
            ...(useRendererBackedInteractiveTerminal ? { rendererBacked: true, activate: focus } : {}),
            // This is authoritative only for interactive Codex. The classifier
            // excludes exec/version/help and other one-shot commands.
            ...(launchCodexAgent ? { launchAgent: 'codex' } : {})
        });
"""


class CompatibilityPatchError(RuntimeError):
    """The target cannot be patched or rolled back without guessing."""


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def patch_source(source: str) -> str:
    if PATCH_MARKER in source:
        return source
    replacements = (
        (IMPORT_ANCHOR, IMPORT_REPLACEMENT),
        (HELPER_ANCHOR, HELPER_REPLACEMENT),
        (CREATE_ANCHOR, CREATE_REPLACEMENT),
    )
    patched = source
    for anchor, replacement in replacements:
        count = patched.count(anchor)
        if count != 1:
            raise CompatibilityPatchError(
                f"expected exactly one patch anchor, found {count}"
            )
        patched = patched.replace(anchor, replacement, 1)
    validate_patched_source(patched)
    return patched


def validate_patched_source(source: str) -> None:
    required = (
        PATCH_MARKER,
        "clientMutationId: (0, crypto_1.randomUUID)()",
        "reconcileExisting: true",
        "error.code === 'runtime_timeout'",
        "error.message === TERMINAL_HANDLE_PUBLICATION_TIMEOUT",
        "shouldUseRendererBackedCodexTerminal)(command)",
        "title?.startsWith('orca-adaptive:')",
        "launchAgent: 'codex'",
    )
    missing = [item for item in required if item not in source]
    if missing:
        raise CompatibilityPatchError(f"patched contract missing: {missing}")
    forbidden = (
        "terminal.list",
        "latest terminal",
        "danger-full-access",
    )
    helper = source[source.index(PATCH_MARKER) : source.index("const terminalFocusHandler")]
    found = [item for item in forbidden if item in helper]
    if found:
        raise CompatibilityPatchError(f"unsafe reconciliation contract: {found}")


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _backup_path(target: Path) -> Path:
    return target.with_name(target.name + BACKUP_SUFFIX)


def _manifest_path(target: Path) -> Path:
    return target.with_name(target.name + MANIFEST_SUFFIX)


def _expected_patched_sha(original: bytes) -> str:
    return sha256_bytes(patch_source(original.decode("utf-8")).encode("utf-8"))


def install_patch(target: Path, *, expected_sha256: str = ORIGINAL_SHA256) -> dict[str, str]:
    content = target.read_bytes()
    current_sha = sha256_bytes(content)
    backup = _backup_path(target)
    manifest = _manifest_path(target)

    if PATCH_MARKER.encode() in content:
        if not backup.is_file() or sha256_bytes(backup.read_bytes()) != expected_sha256:
            raise CompatibilityPatchError("patched target lacks the exact trusted backup")
        validate_patched_source(content.decode("utf-8"))
        expected_patched_sha = _expected_patched_sha(backup.read_bytes())
        if current_sha != expected_patched_sha:
            raise CompatibilityPatchError(
                "patched target differs from the exact deterministic patch"
            )
        return {"status": "already_patched", "sha256": current_sha}
    if current_sha != expected_sha256:
        raise CompatibilityPatchError(
            f"refusing unknown Orca CLI source: expected {expected_sha256}, got {current_sha}"
        )

    if backup.exists():
        if not backup.is_file() or sha256_bytes(backup.read_bytes()) != expected_sha256:
            raise CompatibilityPatchError("existing backup is not the exact trusted original")
    else:
        _atomic_write(backup, content, target.stat().st_mode & 0o777)

    patched = patch_source(content.decode("utf-8")).encode("utf-8")
    mode = target.stat().st_mode & 0o777
    _atomic_write(target, patched, mode)
    record = {
        "schemaVersion": "1",
        "patch": PATCH_MARKER,
        "originalSha256": expected_sha256,
        "patchedSha256": sha256_bytes(patched),
    }
    _atomic_write(
        manifest,
        (json.dumps(record, indent=2, sort_keys=True) + "\n").encode(),
        0o600,
    )
    return {"status": "patched", "sha256": record["patchedSha256"]}


def rollback_patch(
    target: Path,
    *,
    expected_sha256: str = ORIGINAL_SHA256,
    legacy_patched_sha256s: frozenset[str] = LEGACY_PATCHED_SHA256S,
) -> dict[str, str]:
    backup = _backup_path(target)
    if not backup.is_file():
        raise CompatibilityPatchError("trusted rollback backup is missing")
    original = backup.read_bytes()
    backup_sha = sha256_bytes(original)
    if backup_sha != expected_sha256:
        raise CompatibilityPatchError(
            f"rollback backup hash mismatch: expected {expected_sha256}, got {backup_sha}"
        )
    current = target.read_bytes()
    current_sha = sha256_bytes(current)
    if current_sha == expected_sha256:
        return {"status": "already_original", "sha256": current_sha}
    if current_sha in legacy_patched_sha256s:
        # The first live candidate used the exact v1 patch before direct PTY
        # opt-in was added. Permit only that known artifact, and only when its
        # independently verified original backup is present.
        pass
    elif PATCH_MARKER.encode() not in current:
        raise CompatibilityPatchError("refusing to overwrite an unknown modified target")
    else:
        validate_patched_source(current.decode("utf-8"))
        expected_patched_sha = _expected_patched_sha(original)
        if current_sha != expected_patched_sha:
            raise CompatibilityPatchError(
                "refusing modified content that only preserves the patch marker"
            )
    _atomic_write(target, original, target.stat().st_mode & 0o777)
    _manifest_path(target).unlink(missing_ok=True)
    return {"status": "rolled_back", "sha256": expected_sha256}


def patch_status(target: Path, *, expected_sha256: str = ORIGINAL_SHA256) -> dict[str, object]:
    content = target.read_bytes()
    current_sha = sha256_bytes(content)
    backup = _backup_path(target)
    trusted_backup = (
        backup.is_file() and sha256_bytes(backup.read_bytes()) == expected_sha256
    )
    exact_patched = False
    if PATCH_MARKER.encode() in content and trusted_backup:
        try:
            exact_patched = current_sha == _expected_patched_sha(backup.read_bytes())
        except (CompatibilityPatchError, UnicodeError):
            exact_patched = False
    state = (
        "original"
        if current_sha == expected_sha256
        else "patched"
        if exact_patched
        else "unknown"
    )
    return {
        "state": state,
        "sha256": current_sha,
        "trustedBackup": trusted_backup,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Patch the exact Orca 1.4.192 unpacked terminal CLI handler"
    )
    parser.add_argument("action", choices=("install", "rollback", "status"))
    parser.add_argument("--target", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.action == "install":
            result = install_patch(args.target)
        elif args.action == "rollback":
            result = rollback_patch(args.target)
        else:
            result = patch_status(args.target)
    except (CompatibilityPatchError, OSError, UnicodeError) as error:
        parser.exit(1, f"STOP: {error}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
