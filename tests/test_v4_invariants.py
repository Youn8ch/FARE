"""V4 invariant static checks (grow with each phase).

Each check pins one architectural invariant from the V4 plan §4 so that a
regression fails fast instead of drifting silently.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = PROJECT_ROOT / "app"


def _read(relative: str) -> str:
    return (APP_ROOT / relative).read_text(encoding="utf-8")


def test_rule_match_is_called_exactly_once_per_item_path() -> None:
    """Invariant 4.2.1/4.2.2: the formal PolicyBundle.match() call lives only
    in the rule stage; ACL gating consumes RuleStageResult."""

    evaluator = _read("services/evaluator.py")
    assert evaluator.count("self.policies.match(") == 1


def test_single_formal_reduce_entry() -> None:
    """Invariant 4.3.1/4.3.3: exactly one formal reduce_item() per item in the
    evaluator; the pure reduce() algorithm is not called from the evaluator;
    the dead two-phase helper stays deleted (V4-P2)."""

    evaluator = _read("services/evaluator.py")
    assert evaluator.count("self.decision_reducer.reduce_item(") == 1
    assert ".reduce(" not in evaluator
    assert "_apply_semantic_failure" not in evaluator


def test_reducer_owns_the_priority_algorithm() -> None:
    """Invariant 4.3.3: the primary-finding algorithm lives in the reducer;
    the evaluator obtains the ACL-PATH-001 injection condition through
    reducer.primary_of instead of re-implementing priorities."""

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
    assert "class AclStageResult" in types
