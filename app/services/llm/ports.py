"""FARE-owned LLM use-case ports.

Stages depend only on these protocols — never on the adapter internals or
any third-party runtime.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.schemas import LlmExplanationResponse, LlmRequestFindingsResponse, LlmSemanticResponse


@runtime_checkable
class LlmClientProtocol(Protocol):
    """Minimal LLM boundary required by :class:`Evaluator`."""

    mode: str

    @property
    def model_name(self) -> str: ...

    async def analyze(
        self, payload: dict[str, Any]
    ) -> tuple[LlmSemanticResponse, Any]: ...

    async def explain(
        self, payload: dict[str, Any]
    ) -> tuple[LlmExplanationResponse, Any]: ...


@runtime_checkable
class LlmRequestFindingsClientProtocol(LlmClientProtocol, Protocol):
    """Optional application-level finding capability used only in shadow mode."""

    async def analyze_request_findings(
        self,
        inputs: list[dict[str, Any]],
        *,
        request_id: str | None = None,
    ) -> LlmRequestFindingsResponse: ...
