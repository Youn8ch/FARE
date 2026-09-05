"""PHASE-07: the frozen failure-to-business-result matrix.

Every failure kind has exactly one FARE classification and one frozen
business degradation per use case:

- semantic            -> LLM_SEMANTIC_ANALYSIS_FAILURE fail-close on every
                         item, explanation never called;
- request findings    -> shadow rejected, decision untouched;
- explanation         -> template fallback, locked decision untouched.

Transport status/connection failures classify as ``ProviderFailure``,
transport/deadline timeouts as ``TimeoutFailure``, structure/parsing
failures as ``StructuredOutputFailure``, and guard rejections as
``DomainValidationFailure`` (request findings) or the FARE guard error class
(semantic/explanation guards). No raw provider content or secrets may leak
into the public error or the audit event.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from app.config import Settings
from app.main import build_runtime
from app.schemas import EvaluationRequest
from tests.test_architecture_baseline import NETWORK_PLAN_FIXTURE, _payload

OK_ID = "chatcmpl-matrix"


def _completion_response(content: dict | str, *, request_id: str = OK_ID) -> httpx.Response:
    payload_text = content if isinstance(content, str) else json.dumps(content)
    return httpx.Response(
        200,
        json={
            "id": request_id,
            "choices": [{"message": {"content": payload_text}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        },
    )


def _stage_of(request: httpx.Request) -> str:
    body = json.loads(request.content)
    system = body["messages"][0]["content"]
    if "受限语义分析器" in system:
        return "semantic"
    if "风险观察器" in system:
        return "request_findings"
    if "结论解释器" in system:
        return "explanation"
    raise AssertionError(f"unknown LLM stage request: {system[:80]}")


def _connect_error():
    raise httpx.ConnectError("connection refused")


def _connect_timeout():
    raise httpx.ConnectTimeout("timed out")


FAILURE_KINDS = {
    "rate_limited": lambda: httpx.Response(429, json={"error": "slow down"}),
    "server_error": lambda: httpx.Response(500, json={"error": "boom"}),
    "connect_error": lambda: _connect_error(),
    "transport_timeout": lambda: _connect_timeout(),
    "invalid_json": lambda: _completion_response("definitely-not-json"),
    "schema_violation": lambda: _completion_response({"nope": True}),
}

PROVIDER_FAILURE_KINDS = {"rate_limited", "server_error", "connect_error"}
TIMEOUT_FAILURE_KINDS = {"transport_timeout"}
STRUCTURE_FAILURE_KINDS = {"invalid_json", "schema_violation"}

VALID_SEMANTIC = {"analyzed_item_ids": ["__ITEM__"]}
VALID_EXPLANATION = {
    "items": [{"item_id": "__ITEM__", "explanation": "解释。", "recommendation": "建议。"}]
}
VALID_FINDINGS = {"analyzed_item_ids": ["__ITEM__"], "findings": []}


def _staged_handler(
    *,
    item_id: str,
    semantic=None,
    request_findings=None,
    explanation=None,
) -> httpx.Response:
    def handler(request: httpx.Request) -> httpx.Response:
        stage = _stage_of(request)
        spec = {
            "semantic": semantic,
            "request_findings": request_findings,
            "explanation": explanation,
        }[stage]
        if spec is None:
            content = {
                "semantic": VALID_SEMANTIC,
                "request_findings": VALID_FINDINGS,
                "explanation": VALID_EXPLANATION,
            }[stage]
            return _completion_response(
                json.loads(json.dumps(content).replace("__ITEM__", item_id)),
                request_id=OK_ID,
            )
        return spec()

    return handler


def _evaluate_http(
    settings: Settings,
    tmp_path,
    handler,
    *,
    llm_request_findings_mode: str = "off",
    semantic_timeout: float = 5,
    network_plan_fixture=NETWORK_PLAN_FIXTURE,
) -> tuple[object, dict]:
    configured = replace(
        settings,
        llm_client_mode="http",
        llm_base_url="https://model.invalid/v1",
        llm_model="test-model",
        llm_api_key=None,
        llm_semantic_timeout_seconds=semantic_timeout,
        llm_explanation_timeout_seconds=5,
        network_plan_client_mode="mock",
        network_plan_mock_file=network_plan_fixture,
        llm_request_findings_mode=llm_request_findings_mode,
        audit_log_dir=tmp_path / "audit",
    )
    runtime = build_runtime(configured)
    import instructor
    from openai import AsyncOpenAI

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False
    )
    sdk = AsyncOpenAI(
        base_url="https://model.invalid/v1",
        api_key="no-auth",
        http_client=http_client,
        max_retries=0,
    )
    channel = runtime.evaluator.llm_client._channel
    channel._sdk = sdk
    channel.structured = instructor.from_openai(sdk, mode=instructor.Mode.JSON)
    channel._http_client = http_client
    return runtime, channel


def _item_id(request_id: str) -> str:
    return f"{request_id}-001"


@pytest.mark.parametrize("kind", sorted(FAILURE_KINDS))
def test_semantic_failure_matrix_fails_closed_with_unique_classification(
    settings, tmp_path, kind
) -> None:
    item_id = _item_id(f"matrix-semantic-{kind}")
    handler = _staged_handler(
        item_id=item_id,
        semantic=FAILURE_KINDS[kind],
    )
    runtime, _ = _evaluate_http(settings, tmp_path, handler)
    try:
        result = asyncio.run(
            runtime.evaluator.evaluate(
                EvaluationRequest.model_validate(
                    _payload(f"matrix-semantic-{kind}")
                )
            )
        )
    finally:
        asyncio.run(runtime.aclose())

    item = result.response.items[0]
    assert (item.decision, item.reason_code) == (
        "待定",
        "LLM_SEMANTIC_ANALYSIS_FAILURE",
    )
    assert item.decision_trace.semantic_effect == "semantic_failure"
    stage = result.model_raw["stages"]["semantic"]
    assert stage["status"] == "rejected"
    if kind in PROVIDER_FAILURE_KINDS:
        assert stage["fare_error_type"] == "ProviderFailure"
    elif kind in TIMEOUT_FAILURE_KINDS:
        assert stage["fare_error_type"] == "TimeoutFailure"
    else:
        assert stage["fare_error_type"] == "StructuredOutputFailure"
    # frozen degradation: explanation never runs after semantic failure
    assert result.model_raw["stages"]["explanation"]["status"] == "skipped"
    assert result.exceptions and "LLM semantic analysis:" in result.exceptions[0]


@pytest.mark.parametrize("kind", sorted(FAILURE_KINDS))
def test_request_findings_failure_matrix_rejected_without_decision_change(
    settings, tmp_path, kind
) -> None:
    item_id = _item_id(f"matrix-findings-{kind}")
    handler = _staged_handler(
        item_id=item_id,
        request_findings=FAILURE_KINDS[kind],
    )
    runtime, _ = _evaluate_http(
        settings, tmp_path, handler, llm_request_findings_mode="shadow"
    )
    try:
        result = asyncio.run(
            runtime.evaluator.evaluate(
                EvaluationRequest.model_validate(
                    _payload(f"matrix-findings-{kind}")
                )
            )
        )
    finally:
        asyncio.run(runtime.aclose())

    item = result.response.items[0]
    assert item.decision == "合规"  # decision untouched
    findings = result.response.request_findings
    assert findings is not None and findings.status == "rejected"
    assert findings.rejection_reason
    stage = result.model_raw["stages"]["request_findings"]
    assert stage["status"] == "rejected"
    if kind in PROVIDER_FAILURE_KINDS:
        assert stage["fare_error_type"] == "ProviderFailure"
    elif kind in TIMEOUT_FAILURE_KINDS:
        assert stage["fare_error_type"] == "TimeoutFailure"
    else:
        assert stage["fare_error_type"] == "StructuredOutputFailure"
    # explanation still runs and passes
    assert result.model_raw["stages"]["explanation"]["status"] == "passed"


def test_request_findings_guard_rejection_classifies_as_domain_validation(
    settings, tmp_path
) -> None:
    item_id = _item_id("matrix-guard")
    bad_finding = {
        "analyzed_item_ids": [item_id],
        "findings": [
            {
                "finding_id": "f-1",
                "finding_type": "missing_approval_context",
                "affected_item_ids": [item_id],
                "description": "d",
                "evidence": [
                    {
                        "item_id": item_id,
                        "source": "destination_description",
                        "quote": "这句话不存在于任何原文中",
                    }
                ],
                "confidence": 0.8,
                "status": "candidate",
                "question": "q?",
                "question_for_requester": "q?",
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        stage = _stage_of(request)
        if stage == "semantic":
            return _completion_response(
                json.loads(json.dumps(VALID_SEMANTIC).replace("__ITEM__", item_id))
            )
        if stage == "request_findings":
            return _completion_response(bad_finding)
        return _completion_response(
            json.loads(json.dumps(VALID_EXPLANATION).replace("__ITEM__", item_id))
        )

    runtime, _ = _evaluate_http(
        settings, tmp_path, handler, llm_request_findings_mode="shadow"
    )
    try:
        result = asyncio.run(
            runtime.evaluator.evaluate(
                EvaluationRequest.model_validate(_payload("matrix-guard"))
            )
        )
    finally:
        asyncio.run(runtime.aclose())

    stage = result.model_raw["stages"]["request_findings"]
    assert stage["status"] == "rejected"
    assert stage["fare_error_type"] == "DomainValidationFailure"
    assert result.response.items[0].decision == "合规"


def test_deadline_expiry_classifies_as_timeout_failure() -> None:
    """Unit level: one shared deadline covering structure attempts — expiry
    maps to ``TimeoutFailure`` and the frozen business message."""

    from pydantic import BaseModel

    from app.services.llm.errors import LlmDependencyError
    from app.services.llm.provider import ProviderChannel
    from app.services.llm.structured_runtime import complete_structured
    from app.services.llm.telemetry import CompletionTraceRecorder

    class Probe(BaseModel):
        name: str

    async def scenario() -> str:
        channel = ProviderChannel(base_url="https://model.invalid/v1")
        traces = CompletionTraceRecorder(id(channel))

        async def slow_create(**kwargs):
            await asyncio.sleep(0.5)
            raise AssertionError("must be cancelled by the total deadline")

        channel.structured_create = lambda: slow_create  # type: ignore[method-assign]
        try:
            await complete_structured(
                channel=channel,
                traces=traces,
                schema=Probe,
                messages=[{"role": "user", "content": "u"}],
                model="test-model",
                api_key=None,
                temperature=0,
                max_tokens=None,
                top_p=None,
                stop=None,
                thinking=None,
                max_correction_retries=1,
                total_timeout=0.1,
            )
        except LlmDependencyError as exc:
            assert "dependency request failed" in str(exc)
            return type(exc.__cause__).__name__
        raise AssertionError("expected the total deadline to expire")

    fare_type = asyncio.run(scenario())
    assert fare_type == "TimeoutFailure"


def test_explanation_failure_falls_back_and_records_fallback_used(
    settings, tmp_path
) -> None:
    item_id = _item_id("matrix-explanation")
    handler = _staged_handler(item_id=item_id, explanation=FAILURE_KINDS["server_error"])
    runtime, _ = _evaluate_http(settings, tmp_path, handler)
    try:
        result = asyncio.run(
            runtime.evaluator.evaluate(
                EvaluationRequest.model_validate(_payload("matrix-explanation"))
            )
        )
    finally:
        asyncio.run(runtime.aclose())

    item = result.response.items[0]
    assert item.decision == "合规"
    assert item.explanation_source == "template"
    stage = result.model_raw["stages"]["explanation"]
    assert stage["status"] == "rejected"
    assert stage["fare_error_type"] == "ProviderFailure"
    assert stage["fallback_used"] is True


def test_successful_events_carry_usage_and_provider_request_id(
    settings, tmp_path
) -> None:
    item_id = _item_id("matrix-usage")
    handler = _staged_handler(item_id=item_id)
    runtime, _ = _evaluate_http(settings, tmp_path, handler)
    try:
        result = asyncio.run(
            runtime.evaluator.evaluate(
                EvaluationRequest.model_validate(_payload("matrix-usage"))
            )
        )
    finally:
        asyncio.run(runtime.aclose())

    stage = result.model_raw["stages"]["semantic"]
    assert stage["status"] == "passed"
    assert stage["token_usage"] == {
        "prompt_tokens": 3,
        "completion_tokens": 2,
        "total_tokens": 5,
    }
    assert stage["provider_request_id"] == OK_ID
    assert stage["transport_retry_count"] == 0
    assert stage["prompt_version"]


def test_concurrent_traces_do_not_cross(settings, tmp_path) -> None:
    """Per-adapter ContextVar traces: concurrent evaluations must not observe
    each other's completion traces."""

    async def scenario() -> None:
        runtime = build_runtime(settings)
        from tests.helpers.llm import RecordingLlmClient

        runtime.evaluator.llm_client = RecordingLlmClient()

        async def one(rid: str):
            result = await runtime.evaluator.evaluate(
                EvaluationRequest.model_validate(_payload(rid))
            )
            stages = result.model_raw["stages"]
            return (
                stages["semantic"]["status"],
                stages["explanation"]["status"],
                result.model_raw["metrics"]["llm_added_pending_count"],
            )

        results = await asyncio.gather(
            one("concurrent-a"), one("concurrent-b"), one("concurrent-c")
        )
        await runtime.aclose()
        # every evaluation observes only its own completed trace: no stage
        # inherits another request's attempts or failure state
        assert all(
            semantic == "passed" and explanation == "passed"
            for semantic, explanation, _ in results
        )

    asyncio.run(scenario())
