from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

import app.requirement_runner as requirement_runner
from app.config import FareConfig
from app.main import EvaluationServiceError
from app.requirement_runner import evaluate_requirements, run_requirement_batch
from app.schemas import EvaluationRequest
from app.services.requirement_source import (
    ApiRequirementSource,
    LocalRequirementSource,
    RequirementSourceError,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG = FareConfig.load(PROJECT_ROOT / "config/fare.yaml")


def test_local_requirement_source_reads_versioned_batches() -> None:
    source = LocalRequirementSource(
        PROJECT_ROOT / "inputs/network_requirements",
        "*.json",
        False,
    )

    requests = asyncio.run(source.fetch())

    assert [request.request_id for request in requests] == [
        "yaml-bigmodel-example-007"
    ]
    assert requests[0].sources[0].address == "20.1.10.10"


def test_api_requirement_source_uses_yaml_url_token_and_batch_size() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={
                "schema_version": "fare-requirement-batch/v1",
                "requests": [
                    {
                        "request_id": "api-source-001",
                        "sources": [{"address": "20.1.10.10"}],
                        "destinations": [{"address": "16.1.20.20"}],
                        "protocol": "tcp",
                        "ports": [{"start": 443, "end": 443}],
                        "request_description": "API 输入",
                    }
                ],
            },
        )

    source_settings = replace(
        CONFIG.requirement_source,
        mode="api",
        api_url="https://requirements.test/v1/items",
        api_method="GET",
        api_token="test-token",
        batch_size=25,
    )
    source = ApiRequirementSource(
        source_settings,
        transport=httpx.MockTransport(handler),
    )

    requests = asyncio.run(source.fetch())

    assert [request.request_id for request in requests] == ["api-source-001"]
    assert captured == {
        "url": "https://requirements.test/v1/items?batch_size=25",
        "authorization": "Bearer test-token",
    }


def test_api_requirement_source_reuses_and_closes_internal_client() -> None:
    class TrackingTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.requests = 0
            self.closes = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.requests += 1
            return httpx.Response(
                200,
                json={
                    "schema_version": "fare-requirement-batch/v1",
                    "requests": [
                        {
                            "request_id": "api-source-reused",
                            "sources": [{"address": "20.1.10.10"}],
                            "destinations": [{"address": "16.1.20.20"}],
                            "protocol": "tcp",
                            "ports": [{"start": 443, "end": 443}],
                            "request_description": "reused client",
                        }
                    ],
                },
            )

        async def aclose(self) -> None:
            self.closes += 1

    settings = replace(
        CONFIG.requirement_source,
        mode="api",
        api_url="https://requirements.test/v1/items",
    )
    transport = TrackingTransport()
    source = ApiRequirementSource(settings, transport=transport)

    async def fetch_twice_and_close() -> None:
        await source.fetch()
        await source.fetch()
        await source.aclose()
        await source.aclose()

    asyncio.run(fetch_twice_and_close())

    assert transport.requests == 2
    assert transport.closes == 1


def test_api_requirement_source_rejects_invalid_contract() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"items": []})
    )
    settings = replace(
        CONFIG.requirement_source,
        mode="api",
        api_url="https://requirements.test/v1/items",
    )

    with pytest.raises(RequirementSourceError, match="invalid requirement batch"):
        asyncio.run(ApiRequirementSource(settings, transport=transport).fetch())


def test_local_requirement_source_rejects_duplicate_ids(tmp_path: Path) -> None:
    request = {
        "request_id": "duplicate",
        "sources": [{"address": "20.1.10.10"}],
        "destinations": [{"address": "16.1.20.20"}],
        "protocol": "tcp",
        "ports": [{"start": 443, "end": 443}],
        "request_description": "duplicate",
    }
    for name in ("one.json", "two.json"):
        (tmp_path / name).write_text(
            json.dumps(
                {
                    "schema_version": "fare-requirement-batch/v1",
                    "requests": [request],
                }
            ),
            encoding="utf-8",
        )

    with pytest.raises(RequirementSourceError, match="request_id values must be unique"):
        asyncio.run(LocalRequirementSource(tmp_path, "*.json", False).fetch())


