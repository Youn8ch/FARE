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

    @classmethod
    def from_env(cls) -> Settings:
        mock_file = os.getenv("ACL_MOCK_FILE")
        llm_mock_file = os.getenv("LLM_MOCK_FILE")
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
