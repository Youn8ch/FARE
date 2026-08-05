from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import build_runtime, create_app
from app.schemas import EvaluationRequest
from app.services.llm_client import LlmClient, LlmClientProtocol, LlmDependencyError
from tests.conftest import payload
from tests.helpers.llm import RecordingLlmClient

pytestmark = pytest.mark.llm_http


def _http_client(
    transport: httpx.AsyncBaseTransport,
    *,
    retries: int = 1,
    semantic_timeout: float = 1,
) -> LlmClient:
    return LlmClient(
        mode="http",
        base_url="https://model.invalid/v1",
        model="test-model",
        api_key=None,
        mock_file=None,
        semantic_timeout=semantic_timeout,
        explanation_timeout=semantic_timeout,
        max_correction_retries=retries,
        transport=transport,
    )


def _semantic_payload() -> dict[str, Any]:
    return {
        "request_id": "http-contract",
        "items": [{"item_id": "http-contract-001"}],
    }


def _completion(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": content}}]},
    )


def test_http_valid_first_attempt_uses_injected_transport() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        content = json.dumps({"analyzed_item_ids": ["http-contract-001"]})
        return _completion(content)

    client = _http_client(httpx.MockTransport(handler))
    async def call():
        response, raw = await client.analyze(_semantic_payload())
        return response, raw, client.consume_completion_trace()

    response, _, trace = asyncio.run(call())

    assert response.analyzed_item_ids == ["http-contract-001"]
    assert len(requests) == 1
    assert requests[0].url == "https://model.invalid/v1/chat/completions"
    assert trace is not None
    assert trace["attempts"] == 1


@pytest.mark.parametrize(
    "first_content",
    [
        "not-json",
        json.dumps({"analyzed_item_ids": "not-a-list"}),
    ],
    ids=["invalid-json", "invalid-schema"],
)
def test_http_contract_correction_then_valid(first_content: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        content = (
            first_content
            if len(requests) == 1
            else json.dumps({"analyzed_item_ids": ["http-contract-001"]})
        )
        return _completion(content)

    client = _http_client(httpx.MockTransport(handler))

    async def call():
        response, raw = await client.analyze(_semantic_payload())
        return response, raw, client.consume_completion_trace()

    response, _, trace = asyncio.run(call())

    assert response.analyzed_item_ids == ["http-contract-001"]
    assert len(requests) == 2
    assert trace is not None
    assert trace["attempts"] == 2
    assert trace["corrections"] == 1
    assert trace["status"] == "passed"


def test_http_malformed_completion_envelope_uses_correction() -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            return httpx.Response(200, json={"choices": []})
        return _completion(json.dumps({"analyzed_item_ids": ["http-contract-001"]}))

    client = _http_client(httpx.MockTransport(handler))
    response, _ = asyncio.run(client.analyze(_semantic_payload()))

    assert response.analyzed_item_ids == ["http-contract-001"]
    assert request_count == 2


def test_http_malformed_completion_envelope_twice_fails() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"choices": []})

    client = _http_client(httpx.MockTransport(handler), retries=1)
    with pytest.raises(LlmDependencyError, match="schema validation"):
        asyncio.run(client.analyze(_semantic_payload()))

    assert len(requests) == 2


@pytest.mark.parametrize("status_code", [429, 500])
def test_http_status_errors_are_not_correction_retried(status_code: int) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, json={"error": "provider failure"})

    client = _http_client(httpx.MockTransport(handler), retries=1)

    async def call():
        try:
            await client.analyze(_semantic_payload())
        except LlmDependencyError as exc:
            return exc, client.consume_completion_trace()
        raise AssertionError("expected LLM dependency failure")

    error, trace = asyncio.run(call())

    assert "dependency request failed" in str(error)
    assert len(requests) == 1
    assert trace is not None
    assert trace["attempts"] == 1
    assert trace["corrections"] == 0
    assert trace["status"] == "failed"


def test_http_transport_timeout_is_not_correction_retried() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadTimeout("provider timeout", request=request)

    client = _http_client(httpx.MockTransport(handler), retries=1)
    with pytest.raises(LlmDependencyError, match="dependency request failed"):
        asyncio.run(client.analyze(_semantic_payload()))

    assert len(requests) == 1


@pytest.mark.parametrize("api_key", [None, "SENTINEL_PROVIDER_KEY"])
def test_http_request_auth_and_deterministic_parameters(api_key: str | None) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _completion(json.dumps({"analyzed_item_ids": ["http-contract-001"]}))

    client = _http_client(httpx.MockTransport(handler))
    client.api_key = api_key
    asyncio.run(client.analyze(_semantic_payload()))

    request = requests[0]
    body = json.loads(request.content)
    assert body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}
    if api_key:
        assert request.headers["Authorization"] == f"Bearer {api_key}"
    else:
        assert "Authorization" not in request.headers


def test_http_public_error_does_not_expose_api_key(caplog) -> None:
    secret = "SENTINEL_PROVIDER_KEY"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"provider echoed {secret}")

    client = _http_client(httpx.MockTransport(handler))
    client.api_key = secret
    with pytest.raises(LlmDependencyError) as raised:
        asyncio.run(client.analyze(_semantic_payload()))

    assert secret not in str(raised.value)
    assert secret not in caplog.text


