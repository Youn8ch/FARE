from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_CONFIG_PATH = Path("config/fare.yaml")


def _resolve_path(base: Path, value: str | None) -> Path | None:
    if value is None:
        return None
    path = Path(value)
    return path.resolve() if path.is_absolute() else (base / path).resolve()


class _StrictConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ServerConfig(_StrictConfig):
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    max_concurrent_evaluations: int = Field(default=4, ge=1)


class _PolicyConfig(_StrictConfig):
    directory: str


class _ExecutionConfig(_StrictConfig):
    mode: Literal["once", "poll"] = "once"
    batch_size: int = Field(default=100, ge=1)
    poll_interval_seconds: float = Field(default=30.0, gt=0)


class _LocalRequirementConfig(_StrictConfig):
    directory: str
    pattern: str = "*.json"
    recursive: bool = False


class _BearerAuthConfig(_StrictConfig):
    type: Literal["none", "bearer"] = "none"
    token: str | None = None

    @model_validator(mode="after")
    def validate_token(self) -> _BearerAuthConfig:
        if self.type == "bearer" and not self.token:
            raise ValueError("bearer authentication requires token")
        return self


class _ApiRequirementConfig(_StrictConfig):
    url: str
    method: Literal["GET", "POST"] = "GET"
    timeout_seconds: float = Field(default=30.0, gt=0)
    auth: _BearerAuthConfig = Field(default_factory=_BearerAuthConfig)


class _RequirementSourceConfig(_StrictConfig):
    mode: Literal["local", "api"]
    execution: _ExecutionConfig = Field(default_factory=_ExecutionConfig)
    local: _LocalRequirementConfig | None = None
    api: _ApiRequirementConfig | None = None
    output_file: str

    @model_validator(mode="after")
    def validate_active_source(self) -> _RequirementSourceConfig:
        if self.mode == "local" and self.local is None:
            raise ValueError("requirement_source.local is required in local mode")
        if self.mode == "api" and self.api is None:
            raise ValueError("requirement_source.api is required in api mode")
        return self


class _NetworkPlanMockConfig(_StrictConfig):
    fixture: str | None = None


class _NetworkPlanHttpConfig(_StrictConfig):
    url: str | None = None
    query_parameter: str | None = None
    timeout_seconds: float = Field(default=5.0, gt=0)
    batch_timeout_seconds: float = Field(default=15.0, gt=0)
    max_concurrency: int = Field(default=8, ge=1)
    auth: _BearerAuthConfig = Field(default_factory=_BearerAuthConfig)


class _CacheConfig(_StrictConfig):
    ttl_seconds: float = Field(default=0.0, ge=0)
    max_entries: int = Field(default=1024, ge=1)


class _NetworkPlanConfig(_StrictConfig):
    mode: Literal["mock", "http", "offline_catalog"]
    mock: _NetworkPlanMockConfig = Field(default_factory=_NetworkPlanMockConfig)
    http: _NetworkPlanHttpConfig = Field(default_factory=_NetworkPlanHttpConfig)
    cache: _CacheConfig = Field(default_factory=_CacheConfig)

    @model_validator(mode="after")
    def validate_http(self) -> _NetworkPlanConfig:
        if self.mode == "http" and (
            not self.http.url
            or not self.http.query_parameter
            or not self.http.query_parameter.strip()
        ):
            raise ValueError(
                "network_plan.http.url and query_parameter are required in http mode"
            )
        return self


class _AclMockConfig(_StrictConfig):
    fixture: str | None = None


class _AclHttpConfig(_StrictConfig):
    url: str | None = None
    timeout_seconds: float = Field(default=10.0, gt=0)
    auth: _BearerAuthConfig = Field(default_factory=_BearerAuthConfig)


class _AclConfig(_StrictConfig):
    mode: Literal["mock", "http"]
    decision_mode: Literal["advisory", "required"] = "advisory"
    deterministic_pending_mode: Literal["skip", "analyze"] = "analyze"
    max_concurrency: int = Field(default=8, ge=1)
    mock: _AclMockConfig = Field(default_factory=_AclMockConfig)
    http: _AclHttpConfig = Field(default_factory=_AclHttpConfig)

    @model_validator(mode="after")
    def validate_http(self) -> _AclConfig:
        if self.mode == "http" and not self.http.url:
            raise ValueError("acl.http.url is required in http mode")
        return self


class _LlmMockConfig(_StrictConfig):
    fixture: str | None = None


class _LlmHttpConfig(_StrictConfig):
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    semantic_timeout_seconds: float = Field(default=10.0, gt=0)
    explanation_timeout_seconds: float = Field(default=6.0, gt=0)
    max_correction_retries: int = Field(default=1, ge=0, le=2)
    temperature: float = Field(default=0.0, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1)
    top_p: float | None = Field(default=None, ge=0, le=1)
    stream: Literal[False] = False
    stop: str | list[str] | None = None
    thinking: Literal["enabled", "disabled"] | None = None


