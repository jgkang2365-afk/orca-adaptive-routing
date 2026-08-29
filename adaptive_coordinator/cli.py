from __future__ import annotations

import argparse
import json
from pathlib import Path

from .orca import OrcaAdapter
from .routing import Router


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="orca-adaptive")
    sub = result.add_subparsers(dest="command", required=True)
    route = sub.add_parser("route", help="classify a task without launching a worker")
    route.add_argument("task")
    route.add_argument("--new-findings")
    launch = sub.add_parser("launch", help="create an Orca Run/Task and supervised Codex worker")
    launch.add_argument("task")
    launch.add_argument("--workspace", type=Path, default=Path.cwd())
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    router = Router()
    plan = (
        router.reclassify(args.task, args.new_findings)
        if getattr(args, "new_findings", None)
        else router.classify(args.task)
    )
    if args.command == "route":
        print(json.dumps(plan.to_dict(), indent=2))
        return 0
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