def test_requirement_runner_evaluates_local_input_and_writes_result(tmp_path: Path) -> None:
    # 示例需求（办公终端 -> 生产数据库）依赖 OBJECT-001；该规则已从默认规则包
    # 禁用（AC-03 7.4），因此 CLI 批量链路测试改用对象关系 fixture 规则包。
    object_policy = (
        PROJECT_ROOT / "tests/fixtures/policies/network_plan_object_relation"
    )
    source_settings = replace(
        CONFIG.requirement_source,
        output_file=tmp_path / "result.json",
    )
    config = replace(
        CONFIG,
        requirement_source=source_settings,
        settings=replace(
            CONFIG.settings,
            audit_log_dir=tmp_path / "audit",
            llm_client_mode="mock",
            policy_dir=object_policy,
            # OBJECT-001 的显式 object_type 事实只来自 offline 兼容目录
            network_plan_client_mode="offline_catalog",
        ),
    )

    document = run_requirement_batch(config)

    assert document["requirement_source_mode"] == "local"
    assert document["results"][0]["http_status"] == 200
    response = document["results"][0]["response"]
    assert response["decision"] == "待定"
    assert response["items"][0]["reason_code"] == "OBJECT-001"
    assert json.loads((tmp_path / "result.json").read_text(encoding="utf-8")) == document


def test_requirement_evaluation_uses_bounded_concurrency_and_preserves_order() -> None:
    class Response:
        def __init__(self, request_id: str) -> None:
            self.request_id = request_id

        def model_dump(self, *, mode: str) -> dict[str, str]:
            assert mode == "json"
            return {"request_id": self.request_id, "decision": "合规"}

    class RecordingRuntime:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0

        async def evaluate_request(self, request: EvaluationRequest):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.01)
                if request.request_id == "batch-003":
                    raise EvaluationServiceError(409, "conflict", "conflict")
                return Response(request.request_id)
            finally:
                self.active -= 1

    requests = [
        EvaluationRequest.model_validate(
            {
                "request_id": f"batch-{index:03d}",
                "sources": [{"address": "20.1.10.10"}],
                "destinations": [{"address": "16.1.20.20"}],
                "protocol": "tcp",
                "ports": [{"start": 443, "end": 443}],
            }
        )
        for index in range(1, 6)
    ]
    runtime = RecordingRuntime()

    results = asyncio.run(
        evaluate_requirements(runtime, requests, max_concurrency=2)  # type: ignore[arg-type]
    )

    assert runtime.max_active == 2
    assert [result["request_id"] for result in results] == [
        request.request_id for request in requests
    ]
    assert [result["http_status"] for result in results] == [200, 200, 409, 200, 200]
    assert results[2]["response"] == {
        "error": {"code": "conflict", "message": "conflict"}
    }


def test_batch_closes_source_when_runtime_build_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Source:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    source = Source()
    monkeypatch.setattr(
        requirement_runner,
        "build_requirement_source",
        lambda settings: source,
    )

    def fail_runtime_build(settings):
        raise RuntimeError("runtime build failed")

    monkeypatch.setattr(requirement_runner, "build_runtime", fail_runtime_build)

    with pytest.raises(RuntimeError, match="runtime build failed"):
        asyncio.run(requirement_runner.run_requirement_batch_async(CONFIG))

    assert source.closed is True


def test_batch_closes_runtime_even_when_source_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []

    class Source:
        async def aclose(self) -> None:
            closed.append("source")
            raise RuntimeError("source close failed")

    class Runtime:
        async def aclose(self) -> None:
            closed.append("runtime")

    async def completed_batch(config, source, runtime):
        return {"results": []}

    monkeypatch.setattr(
        requirement_runner,
        "build_requirement_source",
        lambda settings: Source(),
    )
    monkeypatch.setattr(requirement_runner, "build_runtime", lambda settings: Runtime())
    monkeypatch.setattr(
        requirement_runner,
        "_run_batch_with_resources",
        completed_batch,
    )

    with pytest.raises(RuntimeError, match="source close failed"):
        asyncio.run(requirement_runner.run_requirement_batch_async(CONFIG))

    assert closed == ["source", "runtime"]