class _LlmFeaturesConfig(_StrictConfig):
    acl_candidate_mode: Literal["off", "shadow"] = "off"
    request_findings_mode: Literal["off", "shadow"] = "off"


class _LlmConfig(_StrictConfig):
    mode: Literal["mock", "http"]
    mock: _LlmMockConfig = Field(default_factory=_LlmMockConfig)
    http: _LlmHttpConfig = Field(default_factory=_LlmHttpConfig)
    features: _LlmFeaturesConfig = Field(default_factory=_LlmFeaturesConfig)

    @model_validator(mode="after")
    def validate_http(self) -> _LlmConfig:
        if self.mode == "http" and (not self.http.base_url or not self.http.model):
            raise ValueError("llm.http.base_url and llm.http.model are required in http mode")
        return self


class _SemanticEffectsConfig(_StrictConfig):
    fact_conflict: Literal["observe_only", "question_only", "review_required"] = (
        "review_required"
    )
    contradiction: Literal["observe_only", "question_only", "review_required"] = (
        "review_required"
    )
    temporary_permanent_conflict: Literal[
        "observe_only", "question_only", "review_required"
    ] = "review_required"
    purpose_target_mismatch: Literal[
        "observe_only", "question_only", "review_required"
    ] = "review_required"
    mixed_business_context: Literal[
        "observe_only", "question_only", "review_required"
    ] = "review_required"
    approval_scope_mismatch: Literal[
        "observe_only", "question_only", "review_required"
    ] = "review_required"
    unclassified_privileged_access: Literal[
        "observe_only", "question_only", "review_required"
    ] = "observe_only"

    def as_dict(self) -> dict[str, str]:
        return self.model_dump(mode="python")


class _EvaluationConfig(_StrictConfig):
    max_items: int = Field(default=256, ge=1)
    network_plan_max_subnets: int = Field(default=64, ge=1)
    semantic_effects: _SemanticEffectsConfig = Field(
        default_factory=_SemanticEffectsConfig
    )


class _AuditConfig(_StrictConfig):
    directory: str
    retention_days: int = Field(default=30, ge=1)


class _FareYamlConfig(_StrictConfig):
    schema_version: Literal["fare-config/v1"]
    config_id: str = Field(min_length=1)
    environment: str = Field(min_length=1)
    server: _ServerConfig = Field(default_factory=_ServerConfig)
    policy: _PolicyConfig
    requirement_source: _RequirementSourceConfig
    network_plan: _NetworkPlanConfig
    acl: _AclConfig
    llm: _LlmConfig
    evaluation: _EvaluationConfig = Field(default_factory=_EvaluationConfig)
    audit: _AuditConfig


@dataclass(frozen=True, slots=True)
class RequirementSourceSettings:
    mode: str
    execution_mode: str
    batch_size: int
    poll_interval_seconds: float
    local_directory: Path | None
    local_pattern: str
    local_recursive: bool
    api_url: str | None
    api_method: str
    api_timeout_seconds: float
    api_token: str | None
    output_file: Path


