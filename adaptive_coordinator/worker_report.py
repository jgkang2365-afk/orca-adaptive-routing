from __future__ import annotations

import base64
import gzip
import json
import re
import subprocess
from collections.abc import Mapping
from typing import Any, Sequence

from .result_sentinel import (
    B64_END_MARKER,
    GZ64_ENCODED_LIMIT,
    GZ64_MARKER,
    RESULT_LIMIT,
    result_contract_mapping,
)


def _three_sentences(value: object) -> bool:
    return isinstance(value, str) and len(re.findall(r"[.!?](?=\s|$)", value.strip())) == 3


def _compact(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _encoded(value: Mapping[str, Any]) -> str:
    return base64.urlsafe_b64encode(gzip.compress(_compact(value), mtime=0)).decode().rstrip("=")


def _contains_secret(value: Any, secret: str) -> bool:
    if not secret:
        return False
    if isinstance(value, Mapping):
        return any(
            _contains_secret(key, secret) or _contains_secret(child, secret)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_secret(child, secret) for child in value)
    return isinstance(value, str) and secret in value


def _evidence_failure(reason: str) -> dict[str, Any]:
    return {
        "status": "failed",
        "summary": (
            "The complete structured result exceeded the durable terminal envelope. "
            "Required evidence was not truncated or altered. "
            "Coordinator evidence repair is required."
        ),
        "failure_class_hint": "INSUFFICIENT_SUCCESS_EVIDENCE",
        "reason": reason,
        "evidence": ["full required evidence was preserved rather than truncated"],
    }


def report_worker_result(
    *,
    result_json: str,
    from_handle: str,
    dispatch_capability: str,
    task_id: str,
    dispatch_id: str,
    executable: str = "orca-ide",
    runner: Any = subprocess.run,
) -> int:
    try:
        candidate = json.loads(result_json)
    except (TypeError, ValueError, RecursionError):
        candidate = None
    result = result_contract_mapping(candidate)
    if result is None or not _three_sentences(result.get("summary")):
        result = _evidence_failure("worker result is invalid or its summary is not exactly three sentences")
    if _contains_secret(result, dispatch_capability):
        result = _evidence_failure("worker result contains protected lifecycle capability material")
    try:
        result_bytes = _compact(result)
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        result = _evidence_failure("worker result cannot be serialized as bounded UTF-8 JSON")
        result_bytes = _compact(result)
    if len(result_bytes) > RESULT_LIMIT:
        result = _evidence_failure("required result exceeds the 64 KiB decoded evidence limit")
    encoded = _encoded(result)
    if len(encoded) > GZ64_ENCODED_LIMIT:
        result = _evidence_failure(
            f"required result exceeds the {GZ64_ENCODED_LIMIT}-character GZ64 terminal envelope"
        )
        encoded = _encoded(result)
    status = str(result.get("status", "failed")).lower()
    succeeded = status in {"success", "succeeded", "completed", "complete"}
    files = result.get("files_modified")
    files_modified: Sequence[str] = (
        [str(item) for item in files] if isinstance(files, list) else []
    )
    command = [
        executable, "orchestration", "send",
        "--from", from_handle,
        "--dispatch-capability", dispatch_capability,
        "--type", "worker_done",
        "--subject", "Worker result",
        "--body", str(result["summary"]),
        "--payload", _compact(result).decode("utf-8"),
        "--task-id", task_id,
        "--dispatch-id", dispatch_id,
        "--outcome", "succeeded" if succeeded else "failed",
        "--files-modified", ",".join(files_modified),
        "--json",
    ]
    try:
        runner(command, check=False, capture_output=True, text=True, timeout=15.0)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    print(f"{GZ64_MARKER}{encoded}{B64_END_MARKER}")
    return 0
