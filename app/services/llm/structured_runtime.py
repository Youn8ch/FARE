"""Bounded structured-output runtime (hand-written until PHASE-06).

One use-case call = one total deadline covering every structure attempt,
every transport request, response parsing, and correction prompt building.
Transport-level retry is zero; structure re-asks are bounded by
``max_correction_retries`` and each attempt inherits the remaining budget,
never a fresh timeout.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from time import perf_counter
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.services.llm.errors import LlmDependencyError
from app.services.llm.prompts import CORRECTION_PROMPT, JSON_ONLY_SUFFIX
from app.services.llm.provider import ProviderChannel
from app.services.llm.telemetry import CompletionTraceRecorder, validation_error_summary

T = TypeVar("T", bound=BaseModel)


def build_request_messages(
    messages: list[dict[str, str]], schema: type[BaseModel]
) -> list[dict[str, str]]:
    request_messages = [dict(message) for message in messages]
    schema_contract = json.dumps(
        schema.model_json_schema(),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    request_messages[0]["content"] = request_messages[0]["content"] + (
        JSON_ONLY_SUFFIX.format(schema_contract=schema_contract)
    )
    return request_messages


def build_request_body(
    *,
    model: str | None,
    temperature: float,
    max_tokens: int | None,
    top_p: float | None,
    stop: str | list[str] | None,
    thinking: str | None,
    request_messages: list[dict[str, str]],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "temperature": temperature,
        "do_sample": False,
        "stream": False,
        "response_format": {"type": "json_object"},
        "messages": request_messages,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if top_p is not None:
        payload["top_p"] = top_p
    if stop is not None:
        payload["stop"] = stop
    if thinking is not None:
        payload["thinking"] = {"type": thinking}
    return payload


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

    request_messages = build_request_messages(messages, schema)
    payload = build_request_body(
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        top_p=top_p,
        stop=stop,
        thinking=thinking,
        request_messages=request_messages,
    )
    attempts_allowed = max_correction_retries + 1
    last_error: Exception | None = None
    last_raw: Any = None
    last_content: str | None = None
    attempt_count = 0
    started = perf_counter()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + total_timeout
    try:
        async with asyncio.timeout(total_timeout):
            # Borrow the lifecycle-owned client without closing it per request.
            async with nullcontext(channel) as client:
                for attempt in range(attempts_allowed):
                    attempt_count = attempt + 1
                    if attempt:
                        if last_content is not None:
                            payload["messages"].append(
                                {"role": "assistant", "content": last_content}
                            )
                        payload["messages"].append(
                            {
                                "role": "user",
                                "content": CORRECTION_PROMPT.format(
                                    error_summary=validation_error_summary(last_error)
                                ),
                            }
                        )
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise TimeoutError
                    try:
                        response = await client.post_chat_completions(
                            payload, timeout=remaining, api_key=api_key
                        )
                        response.raise_for_status()
                    except httpx.HTTPError as exc:
                        last_error = exc
                        break
                    try:
                        last_raw = response.json()
                        content = last_raw["choices"][0]["message"]["content"]
                        last_content = content if isinstance(content, str) else None
                        parsed = schema.model_validate_json(content)
                        traces.record(
                            schema=schema,
                            attempts=attempt_count,
                            started=started,
                            error=None,
                        )
                        return parsed, last_raw
                    except (
                        KeyError,
                        IndexError,
                        ValueError,
                        ValidationError,
                    ) as exc:
                        last_error = exc
    except TimeoutError as exc:
        last_error = exc
    traces.record(
        schema=schema,
        attempts=attempt_count,
        started=started,
        error=last_error,
    )
    if isinstance(last_error, (httpx.HTTPError, TimeoutError)):
        raise LlmDependencyError("LLM dependency request failed") from last_error
    raise LlmDependencyError(
        "LLM response failed schema validation after allowed correction"
    ) from last_error