@dataclass(frozen=True, slots=True)
class ServerSettings:
    host: str
    port: int


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
    acl_deterministic_pending_mode: str = "analyze"
    acl_api_token: str | None = None
    network_plan_api_token: str | None = None
    llm_temperature: float = 0.0
    llm_max_tokens: int | None = None
    llm_top_p: float | None = None
    llm_stop: str | list[str] | None = None
    llm_thinking: str | None = None
    semantic_effects: dict[str, str] = dataclass_field(
        default_factory=lambda: _SemanticEffectsConfig().as_dict()
    )
    config_id: str = "direct-settings"
    environment: str = "unspecified"
    config_fingerprint: str = "direct-settings"

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
        if self.llm_max_correction_retries not in {0, 1, 2}:
            raise ValueError("LLM_MAX_CORRECTION_RETRIES must be 0, 1, or 2")
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
        if self.network_plan_client_mode == "http" and (
            not self.network_plan_api_url or not self.network_plan_http_query_parameter
        ):
            raise ValueError(
                "NETWORK_PLAN_API_URL and NETWORK_PLAN_HTTP_QUERY_PARAMETER are required "
                "when NETWORK_PLAN_CLIENT_MODE=http"
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
        if self.acl_deterministic_pending_mode not in {"skip", "analyze"}:
            raise ValueError(
                "ACL_DETERMINISTIC_PENDING_MODE must be 'skip' or 'analyze'"
            )
        expected_effect_keys = set(_SemanticEffectsConfig.model_fields)
        if set(self.semantic_effects) != expected_effect_keys:
            raise ValueError("semantic effect policy keys must match the approved catalog")
        if not set(self.semantic_effects.values()) <= {
            "observe_only",
            "question_only",
            "review_required",
        }:
            raise ValueError("semantic effect policy contains an unsupported effect")


@dataclass(frozen=True, slots=True)
class FareConfig:
    config_id: str
    environment: str
    path: Path
    server: ServerSettings
    requirement_source: RequirementSourceSettings
    settings: Settings

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CONFIG_PATH) -> FareConfig:
        config_path = Path(path).resolve()
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError(f"missing YAML configuration file: {config_path}") from exc
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError(f"invalid YAML configuration file: {config_path}") from exc
        if not isinstance(raw, dict):
            raise ValueError("YAML configuration root must be an object")
        parsed = _FareYamlConfig.model_validate(raw)
        config_fingerprint = _config_fingerprint(parsed)
        base = config_path.parent
        requirement = parsed.requirement_source
        local = requirement.local
        api = requirement.api
        settings = Settings(
            policy_dir=_resolve_path(base, parsed.policy.directory) or base,
            audit_log_dir=_resolve_path(base, parsed.audit.directory) or base,
            audit_log_retention_days=parsed.audit.retention_days,
            acl_client_mode=parsed.acl.mode,
            acl_mock_file=_resolve_path(base, parsed.acl.mock.fixture),
            acl_api_url=parsed.acl.http.url,
            acl_timeout_seconds=parsed.acl.http.timeout_seconds,
            llm_client_mode=parsed.llm.mode,
            llm_base_url=parsed.llm.http.base_url,
            llm_model=parsed.llm.http.model,
            llm_api_key=parsed.llm.http.api_key,
            llm_mock_file=_resolve_path(base, parsed.llm.mock.fixture),
            llm_semantic_timeout_seconds=parsed.llm.http.semantic_timeout_seconds,
            llm_explanation_timeout_seconds=parsed.llm.http.explanation_timeout_seconds,
            llm_max_correction_retries=parsed.llm.http.max_correction_retries,
            max_concurrent_evaluations=parsed.server.max_concurrent_evaluations,
            llm_acl_candidate_mode=parsed.llm.features.acl_candidate_mode,
            llm_request_findings_mode=parsed.llm.features.request_findings_mode,
            network_plan_client_mode=parsed.network_plan.mode,
            network_plan_mock_file=_resolve_path(base, parsed.network_plan.mock.fixture),
            network_plan_api_url=parsed.network_plan.http.url,
            network_plan_http_query_parameter=(
                parsed.network_plan.http.query_parameter
            ),
            network_plan_timeout_seconds=parsed.network_plan.http.timeout_seconds,
            network_plan_batch_timeout_seconds=(
                parsed.network_plan.http.batch_timeout_seconds
            ),
            network_plan_max_concurrency=parsed.network_plan.http.max_concurrency,
            network_plan_max_subnets_per_request=(
                parsed.evaluation.network_plan_max_subnets
            ),
            network_plan_cache_ttl_seconds=parsed.network_plan.cache.ttl_seconds,
            network_plan_cache_max_entries=parsed.network_plan.cache.max_entries,
            max_evaluation_items=parsed.evaluation.max_items,
            acl_max_concurrency=parsed.acl.max_concurrency,
            acl_decision_mode=parsed.acl.decision_mode,
            acl_deterministic_pending_mode=(
                parsed.acl.deterministic_pending_mode
            ),
            acl_api_token=(
                parsed.acl.http.auth.token
                if parsed.acl.http.auth.type == "bearer"
                else None
            ),
            network_plan_api_token=(
                parsed.network_plan.http.auth.token
                if parsed.network_plan.http.auth.type == "bearer"
                else None
            ),
            llm_temperature=parsed.llm.http.temperature,
            llm_max_tokens=parsed.llm.http.max_tokens,
            llm_top_p=parsed.llm.http.top_p,
            llm_stop=parsed.llm.http.stop,
            llm_thinking=parsed.llm.http.thinking,
            semantic_effects=parsed.evaluation.semantic_effects.as_dict(),
            config_id=parsed.config_id,
            environment=parsed.environment,
            config_fingerprint=config_fingerprint,
        )
        settings.validate()
        return cls(
            config_id=parsed.config_id,
            environment=parsed.environment,
            path=config_path,
            server=ServerSettings(parsed.server.host, parsed.server.port),
            requirement_source=RequirementSourceSettings(
                mode=requirement.mode,
                execution_mode=requirement.execution.mode,
                batch_size=requirement.execution.batch_size,
                poll_interval_seconds=requirement.execution.poll_interval_seconds,
                local_directory=(
                    _resolve_path(base, local.directory) if local is not None else None
                ),
                local_pattern=local.pattern if local is not None else "*.json",
                local_recursive=local.recursive if local is not None else False,
                api_url=api.url if api is not None else None,
                api_method=api.method if api is not None else "GET",
                api_timeout_seconds=api.timeout_seconds if api is not None else 30.0,
                api_token=(
                    api.auth.token
                    if api is not None and api.auth.type == "bearer"
                    else None
                ),
                output_file=_resolve_path(base, requirement.output_file) or base,
            ),
            settings=settings,
        )


def _config_fingerprint(config: _FareYamlConfig) -> str:
    canonical = json.dumps(
        config.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
