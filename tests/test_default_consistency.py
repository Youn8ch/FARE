"""AC-07 acceptance: the four default entry points agree.

1. Settings dataclass defaults (direct construction)
2. YAML schema defaults (minimal profile through FareConfig.load)
3. tests/conftest.py settings fixture (explicit declaration)
4. config/fare.yaml dev profile + README startup command

Dev/test target values: network_plan.mode=mock (versioned fixture),
llm.mode=mock, llm.features.request_findings_mode=off. offline_catalog only
exists when explicitly declared.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import FareConfig, Settings
from app.main import build_runtime, create_app
from tests.conftest import settings as conftest_settings_fixture

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FARE_YAML = PROJECT_ROOT / "config/fare.yaml"

REQUIRED_SETTINGS = dict(
    policy_dir=PROJECT_ROOT / "policies",
    audit_log_dir=Path("unused"),
    audit_log_retention_days=30,
    llm_client_mode="mock",
    llm_base_url=None,
    llm_model=None,
    llm_api_key=None,
    llm_mock_file=None,
    llm_semantic_timeout_seconds=10.0,
    llm_explanation_timeout_seconds=6.0,
    llm_max_correction_retries=1,
    max_concurrent_evaluations=4,
)


def _modes(settings: Settings) -> dict[str, object]:
    return {
        "network_plan": settings.network_plan_client_mode,
        "llm": settings.llm_client_mode,
        "request_findings_feature": settings.llm_request_findings_mode,
    }


DEV_DEFAULTS = {
    "network_plan": "mock",
    "llm": "mock",
    "request_findings_feature": "off",
}


def test_entry1_settings_dataclass_defaults_match_dev_defaults() -> None:
    settings = Settings(**REQUIRED_SETTINGS)
    assert _modes(settings) == DEV_DEFAULTS
    assert settings.network_plan_mock_file is None  # fixture 由调用方显式绑定


def test_entry2_yaml_schema_defaults_match_dev_defaults(tmp_path: Path) -> None:
    raw = yaml.safe_load(FARE_YAML.read_text(encoding="utf-8"))
    raw["config_id"] = "fare-schema-defaults"
    raw["policy"]["directory"] = str(PROJECT_ROOT / "policies")
    raw["audit"]["directory"] = str(tmp_path / "audit")
    raw["requirement_source"] = {
        "mode": "local",
        "local": {
            "directory": str(PROJECT_ROOT / "inputs/network_requirements"),
            "pattern": "*.json",
        },
        "output_file": str(tmp_path / "out.json"),
    }
    raw["llm"].pop("http", None)
    raw["llm"].pop("features", None)
    config_path = tmp_path / "minimal.yaml"
    config_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    config = FareConfig.load(config_path)
    assert config.settings.llm_request_findings_mode == "off"
    # schema-level required keys keep every profile explicit about its modes
    assert config.settings.network_plan_client_mode == "mock"
    assert config.settings.llm_client_mode == "mock"


def test_entry3_conftest_fixture_declares_every_mode_key(settings) -> None:
    assert _modes(settings) == DEV_DEFAULTS
    assert settings.network_plan_mock_file is not None
    assert settings.network_plan_mock_file.is_file()
    # 显式声明的键必须覆盖全部 mode / decision_mode / features
    import inspect

    source = inspect.getsource(conftest_settings_fixture)
    for key in (
        "network_plan_client_mode",
        "llm_request_findings_mode",
    ):
        assert key in source, f"conftest fixture must declare {key} explicitly"


def test_entry4_dev_profile_matches_dev_defaults() -> None:
    config = FareConfig.load(FARE_YAML)
    assert _modes(config.settings) == DEV_DEFAULTS
    raw = yaml.safe_load(FARE_YAML.read_text(encoding="utf-8"))
    fixture = raw["network_plan"]["mock"]["fixture"]
    assert fixture
    resolved = (FARE_YAML.parent / fixture).resolve()
    assert resolved.is_file()
    assert config.settings.network_plan_mock_file == resolved


def test_dev_profile_starts_and_serves_documented_request(settings) -> None:
    """README 启动验收：dev 默认（mock 链路）可复现文档示例请求。"""

    runtime = build_runtime(FareConfig.load(FARE_YAML).settings)
    try:
        assert runtime.network_plan_resolver is not None
    finally:
        import asyncio

        asyncio.run(runtime.aclose())

    with TestClient(create_app(settings)) as client:
        assert client.app.state.runtime.network_plan_resolver is not None
        response = client.post(
            "/v2/evaluations",
            json={
                "request_id": "ac07-dev-default",
                "sources": [{"address": "16.1.30.10", "description": "生产应用"}],
                "destinations": [{"address": "16.1.30.20", "description": "生产应用"}],
                "protocol": "tcp",
                "ports": [{"start": 443, "end": 443}],
                "request_description": "生产应用内部 HTTPS 访问",
            },
        )
    assert response.status_code == 200
    assert response.json()["decision"] == "合规"


@pytest.mark.parametrize(
    "profile",
    [
        "config/fare.yaml",
        "config/fare.external-test.yaml",
        "config/fare.intranet-uat.yaml",
        "config/fare.production.yaml",
        "config/fare.test-area-relations.yaml",
    ],
)
def test_every_profile_declares_modes_explicitly(profile: str) -> None:
    """offline_catalog 不允许成为隐式默认：每个 profile 显式声明。"""

    raw = yaml.safe_load((PROJECT_ROOT / profile).read_text(encoding="utf-8"))
    assert raw["network_plan"]["mode"] in {"mock", "http", "offline_catalog"}
    assert raw["llm"]["mode"] in {"mock", "http"}
    features = raw["llm"].get("features", {})
    assert features.get("request_findings_mode", "off") in {"off", "shadow"}
    # 已移除能力的配置键不允许在任何现役 profile 中出现
    assert "acl" not in raw
    assert "acl_candidate_mode" not in features


def test_removed_capability_config_keys_fail_closed(tmp_path: Path) -> None:
    """配置 schema extra='forbid'：残留的已移除配置键必须显式启动失败。"""

    raw = yaml.safe_load(FARE_YAML.read_text(encoding="utf-8"))
    raw["config_id"] = "fare-legacy-keys"
    raw["policy"]["directory"] = str(PROJECT_ROOT / "policies")
    raw["audit"]["directory"] = str(tmp_path / "audit")
    raw["requirement_source"] = {
        "mode": "local",
        "local": {
            "directory": str(PROJECT_ROOT / "inputs/network_requirements"),
            "pattern": "*.json",
        },
        "output_file": str(tmp_path / "out.json"),
    }
    raw["acl"] = {"mode": "mock"}
    raw["llm"].setdefault("features", {})["acl_candidate_mode"] = "off"
    config_path = tmp_path / "legacy.yaml"
    config_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    from pytest import raises

    with raises(ValidationError) as excinfo:
        FareConfig.load(config_path)
    message = str(excinfo.value)
    assert "acl" in message
