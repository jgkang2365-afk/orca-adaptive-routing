from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PATCHER_PATH = ROOT / "scripts" / "orca_runtime_compat.py"
SPEC = importlib.util.spec_from_file_location("orca_runtime_compat", PATCHER_PATH)
assert SPEC is not None and SPEC.loader is not None
PATCHER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCHER)


def fixture_source() -> str:
    return """\
"use strict";
const codex_command_classification_1 = require("../codex-command-classification");
const format_1 = require("../format");
const flags_1 = require("../flags");
const runtime_client_1 = require("../runtime-client");
const selectors_1 = require("../selectors");
const DEFAULT_TERMINAL_WAIT_RPC_TIMEOUT_MS = 5 * 60 * 1000;
const terminalFocusHandler = async () => {};
const handler = async ({ flags, client, cwd, json }) => {
        const command = (0, flags_1.getOptionalStringFlag)(flags, 'command');
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
};
exports.TERMINAL_HANDLERS = { 'terminal create': handler };
"""


class OrcaRuntimeCompatibilityPatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="orca-runtime-compat.")
        self.target = Path(self.temp.name) / "terminal.js"
        self.original = fixture_source().encode()
        self.expected_sha = hashlib.sha256(self.original).hexdigest()
        self.target.write_bytes(self.original)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def execute_create(
        self,
        command: str,
        *,
        title: str,
        remote: bool = False,
        fail_count: int = 0,
    ) -> dict[str, object]:
        cli = Path(self.temp.name) / "cli"
        handlers = cli / "handlers"
        handlers.mkdir(parents=True, exist_ok=True)
        (handlers / "terminal.js").write_text(PATCHER.patch_source(fixture_source()))
        (cli / "codex-command-classification.js").write_text(
            "exports.shouldUseRendererBackedCodexTerminal = "
            "command => command === 'codex' || "
            "(command.startsWith('codex --') && !command.startsWith('codex --version'));\n"
            "exports.shouldUseRendererBackedInteractiveTerminal = "
            "command => exports.shouldUseRendererBackedCodexTerminal(command) || "
            "command === 'claude';\n"
        )
        (cli / "format.js").write_text("exports.printResult = () => {};\n")
        (cli / "flags.js").write_text(
            "exports.getOptionalStringFlag = (flags, name) => flags.get(name);\n"
        )
        (cli / "runtime-client.js").write_text(
            "class RuntimeClientError extends Error { "
            "constructor(code, message) { super(message); this.code = code; } }\n"
            "exports.RuntimeClientError = RuntimeClientError;\n"
        )
        (cli / "selectors.js").write_text(
            "exports.getBrowserWorktreeSelector = async () => 'worktree-test';\n"
        )
        harness = Path(self.temp.name) / "invoke.js"
        harness.write_text(
            "const handler = require(process.argv[2]).TERMINAL_HANDLERS['terminal create'];\n"
            "const flags = new Map([['command', process.argv[3]], ['title', process.argv[4]]]);\n"
            "const remote = process.argv[5] === 'true';\n"
            "const failCount = Number(process.argv[6]);\n"
            "const RuntimeClientError = require('./cli/runtime-client').RuntimeClientError;\n"
            "const payloads = []; let calls = 0;\n"
            "const client = { isRemote: remote, call: async (_method, payload) => { "
            "calls += 1; payloads.push(payload); "
            "if (calls <= failCount) throw new RuntimeClientError('runtime_timeout', 'timeout'); "
            "return { result: { terminal: {} } }; } };\n"
            "handler({ flags, client, cwd: '.', json: true })"
            ".then(() => console.log(JSON.stringify({ calls, payloads, error: null })))"
            ".catch(error => console.log(JSON.stringify({ calls, payloads, error: error.code })));\n"
        )
        completed = subprocess.run(
            [
                "node",
                str(harness),
                str(handlers / "terminal.js"),
                command,
                title,
                str(remote).lower(),
                str(fail_count),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(completed.stdout)

    def test_install_uses_exact_hash_guard_and_creates_trusted_backup(self) -> None:
        result = PATCHER.install_patch(self.target, expected_sha256=self.expected_sha)
        self.assertEqual(result["status"], "patched")
        patched = self.target.read_text()
        self.assertIn(PATCHER.PATCH_MARKER, patched)
        backup = self.target.with_name(self.target.name + PATCHER.BACKUP_SUFFIX)
        self.assertEqual(backup.read_bytes(), self.original)
        self.assertEqual(
            PATCHER.patch_status(self.target, expected_sha256=self.expected_sha)["state"],
            "patched",
        )

    def test_install_refuses_unknown_source_without_writing_backup(self) -> None:
        self.target.write_text("unknown source\n")
        with self.assertRaises(PATCHER.CompatibilityPatchError):
            PATCHER.install_patch(self.target, expected_sha256=self.expected_sha)
        self.assertFalse(
            self.target.with_name(self.target.name + PATCHER.BACKUP_SUFFIX).exists()
        )

    def test_rollback_restores_exact_original_and_refuses_unknown_target(self) -> None:
        PATCHER.install_patch(self.target, expected_sha256=self.expected_sha)
        result = PATCHER.rollback_patch(self.target, expected_sha256=self.expected_sha)
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(self.target.read_bytes(), self.original)

        PATCHER.install_patch(self.target, expected_sha256=self.expected_sha)
        self.target.write_text("unrecognized post-install mutation\n")
        with self.assertRaises(PATCHER.CompatibilityPatchError):
            PATCHER.rollback_patch(self.target, expected_sha256=self.expected_sha)

    def test_rollback_accepts_only_exact_allowlisted_legacy_with_trusted_backup(self) -> None:
        legacy = b"exact legacy artifact\n"
        legacy_sha = hashlib.sha256(legacy).hexdigest()
        backup = self.target.with_name(self.target.name + PATCHER.BACKUP_SUFFIX)
        backup.write_bytes(self.original)
        self.target.write_bytes(legacy)

        result = PATCHER.rollback_patch(
            self.target,
            expected_sha256=self.expected_sha,
            legacy_patched_sha256s=frozenset({legacy_sha}),
        )
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(self.target.read_bytes(), self.original)

        self.target.write_bytes(legacy + b"unknown mutation")
        with self.assertRaises(PATCHER.CompatibilityPatchError):
            PATCHER.rollback_patch(
                self.target,
                expected_sha256=self.expected_sha,
                legacy_patched_sha256s=frozenset({legacy_sha}),
            )

    def test_reconciliation_is_bounded_and_uses_one_exact_mutation_id(self) -> None:
        patched = PATCHER.patch_source(fixture_source())
        self.assertIn("const TERMINAL_CREATE_RECONCILE_LIMIT = 2", patched)
        self.assertEqual(patched.count("crypto_1.randomUUID)()"), 1)
        self.assertIn("...mutationPayload,\n                    reconcileExisting: true", patched)
        helper = patched[patched.index(PATCHER.PATCH_MARKER) : patched.index("const terminalFocusHandler")]
        self.assertNotIn("terminal.list", helper)
        self.assertNotIn("title", helper)
        self.assertNotIn("worktree", helper)

    def test_reconciliation_only_accepts_runtime_or_exact_publication_timeout(self) -> None:
        patched = PATCHER.patch_source(fixture_source())
        helper = patched[patched.index(PATCHER.PATCH_MARKER) : patched.index("const terminalFocusHandler")]
        self.assertIn("error.code === 'runtime_timeout'", helper)
        self.assertIn("error.message === TERMINAL_HANDLE_PUBLICATION_TIMEOUT", helper)
        self.assertNotIn("runtime_unavailable", helper)
        self.assertNotIn("invalid_runtime_response", helper)

    def test_launch_agent_is_limited_to_interactive_codex_classifier(self) -> None:
        patched = PATCHER.patch_source(fixture_source())
        self.assertIn(
            "shouldUseRendererBackedCodexTerminal)(command)", patched
        )
        self.assertIn(
            "const launchCodexAgent = !client.isRemote &&", patched
        )
        self.assertIn("...(launchCodexAgent ? { launchAgent: 'codex' } : {})", patched)
        self.assertNotIn("useRendererBackedInteractiveTerminal ? { launchAgent", patched)
        # The installed classifier is the authority that excludes one-shot Codex.
        classifier = Path(
            "/mnt/c/Users/USER/AppData/Local/Programs/orca/resources/"
            "app.asar.unpacked/out/cli/codex-command-classification.js"
        )
        if classifier.is_file():
            source = classifier.read_text()
            for command in ("'exec'", "'help'", "'version'"):
                self.assertIn(command, source)

    def test_direct_pty_requires_marker_and_interactive_codex(self) -> None:
        patched = PATCHER.patch_source(fixture_source())
        self.assertIn(
            "const useDirectCodexTerminal = launchCodexAgent && "
            "title?.startsWith('orca-adaptive:')",
            patched,
        )
        self.assertIn(
            "!client.isRemote && !useDirectCodexTerminal && "
            "(0, codex_command_classification_1.shouldUseRendererBackedInteractiveTerminal)(command)",
            patched,
        )
        # One-shot Codex remains governed by the existing classifier and the
        # marker does not alter Claude or any other interactive terminal.
        self.assertIn("const launchCodexAgent = !client.isRemote &&", patched)
        self.assertNotIn(
            "ORCA_ADAPTIVE_DIRECT_CODEX_TERMINAL === '1' && "
            "useRendererBackedInteractiveTerminal",
            patched,
        )

        full_argv = (
            "codex --model gpt-5.6-luna -c model_reasoning_effort=low "
            "--sandbox read-only --ask-for-approval never --no-alt-screen"
        )
        adaptive = self.execute_create(
            full_argv, title="orca-adaptive:investigation"
        )["payloads"][0]
        self.assertNotIn("rendererBacked", adaptive)
        self.assertEqual(adaptive["launchAgent"], "codex")
        self.assertEqual(adaptive["title"], "orca-adaptive:investigation")

        ordinary = self.execute_create(full_argv, title="manual-codex")["payloads"][0]
        self.assertTrue(ordinary["rendererBacked"])
        self.assertEqual(ordinary["launchAgent"], "codex")

        one_shot = self.execute_create(
            "codex exec", title="orca-adaptive:investigation"
        )["payloads"][0]
        self.assertNotIn("rendererBacked", one_shot)
        self.assertNotIn("launchAgent", one_shot)

        other_agent = self.execute_create(
            "claude", title="orca-adaptive:investigation"
        )["payloads"][0]
        self.assertTrue(other_agent["rendererBacked"])
        self.assertNotIn("launchAgent", other_agent)

    def test_local_timeout_never_retries_but_remote_reconciles_exactly(self) -> None:
        local = self.execute_create(
            "codex",
            title="orca-adaptive:investigation",
            fail_count=1,
        )
        self.assertEqual(local["calls"], 1)
        self.assertEqual(local["error"], "runtime_timeout")

        remote = self.execute_create(
            "codex",
            title="orca-adaptive:investigation",
            remote=True,
            fail_count=1,
        )
        self.assertEqual(remote["calls"], 2)
        self.assertIsNone(remote["error"])
        self.assertNotIn("reconcileExisting", remote["payloads"][0])
        self.assertTrue(remote["payloads"][1]["reconcileExisting"])
        self.assertEqual(
            remote["payloads"][0]["clientMutationId"],
            remote["payloads"][1]["clientMutationId"],
        )


if __name__ == "__main__":
    unittest.main()
