from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import Settings
from app.main import create_app
from app.schemas import EvaluationRequest
from app.services.acl_client import MockAclClient
from app.services.network_plan_client import MockNetworkPlanClient
from app.services.rule_loader import PolicyBundle

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RULE_CASE_ROOT = PROJECT_ROOT / "tests/cases/network_plan_rules"
POLICY_FIXTURE_ROOT = PROJECT_ROOT / "tests/fixtures/policies"
INVALID_CASE_FILE = RULE_CASE_ROOT / "invalid_packages.json"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ExpectedAccess(_StrictModel):
    source: str
    destination: str


class _ExpectedResult(_StrictModel):
    decision: Literal["合规", "待定"]
    lookup_count: int = Field(ge=0)
    acl_call_count: int = Field(ge=0)
    accesses: list[_ExpectedAccess] = Field(min_length=1)
    reason_codes: list[str | None]
    matched_rule_ids: list[list[str]]
    source_statuses: list[str]
    destination_statuses: list[str]
    acl_statuses: list[str]

    @model_validator(mode="after")
    def aligned_item_expectations(self) -> _ExpectedResult:
        lengths = {
            len(self.accesses),
            len(self.reason_codes),
            len(self.matched_rule_ids),
            len(self.source_statuses),
            len(self.destination_statuses),
            len(self.acl_statuses),
        }
        if len(lengths) != 1:
            raise ValueError("all expected item arrays must have the same length")
        return self


class _RuleRequirementCase(_StrictModel):
    id: str
    request: EvaluationRequest
    expected: _ExpectedResult


class _RuleRequirementGroup(_StrictModel):
    fixture_version: Literal["2026.08.0"]
    purpose: str = Field(min_length=1)
    policy_dir: str
    network_plan_fixture: str
    cases: list[_RuleRequirementCase] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids_and_requests(self) -> _RuleRequirementGroup:
        ids = [case.id for case in self.cases]
        request_ids = [case.request.request_id for case in self.cases]
        if len(ids) != len(set(ids)) or len(request_ids) != len(set(request_ids)):
            raise ValueError("rule case and request IDs must be unique within a group")
        return self


class _InvalidRuleCase(_StrictModel):
    id: str
    policy_dir: str
    error_contains: str


class _InvalidRuleSuite(_StrictModel):
    fixture_version: Literal["2026.08.0"]
    purpose: str
    cases: list[_InvalidRuleCase] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class _LoadedRuleCase:
    group_path: Path
    group: _RuleRequirementGroup
    case: _RuleRequirementCase

    @property
    def test_id(self) -> str:
        return f"{self.group_path.stem}:{self.case.id}"


def _load_groups() -> list[tuple[Path, _RuleRequirementGroup]]:
    paths = sorted(RULE_CASE_ROOT.glob("*.requirements.json"))
    if len(paths) < 10:
        raise ValueError("at least ten local network rule requirement groups are required")
    groups = [
        (path, _RuleRequirementGroup.model_validate_json(path.read_text(encoding="utf-8")))
        for path in paths
    ]
    policy_dirs = [group.policy_dir for _, group in groups]
    if len(policy_dirs) != len(set(policy_dirs)):
        raise ValueError("each requirement JSON must represent one independent rule package")
    case_ids = [case.id for _, group in groups for case in group.cases]
    request_ids = [case.request.request_id for _, group in groups for case in group.cases]
    if len(case_ids) != len(set(case_ids)) or len(request_ids) != len(set(request_ids)):
        raise ValueError("case and request IDs must be globally unique")
    return groups


VALID_GROUPS = _load_groups()
VALID_CASES = [
    _LoadedRuleCase(path, group, case)
    for path, group in VALID_GROUPS
    for case in group.cases
]
INVALID_SUITE = _InvalidRuleSuite.model_validate_json(
    INVALID_CASE_FILE.read_text(encoding="utf-8")
)


