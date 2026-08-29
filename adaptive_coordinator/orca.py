from __future__ import annotations

import json
import hashlib
import platform
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .models import Authority, Route


class CoordinatorError(RuntimeError):
    pass


class SafetyGateError(CoordinatorError):
    pass


Runner = Callable[[Sequence[str]], dict[str, Any]]
ChangeDetector = Callable[[], Mapping[str, str]]


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
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    try:
        payload = _parse_orca_output(completed.stdout)
    except json.JSONDecodeError as exc:
        if completed.returncode:
            raise CoordinatorError(completed.stderr.strip() or completed.stdout.strip()) from exc
        raise CoordinatorError("Orca did not return JSON") from exc
    if not payload.get("ok", False):
        error = payload.get("error", {})
        raise CoordinatorError(error.get("message", "Orca command failed"))
    return payload["result"]


def _git_changes(workspace: Path) -> Mapping[str, str]:
    completed = subprocess.run(
        ["git", "-C", str(workspace), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=True,
        capture_output=True,
    )
    result: dict[str, str] = {}
    for raw_path in completed.stdout.split(b"\0"):
        if not raw_path:
            continue
        path = raw_path.decode()
        target = workspace / path
        if target.is_file():
            result[path] = hashlib.sha256(target.read_bytes()).hexdigest()
        else:
            result[path] = "<missing>"
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

    def __init__(
        self,
        workspace: str | Path,
        *,
        executable: str = "orca-ide",
        runner: Runner = _default_runner,
        change_detector: ChangeDetector | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.executable = executable
        self.runner = runner
        self.change_detector = change_detector or (lambda: _git_changes(self.workspace))
        self._validate_wsl_workspace()

    def _validate_wsl_workspace(self) -> None:
        if platform.system() != "Linux" or not str(self.workspace).startswith("/home/"):
            raise SafetyGateError("Codex permission enforcement requires a WSL/Linux /home workspace")

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
                "current",
                "--title",
                f"adaptive-{route.phase.value}",
                "--command",
                codex_argv,
                "--json",
            ]
        )
        terminal_handle = terminal["terminal"]["handle"]
        try:
            self.runner(
                [
                    self.executable,
                    "terminal",
                    "wait",
                    "--terminal",
                    terminal_handle,
                    "--for",
                    "tui-idle",
                    "--timeout-ms",
                    "60000",
                    "--json",
                ]
            )
            worker = self.runner(
                [
                    self.executable,
                    "orchestration",
                    "worker-start",
                    "--run",
                    run_id,
                    "--task",
                    task_id,
                    "--terminal",
                    terminal_handle,
                    "--json",
                ]
            )
        except Exception:
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
        delivery = self.runner(
            [
                self.executable,
                "orchestration",
                "check",
                "--run",
                run_id,
                "--wait",
                "--types",
                "worker_done,escalation,question",
                "--timeout-ms",
                str(timeout_ms),
                "--json",
            ]
        )
        messages = delivery.get("messages") or delivery.get("delivery", {}).get("messages") or []
        for message in messages:
            kind = message.get("type") or message.get("message_type")
            dispatch_id = message.get("dispatch_id") or message.get("dispatchId")
            if kind == "worker_done" and dispatch_id == worker.dispatch_id:
                return {"mode": "worker_done", "message": message, "delivery": delivery}
            if kind == "escalation" and dispatch_id == worker.dispatch_id:
                return {"mode": "escalation", "message": message, "delivery": delivery}
        return {"mode": "timeout", "delivery": delivery}

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
            stopped = self.runner(
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
            if "stop_unknown" not in str(exc):
                raise
            stopped = {"state": "stop_unknown", "lastError": "external terminal"}
        if stopped.get("state") == "stop_unknown" and "external" in stopped.get("lastError", ""):
            self.runner(
                [
                    self.executable,
                    "terminal",
                    "close",
                    "--terminal",
                    worker.terminal_handle,
                    "--json",
                ]
            )
        result = json.dumps(
            {
                "summary": summary,
                "settlement": "coordinator_trusted_relay",
                "dispatch_id": worker.dispatch_id,
                "files_modified": list(actual_changes),
            },
            separators=(",", ":"),
        )
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
            closed = self.runner(
                [
                    self.executable,
                    "terminal",
                    "close",
                    "--terminal",
                    worker.terminal_handle,
                    "--json",
                ]
            )
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
