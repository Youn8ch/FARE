"""Bounded structured-output runtime over the OpenAI SDK + Instructor.

One use-case call = one total deadline (``asyncio.timeout``) that contains
every structure attempt, every SDK request, response parsing, and correction
prompt generation. Provider transport retry is frozen at zero
(``AsyncOpenAI(max_retries=0)``); structure re-asks are owned by this loop
and bounded by ``max_correction_retries`` — each attempt inherits the
remaining deadline budget, never a fresh timeout.

Instructor is configured with ``max_retries=0`` so the attempt count, the
correction messages, and the remaining-time budget stay FARE-owned and
byte-compatible with the frozen provider contract.
"""

from __future__ import annotations

import asyncio
from time import perf_counter
from typing import Any, TypeVar

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import BaseModel

from app.services.llm.errors import (
    LlmDependencyError,
    ProviderFailure,
    StructuredOutputFailure,
    TimeoutFailure,
)
from app.services.llm.prompts import CORRECTION_PROMPT
from app.services.llm.provider import ProviderChannel
from app.services.llm.telemetry import CompletionTraceRecorder, validation_error_summary

T = TypeVar("T", bound=BaseModel)

DEPENDENCY_FAILED_MESSAGE = "LLM dependency request failed"
SCHEMA_EXHAUSTED_MESSAGE = (
    "LLM response failed schema validation after allowed correction"
)


def build_request_messages(
    messages: list[dict[str, str]], schema: type[BaseModel]
) -> list[dict[str, str]]:
    """The base message list; Instructor appends the JSON schema contract."""

    return [dict(message) for message in messages]


