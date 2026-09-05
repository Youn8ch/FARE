from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from app.schemas import EvaluationRequest, EvaluationResponse

ClaimStatus = Literal["owner", "cached", "in_progress", "conflict"]
# Versioned audit namespace: the 0.3.0 runtime opens only this database file,
# so a pre-0.3.0 database (fare-audit.sqlite3) is never read or rewritten and
# stays behind as a read-only archive.
_DATABASE_NAME = "fare-audit-v2.sqlite3"
# Record schema epoch: records written before the ACL removal carry the
# removed response contract and must never be replayed as current responses.
AUDIT_SCHEMA_EPOCH = "fare-audit/v2-no-acl"


class AuditSchemaMismatchError(RuntimeError):
    """A stored audit row was written under a different record schema epoch."""


def _require_record_epoch(request_id: str, audit_record_json: str | None) -> None:
    """Fail closed when a cached row was not written by the current epoch."""

    if audit_record_json is None:
        raise AuditSchemaMismatchError(
            f"audit row for request {request_id!r} has no record payload"
        )
    try:
        record = json.loads(audit_record_json)
    except json.JSONDecodeError as exc:
        raise AuditSchemaMismatchError(
            f"audit row for request {request_id!r} is not readable JSON"
        ) from exc
    epoch = record.get("schema_epoch") if isinstance(record, dict) else None
    if epoch != AUDIT_SCHEMA_EPOCH:
        raise AuditSchemaMismatchError(
            f"audit row for request {request_id!r} was written under schema "
            f"epoch {epoch!r}; the live runtime uses {AUDIT_SCHEMA_EPOCH!r}"
        )