def test_http_invalid_schema_twice_uses_one_correction_request() -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return _completion("not-json")

    client = _http_client(httpx.MockTransport(handler), retries=1)
    with pytest.raises(LlmDependencyError):
        asyncio.run(client.analyze(_semantic_payload()))

    assert request_count == 2


def test_http_zero_correction_retries_makes_one_request() -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return _completion("not-json")

    client = _http_client(httpx.MockTransport(handler), retries=0)
    with pytest.raises(LlmDependencyError):
        asyncio.run(client.analyze(_semantic_payload()))

    assert request_count == 1


def test_http_correction_retry_shares_one_total_timeout_budget() -> None:
    request_timeouts: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        request_timeouts.append(float(request.extensions["timeout"]["read"]))
        if len(request_timeouts) == 1:
            return _completion("not-json")
        return _completion(json.dumps({"analyzed_item_ids": ["http-contract-001"]}))

    client = _http_client(
        httpx.MockTransport(handler),
        retries=1,
        semantic_timeout=0.08,
    )
    response, _ = asyncio.run(client.analyze(_semantic_payload()))

    assert response.analyzed_item_ids == ["http-contract-001"]
    assert len(request_timeouts) == 2
    assert 0 < request_timeouts[1] <= request_timeouts[0]
    assert request_timeouts[0] <= 0.08 + 1e-6


@pytest.mark.parametrize("stage", ["semantic", "explanation"])
def test_mock_schema_validation_uses_dependency_error(
    stage: str, tmp_path: Path
) -> None:
    fixture = tmp_path / f"invalid-{stage}.json"
    fixture.write_text(
        json.dumps({"version": "invalid-schema", "default": {stage: {}}}),
        encoding="utf-8",
    )
    client = LlmClient(
        mode="mock",
        base_url=None,
        model=None,
        api_key=None,
        mock_file=fixture,
        semantic_timeout=1,
        explanation_timeout=1,
        max_correction_retries=1,
    )
    llm_payload = {"request_id": "invalid-mock", "items": []}
    operation = (
        client.analyze(llm_payload)
        if stage == "semantic"
        else client.explain(llm_payload)
    )

    with pytest.raises(LlmDependencyError, match=f"mock LLM {stage}"):
        asyncio.run(operation)


def test_recording_client_returns_passthrough_and_records_calls(settings) -> None:
    recorder = RecordingLlmClient()
    assert isinstance(recorder, LlmClientProtocol)
    runtime = build_runtime(settings)
    runtime.evaluator.llm_client = recorder
    request = EvaluationRequest.model_validate(
        payload(request_id="recording-passthrough")
    )

    result = asyncio.run(runtime.evaluator.evaluate(request))

    assert recorder.semantic_calls == 1
    assert recorder.explanation_calls == 1
    assert recorder.semantic_item_ids == [["recording-passthrough-001"]]
    assert recorder.explanation_item_ids == [["recording-passthrough-001"]]
    assert result.response.items[0].explanation_source == "llm"


def test_semantic_failure_does_not_call_explanation(settings) -> None:
    recorder = RecordingLlmClient(fail_stage="semantic")
    runtime = build_runtime(settings)
    runtime.evaluator.llm_client = recorder
    request = EvaluationRequest.model_validate(
        payload(
            request_id="recording-semantic-failure",
            sources=[
                {"address": "16.1.30.10", "description": "生产应用 A"},
                {"address": "16.1.30.11", "description": "生产应用 B"},
            ],
        )
    )

    result = asyncio.run(runtime.evaluator.evaluate(request))

    assert recorder.semantic_calls == 1
    assert recorder.semantic_item_ids == [
        ["recording-semantic-failure-001", "recording-semantic-failure-002"]
    ]
    assert recorder.explanation_calls == 0
    assert recorder.explanation_item_ids == []
    assert recorder.failed_stage == "semantic"
    assert all(
        item.reason_code == "LLM_SEMANTIC_ANALYSIS_FAILURE"
        for item in result.response.items
    )


def test_validation_and_idempotency_shortcuts_do_not_add_llm_calls(settings) -> None:
    recorder = RecordingLlmClient()
    with TestClient(create_app(settings)) as client:
        client.app.state.runtime.evaluator.llm_client = recorder
        value = payload(request_id="recording-idempotency")

        assert client.post("/v1/evaluations", json=value).status_code == 200
        assert (recorder.semantic_calls, recorder.explanation_calls) == (1, 1)

        assert client.post("/v1/evaluations", json=value).status_code == 200
        assert (recorder.semantic_calls, recorder.explanation_calls) == (1, 1)

        conflicting = {**value, "request_description": "different normalized input"}
        assert client.post("/v1/evaluations", json=conflicting).status_code == 409
        assert (recorder.semantic_calls, recorder.explanation_calls) == (1, 1)

        invalid = payload(
            request_id="recording-schema-error",
            ports=[{"start": 443, "end": 1}],
        )
        assert client.post("/v1/evaluations", json=invalid).status_code == 422
        assert (recorder.semantic_calls, recorder.explanation_calls) == (1, 1)
