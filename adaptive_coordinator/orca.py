from __future__ import annotations

import json
import hashlib
import os
import platform
import re
import shlex
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .models import Authority, Route
from .result_sentinel import final_marked_structured_result, result_contract_mapping


class CoordinatorError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.code = code
        super().__init__(f"{code}: {message}" if code else message)


class SafetyGateError(CoordinatorError):
    pass


class LifecycleSettlementError(CoordinatorError):
    """A fenced worker could not be settled; this is never a model failure."""

    pass


Runner = Callable[[Sequence[str]], dict[str, Any]]
ChangeDetector = Callable[[], Mapping[str, str]]


def _bounded_json_mapping(value: Any, limit: int = 65_536) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str) or len(value.encode(errors="replace")) > limit:
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, RecursionError):
        return None
    return decoded if isinstance(decoded, Mapping) else None


def _parse_orca_output(output: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    cursor = 0
    payloads: list[dict[str, Any]] = []
    while cursor < len(output):
        while cursor < len(output) and output[cursor].isspace():
            cursor += 1
        if cursor >= len(output):
            break
        payload, cursor = decoder.raw_decode(output, cursor)
        if isinstance(payload, dict):
            payloads.append(payload)
    for payload in reversed(payloads):
        if "ok" in payload:
            return payload
    raise json.JSONDecodeError("No Orca response document", output, 0)


def _default_runner(command: Sequence[str]) -> dict[str, Any]:
    # Orca commands cross the Windows/WSL transport boundary.  The CLI's own
    # --timeout-ms has historically failed to return on a broken transport, so
    # enforce a coordinator-side wall-clock bound as well.  Commands without an
    # explicit timeout retain a conservative one-minute operational ceiling.
    wall_timeout = 60.0
    if "--timeout-ms" in command:
        try:
            requested = int(command[command.index("--timeout-ms") + 1]) / 1000
            wall_timeout = max(5.0, requested + 5.0)
        except (ValueError, IndexError):
            pass
    try:
        completed = subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=wall_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise CoordinatorError(
            f"Orca command exceeded coordinator wall-clock limit ({wall_timeout:.3f}s)",
            code="orca_command_timeout",
        ) from exc
    try:
        payload = _parse_orca_output(completed.stdout)
    except json.JSONDecodeError as exc:
        if completed.returncode:
            raise CoordinatorError(completed.stderr.strip() or completed.stdout.strip()) from exc
        raise CoordinatorError("Orca did not return JSON") from exc
    if not payload.get("ok", False):
        error = payload.get("error", {})
        raise CoordinatorError(error.get("message", "Orca command failed"), code=error.get("code"))
    return payload["result"]


def _git_changes(workspace: Path) -> Mapping[str, str]:
    index_output = subprocess.run(
        ["git", "-C", str(workspace), "ls-files", "--stage", "-z"],
        check=True, capture_output=True,
    ).stdout
    status_output = subprocess.run(
        ["git", "-C", str(workspace), "status", "--porcelain=v2", "-z", "--untracked-files=all"],
        check=True, capture_output=True,
    ).stdout

    index: dict[str, list[str]] = {}
    for record in index_output.split(b"\0"):
        if not record or b"\t" not in record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        path = raw_path.decode(errors="surrogateescape")
        index.setdefault(path, []).append(metadata.decode(errors="replace"))

    status_by_path: dict[str, str] = {}
    records = status_output.split(b"\0")
    cursor = 0
    while cursor < len(records):
        record = records[cursor]
        cursor += 1
        if not record:
            continue
        kind = record[:1]
        maxsplit = {b"1": 8, b"2": 9, b"u": 10}.get(kind)
        if maxsplit is not None:
            parts = record.split(b" ", maxsplit)
            if len(parts) <= maxsplit:
                continue
            raw_path = parts[maxsplit]
            signature = record
            if kind == b"2" and cursor < len(records):
                signature += b"\0" + records[cursor]
                cursor += 1
        elif kind in {b"?", b"!"} and record[1:2] == b" ":
            raw_path = record[2:]
            signature = record
        else:
            continue
        status_by_path[raw_path.decode(errors="surrogateescape")] = hashlib.sha256(signature).hexdigest()

    result: dict[str, str] = {}
    for path in sorted(set(index) | set(status_by_path)):
        target = workspace / path
        try:
            metadata = target.lstat()
        except FileNotFoundError:
            worktree = "missing"
        else:
            if stat.S_ISREG(metadata.st_mode):
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
                git_mode = "100755" if metadata.st_mode & stat.S_IXUSR else "100644"
                worktree = f"file:{git_mode}:{digest}"
            elif stat.S_ISLNK(metadata.st_mode):
                digest = hashlib.sha256(os.readlink(target).encode(errors="surrogateescape")).hexdigest()
                worktree = f"symlink:120000:{digest}"
            elif stat.S_ISDIR(metadata.st_mode):
                worktree = "directory:160000"
            else:
                worktree = f"other:{stat.S_IFMT(metadata.st_mode):o}"
        fingerprint = {
            "index": sorted(index.get(path, [])),
            "status": status_by_path.get(path),
            "worktree": worktree,
        }
        result[path] = hashlib.sha256(
            json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    return result


@dataclass(frozen=True)
class WorkerHandle:
    task_id: str
    dispatch_id: str
    terminal_handle: str
    route: Route
    baseline_changes: tuple[tuple[str, str], ...] = ()


class OrcaAdapter:
    """Thin adapter: Coordinator owns decisions; Orca owns lifecycle primitives."""

    # `terminal wait --for tui-idle` can win a short race with Orca's agent
    # registry.  These are readiness retries for the *same* terminal, not new
    # worker attempts and not a reason to change model capability.
    WORKER_START_READINESS_DELAYS = (0.25, 0.5, 1.0, 2.0)
    # TUI-idle only describes the rendered terminal.  After an update prompt
    # is skipped, Orca can need longer to recognize the Codex process as an
    # agent.  Poll the same terminal's structured identity before dispatch;
    # never create a replacement terminal merely because registration lags.
    AGENT_IDENTITY_READINESS_DELAYS = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0)
    LIFECYCLE_CHECK_BACKOFF_SECONDS = 0.05
    LIFECYCLE_CHECK_SLICE_MS = 2_000
    LIFECYCLE_IDLE_PROBE_SLICE_MS = 250

    def __init__(
        self,
        workspace: str | Path,
        *,
        executable: str = "orca-ide",
        runner: Runner = _default_runner,
        change_detector: ChangeDetector | None = None,
        worktree_selector: str | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.executable = executable
        self.runner = runner
        self.change_detector = change_detector or (lambda: _git_changes(self.workspace))
        self._pending_lifecycle_messages: list[Mapping[str, Any]] = []
        self._closed_terminal_handles: set[str] = set()
        self._validate_wsl_workspace()
        self.worktree_selector = worktree_selector or self._resolve_orca_worktree_selector()

    def _validate_wsl_workspace(self) -> None:
        if platform.system() != "Linux" or not str(self.workspace).startswith("/home/"):
            raise SafetyGateError("Codex permission enforcement requires a WSL/Linux /home workspace")

    def _resolve_orca_worktree_selector(self) -> str:
        completed = subprocess.run(
            ["wslpath", "-w", str(self.workspace)],
            check=False,
            capture_output=True,
            text=True,
        )
        windows_path = completed.stdout.strip()
        if completed.returncode or not windows_path.startswith("\\\\"):
            raise SafetyGateError("Could not resolve the WSL workspace to an Orca worktree path")
        return f"path:{windows_path}"

    def codex_command(self, route: Route) -> list[str]:
        if route.authority not in (Authority.READ_ONLY, Authority.WORKSPACE_WRITE):
            raise SafetyGateError("danger-full-access is never selected automatically")
        command = [
            "codex",
            "-C",
            str(self.workspace),
            "--model",
            route.model,
            "-c",
            f'model_reasoning_effort="{route.effort}"',
        ]
        if route.authority is Authority.READ_ONLY:
            command += ["--sandbox", "read-only", "--ask-for-approval", "never"]
        else:
            # Keep authority enforcement explicit. Automatic review can approve
            # an outside-workspace request, while approval=never makes such a
            # request fail without involving the user.
            command += ["--sandbox", "workspace-write", "--ask-for-approval", "never"]
        command += ["--no-alt-screen"]
        return command

    def create_run(self, objective: str) -> str:
        result = self.runner(
            [self.executable, "orchestration", "run-create", "--objective", objective, "--json"]
        )
        return result["run"]["id"]

    def create_task(self, run_id: str, title: str, spec: str) -> str:
        result = self.runner(
            [
                self.executable,
                "orchestration",
                "task-create",
                "--run",
                run_id,
                "--task-title",
                title,
                "--spec",
                spec,
                "--json",
            ]
        )
        return result["task"]["id"]

    def _wait_for_tui_idle(
        self,
        terminal_handle: str,
        timeout_ms: int,
        *,
        allow_update_skip: bool,
    ) -> dict[str, Any]:
        def wait() -> dict[str, Any]:
            return self.runner(
                [
                    self.executable,
                    "terminal",
                    "wait",
                    "--terminal",
                    terminal_handle,
                    "--for",
                    "tui-idle",
                    "--timeout-ms",
                    str(timeout_ms),
                    "--json",
                ]
            )

        wait_response = wait()
        wait_envelope = wait_response.get("wait")
        readiness = dict(wait_envelope) if isinstance(wait_envelope, Mapping) else wait_response
        blocked_reason = readiness.get("blockedReason")
        if blocked_reason == "codex-update-prompt" and allow_update_skip:
            # Keep the installed Codex snapshot unchanged. Option 2 is the
            # prompt's explicit Skip action; option 1 (Update) is never sent.
            self.runner(
                [
                    self.executable,
                    "terminal",
                    "send",
                    "--terminal",
                    terminal_handle,
                    "--text",
                    "2",
                    "--enter",
                    "--json",
                ]
            )
            wait_response = wait()
            wait_envelope = wait_response.get("wait")
            readiness = dict(wait_envelope) if isinstance(wait_envelope, Mapping) else wait_response
            blocked_reason = readiness.get("blockedReason")

        if (
            readiness.get("condition") != "tui-idle"
            or readiness.get("satisfied") is not True
            or blocked_reason
        ):
            reason = blocked_reason or "invalid-tui-idle-readiness"
            raise CoordinatorError(
                f"agent terminal readiness blocked: {reason}",
                code="agent_readiness_blocked",
            )
        return readiness

    def _wait_for_agent_identity(self, terminal_handle: str) -> None:
        for attempt in range(len(self.AGENT_IDENTITY_READINESS_DELAYS) + 1):
            response = self.runner(
                [
                    self.executable,
                    "terminal",
                    "show",
                    "--terminal",
                    terminal_handle,
                    "--json",
                ]
            )
            terminal = response.get("terminal")
            if not isinstance(terminal, Mapping):
                raise CoordinatorError(
                    "terminal show returned no structured terminal identity",
                    code="agent_unconfigured",
                )
            reported_handle = terminal.get("handle")
            if reported_handle != terminal_handle:
                raise CoordinatorError(
                    "terminal show identity does not match the created terminal",
                    code="agent_unconfigured",
                )
            identity = terminal.get("agentIdentity")
            if identity == "codex":
                return
            if identity not in (None, ""):
                raise CoordinatorError(
                    f"terminal is running an unexpected agent: {identity}",
                    code="agent_unconfigured",
                )
            if attempt >= len(self.AGENT_IDENTITY_READINESS_DELAYS):
                break
            time.sleep(self.AGENT_IDENTITY_READINESS_DELAYS[attempt])
            # A version prompt can surface after an earlier idle observation.
            # Recheck the same terminal and select only Codex's explicit Skip
            # action; this never updates Codex or weakens the sandbox.
            self._wait_for_tui_idle(
                terminal_handle,
                10000,
                allow_update_skip=True,
            )
        raise CoordinatorError(
            "terminal is not running a recognized agent",
            code="agent_unconfigured",
        )

    def start_worker(
        self,
        run_id: str,
        task_id: str,
        route: Route,
        *,
        assessment_approved: bool = False,
    ) -> WorkerHandle:
        if route.requires_assessment and not assessment_approved:
            raise SafetyGateError("Critical WRITE requires a completed READ-ONLY assessment")
        baseline_changes = tuple(sorted(self.change_detector().items()))
        codex_argv = shlex.join(self.codex_command(route))
        terminal = self.runner(
            [
                self.executable,
                "terminal",
                "create",
                "--worktree",
                self.worktree_selector,
                "--title",
                f"adaptive-{route.phase.value}",
                "--command",
                codex_argv,
                "--json",
            ]
        )
        terminal_handle = terminal["terminal"]["handle"]
        try:
            self._wait_for_tui_idle(
                terminal_handle,
                60000,
                allow_update_skip=True,
            )
            self._wait_for_agent_identity(terminal_handle)
            worker_start = [
                self.executable,
                "orchestration",
                "worker-start",
                "--run",
                run_id,
                "--task",
                task_id,
                "--terminal",
                terminal_handle,
                "--worktree",
                self.worktree_selector,
                "--json",
            ]
            for attempt in range(len(self.WORKER_START_READINESS_DELAYS) + 1):
                try:
                    worker = self.runner(worker_start)
                    break
                except CoordinatorError as exc:
                    if exc.code != "agent_unconfigured" or attempt >= len(self.WORKER_START_READINESS_DELAYS):
                        raise
                    time.sleep(self.WORKER_START_READINESS_DELAYS[attempt])
                    # Reconfirm that the same terminal is idle before asking
                    # Orca to bind it again. No second terminal is created.
                    self._wait_for_tui_idle(
                        terminal_handle,
                        10000,
                        # A newly started Codex TUI can surface the same
                        # version prompt again while Orca's agent registry is
                        # catching up. Keep the bounded readiness retry on the
                        # same terminal and select only the exact Skip action.
                        allow_update_skip=True,
                    )
        except Exception as start_error:
            try:
                self.runner(
                    [
                        self.executable,
                        "terminal",
                        "close",
                        "--terminal",
                        terminal_handle,
                        "--json",
                    ]
                )
            except Exception as close_error:
                # Keep the original structured code so the Coordinator cannot
                # mistake readiness or placement for model failure, while also
                # making a possible residual terminal explicit.
                code = start_error.code if isinstance(start_error, CoordinatorError) else None
                raise CoordinatorError(
                    f"{start_error}; created terminal cleanup failed: {close_error}",
                    code=code,
                ) from start_error
            raise
        dispatch_id = worker.get("dispatchId") or worker["dispatch"]["id"]
        return WorkerHandle(task_id, dispatch_id, terminal_handle, route, baseline_changes)

    def fail_task(self, run_id: str, task_id: str, reason: str) -> None:
        self.runner(
            [
                self.executable,
                "orchestration",
                "task-update",
                "--run",
                run_id,
                "--id",
                task_id,
                "--status",
                "failed",
                "--result",
                json.dumps({"reason": reason}, separators=(",", ":")),
                "--json",
            ]
        )

    def settle_escalation(self, run_id: str, worker: WorkerHandle, finding: str) -> None:
        """Fence the reporting Dispatch before the Coordinator reclassifies it."""
        try:
            self._fence_and_close_owned(worker)
            self.fail_task(run_id, worker.task_id, f"superseded by Coordinator escalation: {finding}")
        except CoordinatorError as exc:
            raise LifecycleSettlementError(f"escalation settlement failed after fencing: {exc}") from exc

    def fence(self, worker: WorkerHandle) -> dict[str, Any]:
        """Stop a Dispatch before its Task is updated or its terminal released."""
        try:
            return self.runner(
                [
                    self.executable,
                    "orchestration",
                    "worker-stop",
                    "--dispatch",
                    worker.dispatch_id,
                    "--json",
                ]
            )
        except CoordinatorError as exc:
            message = str(exc).lower()
            external_terminal = re.fullmatch(
                r"(?:stop_unknown:\s*)?(?:"
                r"external terminal|"
                r"worker (?:uses|is (?:attached to|backed by)) an? external terminal|"
                r"dispatch uses an? external terminal"
                r")(?:[.!]|: [^\n]+)?",
                message.strip(),
            )
            if exc.code == "stop_unknown" and external_terminal:
                return {"state": "stop_unknown", "lastError": "external terminal"}
            inactive_external = re.fullmatch(
                rf"(?:dispatch_inactive:\s*)?dispatch "
                rf"{re.escape(worker.dispatch_id.lower())} cannot stop from stop_unknown\.",
                message.strip(),
            )
            if exc.code == "dispatch_inactive" and inactive_external:
                # Orca reports this exact state for an inactive Dispatch that
                # is still backed by the external terminal owned by this
                # WorkerHandle. The anchored Dispatch identity is mandatory.
                return {"state": "stop_unknown", "lastError": "external terminal"}
            settled = re.fullmatch(
                r"(?:stop_unknown:\s*)?(?:"
                r"no active worker(?:\s+for(?:\s+this)?\s+dispatch)?|"
                r"worker (?:is )?already (?:settled|stopped|completed)|"
                r"dispatch ctx_[a-z0-9]+ (?:is )?(?:already )?(?:settled|stopped|completed)"
                r")[.!]?",
                message.strip(),
            )
            if exc.code == "stop_unknown" and settled:
                return {"state": "already_settled"}
            raise

    def _close_terminal_once(self, terminal_handle: str) -> dict[str, Any]:
        if terminal_handle in self._closed_terminal_handles:
            return {"closed": True, "alreadyClosed": True}
        closed = self.runner(
            [
                self.executable,
                "terminal",
                "close",
                "--terminal",
                terminal_handle,
                "--json",
            ]
        )
        self._closed_terminal_handles.add(terminal_handle)
        return closed

    def _fence_and_close_owned(self, worker: WorkerHandle) -> dict[str, Any]:
        stopped = self.fence(worker)
        self._close_terminal_once(worker.terminal_handle)
        return stopped

    def fail_worker(self, run_id: str, worker: WorkerHandle, reason: str) -> None:
        try:
            self._fence_and_close_owned(worker)
            self.fail_task(run_id, worker.task_id, reason)
        except CoordinatorError as exc:
            raise LifecycleSettlementError(f"failure settlement failed after fencing: {exc}") from exc

    def read_result(self, worker: WorkerHandle, limit: int = 200) -> dict[str, Any]:
        return self.runner(
            [
                self.executable,
                "orchestration",
                "worker-read",
                "--dispatch",
                worker.dispatch_id,
                "--source",
                "auto",
                "--limit",
                str(limit),
                "--json",
            ]
        )

    def wait_for_completion(self, run_id: str, worker: WorkerHandle, timeout_ms: int) -> dict[str, Any]:
        deadline = time.monotonic() + max(timeout_ms, 0) / 1000
        delivery: dict[str, Any] = {}
        delivery_to_ack: str | None = None

        def acknowledge(delivery_id: str) -> None:
            # Acknowledge only the fully inspected FIFO batch. `--peek` leaves
            # the next batch unread for this or another logical Gate.
            self.runner(
                [
                    self.executable, "orchestration", "check",
                    "--run", run_id, "--ack", delivery_id, "--peek",
                    "--types", "worker_done,escalation,question", "--json",
                ]
            )

        def identity(message: Mapping[str, Any]) -> tuple[str, str | None] | None:
            embedded = _bounded_json_mapping(message.get("payload")) or {}
            dispatch_ids = tuple(
                str(value) for value in (
                    message.get("dispatch_id"), message.get("dispatchId"),
                    embedded.get("dispatch_id"), embedded.get("dispatchId"),
                ) if value is not None
            )
            task_ids = tuple(
                str(value) for value in (
                    message.get("task_id"), message.get("taskId"),
                    embedded.get("task_id"), embedded.get("taskId"),
                ) if value is not None
            )
            if (not dispatch_ids or len(set(dispatch_ids)) != 1
                    or len(set(task_ids)) > 1):
                return None
            return dispatch_ids[0], task_ids[0] if task_ids else None

        def inspect_batch(
            messages: Sequence[Any],
        ) -> Mapping[str, Any] | None:
            selected: list[Mapping[str, Any]] = []
            for candidate in messages:
                if not isinstance(candidate, Mapping):
                    continue
                kind = candidate.get("type") or candidate.get("message_type")
                if kind not in {"worker_done", "escalation", "question"}:
                    continue
                message_identity = identity(candidate)
                if message_identity is None:
                    continue
                dispatch_id, task_id = message_identity
                if dispatch_id == worker.dispatch_id and (
                    task_id is None or task_id == worker.task_id
                ):
                    selected.append(candidate)
                else:
                    self._pending_lifecycle_messages.append(candidate)
            if len(self._pending_lifecycle_messages) > 256:
                raise CoordinatorError(
                    "pending lifecycle inbox exceeded its bounded capacity",
                    code="lifecycle_inbox_overflow",
                )
            if len(selected) > 1:
                raise CoordinatorError(
                    "multiple lifecycle outcomes target the same Dispatch in one batch",
                    code="lifecycle_batch_conflict",
                )
            return selected[0] if selected else None

        def probe_terminal_result(
            remaining_seconds: float,
        ) -> tuple[dict[str, Any], Mapping[str, Any] | None]:
            probe_timeout_ms = min(
                self.LIFECYCLE_IDLE_PROBE_SLICE_MS,
                max(1, int(remaining_seconds * 1000)),
            )
            probe_response = self.runner(
                [
                    self.executable, "terminal", "wait",
                    "--terminal", worker.terminal_handle,
                    "--for", "tui-idle",
                    "--timeout-ms", str(probe_timeout_ms), "--json",
                ]
            )
            probe_envelope = probe_response.get("wait")
            readiness = (dict(probe_envelope)
                         if isinstance(probe_envelope, Mapping) else probe_response)
            if (
                readiness.get("condition") == "tui-idle"
                and readiness.get("satisfied") is True
                and readiness.get("blockedReason") is None
            ):
                candidate = self.read_result(worker)
                result, _ = final_marked_structured_result(candidate)
                return readiness, result
            return readiness, None

        def completion_from(
            message: Mapping[str, Any], source: Mapping[str, Any],
        ) -> dict[str, Any]:
            kind = message.get("type") or message.get("message_type")
            if kind == "worker_done":
                structured = (
                    result_contract_mapping(_bounded_json_mapping(message.get("payload")))
                    or result_contract_mapping(_bounded_json_mapping(message.get("body")))
                )
                completion: dict[str, Any] = {
                    "mode": "worker_done", "message": message,
                    "result": structured, "delivery": source,
                }
                if structured is None:
                    while True:
                        remaining_seconds = deadline - time.monotonic()
                        if remaining_seconds <= 0:
                            completion["safe_to_read"] = False
                            break
                        readiness, result = probe_terminal_result(remaining_seconds)
                        if result is not None:
                            completion["result"] = result
                            completion["readiness"] = readiness
                            break
                        remaining_seconds = deadline - time.monotonic()
                        if remaining_seconds <= 0:
                            completion["safe_to_read"] = False
                            break
                        time.sleep(min(
                            self.LIFECYCLE_CHECK_BACKOFF_SECONDS,
                            remaining_seconds,
                        ))
                return completion
            return {"mode": str(kind), "message": message, "delivery": source}

        pending = list(getattr(self, "_pending_lifecycle_messages", []))
        self._pending_lifecycle_messages = []
        pending_match = inspect_batch(pending)
        if pending_match is not None:
            return completion_from(pending_match, {"source": "pending_inbox"})

        while True:
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                break
            check_timeout_ms = min(
                self.LIFECYCLE_CHECK_SLICE_MS,
                max(1, int(remaining_seconds * 1000)),
            )
            check_command = [
                self.executable, "orchestration", "check", "--run", run_id,
            ]
            if delivery_to_ack:
                check_command += ["--ack", delivery_to_ack]
            check_command += [
                "--wait", "--types", "worker_done,escalation,question",
                "--timeout-ms", str(max(1, check_timeout_ms)), "--json",
            ]
            delivery = self.runner(check_command)
            delivery_to_ack = None
            delivery_id = delivery.get("deliveryId") or delivery.get("delivery_id")
            messages = delivery.get("messages") or delivery.get("delivery", {}).get("messages") or []
            selected = inspect_batch(messages)
            if selected is not None:
                if delivery_id:
                    acknowledge(str(delivery_id))
                return completion_from(selected, delivery)

            # Orca check replays the oldest unacknowledged FIFO batch. Carry
            # its acknowledgement into the next check only after every message
            # in this batch was inspected and rejected for this worker.
            if delivery_id:
                delivery_to_ack = str(delivery_id)

            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                break
            if not messages:
                readiness, result = probe_terminal_result(remaining_seconds)
                if result is not None:
                    if delivery_to_ack:
                        acknowledge(delivery_to_ack)
                    return {
                        "mode": "timeout", "delivery": delivery,
                        "safe_to_read": True, "readiness": readiness,
                        "result": result,
                    }

            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                break
            time.sleep(min(self.LIFECYCLE_CHECK_BACKOFF_SECONDS, remaining_seconds))

        # Orca requires a positive terminal-wait timeout. Once the shared
        # deadline is exhausted there is no safe budget left to prove idle, so
        # explicitly forbid terminal fallback rather than reading a live TUI.
        if delivery_to_ack:
            acknowledge(delivery_to_ack)
        return {"mode": "timeout", "delivery": delivery, "safe_to_read": False}

    def trusted_relay(
        self,
        run_id: str,
        worker: WorkerHandle,
        summary: str,
        *,
        files_modified: Sequence[str],
    ) -> None:
        baseline = dict(worker.baseline_changes)
        current = dict(self.change_detector())
        actual_changes = tuple(
            sorted(path for path in set(baseline) | set(current) if baseline.get(path) != current.get(path))
        )
        reported_changes = tuple(files_modified)
        if set(actual_changes) != set(reported_changes):
            raise SafetyGateError("Relay file evidence does not match the workspace Git status")
        if worker.route.authority is Authority.READ_ONLY and actual_changes:
            raise SafetyGateError("READ-ONLY relay rejected because files were modified")
        try:
            self._fence_and_close_owned(worker)
        except CoordinatorError as exc:
            raise LifecycleSettlementError(f"trusted relay fencing failed: {exc}") from exc
        result = json.dumps(
            {
                "summary": summary,
                "settlement": "coordinator_trusted_relay",
                "dispatch_id": worker.dispatch_id,
                "files_modified": list(actual_changes),
            },
            separators=(",", ":"),
        )
        try:
            self.runner(
                [
                    self.executable,
                    "orchestration",
                    "task-update",
                    "--run",
                    run_id,
                    "--id",
                    worker.task_id,
                    "--status",
                    "completed",
                    "--result",
                    result,
                    "--json",
                ]
            )
        except CoordinatorError as exc:
            raise LifecycleSettlementError(f"trusted relay task settlement failed: {exc}") from exc

    def release(self, worker: WorkerHandle) -> dict[str, Any]:
        result = self.runner(
            [
                self.executable,
                "orchestration",
                "worker-release",
                "--dispatch",
                worker.dispatch_id,
                "--json",
            ]
        )
        if result.get("state") == "retained" and result.get("reason") == "external_terminal":
            closed = self._close_terminal_once(worker.terminal_handle)
            finalized = self.runner(
                [
                    self.executable,
                    "orchestration",
                    "worker-release",
                    "--dispatch",
                    worker.dispatch_id,
                    "--json",
                ]
            )
            return {"state": finalized.get("state"), "release": finalized, "terminal": closed}
        return result
