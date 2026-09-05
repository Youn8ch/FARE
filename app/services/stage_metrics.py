"""V4-P5b: LLM stage metrics recording, extracted verbatim from
``evaluator.py`` with zero behavior change (mechanical move).

Metrics are observability only and never participate in business decisions.
"""

from __future__ import annotations

from time import perf_counter
from typing import Any

from app.schemas import EvaluationItem
from app.services.llm import LlmClientProtocol


def llm_metadata(client: LlmClientProtocol, policy_version: str) -> dict[str, Any]:
    return {
        "client_mode": client.mode,
        "model_version": client.model_name,
        "policy_version": policy_version,
        "prompt_versions": dict(getattr(client, "prompt_versions", {}) or {}),
        "fixture_version": getattr(client, "fixture_version", None),
    }


def record_llm_stage(
    model_raw: dict[str, Any],
    stage: str,
    started: float,
    status: str,
    error: Exception | None,
    client: LlmClientProtocol,
) -> None:
    trace = None
    consume = getattr(client, "consume_completion_trace", None)
    if callable(consume):
        trace = consume()
    record: dict[str, Any] = {
        "status": status,
        "duration_ms": round((perf_counter() - started) * 1000, 3),
        "error_type": type(error).__name__ if error is not None else None,
        "attempts": 0,
        "corrections": 0,
    }
    if isinstance(trace, dict):
        record["attempts"] = int(trace.get("attempts", 0))
        record["corrections"] = int(trace.get("corrections", 0))
        record["provider_duration_ms"] = trace.get("duration_ms")
        record["completion_schema"] = trace.get("schema")
        record["completion_status"] = trace.get("status")
        record["completion_error_type"] = trace.get("error_type")
        record["completion_error_detail"] = trace.get("error_detail")
        if record["error_type"] is None and trace.get("error_type"):
            record["error_type"] = str(trace["error_type"])
    model_raw["stages"][stage] = record


def llm_metrics(
    stages: dict[str, dict[str, Any]],
    *,
    llm_added_pending_count: int,
    items: list[EvaluationItem],
) -> dict[str, Any]:
    attempts = sum(int(stage.get("attempts", 0)) for stage in stages.values())
    corrections = sum(int(stage.get("corrections", 0)) for stage in stages.values())
    rejected = sum(stage.get("status") == "rejected" for stage in stages.values())
    schema_rejected = sum(
        stage.get("completion_status") == "failed" for stage in stages.values()
    )
    output_guard_rejected = sum(
        stage.get("status") == "rejected"
        and stage.get("completion_status") == "passed"
        for stage in stages.values()
    )
    dependency_failed = sum(
        stage.get("status") == "rejected"
        and stage.get("completion_status") not in {"passed", "failed"}
        for stage in stages.values()
    )
    traces = [item.decision_trace for item in items if item.decision_trace is not None]
    return {
        "schema_attempt_count": attempts,
        "schema_correction_count": corrections,
        "schema_correction_rate": round(corrections / attempts, 6) if attempts else 0.0,
        "guard_rejection_count": rejected,
        "model_schema_rejection_count": schema_rejected,
        "model_output_guard_rejection_count": output_guard_rejected,
        "model_dependency_failure_count": dependency_failed,
        "explanation_fallback_count": int(
            stages.get("explanation", {}).get("status") != "passed"
        ),
        "llm_added_pending_count": llm_added_pending_count,
        "model_business_downgrade_count": sum(
            trace.semantic_effect == "downgraded" for trace in traces
        ),
        "model_observation_only_count": sum(
            trace.semantic_effect == "observation_only" for trace in traces
        ),
        "model_question_only_count": sum(
            trace.semantic_effect == "question_only" for trace in traces
        ),
    }
