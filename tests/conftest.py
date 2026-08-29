from __future__ import annotations

import socket
from ipaddress import ip_address
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# dev/test 默认 fixture（AC-07）：显式绑定版本化网段规划 fixture，禁止依赖
# dataclass 隐式默认或离线目录兼容链路。
CORE_NETWORK_PLAN_FIXTURE = PROJECT_ROOT / "tests/fixtures/network_plan/core_catalog.v1.json"


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allow test-runner loopback sockets while blocking external connections."""

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def is_loopback(address) -> bool:
        if not isinstance(address, tuple) or not address:
            return True
        try:
            return ip_address(address[0]).is_loopback
        except ValueError:
            return False

    def guarded_connect(sock, address):
        if is_loopback(address):
            return original_connect(sock, address)
        raise AssertionError("real network access is forbidden in the default test suite")

    def guarded_connect_ex(sock, address):
        if is_loopback(address):
            return original_connect_ex(sock, address)
        raise AssertionError("real network access is forbidden in the default test suite")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Explicit dev/test defaults shared by every entry point (AC-07).

    Every mode / decision_mode / feature key is declared here; this fixture
    must not rely on Settings dataclass defaults.
    """

    return Settings(
        policy_dir=Path("policies").resolve(),
        audit_log_dir=tmp_path / "audit",
        audit_log_retention_days=30,
        acl_client_mode="mock",
        acl_decision_mode="advisory",
        acl_deterministic_pending_mode="analyze",
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
        llm_acl_candidate_mode="off",
        llm_request_findings_mode="off",
        network_plan_client_mode="mock",
        network_plan_mock_file=CORE_NETWORK_PLAN_FIXTURE,
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
