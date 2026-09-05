"""V4 invariant static checks (grow with each phase).

Each check pins one architectural invariant from the V4 plan §4 so that a
regression fails fast instead of drifting silently. Updated for the ACL-free
main chain (PHASE-03): the ACL stage no longer exists anywhere in app/.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.helpers.llm import RecordingLlmClient
from tests.test_architecture_baseline import _mock_chain, _payload

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = PROJECT_ROOT / "app"


def _read(relative: str) -> str:
    return (APP_ROOT / relative).read_text(encoding="utf-8")


def test_rule_match_is_called_exactly_once_per_item_path() -> None:
    """Invariant 4.2.1: the formal PolicyBundle.match() call lives only in the
    rule stage; no other stage or the evaluator re-matches."""

    rule_stage = _read("services/stages/rule_stage.py")
    assert rule_stage.count("policies.match(") == 1
    evaluator = _read("services/evaluator.py")
    assert "policies.match(" not in evaluator


def test_acl_stage_is_fully_removed() -> None:
    """Invariant (PHASE-03): no ACL stage module, no ACL finding source, and
    no ACL client/extract/merge modules remain in the runtime."""

    for relative in (
        "services/stages/acl_stage.py",
        "services/acl_client.py",
        "services/acl_extract.py",
        "services/acl_candidate_merge.py",
        "services/response_assembler.py",
    ):
        assert not (APP_ROOT / relative).exists(), relative
    reducer = _read("services/decision_reducer.py")
    assert 'Literal["network", "rule", "semantic"]' in reducer
    assert "PRIORITY_ACL" not in reducer
    schemas = _read("schemas.py")
    assert "acl" not in schemas.lower().replace("dataclass", "")
    for banned in ("AclAnalysis", "AclCandidateAnalysis", "acl_verification_status",
                   "acl_no_path", "LlmAclExtraction"):
        assert banned not in schemas, banned


def test_single_formal_reduce_entry() -> None:
    """Invariant 4.3.1/4.3.3: exactly one formal reduce_item() per item in the
    reduce stage; the pure reduce() algorithm is not called from stages or
    evaluator; the dead two-phase helper stays deleted (V4-P2)."""

    reduce_stage = _read("services/stages/reduce_stage.py")
    assert reduce_stage.count("decision_reducer.reduce_item(") == 1
    for module in ("services/evaluator.py",
                   "services/stages/rule_stage.py",
                   "services/stages/semantic_stage.py",
                   "services/stages/post_decision_stage.py"):
        body = _read(module)
        assert ".reduce(" not in body, module
        assert "_apply_semantic_failure" not in body, module


def test_reducer_owns_the_priority_algorithm() -> None:
    """Invariant 4.3.3: the primary-finding algorithm lives in the reducer;
    stages never re-implement priorities."""

    reducer = _read("services/decision_reducer.py")
    assert "def primary_of(" in reducer
    assert "def reduce_item(" in reducer
    assert "class ItemFindingSet" in reducer
    assert "class SemanticTrace" in reducer


def test_resolver_has_no_second_fact_channel() -> None:
    """Invariant 4.1.2/4.1.5 (V4-P3): the resolver never imports the catalog,
    holds no offline_catalog, and consumes only the typed ProviderLookup."""

    resolver = _read("services/network_plan_resolver.py")
    for banned in (
        "NetworkCatalog",
        "NetworkEntry",
        "legacy_entry",
        "_legacy_entry(",
        "offline_catalog",
    ):
        assert banned not in resolver, banned

    provider = _read("services/network_fact_provider.py")
    assert "class ProviderNetworkFact" in provider
    assert "class ExplicitNetworkClassification" in provider
    assert "class OfflineCatalogNetworkFactProvider" in provider


def test_all_provider_modes_share_one_typed_result() -> None:
    """Invariant 4.1.3: the three providers return the same ProviderLookup."""

    provider = _read("services/network_fact_provider.py")
    for cls in (
        "class HttpNetworkFactProvider",
        "class MockNetworkFactProvider",
        "class OfflineCatalogNetworkFactProvider",
    ):
        assert cls in provider, cls
    # 每个实现的 lookup 返回类型都是 ProviderLookup（ABC + 三个实现）
    assert provider.count("async def lookup(self") == 4


def test_stage_contracts_are_frozen_dataclasses() -> None:
    """Invariant: stage boundaries pass frozen dataclasses, not bare dicts."""

    types = _read("services/evaluation_types.py")
    assert "@dataclass(frozen=True, slots=True)" in types
    assert "class RuleStageResult" in types
    assert "class EvaluationItemContext" in types
    assert "AclStageResult" not in types


def test_openapi_and_response_contract_carry_no_verification_fields(
    settings, client
) -> None:
    """PHASE-03 negative gate: the response/OpenAPI contract carries no
    candidate-path verification fields anywhere."""

    spec = client.get("/openapi.json").json()
    body = client.post("/v2/evaluations", json=_payload("invariant-openapi")).json()
    serialized_spec = json.dumps(spec)
    serialized_body = json.dumps(body)
    for banned in (
        "acl_analysis",
        "acl_candidate",
        "acl_verification_status",
        "acl_no_path",
    ):
        assert banned not in serialized_spec, banned
        assert banned not in serialized_body, banned


def test_removed_finding_codes_are_unreachable_in_runtime_maps() -> None:
    """PHASE-03 negative gate: the legacy candidate-path finding codes and the
    dedicated text maps no longer exist in the finding factory."""

    import app.services.finding_factory as factory

    assert not hasattr(factory, "acl_findings")
    assert not hasattr(factory, "ACL_ERROR_TEXT")
    factory_source = (APP_ROOT / "services/finding_factory.py").read_text(
        encoding="utf-8"
    )
    for legacy_code in (
        "ACL_DEPENDENCY_FAILURE",
        "ACL_FACT_AMBIGUOUS",
        "ACL_PORT_MISMATCH",
        "ACL_FIREWALL_UNRESOLVED",
        "ACL-PATH-001",
    ):
        assert legacy_code not in factory_source


def test_semantic_payload_prompt_versions_and_metrics_carry_no_removed_stage(
    settings,
) -> None:
    """PHASE-03 negative gate: the semantic payload, prompt versions, and
    stage metrics expose no candidate-path evidence channel or stage."""

    import asyncio
    import json as _json

    from app.main import build_runtime
    from app.schemas import EvaluationRequest

    runtime = build_runtime(_mock_chain(settings))
    runtime.evaluator.llm_client = RecordingLlmClient()
    try:
        result = asyncio.run(
            runtime.evaluator.evaluate(
                EvaluationRequest.model_validate(_payload("invariant-payload"))
            )
        )
    finally:
        asyncio.run(runtime.aclose())

    payload_serialized = _json.dumps(result.model_raw["semantic_input"])
    for banned in ("acl_analysis", "acl_config"):
        assert banned not in payload_serialized, banned
    assert set(result.model_raw["metadata"]["prompt_versions"]) == {
        "semantic",
        "request_findings",
        "explanation",
    }
    assert set(result.model_raw["stages"]) == {
        "semantic",
        "request_findings",
        "explanation",
    }


def test_llm_boundary_is_split_into_fare_owned_modules() -> None:
    """PHASE-05 gate: the LLM boundary lives in app/services/llm/ with FARE
    ownership of ports/prompts/errors/telemetry; stages import only the
    ports, and the old client module is a pure re-export facade."""

    llm_dir = APP_ROOT / "services" / "llm"
    for module in (
        "__init__.py",
        "ports.py",
        "contracts.py",
        "prompts.py",
        "errors.py",
        "provider.py",
        "structured_runtime.py",
        "adapter.py",
        "telemetry.py",
        "mock_adapter.py",
    ):
        assert (llm_dir / module).is_file(), module

    for stage in ("semantic_stage.py", "post_decision_stage.py"):
        source = (APP_ROOT / "services" / "stages" / stage).read_text(encoding="utf-8")
        assert "from app.services.llm import" in source
        assert "from app.services.llm_client" not in source

    facade = (APP_ROOT / "services" / "llm_client.py").read_text(encoding="utf-8")
    assert "class LlmClient" not in facade

    taxonomy = (llm_dir / "errors.py").read_text(encoding="utf-8")
    for cls in (
        "class FareLlmError",
        "class ProviderFailure",
        "class TimeoutFailure",
        "class StructuredOutputFailure",
        "class DomainValidationFailure",
        "class SemanticPolicyFailure",
    ):
        assert cls in taxonomy, cls


def test_llm_error_messages_do_not_embed_provider_payloads(settings) -> None:
    """PHASE-05 gate: typed failures carry bounded summaries, never raw
    provider responses or prompts."""

    from app.services.llm import StructuredOutputFailure, TimeoutFailure

    failure = StructuredOutputFailure(
        "LLM response failed schema validation after allowed correction",
        attempts=3,
        validation_summary="root: Field required; rejected input={...}",
    )
    assert len(str(failure)) < 200
    assert failure.attempts == 3
    timeout = TimeoutFailure(
        "LLM dependency request failed", deadline_seconds=10.0, attempts=2
    )
    assert timeout.deadline_seconds == 10.0
    assert timeout.attempts == 2
