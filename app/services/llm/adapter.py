"""FARE LLM adapter: the only use-case entry point above the provider.

Implements the three FARE use cases (semantic analysis, request findings,
explanation) over the structured runtime + provider channel. FARE owns the
use-case deadline, error translation, and guard invocation; the structured
runtime only proves schema validity.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.schemas import LlmExplanationResponse, LlmRequestFindingsResponse, LlmSemanticResponse
from app.services.llm.errors import (
    DomainValidationFailure,
    LlmDependencyError,
)
from app.services.llm.mock_adapter import (
    explanation_mock_response,
    fixture_response,
    load_fixture,
    request_findings_mock_response,
    semantic_mock_response,
)
from app.services.llm.ports import LlmClientProtocol
from app.services.llm.prompts import (
    EXPLANATION_SYSTEM_PROMPT,
    PROMPT_VERSIONS,
    REQUEST_FINDINGS_SYSTEM_PROMPT,
    SEMANTIC_SYSTEM_PROMPT,
)
from app.services.llm.provider import ProviderChannel
from app.services.llm.structured_runtime import complete_structured
from app.services.llm.telemetry import CompletionTraceRecorder
from app.services.output_guard import RequestFindingGuardError, guard_request_findings


def _request_finding_evidence_sources(
    inputs: list[dict[str, Any]],
) -> dict[str, dict[str, str]]:
    source_names = (
        "request_description",
        "source_description",
        "destination_description",
    )
    return {
        str(item["item_id"]): {
            source: str(item.get(source, "")) for source in source_names
        }
        for item in inputs
    }


class LlmClient(LlmClientProtocol):
    """Batch semantic/explanation adapter with a deterministic offline mock boundary."""

    def __init__(
        self,
        *,
        mode: str,
        base_url: str | None,
        model: str | None,
        api_key: str | None,
        mock_file: Path | None,
        semantic_timeout: float,
        explanation_timeout: float,
        max_correction_retries: int,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        top_p: float | None = None,
        stop: str | list[str] | None = None,
        thinking: str | None = None,
        transport: Any = None,
        client: Any = None,
    ) -> None:
        self.mode = mode
        self.semantic_timeout = semantic_timeout
        self.explanation_timeout = explanation_timeout
        self.max_correction_retries = max_correction_retries
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.top_p = top_p
        self.stop = stop
        self.thinking = thinking
        self._channel = ProviderChannel(
            base_url=base_url,
            transport=transport,
            client=client,
        )
        self.base_url = self._channel.base_url
        self.model = model
        self.api_key = api_key
        self._fixture = load_fixture(mock_file) if mock_file else None
        self._traces = CompletionTraceRecorder(id(self))

    @property
    def enabled(self) -> bool:
        return True

    @property
    def model_name(self) -> str:
        if self.mode == "mock":
            return str((self._fixture or {}).get("version", "builtin-2026.07.0"))
        return self.model or "unconfigured"

    @property
    def fixture_version(self) -> str | None:
        if not self._fixture:
            return None
        return str(
            self._fixture.get("fixture_version")
            or self._fixture.get("version")
            or "unversioned"
        )

    @property
    def prompt_versions(self) -> dict[str, str]:
        return dict(PROMPT_VERSIONS)

    def consume_completion_trace(self) -> dict[str, Any] | None:
        return self._traces.consume()

    async def aclose(self) -> None:
        await self._channel.aclose()

    async def analyze(self, payload: dict[str, Any]) -> tuple[LlmSemanticResponse, Any]:
        if self.mode == "mock":
            response = semantic_mock_response(self._fixture, payload)
            return response, response.model_dump(mode="json")

        messages = [
            {"role": "system", "content": SEMANTIC_SYSTEM_PROMPT},
            {"role": "user", "content": self._json(payload)},
        ]
        parsed, raw = await self._complete(
            messages, LlmSemanticResponse, self.semantic_timeout
        )
        return parsed, raw

    async def explain(self, payload: dict[str, Any]) -> tuple[LlmExplanationResponse, Any]:
        if self.mode == "mock":
            response = explanation_mock_response(self._fixture, payload)
            return response, response.model_dump(mode="json")

        messages = [
            {"role": "system", "content": EXPLANATION_SYSTEM_PROMPT},
            {"role": "user", "content": self._json(payload)},
        ]
        return await self._complete(
            messages, LlmExplanationResponse, self.explanation_timeout
        )

    async def analyze_request_findings(
        self,
        inputs: list[dict[str, Any]],
        *,
        request_id: str | None = None,
    ) -> LlmRequestFindingsResponse:
        """Return guarded application-level findings without changing any decision."""
        if not inputs:
            return LlmRequestFindingsResponse(analyzed_item_ids=[])
        if self.mode == "mock":
            parsed = request_findings_mock_response(self._fixture, inputs, request_id)
        else:
            messages = [
                {"role": "system", "content": REQUEST_FINDINGS_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": self._json(
                        {"request_id": request_id, "items": inputs}
                    ),
                },
            ]
            parsed, _ = await self._complete(
                messages, LlmRequestFindingsResponse, self.semantic_timeout
            )

        try:
            return guard_request_findings(
                parsed,
                evidence_sources=_request_finding_evidence_sources(inputs),
            )
        except RequestFindingGuardError as exc:
            failure = DomainValidationFailure(
                f"LLM request findings output was rejected: {exc}",
                guard_code="REQUEST_FINDINGS_GUARD",
                item_ids=[str(item.get("item_id")) for item in inputs],
            )
            # __cause__ stays the typed failure so audit sees the FARE
            # taxonomy class; the guard detail is preserved in the message.
            error = LlmDependencyError(str(failure))
            error.__cause__ = failure
            raise error  # noqa: B904 - __cause__ must stay the typed failure

    async def _complete(
        self, messages: list[dict[str, str]], schema: type, timeout: float | None = None
    ) -> tuple[Any, Any]:
        if self.mode != "http" or not self._channel.available:
            raise LlmDependencyError("HTTP model completion is unavailable in mock mode")
        return await complete_structured(
            channel=self._channel,
            traces=self._traces,
            schema=schema,
            messages=messages,
            model=self.model,
            api_key=self.api_key,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            top_p=self.top_p,
            stop=self.stop,
            thinking=self.thinking,
            max_correction_retries=self.max_correction_retries,
            total_timeout=self.semantic_timeout if timeout is None else timeout,
        )

    @staticmethod
    def _json(value: Any) -> str:
        import json

        return json.dumps(value, ensure_ascii=False)


__all__ = ["LlmClient", "fixture_response"]
