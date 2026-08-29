from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.__main__ import configuration_summary
from app.config import FareConfig

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config/fare.yaml"


def test_single_yaml_configuration_maps_all_runtime_sections() -> None:
    config = FareConfig.load(CONFIG_PATH)

    assert config.config_id == "fare-local-test"
    assert config.settings.config_id == "fare-local-test"
    assert config.settings.environment == "test"
    assert len(config.settings.config_fingerprint) == 64
    assert config.settings.policy_dir == PROJECT_ROOT / "policies"
    assert config.requirement_source.mode == "local"
    assert config.requirement_source.local_directory == (
        PROJECT_ROOT / "inputs/network_requirements"
    )
    assert config.settings.network_plan_client_mode == "mock"
    assert config.settings.acl_client_mode == "mock"
    assert config.settings.acl_decision_mode == "advisory"
    assert config.settings.acl_deterministic_pending_mode == "skip"
    assert config.settings.llm_client_mode == "mock"
    assert config.settings.llm_acl_candidate_mode == "off"
    assert config.settings.llm_request_findings_mode == "off"
    assert config.requirement_source.api_url == (
        "http://127.0.0.1:9000/v1/network-requirements"
    )
    assert config.settings.acl_api_url == "http://127.0.0.1:9002/v1/acl/analyze"
    assert config.settings.acl_api_token == "test-acl-token"
    assert config.settings.network_plan_api_token == "test-network-plan-token"
    assert config.settings.llm_base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert config.settings.llm_model == "glm-4.5-air"
    assert config.settings.llm_api_key is None
    assert config.settings.llm_max_correction_retries == 2
    assert config.settings.llm_temperature == 0
    assert config.settings.llm_max_tokens == 4096
    assert config.settings.llm_top_p == 1
    assert config.settings.llm_thinking == "disabled"
    assert config.settings.semantic_effects == {
        "fact_conflict": "review_required",
        "contradiction": "review_required",
        "temporary_permanent_conflict": "review_required",
        "purpose_target_mismatch": "review_required",
        "mixed_business_context": "review_required",
        "approval_scope_mismatch": "review_required",
        "unclassified_privileged_access": "observe_only",
    }


def test_yaml_configuration_does_not_read_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POLICY_DIR", "should-not-be-used")
    monkeypatch.setenv("LLM_API_KEY", "should-not-be-used")

    config = FareConfig.load(CONFIG_PATH)

    assert config.settings.policy_dir == PROJECT_ROOT / "policies"
    assert config.settings.llm_api_key != "should-not-be-used"


def test_yaml_paths_are_resolved_from_configuration_directory(tmp_path: Path) -> None:
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    raw["policy"]["directory"] = "policy"
    raw["audit"]["directory"] = "audit"
    raw["requirement_source"]["local"]["directory"] = "requirements"
    config_path = tmp_path / "fare.yaml"
    config_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    config = FareConfig.load(config_path)

    assert config.settings.policy_dir == tmp_path / "policy"
    assert config.settings.audit_log_dir == tmp_path / "audit"
    assert config.requirement_source.local_directory == tmp_path / "requirements"


def test_unknown_yaml_field_fails_closed(tmp_path: Path) -> None:
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    raw["unknown_setting"] = True
    config_path = tmp_path / "fare.yaml"
    config_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ValidationError, match="unknown_setting"):
        FareConfig.load(config_path)


def test_unknown_semantic_effect_fails_closed(tmp_path: Path) -> None:
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    raw["evaluation"]["semantic_effects"]["generic_risk"] = "review_required"
    config_path = tmp_path / "fare.yaml"
    config_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ValidationError, match="generic_risk"):
        FareConfig.load(config_path)


def test_api_mode_requires_requirement_api_section(tmp_path: Path) -> None:
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    raw["requirement_source"]["mode"] = "api"
    raw["requirement_source"]["api"] = None
    config_path = tmp_path / "fare.yaml"
    config_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ValidationError, match="requirement_source.api"):
        FareConfig.load(config_path)


def test_network_plan_http_mode_requires_query_parameter(tmp_path: Path) -> None:
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    raw["network_plan"]["mode"] = "http"
    raw["network_plan"]["http"]["query_parameter"] = None
    config_path = tmp_path / "fare.yaml"
    config_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ValidationError, match="query_parameter"):
        FareConfig.load(config_path)


def test_direct_settings_construction_remains_available_for_unit_tests() -> None:
    config = FareConfig.load(CONFIG_PATH)
    updated = replace(config.settings, max_evaluation_items=1)
    assert updated.max_evaluation_items == 1


def test_deployment_profiles_are_valid_and_isolated() -> None:
    paths = [
        PROJECT_ROOT / "config/fare.external-test.yaml",
        PROJECT_ROOT / "config/fare.intranet-uat.yaml",
        PROJECT_ROOT / "config/fare.production.yaml",
    ]
    profiles = [FareConfig.load(path) for path in paths]

    assert [profile.environment for profile in profiles] == [
        "external-test",
        "intranet-uat",
        "production",
    ]
    assert [profile.settings.network_plan_client_mode for profile in profiles] == [
        "offline_catalog",
        "http",
        "http",
    ]
    assert [profile.settings.acl_client_mode for profile in profiles] == [
        "mock",
        "mock",
        "http",
    ]
    assert len({profile.settings.config_fingerprint for profile in profiles}) == 3
    assert len({profile.settings.audit_log_dir for profile in profiles}) == 3
    assert len({profile.requirement_source.output_file for profile in profiles}) == 3


def test_tracked_config_profiles_do_not_embed_llm_api_keys() -> None:
    for path in sorted((PROJECT_ROOT / "config").glob("fare*.yaml")):
        if path.name == "fare.local.yaml":
            continue
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert raw["llm"]["http"]["api_key"] is None, (
            f"{path.name} must not embed an llm api_key; move it to fare.local.yaml"
        )


def test_configuration_summary_exposes_profile_identity_without_loading_runtime() -> None:
    config = FareConfig.load(PROJECT_ROOT / "config/fare.intranet-uat.yaml")

    summary = configuration_summary(config)

    assert summary["status"] == "valid"
    assert summary["config_id"] == "fare-intranet-uat"
    assert summary["environment"] == "intranet-uat"
    assert summary["config_fingerprint"] == config.settings.config_fingerprint
    assert summary["network_plan_mode"] == "http"
    assert summary["acl_mode"] == "mock"
    assert summary["acl_deterministic_pending_mode"] == "skip"
