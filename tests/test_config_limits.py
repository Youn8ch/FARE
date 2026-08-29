from __future__ import annotations

from dataclasses import replace

import pytest

from app.config import Settings


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"audit_log_retention_days": 0}, "AUDIT_LOG_RETENTION_DAYS"),
        ({"acl_client_mode": "invalid"}, "ACL_CLIENT_MODE"),
        (
            {"acl_client_mode": "http", "acl_api_url": None},
            "ACL_API_URL",
        ),
        ({"llm_client_mode": "invalid"}, "LLM_CLIENT_MODE"),
        (
            {
                "llm_client_mode": "http",
                "llm_base_url": None,
                "llm_model": None,
            },
            "LLM_BASE_URL and LLM_MODEL",
        ),
        ({"acl_timeout_seconds": 0}, "dependency timeouts"),
        ({"llm_semantic_timeout_seconds": 0}, "dependency timeouts"),
        ({"llm_explanation_timeout_seconds": 0}, "dependency timeouts"),
        ({"llm_max_correction_retries": -1}, "LLM_MAX_CORRECTION_RETRIES"),
        ({"llm_max_correction_retries": 3}, "LLM_MAX_CORRECTION_RETRIES"),
        ({"max_concurrent_evaluations": 0}, "MAX_CONCURRENT_EVALUATIONS"),
        ({"network_plan_client_mode": "invalid"}, "NETWORK_PLAN_CLIENT_MODE"),
        (
            {"network_plan_client_mode": "http", "network_plan_api_url": None},
            "NETWORK_PLAN_API_URL",
        ),
        (
            {
                "network_plan_client_mode": "http",
                "network_plan_api_url": "https://network-plan.test",
                "network_plan_http_query_parameter": None,
            },
            "NETWORK_PLAN_HTTP_QUERY_PARAMETER",
        ),
        ({"network_plan_timeout_seconds": 0}, "network plan timeouts"),
        ({"network_plan_batch_timeout_seconds": 0}, "network plan timeouts"),
        ({"network_plan_max_concurrency": 0}, "limits must be positive"),
        ({"network_plan_max_subnets_per_request": 0}, "limits must be positive"),
        ({"max_evaluation_items": 0}, "limits must be positive"),
        ({"acl_max_concurrency": 0}, "limits must be positive"),
        ({"network_plan_cache_ttl_seconds": -1}, "must not be negative"),
        ({"network_plan_cache_max_entries": 0}, "must be positive"),
        ({"acl_decision_mode": "invalid"}, "ACL_DECISION_MODE"),
        (
            {"acl_deterministic_pending_mode": "invalid"},
            "ACL_DETERMINISTIC_PENDING_MODE",
        ),
    ],
)
def test_invalid_setting_limits_are_rejected(
    settings: Settings, updates: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(settings, **updates).validate()


@pytest.mark.parametrize("retry_count", [0, 1])
def test_supported_llm_correction_retry_limits_are_valid(
    settings: Settings, retry_count: int
) -> None:
    replace(
        settings,
        audit_log_retention_days=1,
        acl_timeout_seconds=0.001,
        llm_semantic_timeout_seconds=0.001,
        llm_explanation_timeout_seconds=0.001,
        llm_max_correction_retries=retry_count,
        max_concurrent_evaluations=1,
    ).validate()
