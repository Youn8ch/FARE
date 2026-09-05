"""Deterministic offline mock adapter.

The mock boundary never touches the structured runtime, the provider
channel, or any third-party code: fixtures and the builtin semantic response
are resolved directly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.schemas import (
    LlmExplanationResponse,
    LlmRequestFindingsResponse,
    LlmSemanticClaim,
    LlmSemanticResponse,
)
from app.services.llm.errors import LlmDependencyError


def load_fixture(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid LLM mock fixture: {path}") from exc
    if not isinstance(value, dict) or not value.get("version"):
        raise ValueError("LLM mock fixture must be an object with a version")
    return value


def fixture_response(
    fixture: dict[str, Any] | None, request_id: str | None, stage: str
) -> Any:
    if not fixture:
        return None
    response = (
        fixture.get("responses", {}).get(request_id) if request_id else None
    )
    if response and stage in response:
        return response[stage]
    return fixture.get("default", {}).get(stage)


def builtin_semantic(payload: dict[str, Any]) -> LlmSemanticResponse:
    claims: list[LlmSemanticClaim] = []
    for index, item in enumerate(payload["items"], start=1):
        evidence = item.get("request_description") or item.get("source_description")
        source = (
            "request_description"
            if item.get("request_description")
            else "source_description"
        )
        if not evidence:
            evidence = item.get("destination_description")
            source = "destination_description"
        if evidence:
            claims.append(
                LlmSemanticClaim(
                    claim_id=f"claim-{index:03d}",
                    scope=item["item_id"],
                    claim_type="request_context",
                    value=evidence,
                    source=source,
                    evidence=evidence,
                    confidence=1.0,
                )
            )
    return LlmSemanticResponse(
        analyzed_item_ids=[item["item_id"] for item in payload["items"]],
        claims=claims,
    )


def semantic_mock_response(
    fixture: dict[str, Any] | None, payload: dict[str, Any]
) -> LlmSemanticResponse:
    response = fixture_response(fixture, payload["request_id"], "semantic")
    try:
        return (
            LlmSemanticResponse.model_validate(response)
            if response is not None
            else builtin_semantic(payload)
        )
    except ValidationError as exc:
        raise LlmDependencyError(
            "mock LLM semantic response failed schema validation"
        ) from exc


def explanation_mock_response(
    fixture: dict[str, Any] | None, payload: dict[str, Any]
) -> LlmExplanationResponse:
    response = fixture_response(fixture, payload["request_id"], "explanation")
    try:
        return (
            LlmExplanationResponse.model_validate(response)
            if response is not None
            else LlmExplanationResponse.model_validate(
                {
                    "items": [
                        {
                            "item_id": item["item_id"],
                            "explanation": item["reason"],
                            "recommendation": item["recommendation"],
                        }
                        for item in payload["items"]
                    ]
                }
            )
        )
    except ValidationError as exc:
        raise LlmDependencyError(
            "mock LLM explanation response failed schema validation"
        ) from exc


def request_findings_mock_response(
    fixture: dict[str, Any] | None,
    inputs: list[dict[str, Any]],
    request_id: str | None,
) -> LlmRequestFindingsResponse:
    response = fixture_response(fixture, request_id, "request_findings")
    try:
        return (
            LlmRequestFindingsResponse.model_validate(response)
            if response is not None
            else LlmRequestFindingsResponse(
                analyzed_item_ids=[item["item_id"] for item in inputs]
            )
        )
    except ValidationError as exc:
        raise LlmDependencyError(
            "mock LLM request findings response failed schema validation"
        ) from exc