def build_request_body(
    *,
    model: str | None,
    temperature: float,
    max_tokens: int | None,
    top_p: float | None,
    stop: str | list[str] | None,
    thinking: str | None,
) -> dict[str, Any]:
    """Explicit creation kwargs; provider-specific fields ride ``extra_body``."""

    payload: dict[str, Any] = {
        "temperature": temperature,
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    extra_body: dict[str, Any] = {"do_sample": False}
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if top_p is not None:
        payload["top_p"] = top_p
    if stop is not None:
        payload["stop"] = stop
    if thinking is not None:
        extra_body["thinking"] = {"type": thinking}
    payload["extra_body"] = extra_body
    return payload


def _with_cause(
    error: LlmDependencyError, cause: BaseException | None
) -> LlmDependencyError:
    error.__cause__ = cause
    return error


def _classify_provider_cause(cause: BaseException | None) -> LlmDependencyError:
    """Map SDK/Instructor exception causes to the FARE typed taxonomy and the
    frozen business message."""

    if isinstance(cause, APIStatusError):
        return _with_cause(
            LlmDependencyError(DEPENDENCY_FAILED_MESSAGE),
            ProviderFailure(
                DEPENDENCY_FAILED_MESSAGE,
                status_code=getattr(cause, "status_code", None),
                retryable=bool(
                    getattr(cause, "status_code", 0) in {429, 500, 502, 503, 504}
                ),
            ),
        )
    if isinstance(cause, (APIConnectionError, APITimeoutError, httpx.HTTPError)):
        return _with_cause(
            LlmDependencyError(DEPENDENCY_FAILED_MESSAGE),
            ProviderFailure(DEPENDENCY_FAILED_MESSAGE, retryable=False),
        )
    return _with_cause(LlmDependencyError(DEPENDENCY_FAILED_MESSAGE), cause)


async def complete_structured[T: BaseModel](
    *,
    channel: ProviderChannel,
    traces: CompletionTraceRecorder,
    schema: type[T],
    messages: list[dict[str, str]],
    model: str | None,
    api_key: str | None,
    temperature: float,
    max_tokens: int | None,
    top_p: float | None,
    stop: str | list[str] | None,
    thinking: str | None,
    max_correction_retries: int,
    total_timeout: float,
) -> tuple[T, Any]:
    """Run the bounded structure loop under one shared total deadline."""

    create = channel.structured_create()
    body = build_request_body(
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        top_p=top_p,
        stop=stop,
        thinking=thinking,
    )
    conversation: list[dict[str, str]] = build_request_messages(messages, schema)
    attempts_allowed = max_correction_retries + 1
    last_error: Exception | None = None
    attempt_count = 0
    transport_failure: BaseException | None = None
    started = perf_counter()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + total_timeout
    try:
        async with asyncio.timeout(total_timeout):
            for attempt in range(attempts_allowed):
                attempt_count = attempt + 1
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError
                try:
                    parsed, completion = await create(
                        model=model,
                        messages=conversation,
                        response_model=schema,
                        max_retries=0,
                        timeout=remaining,
                        extra_headers=channel.auth_headers(api_key),
                        **body,
                    )
                except Exception as exc:
                    # Instructor wraps API and validation failures alike; the
                    # original cause carries the real classification.
                    cause = exc.__cause__ or exc
                    last_error = cause
                    if isinstance(
                        cause,
                        (APIStatusError, APIConnectionError, APITimeoutError, httpx.HTTPError),
                    ):
                        # Transport/API errors are never correction-retried
                        # (frozen historical behavior) and never consume the
                        # structure budget.
                        transport_failure = cause
                        break
                    if attempt + 1 < attempts_allowed:
                        # Bounded re-ask: the frozen correction message
                        # carries only a bounded validation summary.
                        content = _content_of(
                            _completion_dump(getattr(exc, "last_completion", None))
                        )
                        if content is not None:
                            conversation.append(
                                {"role": "assistant", "content": content}
                            )
                        conversation.append(
                            {
                                "role": "user",
                                "content": CORRECTION_PROMPT.format(
                                    error_summary=validation_error_summary(cause)
                                ),
                            }
                        )
                        continue
                    break
                raw = _completion_dump(completion)
                traces.record(
                    schema=schema,
                    attempts=attempt_count,
                    started=started,
                    error=None,
                    usage=_usage_of(completion),
                    provider_request_id=_provider_request_id(completion),
                )
                return parsed, raw
    except TimeoutError as exc:
        last_error = exc
    traces.record(
        schema=schema,
        attempts=attempt_count,
        started=started,
        error=last_error,
    )
    if transport_failure is not None:
        raise _classify_provider_cause(transport_failure) from transport_failure
    raise _final_error(
        last_error,
        attempts=attempt_count,
        total_timeout=total_timeout,
    )


def _final_error(
    last_error: Exception | None, *, attempts: int, total_timeout: float
) -> LlmDependencyError:
    if isinstance(last_error, TimeoutError):
        return _with_cause(
            LlmDependencyError(DEPENDENCY_FAILED_MESSAGE),
            TimeoutFailure(
                DEPENDENCY_FAILED_MESSAGE,
                deadline_seconds=total_timeout,
                attempts=attempts,
            ),
        )
    # Transport/API errors raise immediately inside the loop; every error that
    # survives the bounded loop is a structure/parsing failure.
    return _with_cause(
        LlmDependencyError(SCHEMA_EXHAUSTED_MESSAGE),
        StructuredOutputFailure(
            SCHEMA_EXHAUSTED_MESSAGE,
            attempts=attempts,
            validation_summary=validation_error_summary(last_error),
        ),
    )


def _content_of(raw: Any) -> str | None:
    try:
        content = raw["choices"][0]["message"]["content"]
        return content if isinstance(content, str) else None
    except (KeyError, IndexError, TypeError):
        return None


def _completion_dump(completion: Any) -> Any:
    dump = getattr(completion, "model_dump", None)
    if callable(dump):
        try:
            return dump()
        except Exception:  # noqa: BLE001 - metadata must never break the result
            return completion
    return completion


def _usage_of(completion: Any) -> dict[str, Any] | None:
    usage = getattr(completion, "usage", None)
    if usage is None:
        return None
    dump = getattr(usage, "model_dump", None)
    raw = dump() if callable(dump) else dict(usage)
    if not isinstance(raw, dict):
        return None
    # Freeze the audit surface on the three canonical counters.
    return {
        "prompt_tokens": raw.get("prompt_tokens"),
        "completion_tokens": raw.get("completion_tokens"),
        "total_tokens": raw.get("total_tokens"),
    }


def _provider_request_id(completion: Any) -> str | None:
    value = getattr(completion, "id", None)
    return str(value) if value is not None else None
