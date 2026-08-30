from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path

from .orca import OrcaAdapter
from .runner import PhaseStatus, ProductionRunner
from .routing import Router
from .worker_report import report_worker_result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="orca-adaptive")
    result.add_argument("--version", action="store_true", help="show package version and installed commit")
    sub = result.add_subparsers(dest="command", required=True)
    route = sub.add_parser("route", help="classify a task without launching a worker")
    route.add_argument("task")
    route.add_argument("--new-findings")
    launch = sub.add_parser("launch", help="create an Orca Run/Task and supervised Codex worker")
    launch.add_argument("task")
    launch.add_argument("--workspace", type=Path, default=Path.cwd())
    run = sub.add_parser("run", help="execute the complete adaptive RoutingPlan")
    run.add_argument("task")
    run.add_argument("--workspace", type=Path, default=Path.cwd())
    run.add_argument("--timeout-ms", type=int, default=300_000)
    report = sub.add_parser("worker-report", help=argparse.SUPPRESS)
    report.add_argument("--result-json", required=True)
    report.add_argument("--from", dest="from_handle", required=True)
    report.add_argument("--dispatch-capability", required=True)
    report.add_argument("--task-id", required=True)
    report.add_argument("--dispatch-id", required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    # argparse required subcommands and a global --version do not compose, so
    # handle the metadata-only path before parsing commands.
    if argv is None:
        import sys
        argv = sys.argv[1:]
    if argv == ["--version"]:
        try:
            version = importlib.metadata.version("orca-adaptive-routing")
        except importlib.metadata.PackageNotFoundError:
            version = "0.2.0"
        marker = Path(__file__).resolve().parent.parent / "INSTALL_COMMIT"
        commit = marker.read_text().strip() if marker.is_file() else "source-tree"
        print(json.dumps({"package_version": version, "installed_commit": commit}, separators=(",", ":")))
        return 0
    args = parser().parse_args(argv)
    if args.command == "worker-report":
        return report_worker_result(
            result_json=args.result_json,
            from_handle=args.from_handle,
            dispatch_capability=args.dispatch_capability,
            task_id=args.task_id,
            dispatch_id=args.dispatch_id,
        )
    router = Router()
    plan = (
        router.reclassify(args.task, args.new_findings)
        if getattr(args, "new_findings", None)
        else router.classify(args.task)
    )
    if args.command == "route":
        print(json.dumps(plan.to_dict(), indent=2))
        return 0
    if args.command == "run":
        result = ProductionRunner(router=router, timeout_ms=args.timeout_ms).run(
            args.task,
            args.workspace,
        )
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        return 0 if result.final_status is PhaseStatus.SUCCESS else 1
    route = plan.routes[0]
    adapter = OrcaAdapter(args.workspace)
    run_id = adapter.create_run(args.task)
    task_id = adapter.create_task(run_id, f"{route.role}: {args.task[:80]}", args.task)
    try:
        worker = adapter.start_worker(
            run_id,
            task_id,
            route,
        )
    except Exception as exc:
        adapter.fail_task(run_id, task_id, str(exc))
        raise
    print(json.dumps({"run_id": run_id, "task_id": task_id, "dispatch_id": worker.dispatch_id}))
    return 0
