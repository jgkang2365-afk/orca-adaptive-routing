from __future__ import annotations

import base64
import binascii
import json
import re
import zlib
from typing import Any, Iterator, Mapping, Sequence


RESULT_LIMIT = 65_536
GZ64_ENCODED_LIMIT = 4_096
B64_MARKER = "ADAPTIVE_RESULT_B64:"
GZ64_MARKER = "ADAPTIVE_RESULT_GZ64:"
B64_END_MARKER = ":END_ADAPTIVE_RESULT"
JSON_MARKER = "ADAPTIVE_RESULT_JSON:"


def _terminal_text(payload: Mapping[str, Any], limit: int) -> str:
    chunks: list[str] = []
    remaining = limit

    def collect(value: Any, key: str = "") -> None:
        nonlocal remaining
        if remaining <= 0:
            return
        if isinstance(value, Mapping):
            for child_key, child in value.items():
                collect(child, str(child_key).lower())
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for child in value:
                collect(child, key)
        elif isinstance(value, str) and key in {
            "tail", "preview", "output", "finaloutput", "body", "text",
        }:
            bounded = value[-remaining:]
            chunks.append(bounded)
            remaining -= len(bounded)

    collect(payload)
    text = "\n".join(chunks)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) > limit:
        text = encoded[-limit:].decode("utf-8", errors="ignore")
    return text


def result_contract_mapping(value: Any) -> Mapping[str, Any] | None:
    if not (isinstance(value, Mapping) and value.get("status") is not None):
        return None
    if not any(key in value for key in (
        "conclusion", "evidence", "files_modified", "tests_run",
        "verification_outcome", "risks", "write_ready",
    )):
        return None
    return value


