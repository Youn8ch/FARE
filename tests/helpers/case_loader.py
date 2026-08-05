from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.catalog import NetworkCatalog
from app.services.splitter import split_request
from tests.case_schema import CaseSuite, EvaluationCase

TEST_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = TEST_ROOT.parent
FIXTURE_ROOT = TEST_ROOT / "fixtures"
LLM_FIXTURE_ROOT = FIXTURE_ROOT / "llm"
POLICY_ROOTS = (REPO_ROOT / "policies", FIXTURE_ROOT / "policies")
SYMBOL_PATTERN = re.compile(r"@item:([1-9][0-9]*)\Z")
SYMBOL_FIELDS = {"scope", "item_id"}
SYMBOL_LIST_FIELDS = {"analyzed_item_ids", "affected_item_ids"}


@dataclass(frozen=True, slots=True)
class LoadedCase:
    case: EvaluationCase
    item_ids: list[str]
    acl_fixture: Path | None
    policy_dir: Path
    llm_fixtures: dict[str, dict[str, Any]]


def load_suite(path: Path) -> CaseSuite:
    document = json.loads(path.read_text(encoding="utf-8"))
    suite = CaseSuite.model_validate(document)
    expected_name = path.name.removesuffix(".v2.json")
    if suite.suite != expected_name:
        raise ValueError("suite name does not match its v2 filename")
    return suite


def load_all_suites(case_root: Path) -> list[CaseSuite]:
    suites = [load_suite(path) for path in sorted(case_root.glob("*.v2.json"))]
    case_ids = [case.id for suite in suites for case in suite.cases]
    request_ids = [case.request.request_id for suite in suites for case in suite.cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("case IDs must be globally unique")
    if len(request_ids) != len(set(request_ids)):
        raise ValueError("request IDs must be globally unique")
    return suites


def prepare_case(case: EvaluationCase) -> LoadedCase:
    policy_dir = _policy_path(case.dependencies.policy_dir)
    catalog = NetworkCatalog.load(policy_dir / "network_catalog.yaml")
    raw_count = (
        len(case.request.sources)
        * len(case.request.destinations)
        * len(case.request.ports)
    )
    if raw_count > 4:
        raise ValueError("ordinary end-to-end cases may not exceed four combinations")
    combinations = split_request(case.request, catalog)
    if len(combinations) > 4:
        raise ValueError("ordinary end-to-end cases may not exceed four combinations")
    if len(case.expected.items) != len(combinations):
        raise ValueError("expected item set must match production splitter output")

    item_ids = [
        f"{case.request.request_id}-{index:03d}"
        for index in range(1, len(combinations) + 1)
    ]
    acl_fixture = (
        _safe_path(FIXTURE_ROOT, case.dependencies.acl_fixture)
        if case.dependencies.acl_fixture
        else None
    )
    if acl_fixture:
        _validate_fixture_metadata(_read_json(acl_fixture), acl_fixture)

    llm_fixtures: dict[str, dict[str, Any]] = {}
    for stage, relative in (
        ("semantic", case.llm.semantic_fixture),
        ("acl_candidates", case.llm.acl_candidate_fixture),
        ("request_findings", case.llm.request_finding_fixture),
        ("explanation", case.llm.explanation_fixture),
    ):
        if relative is None:
            continue
        fixture_path = _safe_path(LLM_FIXTURE_ROOT, relative)
        document = _read_json(fixture_path)
        _validate_fixture_metadata(document, fixture_path)
        llm_fixtures[stage] = resolve_item_symbols(document, item_ids)

    return LoadedCase(case, item_ids, acl_fixture, policy_dir, llm_fixtures)


def resolve_item_symbols(value: Any, item_ids: list[str], field: str | None = None) -> Any:
    if isinstance(value, dict):
        return {
            key: resolve_item_symbols(item, item_ids, key)
            for key, item in value.items()
        }
    if isinstance(value, list):
        resolved = [resolve_item_symbols(item, item_ids, field) for item in value]
        if field in SYMBOL_LIST_FIELDS:
            if field == "affected_item_ids" and not resolved:
                raise ValueError("affected_item_ids cannot be empty")
            if len(resolved) != len(set(resolved)):
                raise ValueError(f"{field} cannot contain duplicate item references")
            if field == "analyzed_item_ids" and set(resolved) != set(item_ids):
                raise ValueError("analyzed_item_ids must cover the complete item set")
        return resolved
    if isinstance(value, str) and field in SYMBOL_FIELDS | SYMBOL_LIST_FIELDS:
        match = SYMBOL_PATTERN.fullmatch(value)
        if not match:
            raise ValueError(f"invalid symbolic item reference: {value}")
        index = int(match.group(1))
        if index > len(item_ids):
            raise ValueError(f"symbolic item reference is out of range: {value}")
        return item_ids[index - 1]
    return value


def _policy_path(relative: str | None) -> Path:
    if relative is None:
        return POLICY_ROOTS[0].resolve(strict=True)
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("policy path must be a contained relative path")
    resolved = (REPO_ROOT / candidate).resolve(strict=True)
    allowed = [root.resolve(strict=True) for root in POLICY_ROOTS]
    if not any(resolved == root or resolved.is_relative_to(root) for root in allowed):
        raise ValueError("policy path escapes allowed roots")
    return resolved


def _safe_path(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("fixture path must be a contained relative path")
    resolved_root = root.resolve(strict=True)
    resolved = (resolved_root / candidate).resolve(strict=True)
    if not resolved.is_relative_to(resolved_root):
        raise ValueError("fixture path escapes its allowed root")
    if not resolved.is_file():
        raise ValueError("fixture path must resolve to a file")
    return resolved


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"fixture must contain an object: {path}")
    return value


def _validate_fixture_metadata(value: dict[str, Any], path: Path) -> None:
    if not value.get("fixture_version") or not value.get("purpose"):
        raise ValueError(f"v2 fixture is missing version or purpose: {path}")
