"""Realistic network request suite (PHASE-01 migration baseline).

One frozen dataset (``realistic_network_requests.v1.json``) drives two
contracts:

* ``legacy`` — the ACL-present runtime; used only for migration evidence.
* ``acl_free`` — the target contract; the final acceptance gate.

``ACTIVE_CONTRACT`` flips to ``acl_free`` in the ACL-removal phase; from that
commit on, ``legacy_expected`` is no longer asserted anywhere.
"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.schemas import (
    LlmExplanationResponse,
    LlmRequestFindingsResponse,
    LlmSemanticResponse,
)
from tests.case_schema import RealisticCase
from tests.helpers.case_loader import (
    FIXTURE_ROOT,
    REALISTIC_POLICY_DIR,
    load_realistic_suite,
    prepare_realistic_llm_fixtures,
    realistic_item_ids,
)
from tests.helpers.llm import RecordingLlmClient

TEST_ROOT = Path(__file__).resolve().parent
SUITE_PATH = TEST_ROOT / "cases" / "evaluations" / "realistic_network_requests.v1.json"
SUITE = load_realistic_suite(SUITE_PATH)

# PHASE-01 freezes legacy evidence against the ACL-present runtime. The ACL
# removal phase flips this constant to "acl_free" in its own commit; after
# that flip the legacy expectations are dead evidence and never asserted.
ACTIVE_CONTRACT: Literal["legacy", "acl_free"] = "acl_free"

NETWORK_PLAN_FIXTURE_ROOT = FIXTURE_ROOT / "network_plan"
DOCUMENTATION_BLOCKS = (
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
)

Loaded = tuple[RealisticCase, list[str], dict[str, dict[str, Any]]]


def _loaded(case: RealisticCase) -> Loaded:
    item_ids = realistic_item_ids(
        case.request.request_id,
        len(case.request.sources),
        len(case.request.destinations),
        len(case.request.ports),
    )
    llm_fixtures = prepare_realistic_llm_fixtures(case.llm_profile, item_ids)
    return case, item_ids, llm_fixtures


LOADED: list[Loaded] = [_loaded(case) for case in SUITE.cases]


def _recorder(loaded: Loaded) -> RecordingLlmClient:
    case, _, llm_fixtures = loaded
    semantic = llm_fixtures.get("semantic", {}).get("semantic")
    findings = llm_fixtures.get("request_findings", {}).get("request_findings")
    explanation = llm_fixtures.get("explanation", {}).get("explanation")
    return RecordingLlmClient(
        fail_stage=case.llm_profile.fail_stage,
        semantic_response=(
            LlmSemanticResponse.model_validate(semantic) if semantic else None
        ),
        request_findings=(
            LlmRequestFindingsResponse.model_validate(findings) if findings else None
        ),
        explanation_response=(
            LlmExplanationResponse.model_validate(explanation)
            if explanation is not None
            else None
        ),
    )


def _case_settings(settings: Settings, loaded: Loaded) -> Settings:
    case = loaded[0]
    network_fixture = (
        NETWORK_PLAN_FIXTURE_ROOT / case.dependency_profile.network_plan_fixture
    )
    if not network_fixture.is_file():
        raise ValueError(f"missing network plan fixture: {network_fixture}")
    case_settings = replace(
        settings,
        policy_dir=REALISTIC_POLICY_DIR,
        network_plan_client_mode="mock",
        network_plan_mock_file=network_fixture,
        llm_request_findings_mode=case.llm_profile.request_findings_mode,
    )
    overrides = case.settings_overrides
    if overrides is not None:
        if overrides.max_evaluation_items is not None:
            case_settings = replace(
                case_settings, max_evaluation_items=overrides.max_evaluation_items
            )
        if overrides.network_plan_max_subnets is not None:
            case_settings = replace(
                case_settings,
                network_plan_max_subnets_per_request=overrides.network_plan_max_subnets,
            )
    return case_settings


def _expected_for(case: RealisticCase) -> dict[str, Any]:
    if ACTIVE_CONTRACT == "legacy":
        return case.legacy_expected.model_dump(mode="json")
    return case.target_expected.model_dump(mode="json")


def _assert_items(
    body: dict[str, Any], expected: dict[str, Any], item_ids: list[str]
) -> None:
    assert len(body["items"]) == len(expected["items"])
    for actual, exp in zip(body["items"], expected["items"], strict=True):
        assert actual["item_id"] == item_ids[exp["index"] - 1]
        assert actual["access"] == exp["access"]
        assert actual["decision"] == exp["decision"]
        assert actual["reason_type"] == exp["reason_type"]
        assert actual["reason_code"] == exp["reason_code"]
        assert [rule["id"] for rule in actual["matched_rules"]] == exp["matched_rule_ids"]
        assert [finding["code"] for finding in actual["decision_findings"]] == (
            exp["finding_codes"]
        )
        primary = [f for f in actual["decision_findings"] if f["is_primary"]]
        if exp["primary_finding_code"] is None:
            assert primary == []
        else:
            assert len(primary) == 1
            assert primary[0]["code"] == exp["primary_finding_code"]
        if expected.get("semantic_effect") is not None:
            assert actual["decision_trace"]["semantic_effect"] == expected["semantic_effect"]


def _assert_llm_surface(
    body: dict[str, Any], recorder: RecordingLlmClient, expected: dict[str, Any]
) -> None:
    calls = expected["llm_calls"]
    assert recorder.semantic_calls == calls["semantic"]
    assert recorder.request_finding_calls == calls["request_findings"]
    assert recorder.explanation_calls == calls["explanation"]

    if expected.get("request_findings") is not None:
        rf = body["request_findings"]
        assert rf["status"] == expected["request_findings"]["status"]
        assert len(rf["findings"]) == expected["request_findings"]["finding_count"]
    else:
        assert "request_findings" not in body

    llm_text = expected.get("llm_text")
    if llm_text is not None:
        has_llm_text = all(
            "llm_explanation" in item and "llm_recommendation" in item
            for item in body["items"]
        )
        assert has_llm_text is (llm_text["presence"] == "present")
        assert all(
            (item["explanation_source"] == "template") is llm_text["fallback"]
            for item in body["items"]
        )


def _assert_acl_free(body: dict[str, Any]) -> None:
    serialized = str(body)
    assert "acl_analysis" not in serialized
    assert "acl_candidate" not in serialized
    assert "acl_verification_status" not in serialized
    assert "ACL_" not in serialized
    assert "acl_no_path" not in serialized


@pytest.mark.parametrize("loaded", LOADED, ids=lambda loaded: loaded[0].id)
def test_realistic_network_request(loaded: Loaded, settings: Settings) -> None:
    case, item_ids, _ = loaded
    expected = _expected_for(case)
    recorder = _recorder(loaded)
    case_settings = _case_settings(settings, loaded)
    with TestClient(create_app(case_settings)) as client:
        client.app.state.runtime.evaluator.llm_client = recorder
        payload = case.request.model_dump(mode="json")
        response = client.post("/v2/evaluations", json=payload)

        if case.replay is not None:
            _run_replay(client, case, payload, expected, recorder, item_ids)
            return

        body = response.json()
        assert response.status_code == expected["http_status"]
        if expected["error_code"] is not None:
            assert body["error"]["code"] == expected["error_code"]
        else:
            assert body["request_id"] == case.request.request_id
            assert body["decision"] == expected["decision"]
            _assert_items(body, expected, item_ids)
        _assert_llm_surface(body, recorder, expected)
        _assert_acl_free(body)


def _run_replay(
    client: TestClient,
    case: RealisticCase,
    payload: dict[str, Any],
    expected: dict[str, Any],
    recorder: RecordingLlmClient,
    item_ids: list[str],
) -> None:
    first = client.post("/v2/evaluations", json=payload)
    assert first.status_code == expected["http_status"]
    first_body = first.json()
    assert first_body["decision"] == expected["decision"]
    _assert_items(first_body, expected, item_ids)

    if case.replay.mode == "same":
        second = client.post("/v2/evaluations", json=payload)
        assert second.status_code == 200
        second_body = second.json()
        second_body.pop("audit_id", None)
        first_body.pop("audit_id", None)
        assert second_body == first_body
    else:
        conflicted = dict(payload)
        conflicted["request_description"] = (
            payload["request_description"] + " 修改后的不同输入。"
        )
        second = client.post("/v2/evaluations", json=conflicted)
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "idempotency_conflict"

    _assert_llm_surface(first_body, recorder, expected)
    _assert_acl_free(first_body)


def test_realistic_suite_metadata() -> None:
    assert SUITE.suite == "realistic_network"
    assert len(SUITE.cases) >= 24
    ids = [case.id for case in SUITE.cases]
    assert len(ids) == len(set(ids))
    for case in SUITE.cases:
        assert case.approved_differences, f"{case.id} needs approved differences"
        assert case.invariants, f"{case.id} needs invariants"


def test_realistic_acl_migration_cases_carry_approved_differences() -> None:
    migration_ids = {f"RN-{number:03d}" for number in range(29, 36)}
    migration = [case for case in SUITE.cases if case.id in migration_ids]
    assert len(migration) == len(migration_ids)
    for case in migration:
        assert any("批准" in diff for diff in case.approved_differences), case.id
        # The legacy evidence is retained verbatim but is never the gate.
        assert case.legacy_expected.model_dump(mode="json") is not None


def test_realistic_addresses_are_documentation_reserved() -> None:
    for case in SUITE.cases:
        for endpoint in (*case.request.sources, *case.request.destinations):
            if endpoint.address.lower() == "any":
                continue
            network = ipaddress.ip_network(endpoint.address, strict=False)
            assert any(network.subnet_of(block) for block in DOCUMENTATION_BLOCKS), case.id
    for fixture in (NETWORK_PLAN_FIXTURE_ROOT / "realistic").glob("*.json"):
        document = json.loads(fixture.read_text(encoding="utf-8"))
        assert document.get("fixture_version")
        assert document.get("purpose")
