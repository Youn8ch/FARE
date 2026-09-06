"""FARE-owned LLM error taxonomy.

The taxonomy classifies every internal failure of an LLM use case. The
business-facing exception (``LlmDependencyError``) and the frozen business
degradations (semantic fail-close, request findings rejected, explanation
fallback) stay exactly where they are; this module only names the causes.

- ``SemanticPolicyFailure`` is reserved for startup configuration or
  programming errors; it never wraps ordinary model output failures.
- Exception messages never carry raw provider responses, full prompts, or
  secrets.
"""

from __future__ import annotations


class FareLlmError(RuntimeError):
    """Base class for FARE-classified LLM use-case failures."""


class ProviderFailure(FareLlmError):
    """The provider transport returned an error status or connection failure."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        provider_request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.provider_request_id = provider_request_id


class TimeoutFailure(FareLlmError):
    """The use-case deadline expired; attempts contains the outbound count."""

    def __init__(self, message: str, *, deadline_seconds: float, attempts: int) -> None:
        super().__init__(message)
        self.deadline_seconds = deadline_seconds
        self.attempts = attempts


class StructuredOutputFailure(FareLlmError):
    """Structured output stayed invalid through every allowed attempt."""

    def __init__(
        self, message: str, *, attempts: int, validation_summary: str
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.validation_summary = validation_summary


class DomainValidationFailure(FareLlmError):
    """A FARE domain guard rejected the structured candidate."""

    def __init__(
        self, message: str, *, guard_code: str, item_ids: list[str]
    ) -> None:
        super().__init__(message)
        self.guard_code = guard_code
        self.item_ids = item_ids


class SemanticPolicyFailure(FareLlmError):
    """Semantic effect configuration is invalid (startup/programming error)."""

    def __init__(self, message: str, *, config_key: str, invalid_value: object) -> None:
        super().__init__(message)
        self.config_key = config_key
        self.invalid_value = invalid_value


class LlmDependencyError(RuntimeError):
    """Business-facing LLM dependency failure (frozen semantics).

    Kept as the exception every stage already maps to its frozen business
    degradation; the typed classes above classify the cause internally.
    """
