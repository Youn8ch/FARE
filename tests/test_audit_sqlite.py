from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.schemas import EvaluationRequest, EvaluationResponse
from app.services.audit import AuditStore, request_hash


def _store(directory: Path, *, scope: str = "test-scope") -> AuditStore:
    store = AuditStore(
        directory,
        30,
        config_id="test-config",
        environment="test",
        config_fingerprint=scope,
    )
    store.initialize()
    return store


def _request(request_id: str, *, description: str = "test request") -> EvaluationRequest:
    return EvaluationRequest.model_validate(
        {
            "request_id": request_id,
            "sources": [{"address": "192.0.2.1", "description": "source"}],
            "destinations": [{"address": "198.51.100.2", "description": "destination"}],
            "protocol": "tcp",
            "ports": [{"start": 443, "end": 443}],
            "request_description": description,
        }
    )


def _response(request_id: str, *, audit_id: str) -> EvaluationResponse:
    return EvaluationResponse.model_validate(
        {
            "request_id": request_id,
            "config_id": "test-config",
            "environment": "test",
            "config_fingerprint": "test-scope",
            "decision": "\u5408\u89c4",
            "policy_version": "test-policy",
            "model": {"name": "test-model", "version": "1"},
            "semantic_analysis": {"analyzed_item_ids": []},
            "items": [],
            "acl_analysis": {
                "raw_analysis": "",
                "raw_config": "",
                "extracted_facts": {},
            },
            "audit_id": audit_id,
        }
    )


async def _persist(
    store: AuditStore,
    request: EvaluationRequest,
    response: EvaluationResponse,
    *,
    model_raw=None,
) -> None:
    await store.persist(
        request=request,
        input_hash=request_hash(request),
        response=response,
        acl_raw=[],
        model_raw=model_raw,
    )


def test_concurrent_cross_instance_claim_has_exactly_one_owner(tmp_path: Path) -> None:
    async def scenario() -> None:
        stores = [_store(tmp_path / "audit") for _ in range(8)]
        request = _request("concurrent-claim")
        digest = request_hash(request)

        results = await asyncio.gather(
            *(store.claim(request.request_id, digest) for store in stores)
        )

        assert [status for status, _ in results].count("owner") == 1
        assert [status for status, _ in results].count("in_progress") == 7
        owner = stores[[status for status, _ in results].index("owner")]
        await owner.abandon(request.request_id)

    asyncio.run(scenario())


def test_abandon_cannot_release_another_instance_claim(tmp_path: Path) -> None:
    async def scenario() -> None:
        first = _store(tmp_path / "audit")
        second = _store(tmp_path / "audit")
        request = _request("owner-scoped-abandon")
        digest = request_hash(request)

        assert await first.claim(request.request_id, digest) == ("owner", None)
        await second.abandon(request.request_id)
        assert await second.claim(request.request_id, digest) == ("in_progress", None)

        await first.abandon(request.request_id)
        assert await second.claim(request.request_id, digest) == ("owner", None)
        await second.abandon(request.request_id)

    asyncio.run(scenario())