def _messages(payload: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    for key in ("result", "worker_result", "message", "worker", "delivery", "payload"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            yield from _messages(value)
    yield payload


def explicit_orca_failure_status(payload: Mapping[str, Any]) -> str | None:
    failed = {"failed", "failure", "error", "blocked", "cancelled", "canceled", "crashed"}
    for candidate in _messages(payload):
        value = candidate.get("status")
        statuses: list[str] = []
        if isinstance(value, Mapping):
            statuses.extend(str(item).lower() for item in value.values())
        elif value is not None:
            statuses.append(str(value).lower())
        match = next((status for status in statuses if status in failed), None)
        if match:
            return match
    return None


def _trailing_error(trailing: str, marker_name: str) -> str | None:
    # TUI color/reset/control sequences are ignorable; printable output is not.
    trailing = re.sub(
        r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", "", trailing,
    )
    semantic_failure = (
        r"\b[1-9][0-9]*\s+(?:tests?\s+)?failed\b",
        r"\bassertion(?:error| failed| failure)?\b",
        r"\bworker crashed\b", r"\bfatal error\b", r"\btraceback\b",
        r"\bprocess exited with code\s+[1-9][0-9]*\b",
    )
    transport_noise = (
        r"transport (?:error|failure):\s*(?:wsl\s+)?vsock(?: endpoint)?\s+(?:unavailable|failed|refused)",
        r"worker_done delivery (?:failed|failure|error)(?::\s*(?:wsl\s+)?vsock(?: endpoint)?\s+(?:unavailable|failed|refused))?",
        r"lifecycle transport (?:failed|failure|error)(?::\s*(?:wsl\s+)?vsock(?: endpoint)?\s+(?:unavailable|failed|refused))?",
        r"(?:failed|unable) to (?:send|deliver) worker_done via (?:wsl\s+)?vsock",
    )
    tui_chrome = (
        r"[─━═╭╮╰╯│\s]+",
        r"› Ask Codex to do anything(?:\s+.*)?",
    )
    trailing_lines = [line.strip() for line in trailing.splitlines() if line.strip()]
    if any(
        any(re.search(pattern, line, flags=re.IGNORECASE) for pattern in semantic_failure)
        or not any(re.fullmatch(pattern, line, flags=re.IGNORECASE)
                   for pattern in (*transport_noise, *tui_chrome))
        for line in trailing_lines
    ):
        return f"substantive output exists after final {marker_name} marker"
    return None


def _decode_b64_result(
    text: str, marked: int, limit: int,
) -> tuple[Mapping[str, Any] | None, str | None]:
    start = marked + len(B64_MARKER)
    end = text.find(B64_END_MARKER, start)
    if end < 0:
        return None, "final ADAPTIVE_RESULT_B64 marker is malformed or truncated"
    encoded = re.sub(r"\s+", "", text[start:end])
    if not encoded or len(encoded.encode("ascii", errors="replace")) > limit:
        return None, "final ADAPTIVE_RESULT_B64 marker is malformed or truncated"
    if not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", encoded):
        return None, "final ADAPTIVE_RESULT_B64 marker is malformed or truncated"
    padded = encoded + ("=" * (-len(encoded) % 4))
    try:
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
        if len(decoded) > limit:
            return None, "final ADAPTIVE_RESULT_B64 exceeds the bounded result limit"
        value = json.loads(decoded.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError, RecursionError):
        return None, "final ADAPTIVE_RESULT_B64 marker is malformed or truncated"
    trailing = text[end + len(B64_END_MARKER):]
    error = _trailing_error(trailing, "ADAPTIVE_RESULT_B64")
    if error:
        return None, error
    result = result_contract_mapping(value)
    if result is None:
        return None, "final ADAPTIVE_RESULT_B64 does not satisfy the worker result contract"
    return result, None


def _decode_gz64_result(
    text: str, marked: int, limit: int,
) -> tuple[Mapping[str, Any] | None, str | None]:
    start = marked + len(GZ64_MARKER)
    end = text.find(B64_END_MARKER, start)
    label = "ADAPTIVE_RESULT_GZ64"
    if end < 0:
        return None, f"final {label} marker is malformed or truncated"
    encoded = re.sub(r"\s+", "", text[start:end])
    if not encoded or len(encoded.encode("ascii", errors="replace")) > GZ64_ENCODED_LIMIT:
        return None, f"final {label} marker is malformed or truncated"
    if not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", encoded):
        return None, f"final {label} marker is malformed or truncated"
    padded = encoded + ("=" * (-len(encoded) % 4))
    try:
        compressed = base64.b64decode(padded, altchars=b"-_", validate=True)
        inflater = zlib.decompressobj(wbits=31)
        decoded = inflater.decompress(compressed, limit + 1)
        if (len(decoded) > limit or not inflater.eof or inflater.unconsumed_tail
                or inflater.unused_data):
            return None, f"final {label} exceeds the bounded result limit"
        value = json.loads(decoded.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError, zlib.error, RecursionError):
        return None, f"final {label} marker is malformed or truncated"
    error = _trailing_error(text[end + len(B64_END_MARKER):], label)
    if error:
        return None, error
    result = result_contract_mapping(value)
    if result is None:
        return None, f"final {label} does not satisfy the worker result contract"
    return result, None


def _decode_json_result(
    text: str, marked: int,
) -> tuple[Mapping[str, Any] | None, str | None]:
    start = marked + len(JSON_MARKER)
    while start < len(text) and text[start].isspace():
        start += 1
    try:
        value, end = json.JSONDecoder().raw_decode(text, start)
    except (json.JSONDecodeError, RecursionError):
        return None, "final ADAPTIVE_RESULT_JSON marker is malformed or truncated"
    error = _trailing_error(text[end:], "ADAPTIVE_RESULT_JSON")
    if error:
        return None, error
    result = result_contract_mapping(value)
    if result is None:
        return None, "final ADAPTIVE_RESULT_JSON does not satisfy the worker result contract"
    return result, None


def final_marked_structured_result(
    payload: Mapping[str, Any], limit: int = RESULT_LIMIT,
) -> tuple[Mapping[str, Any] | None, str | None]:
    """Decode the last framed/legacy marker at the visible output boundary."""
    text = _terminal_text(payload, limit)
    b64_marked = text.rfind(B64_MARKER)
    gz64_marked = text.rfind(GZ64_MARKER)
    json_marked = text.rfind(JSON_MARKER)
    if b64_marked < 0 and gz64_marked < 0 and json_marked < 0:
        return None, None
    newest = max(b64_marked, gz64_marked, json_marked)
    if newest == gz64_marked:
        result, error = _decode_gz64_result(text, gz64_marked, limit)
    elif newest == b64_marked:
        result, error = _decode_b64_result(text, b64_marked, limit)
    else:
        result, error = _decode_json_result(text, json_marked)
    if result is None or error:
        return result, error
    result_status = result.get("status")
    if isinstance(result_status, Mapping):
        result_status = result_status.get("worker") or result_status.get("terminal")
    result_failed = str(result_status).lower() in {
        "failed", "failure", "error", "blocked", "cancelled", "canceled", "crashed",
    }
    explicit_failure = explicit_orca_failure_status(payload)
    if explicit_failure and not result_failed:
        return None, f"Orca status {explicit_failure}"
    return result, None
