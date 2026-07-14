from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from app.schemas import EvaluationRequest, EvaluationResponse

ClaimStatus = Literal["owner", "cached", "in_progress", "conflict"]
SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|authorization|password|token)(\s*[=:]\s*)([^\s,;]+)"
)


def canonical_request(request: EvaluationRequest) -> dict[str, Any]:
    def address(item: Any) -> dict[str, str]:
        normalized = item.address.lower()
        if normalized != "any":
            normalized = str(ipaddress.ip_network(normalized, strict=False))
        return {"address": normalized, "description": item.description}

    return {
        "request_id": request.request_id,
        "sources": sorted(
            (address(item) for item in request.sources),
            key=lambda x: (x["address"], x["description"]),
        ),
        "destinations": sorted(
            (address(item) for item in request.destinations),
            key=lambda x: (x["address"], x["description"]),
        ),
        "protocol": request.protocol.lower(),
        "ports": sorted(
            ({"start": port.start, "end": port.end} for port in request.ports),
            key=lambda x: (x["start"], x["end"]),
        ),
        "request_description": request.request_description,
    }


def request_hash(request: EvaluationRequest) -> str:
    encoded = json.dumps(
        canonical_request(request), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class AuditStore:
    def __init__(self, directory: Path, retention_days: int) -> None:
        self.directory = directory
        self.retention_days = retention_days
        self._completed: dict[str, tuple[str, EvaluationResponse]] = {}
        self._in_progress: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def initialize(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        probe = self.directory / ".write-probe"
        probe.write_text("ready", encoding="utf-8")
        probe.unlink()
        cutoff = datetime.now(UTC).date() - timedelta(days=self.retention_days - 1)
        for path in self.directory.glob("fare-audit-*.jsonl"):
            file_date = _date_from_name(path)
            if file_date and file_date < cutoff:
                path.unlink()
                continue
            self._load_file(path)

    def _load_file(self, path: Path) -> None:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    response = EvaluationResponse.model_validate(record["final_response"])
                    self._completed[record["request_id"]] = (record["input_hash"], response)
                except (json.JSONDecodeError, KeyError, ValueError) as exc:
                    raise ValueError(f"invalid audit record {path}:{line_number}") from exc

    async def claim(
        self, request_id: str, input_hash: str
    ) -> tuple[ClaimStatus, EvaluationResponse | None]:
        async with self._lock:
            completed = self._completed.get(request_id)
            if completed:
                return (
                    ("cached", completed[1])
                    if completed[0] == input_hash
                    else ("conflict", None)
                )
            running_hash = self._in_progress.get(request_id)
            if running_hash is not None:
                return ("in_progress", None) if running_hash == input_hash else ("conflict", None)
            self._in_progress[request_id] = input_hash
            return "owner", None

    async def persist(
        self,
        *,
        request: EvaluationRequest,
        input_hash: str,
        response: EvaluationResponse,
        acl_raw: list[dict[str, Any]],
        model_raw: Any = None,
        exceptions: list[str] | None = None,
    ) -> None:
        record = {
            "audit_id": response.audit_id,
            "request_id": request.request_id,
            "input_hash": input_hash,
            "evaluated_at": datetime.now(UTC).isoformat(),
            "policy_version": response.policy_version,
            "model": response.model.model_dump(mode="json"),
            "normalized_input": _redact(canonical_request(request)),
            "acl_raw": _redact(acl_raw),
            "model_raw": _redact(model_raw),
            "final_response": response.model_dump(mode="json"),
            "exceptions": _redact(exceptions or []),
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        path = self.directory / f"fare-audit-{datetime.now(UTC).date().isoformat()}.jsonl"
        async with self._lock:
            try:
                with path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError:
                self._in_progress.pop(request.request_id, None)
                raise
            self._completed[request.request_id] = (input_hash, response)
            self._in_progress.pop(request.request_id, None)

    async def abandon(self, request_id: str) -> None:
        async with self._lock:
            self._in_progress.pop(request_id, None)


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return SECRET_PATTERN.sub(r"\1\2[REDACTED]", value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if key.lower() in {"api_key", "authorization", "password", "token"}
                else _redact(item)
            )
            for key, item in value.items()
        }
    return value


def _date_from_name(path: Path):
    try:
        return datetime.strptime(path.stem.removeprefix("fare-audit-"), "%Y-%m-%d").date()
    except ValueError:
        return None