def test_expired_claim_never_allows_request_id_reuse_with_different_input(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        directory = tmp_path / "audit"
        first = _store(directory)
        second = _store(directory)
        request = _request("expired-conflict")
        conflicting = _request("expired-conflict", description="different input")

        assert await first.claim(request.request_id, request_hash(request)) == ("owner", None)
        with sqlite3.connect(directory / "fare-audit.sqlite3") as connection:
            connection.execute(
                """
                UPDATE audit_requests
                SET updated_at = '2000-01-01T00:00:00+00:00'
                WHERE config_fingerprint = ? AND request_id = ?
                """,
                ("test-scope", request.request_id),
            )

        assert await second.claim(conflicting.request_id, request_hash(conflicting)) == (
            "conflict",
            None,
        )
        assert await first.claim(request.request_id, request_hash(request)) == (
            "in_progress",
            None,
        )
        assert await second.claim(request.request_id, request_hash(request)) == ("owner", None)
        await second.abandon(request.request_id)

    asyncio.run(scenario())


def test_cross_instance_conflict_and_completed_replay(tmp_path: Path) -> None:
    async def scenario() -> None:
        first = _store(tmp_path / "audit")
        second = _store(tmp_path / "audit")
        request = _request("cross-instance-replay")
        conflicting = _request("cross-instance-replay", description="different")
        digest = request_hash(request)

        assert await first.claim(request.request_id, digest) == ("owner", None)
        assert await second.claim(conflicting.request_id, request_hash(conflicting)) == (
            "conflict",
            None,
        )
        response = _response(request.request_id, audit_id="audit-cross-instance")
        await _persist(first, request, response)

        status, cached = await second.claim(request.request_id, digest)
        assert status == "cached"
        assert cached == response

        with sqlite3.connect(tmp_path / "audit" / "fare-audit.sqlite3") as connection:
            row = connection.execute(
                """
                SELECT state, COUNT(*)
                FROM audit_requests
                WHERE config_fingerprint = ? AND request_id = ?
                """,
                ("test-scope", request.request_id),
            ).fetchone()
        assert row == ("completed", 1)

    asyncio.run(scenario())


def test_sqlite_replay_does_not_depend_on_jsonl_archive(tmp_path: Path) -> None:
    async def scenario() -> None:
        directory = tmp_path / "audit"
        first = _store(directory)
        request = _request("sqlite-authoritative")
        digest = request_hash(request)
        response = _response(request.request_id, audit_id="audit-sqlite")

        assert await first.claim(request.request_id, digest) == ("owner", None)
        await _persist(first, request, response)
        for path in directory.glob("fare-audit-*.jsonl"):
            path.unlink()

        restarted = _store(directory)
        status, cached = await restarted.claim(request.request_id, digest)
        assert status == "cached"
        assert cached == response

    asyncio.run(scenario())


def test_completed_restart_does_not_reparse_jsonl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        directory = tmp_path / "audit"
        first = _store(directory)
        request = _request("one-time-bootstrap")
        response = _response(request.request_id, audit_id="audit-one-time")

        assert await first.claim(request.request_id, request_hash(request)) == ("owner", None)
        await _persist(first, request, response)

        restarted = AuditStore(
            directory,
            30,
            config_id="test-config",
            environment="test",
            config_fingerprint="test-scope",
        )

        def unexpected_import(*_args) -> None:
            raise AssertionError("completed SQLite state must not trigger a JSONL rescan")

        monkeypatch.setattr(restarted, "_import_file", unexpected_import)
        restarted.initialize()
        status, cached = await restarted.claim(request.request_id, request_hash(request))
        assert status == "cached"
        assert cached == response

    asyncio.run(scenario())


def test_legacy_jsonl_is_imported_for_restart_replay(tmp_path: Path) -> None:
    async def scenario() -> None:
        directory = tmp_path / "audit"
        directory.mkdir()
        request = _request("legacy-jsonl")
        response = _response(request.request_id, audit_id="audit-legacy")
        record = {
            "audit_id": response.audit_id,
            "request_id": request.request_id,
            "input_hash": request_hash(request),
            "evaluated_at": datetime.now(UTC).isoformat(),
            "final_response": response.model_dump(mode="json"),
        }
        archive = directory / f"fare-audit-{datetime.now(UTC).date().isoformat()}.jsonl"
        archive.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

        store = _store(directory)
        status, cached = await store.claim(request.request_id, request_hash(request))
        assert status == "cached"
        assert cached == response
        with sqlite3.connect(directory / "fare-audit.sqlite3") as connection:
            scope = connection.execute(
                "SELECT config_fingerprint FROM audit_requests WHERE request_id = ?",
                (request.request_id,),
            ).fetchone()
        assert scope == ("test-scope",)

    asyncio.run(scenario())


def test_processing_row_rescans_jsonl_to_recover_interrupted_persist(tmp_path: Path) -> None:
    async def scenario() -> None:
        directory = tmp_path / "audit"
        first = _store(directory)
        request = _request("interrupted-persist")
        response = _response(request.request_id, audit_id="audit-interrupted")
        digest = request_hash(request)
        assert await first.claim(request.request_id, digest) == ("owner", None)

        record = {
            "audit_id": response.audit_id,
            "request_id": request.request_id,
            "input_hash": digest,
            "config_fingerprint": "test-scope",
            "evaluated_at": datetime.now(UTC).isoformat(),
            "final_response": response.model_dump(mode="json"),
        }
        archive = directory / f"fare-audit-{datetime.now(UTC).date().isoformat()}.jsonl"
        archive.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

        restarted = _store(directory)
        status, cached = await restarted.claim(request.request_id, digest)
        assert status == "cached"
        assert cached == response

    asyncio.run(scenario())


def test_concurrent_archival_is_valid_jsonl_and_sqlite_is_redacted(tmp_path: Path) -> None:
    async def scenario() -> None:
        directory = tmp_path / "audit"
        first = _store(directory)
        second = _store(directory)
        first_request = _request("archive-one", description="token=first-secret")
        second_request = _request("archive-two", description="authorization=second-secret")

        assert await first.claim(first_request.request_id, request_hash(first_request)) == (
            "owner",
            None,
        )
        assert await second.claim(second_request.request_id, request_hash(second_request)) == (
            "owner",
            None,
        )
        await asyncio.gather(
            _persist(
                first,
                first_request,
                _response(first_request.request_id, audit_id="audit-one"),
                model_raw={"api_key": "first-secret"},
            ),
            _persist(
                second,
                second_request,
                _response(second_request.request_id, audit_id="audit-two"),
                model_raw={"password": "second-secret"},
            ),
        )

        archive = next(directory.glob("fare-audit-*.jsonl"))
        records = [json.loads(line) for line in archive.read_text(encoding="utf-8").splitlines()]
        assert {record["request_id"] for record in records} == {"archive-one", "archive-two"}

        with sqlite3.connect(directory / "fare-audit.sqlite3") as connection:
            stored = "\n".join(
                row[0]
                for row in connection.execute(
                    "SELECT audit_record_json FROM audit_requests ORDER BY request_id"
                )
            )
        assert "first-secret" not in stored
        assert "second-secret" not in stored
        assert "[REDACTED]" in stored

    asyncio.run(scenario())