def _repo_path(relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("test asset path must be a contained repository-relative path")
    path = (PROJECT_ROOT / candidate).resolve(strict=True)
    if not path.is_relative_to(PROJECT_ROOT):
        raise ValueError("test asset path escapes the repository")
    return path


@pytest.mark.parametrize("loaded", VALID_CASES, ids=lambda loaded: loaded.test_id)
def test_local_network_rule_requirement_group(
    loaded: _LoadedRuleCase, settings: Settings
) -> None:
    case = loaded.case
    policy_dir = _repo_path(loaded.group.policy_dir)
    network_plan_fixture = _repo_path(loaded.group.network_plan_fixture)
    assert not (policy_dir / "network_catalog.yaml").exists()
    configured = replace(
        settings,
        policy_dir=policy_dir,
        network_plan_client_mode="mock",
        network_plan_mock_file=network_plan_fixture,
        acl_decision_mode="advisory",
    )
    with TestClient(create_app(configured)) as client:
        runtime = client.app.state.runtime
        response = client.post(
            "/v1/evaluations", json=case.request.model_dump(mode="json")
        )
        resolver = runtime.network_plan_resolver
        assert resolver is not None
        assert isinstance(resolver.client, MockNetworkPlanClient)
        assert isinstance(runtime.evaluator.acl_client, MockAclClient)
        lookup_calls = list(resolver.client.calls)
        acl_calls = list(runtime.evaluator.acl_client.calls)

    expected = case.expected
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == expected.decision
    assert len(body["network_analysis"]["lookups"]) == expected.lookup_count
    assert len(lookup_calls) == len(set(lookup_calls)) == expected.lookup_count
    assert len(acl_calls) == expected.acl_call_count
    assert len(body["items"]) == len(expected.accesses)
    for index, item in enumerate(body["items"]):
        assert item["access"]["source"] == expected.accesses[index].source
        assert item["access"]["destination"] == expected.accesses[index].destination
        assert item["reason_code"] == expected.reason_codes[index]
        assert [rule["id"] for rule in item["matched_rules"]] == (
            expected.matched_rule_ids[index]
        )
        assert item["source_network_fact_status"] == expected.source_statuses[index]
        assert (
            item["destination_network_fact_status"]
            == expected.destination_statuses[index]
        )
        assert item["acl_verification_status"] == expected.acl_statuses[index]


@pytest.mark.parametrize("case", INVALID_SUITE.cases, ids=lambda case: case.id)
def test_invalid_network_plan_rule_package_fails_closed(
    case: _InvalidRuleCase,
) -> None:
    with pytest.raises(ValueError, match=case.error_contains):
        PolicyBundle.load(_repo_path(case.policy_dir))


def test_at_least_ten_new_valid_rule_packages_are_independently_loadable() -> None:
    new_package_names = {
        "network_plan_area_direction",
        "network_plan_region_relation",
        "network_plan_platform_relation",
        "network_plan_usage_direction",
        "network_plan_special_ports",
        "network_plan_any_address",
        "network_plan_prefix_limit",
        "network_plan_port_span",
        "network_plan_combination_count",
        "network_plan_multi_condition",
    }
    package_paths = [POLICY_FIXTURE_ROOT / name for name in sorted(new_package_names)]
    assert len(package_paths) >= 10
    bundles = [PolicyBundle.load(path) for path in package_paths]
    assert len({bundle.version for bundle in bundles}) == len(package_paths)
    assert all("ACL-PATH-001" in bundle.rule_ids for bundle in bundles)


def test_all_valid_network_plan_rule_packages_remain_loadable() -> None:
    package_paths = [
        path
        for path in POLICY_FIXTURE_ROOT.glob("network_plan_*")
        if path.is_dir() and "_invalid_" not in path.name
    ]
    assert len(package_paths) >= 13
    assert all(PolicyBundle.load(path).rule_ids for path in package_paths)


def test_requirement_files_are_local_versioned_groups() -> None:
    assert len(VALID_GROUPS) >= 10
    for path, group in VALID_GROUPS:
        document = json.loads(path.read_text(encoding="utf-8"))
        assert path.parent == RULE_CASE_ROOT
        assert document["fixture_version"] == "2026.08.0"
        assert document["purpose"] == group.purpose
        assert group.cases