_CLAIM_LEASE = timedelta(hours=1)
_SQLITE_TIMEOUT_SECONDS = 30.0
SECRET_PATTERN = re.compile(
    r"(?i)((?:api[_-]?key|authorization|password|token)\s*[=:]\s*)"
    r"(?:bearer\s+)?([^\s,;]+)"
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
    def __init__(
        self,
        directory: Path,
        retention_days: int,
        *,
        config_id: str = "direct-settings",
        environment: str = "unspecified",
        config_fingerprint: str = "direct-settings",
    ) -> None:
        self.directory = directory
        self.retention_days = retention_days
        self.config_id = config_id
        self.environment = environment
        self.config_fingerprint = config_fingerprint
        self._scope = config_fingerprint
        self._owner_token = str(uuid4())
        self._database_path = self.directory / _DATABASE_NAME

    def initialize(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        probe = self.directory / ".write-probe"
        probe.write_text("ready", encoding="utf-8")
        probe.unlink()
        self._initialize_database()

    def _initialize_database(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_requests (
                    config_fingerprint TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('processing', 'completed')),
                    response_json TEXT,
                    audit_record_json TEXT,
                    owner_token TEXT,
                    claimed_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    PRIMARY KEY (config_fingerprint, request_id),
                    CHECK (
                        (state = 'processing' AND response_json IS NULL
                            AND audit_record_json IS NULL AND completed_at IS NULL)
                        OR
                        (state = 'completed' AND response_json IS NOT NULL
                            AND audit_record_json IS NOT NULL AND completed_at IS NOT NULL)
                    )
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_audit_requests_completed_at
                ON audit_requests (completed_at)
                WHERE state = 'completed'
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO audit_metadata (key, value)
                VALUES ('schema_epoch', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (AUDIT_SCHEMA_EPOCH,),
            )

        cutoff = datetime.now(UTC).date() - timedelta(days=self.retention_days - 1)
        cutoff_timestamp = datetime.combine(cutoff, datetime.min.time(), tzinfo=UTC).isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    DELETE FROM audit_requests
                    WHERE state = 'completed' AND completed_at < ?
                    """,
                    (cutoff_timestamp,),
                )
                bootstrap_complete = connection.execute(
                    """
                    SELECT 1 FROM audit_metadata
                    WHERE key = 'jsonl_bootstrap_complete'
                    """
                ).fetchone()
                has_processing = connection.execute(
                    "SELECT 1 FROM audit_requests WHERE state = 'processing' LIMIT 1"
                ).fetchone()
                import_archives = bootstrap_complete is None or has_processing is not None
                for path in sorted(self.directory.glob("fare-audit-*.jsonl")):
                    file_date = _date_from_name(path)
                    if file_date and file_date < cutoff:
                        path.unlink()
                        continue
                    if import_archives:
                        self._import_file(connection, path)
                connection.execute(
                    """
                    INSERT INTO audit_metadata (key, value)
                    VALUES ('jsonl_bootstrap_complete', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (datetime.now(UTC).isoformat(),),
                )
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise

    def _import_file(self, connection: sqlite3.Connection, path: Path) -> None:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    if record.get("schema_epoch") != AUDIT_SCHEMA_EPOCH:
                        # Historical archive: never imported into the live
                        # cache and never rewritten on disk.
                        continue
                    response = redact_evaluation_response(
                        EvaluationResponse.model_validate(record["final_response"])
                    )
                    # Pre-SQLite archives did not carry a configuration fingerprint.
                    # Import those records into the scope performing the one-time
                    # migration so their replay/conflict semantics remain reachable.
                    scope = str(record.get("config_fingerprint") or self._scope)
                    request_id = str(record["request_id"])
                    input_hash = str(record["input_hash"])
                    completed_at = str(record["evaluated_at"])
                    sanitized_record = redact_value(record)
                    response_json = response.model_dump_json()
                    record_json = json.dumps(
                        sanitized_record, ensure_ascii=False, separators=(",", ":")
                    )
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    raise ValueError(f"invalid audit record {path}:{line_number}") from exc
                connection.execute(
                    """
                    INSERT INTO audit_requests (
                        config_fingerprint,
                        request_id,
                        input_hash,
                        state,
                        response_json,
                        audit_record_json,
                        owner_token,
                        claimed_at,
                        updated_at,
                        completed_at
                    ) VALUES (?, ?, ?, 'completed', ?, ?, NULL, ?, ?, ?)
                    ON CONFLICT(config_fingerprint, request_id) DO UPDATE SET
                        state = 'completed',
                        response_json = excluded.response_json,
                        audit_record_json = excluded.audit_record_json,
                        owner_token = NULL,
                        updated_at = excluded.updated_at,
                        completed_at = excluded.completed_at
                    WHERE audit_requests.state = 'processing'
                        AND audit_requests.input_hash = excluded.input_hash
                    """,
                    (
                        scope,
                        request_id,
                        input_hash,
                        response_json,
                        record_json,
                        completed_at,
                        completed_at,
                        completed_at,
                    ),
                )

    async def claim(
        self, request_id: str, input_hash: str
    ) -> tuple[ClaimStatus, EvaluationResponse | None]:
        return await asyncio.to_thread(self._claim_sync, request_id, input_hash)

    def _claim_sync(
        self, request_id: str, input_hash: str
    ) -> tuple[ClaimStatus, EvaluationResponse | None]:
        now = datetime.now(UTC)
        now_text = now.isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT input_hash, state, response_json, audit_record_json,
                           owner_token, updated_at
                    FROM audit_requests
                    WHERE config_fingerprint = ? AND request_id = ?
                    """,
                    (self._scope, request_id),
                ).fetchone()
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO audit_requests (
                            config_fingerprint,
                            request_id,
                            input_hash,
                            state,
                            response_json,
                            audit_record_json,
                            owner_token,
                            claimed_at,
                            updated_at,
                            completed_at
                        ) VALUES (?, ?, ?, 'processing', NULL, NULL, ?, ?, ?, NULL)
                        """,
                        (
                            self._scope,
                            request_id,
                            input_hash,
                            self._owner_token,
                            now_text,
                            now_text,
                        ),
                    )
                    connection.commit()
                    return "owner", None

                if row["input_hash"] != input_hash:
                    connection.commit()
                    return "conflict", None

                if (
                    row["state"] == "processing"
                    and row["owner_token"] == self._owner_token
                ):
                    # The same live Runtime may have a slow evaluation still using
                    # this Store-level token. Never turn a retry into a second owner;
                    # a restarted Runtime has a new token and may take over the lease.
                    connection.commit()
                    return "in_progress", None

                if row["state"] == "processing" and self._claim_expired(row["updated_at"], now):
                    connection.execute(
                        """
                        UPDATE audit_requests
                        SET input_hash = ?, owner_token = ?, claimed_at = ?, updated_at = ?
                        WHERE config_fingerprint = ? AND request_id = ?
                        """,
                        (
                            input_hash,
                            self._owner_token,
                            now_text,
                            now_text,
                            self._scope,
                            request_id,
                        ),
                    )
                    connection.commit()
                    return "owner", None

                if row["state"] == "processing":
                    connection.commit()
                    return "in_progress", None
                if row["state"] != "completed" or row["response_json"] is None:
                    raise ValueError(
                        f"invalid audit state for request {request_id!r}: {row['state']!r}"
                    )
                _require_record_epoch(request_id, row["audit_record_json"])
                response = EvaluationResponse.model_validate_json(row["response_json"])
                connection.commit()
                return "cached", response
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise

    async def persist(
        self,
        *,
        request: EvaluationRequest,
        input_hash: str,
        response: EvaluationResponse,
        model_raw: Any = None,
        exceptions: list[str] | None = None,
        network_plan_raw: list[dict[str, object]] | None = None,
    ) -> None:
        evaluated_at = datetime.now(UTC)
        record = {
            "schema_epoch": AUDIT_SCHEMA_EPOCH,
            "audit_id": response.audit_id,
            "request_id": request.request_id,
            "input_hash": input_hash,
            "config_id": self.config_id,
            "environment": self.environment,
            "config_fingerprint": self.config_fingerprint,
            "evaluated_at": evaluated_at.isoformat(),
            "policy_version": response.policy_version,
            "model": response.model.model_dump(mode="json"),
            "normalized_input": redact_value(canonical_request(request)),
            "network_plan_raw": redact_value(network_plan_raw or []),
            "model_raw": redact_value(model_raw),
            "final_response": redact_value(response.model_dump(mode="json")),
            "exceptions": redact_value(exceptions or []),
        }
        sanitized_response = EvaluationResponse.model_validate(record["final_response"])
        response_json = sanitized_response.model_dump_json()
        record_json = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        line = record_json + "\n"
        path = self.directory / f"fare-audit-{evaluated_at.date().isoformat()}.jsonl"
        await asyncio.to_thread(
            self._persist_sync,
            request.request_id,
            input_hash,
            response_json,
            record_json,
            evaluated_at.isoformat(),
            path,
            line,
        )

    def _persist_sync(
        self,
        request_id: str,
        input_hash: str,
        response_json: str,
        record_json: str,
        completed_at: str,
        path: Path,
        line: str,
    ) -> None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT input_hash, state, owner_token
                    FROM audit_requests
                    WHERE config_fingerprint = ? AND request_id = ?
                    """,
                    (self._scope, request_id),
                ).fetchone()
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO audit_requests (
                            config_fingerprint,
                            request_id,
                            input_hash,
                            state,
                            response_json,
                            audit_record_json,
                            owner_token,
                            claimed_at,
                            updated_at,
                            completed_at
                        ) VALUES (?, ?, ?, 'processing', NULL, NULL, ?, ?, ?, NULL)
                        """,
                        (
                            self._scope,
                            request_id,
                            input_hash,
                            self._owner_token,
                            completed_at,
                            completed_at,
                        ),
                    )
                    row = connection.execute(
                        """
                        SELECT input_hash, state, owner_token
                        FROM audit_requests
                        WHERE config_fingerprint = ? AND request_id = ?
                        """,
                        (self._scope, request_id),
                    ).fetchone()
                if row["input_hash"] != input_hash:
                    raise ValueError(
                        f"request {request_id!r} was claimed with a different input hash"
                    )
                if row["state"] == "completed":
                    connection.commit()
                    return
                if row["owner_token"] != self._owner_token:
                    raise RuntimeError(f"request {request_id!r} is owned by another evaluator")
                with path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
                updated = connection.execute(
                    """
                    UPDATE audit_requests
                    SET state = 'completed',
                        response_json = ?,
                        audit_record_json = ?,
                        owner_token = NULL,
                        updated_at = ?,
                        completed_at = ?
                    WHERE config_fingerprint = ?
                        AND request_id = ?
                        AND input_hash = ?
                        AND state = 'processing'
                        AND owner_token = ?
                    """,
                    (
                        response_json,
                        record_json,
                        completed_at,
                        completed_at,
                        self._scope,
                        request_id,
                        input_hash,
                        self._owner_token,
                    ),
                )
                if updated.rowcount != 1:
                    raise RuntimeError(f"lost ownership of request {request_id!r}")
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise

    async def abandon(self, request_id: str) -> None:
        await asyncio.to_thread(self._abandon_sync, request_id)

    def _abandon_sync(self, request_id: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    DELETE FROM audit_requests
                    WHERE config_fingerprint = ?
                        AND request_id = ?
                        AND state = 'processing'
                        AND owner_token = ?
                    """,
                    (self._scope, request_id, self._owner_token),
                )
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            timeout=_SQLITE_TIMEOUT_SECONDS,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {int(_SQLITE_TIMEOUT_SECONDS * 1000)}")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @staticmethod
    def _claim_expired(updated_at: str, now: datetime) -> bool:
        try:
            parsed = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"invalid processing claim timestamp: {updated_at!r}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return now - parsed.astimezone(UTC) >= _CLAIM_LEASE


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return SECRET_PATTERN.sub(r"\1[REDACTED]", value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if key.lower() in {"api_key", "authorization", "password", "token"}
                else redact_value(item)
            )
            for key, item in value.items()
        }
    return value


def redact_evaluation_response(response: EvaluationResponse) -> EvaluationResponse:
    """Return the exact sanitized representation used by the API and audit replay."""
    return EvaluationResponse.model_validate(
        redact_value(response.model_dump(mode="json"))
    )


def _date_from_name(path: Path):
    try:
        return datetime.strptime(path.stem.removeprefix("fare-audit-"), "%Y-%m-%d").date()
    except ValueError:
        return None
