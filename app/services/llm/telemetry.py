"""FARE-owned per-call completion telemetry.

The trace ContextVar is owned by the adapter instance (no global mutable
hook), so concurrent requests cannot cross traces. Raw completion content is
truncated and summarized before it reaches any audit sink.
"""

from __future__ import annotations

from contextvars import ContextVar
from time import perf_counter
from typing import Any

from pydantic import BaseModel, ValidationError


def validation_error_summary(error: Exception | None) -> str:
    """A bounded, secret-free summary of the last validation failure."""

    if isinstance(error, ValidationError):
        parts = []
        for item in error.errors()[:8]:
            location = ".".join(str(value) for value in item.get("loc", ())) or "root"
            rejected = repr(item.get("input"))[:120]
            parts.append(
                f"{location}: {item.get('msg', 'invalid value')}; "
                f"rejected input={rejected}"
            )
        return "; ".join(parts) or "Pydantic schema validation failed"
    if isinstance(error, (ValueError, TypeError)):
        return "输出不是满足约定结构的有效 JSON"
    return "响应包络或 JSON 结构不符合约定"


class CompletionTraceRecorder:
    """Per-adapter ContextVar trace log (consumed by stage metrics)."""

    def __init__(self, instance_id: int) -> None:
        self._traces: ContextVar[tuple[dict[str, Any], ...]] = ContextVar(
            f"fare_llm_traces_{instance_id}", default=()
        )

    def record(
        self,
        *,
        schema: type[BaseModel],
        attempts: int,
        started: float,
        error: Exception | None,
        usage: dict[str, Any] | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        trace = {
            "schema": schema.__name__,
            "attempts": attempts,
            "corrections": max(0, attempts - 1),
            "duration_ms": round((perf_counter() - started) * 1000, 3),
            "status": "passed" if error is None else "failed",
            "error_type": type(error).__name__ if error is not None else None,
            "error_detail": (
                validation_error_summary(error)[:2000]
                if error is not None
                else None
            ),
        }
        if usage is not None:
            trace["usage"] = usage
        if provider_request_id is not None:
            trace["provider_request_id"] = provider_request_id
        self._traces.set((*self._traces.get(), trace))

    def consume(self) -> dict[str, Any] | None:
        traces = self._traces.get()
        if not traces:
            return None
        self._traces.set(traces[:-1])
        return dict(traces[-1])
