from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    policy_dir: Path
    audit_log_dir: Path
    audit_log_retention_days: int
    acl_client_mode: str
    acl_mock_file: Path | None
    acl_api_url: str | None
    acl_timeout_seconds: float
    llm_client_mode: str
    llm_base_url: str | None
    llm_model: str | None
    llm_api_key: str | None
    llm_mock_file: Path | None
    llm_semantic_timeout_seconds: float
    llm_explanation_timeout_seconds: float
    llm_max_correction_retries: int
    max_concurrent_evaluations: int
    llm_acl_candidate_mode: str = "off"
    llm_request_findings_mode: str = "off"
    # Direct construction keeps the legacy explicit offline behavior for compatibility.
    # from_env() uses the new production default (mock) below.
    network_plan_client_mode: str = "offline_catalog"
    network_plan_mock_file: Path | None = None
    network_plan_api_url: str | None = None
    network_plan_http_query_parameter: str | None = None
    network_plan_timeout_seconds: float = 5.0
    network_plan_batch_timeout_seconds: float = 15.0
    network_plan_max_concurrency: int = 8
    network_plan_max_subnets_per_request: int = 64
    network_plan_cache_ttl_seconds: float = 0.0
    network_plan_cache_max_entries: int = 1024
    max_evaluation_items: int = 256
    acl_max_concurrency: int = 8
    acl_decision_mode: str = "required"

    @classmethod
    def from_env(cls) -> Settings:
        mock_file = os.getenv("ACL_MOCK_FILE")
        llm_mock_file = os.getenv("LLM_MOCK_FILE")
        network_plan_mock_file = os.getenv("NETWORK_PLAN_MOCK_FILE")
        legacy_enabled = os.getenv("LLM_ENABLED")
        default_llm_mode = "http" if legacy_enabled and _bool("LLM_ENABLED", False) else "mock"
        settings = cls(
            policy_dir=Path(os.getenv("POLICY_DIR", "policies")),
            audit_log_dir=Path(os.getenv("AUDIT_LOG_DIR", "audit_logs")),
            audit_log_retention_days=int(os.getenv("AUDIT_LOG_RETENTION_DAYS", "30")),
            acl_client_mode=os.getenv("ACL_CLIENT_MODE", "mock").strip().lower(),
            acl_mock_file=Path(mock_file) if mock_file else None,
            acl_api_url=os.getenv("ACL_API_URL"),
            acl_timeout_seconds=float(os.getenv("ACL_TIMEOUT_SECONDS", "10")),
            llm_client_mode=os.getenv("LLM_CLIENT_MODE", default_llm_mode).strip().lower(),
            llm_base_url=os.getenv("LLM_BASE_URL"),
            llm_model=os.getenv("LLM_MODEL"),
            llm_api_key=os.getenv("LLM_API_KEY"),
            llm_mock_file=Path(llm_mock_file) if llm_mock_file else None,
            llm_semantic_timeout_seconds=float(
                os.getenv("LLM_SEMANTIC_TIMEOUT_SECONDS", "10")
            ),
            llm_explanation_timeout_seconds=float(
                os.getenv("LLM_EXPLANATION_TIMEOUT_SECONDS", "6")
            ),
            llm_max_correction_retries=int(os.getenv("LLM_MAX_CORRECTION_RETRIES", "1")),
            max_concurrent_evaluations=int(os.getenv("MAX_CONCURRENT_EVALUATIONS", "4")),
            llm_acl_candidate_mode=os.getenv(
                "LLM_ACL_CANDIDATE_MODE", "off"
            ).strip().lower(),
            llm_request_findings_mode=os.getenv(
                "LLM_REQUEST_FINDINGS_MODE", "off"
            ).strip().lower(),
            network_plan_client_mode=os.getenv(
                "NETWORK_PLAN_CLIENT_MODE", "mock"
            ).strip().lower(),
            network_plan_mock_file=(
                Path(network_plan_mock_file) if network_plan_mock_file else None
            ),
            network_plan_api_url=os.getenv("NETWORK_PLAN_API_URL"),
            network_plan_http_query_parameter=os.getenv(
                "NETWORK_PLAN_HTTP_QUERY_PARAMETER"
            ),
            network_plan_timeout_seconds=float(
                os.getenv("NETWORK_PLAN_TIMEOUT_SECONDS", "5")
            ),
            network_plan_batch_timeout_seconds=float(
                os.getenv("NETWORK_PLAN_BATCH_TIMEOUT_SECONDS", "15")
            ),
            network_plan_max_concurrency=int(
                os.getenv("NETWORK_PLAN_MAX_CONCURRENCY", "8")
            ),
            network_plan_max_subnets_per_request=int(
                os.getenv("NETWORK_PLAN_MAX_SUBNETS_PER_REQUEST", "64")
            ),
            network_plan_cache_ttl_seconds=float(
                os.getenv("NETWORK_PLAN_CACHE_TTL_SECONDS", "0")
            ),
            network_plan_cache_max_entries=int(
                os.getenv("NETWORK_PLAN_CACHE_MAX_ENTRIES", "1024")
            ),
            max_evaluation_items=int(os.getenv("MAX_EVALUATION_ITEMS", "256")),
            acl_max_concurrency=int(os.getenv("ACL_MAX_CONCURRENCY", "8")),
            acl_decision_mode=os.getenv(
                "ACL_DECISION_MODE", "advisory"
            ).strip().lower(),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.audit_log_retention_days < 1:
            raise ValueError("AUDIT_LOG_RETENTION_DAYS must be at least 1")
        if self.acl_client_mode not in {"mock", "http"}:
            raise ValueError("ACL_CLIENT_MODE must be 'mock' or 'http'")
        if self.acl_client_mode == "http" and not self.acl_api_url:
            raise ValueError("ACL_API_URL is required when ACL_CLIENT_MODE=http")
        if self.llm_client_mode not in {"mock", "http"}:
            raise ValueError("LLM_CLIENT_MODE must be 'mock' or 'http'")
        if self.llm_client_mode == "http" and (not self.llm_base_url or not self.llm_model):
            raise ValueError("LLM_BASE_URL and LLM_MODEL are required when LLM_CLIENT_MODE=http")
        if (
            self.acl_timeout_seconds <= 0
            or self.llm_semantic_timeout_seconds <= 0
            or self.llm_explanation_timeout_seconds <= 0
        ):
            raise ValueError("dependency timeouts must be positive")
        if self.llm_max_correction_retries not in {0, 1}:
            raise ValueError("LLM_MAX_CORRECTION_RETRIES must be 0 or 1")
        if self.max_concurrent_evaluations < 1:
            raise ValueError("MAX_CONCURRENT_EVALUATIONS must be at least 1")
        if self.llm_acl_candidate_mode not in {"off", "shadow"}:
            raise ValueError("LLM_ACL_CANDIDATE_MODE must be 'off' or 'shadow'")
        if self.llm_request_findings_mode not in {"off", "shadow", "guarded"}:
            raise ValueError(
                "LLM_REQUEST_FINDINGS_MODE must be 'off', 'shadow', or 'guarded'"
            )
        if self.llm_request_findings_mode == "guarded":
            raise ValueError(
                "LLM_REQUEST_FINDINGS_MODE=guarded is not approved; use off or shadow"
            )
        if self.network_plan_client_mode not in {"mock", "http", "offline_catalog"}:
            raise ValueError(
                "NETWORK_PLAN_CLIENT_MODE must be 'mock', 'http', or 'offline_catalog'"
            )
        if self.network_plan_client_mode == "http" and not self.network_plan_api_url:
            raise ValueError(
                "NETWORK_PLAN_API_URL is required when NETWORK_PLAN_CLIENT_MODE=http"
            )
        if (
            self.network_plan_timeout_seconds <= 0
            or self.network_plan_batch_timeout_seconds <= 0
        ):
            raise ValueError("network plan timeouts must be positive")
        if (
            self.network_plan_max_concurrency < 1
            or self.network_plan_max_subnets_per_request < 1
            or self.max_evaluation_items < 1
            or self.acl_max_concurrency < 1
        ):
            raise ValueError("network plan, evaluation, and ACL limits must be positive")
        if self.network_plan_cache_ttl_seconds < 0:
            raise ValueError("NETWORK_PLAN_CACHE_TTL_SECONDS must not be negative")
        if self.network_plan_cache_max_entries < 1:
            raise ValueError("NETWORK_PLAN_CACHE_MAX_ENTRIES must be positive")
        if self.acl_decision_mode not in {"advisory", "required"}:
            raise ValueError("ACL_DECISION_MODE must be 'advisory' or 'required'")
