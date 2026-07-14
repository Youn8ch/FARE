from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        policy_dir=Path("policies").resolve(),
        audit_log_dir=tmp_path / "audit",
        audit_log_retention_days=30,
        acl_client_mode="mock",
        acl_mock_file=None,
        acl_api_url=None,
        acl_timeout_seconds=10,
        llm_client_mode="mock",
        llm_base_url=None,
        llm_model=None,
        llm_api_key=None,
        llm_mock_file=None,
        llm_semantic_timeout_seconds=10,
        llm_explanation_timeout_seconds=6,
        llm_max_correction_retries=1,
        max_concurrent_evaluations=4,
    )


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def payload(**updates):
    value = {
        "request_id": "fare-test-001",
        "sources": [{"address": "16.1.30.10", "description": "生产应用 A"}],
        "destinations": [{"address": "16.1.30.20", "description": "生产应用 B"}],
        "protocol": "tcp",
        "ports": [{"start": 443, "end": 443}],
        "request_description": "生产应用 HTTPS 访问",
    }
    value.update(updates)
    return value
