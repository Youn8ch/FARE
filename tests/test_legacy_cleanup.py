"""AC-08 acceptance: legacy runtime references are gone (static checks).

Residual hits must only belong to compatibility/provider boundaries, as the
plan allows (12.5).
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = PROJECT_ROOT / "app"


def _read(relative: str) -> str:
    return (APP_ROOT / relative).read_text(encoding="utf-8")


def test_static01_no_split_request_in_app_runtime() -> None:
    for path in APP_ROOT.rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if "split_request" in line:
                assert line.lstrip().startswith("def "), (
                    f"unexpected split_request reference: {path}: {line.strip()}"
                )
    # split_request 本体已删除
    assert "def split_request(" not in _read("services/splitter.py")


def test_static02_catalog_segment_confined_to_provider_boundary() -> None:
    """CatalogSegment 只允许出现在 catalog.py（定义）与 canonical.py（兼容
    规范化边界）；RuleEngine / Evaluator / main 不得运行时依赖。"""

    for module in (
        "services/rule_loader.py",
        "services/evaluator.py",
        "main.py",
        "config.py",
        "schemas.py",
    ):
        assert "CatalogSegment" not in _read(module), module


def test_static03_no_resolver_none_compatibility_branch() -> None:
    assert "network_plan_resolver is None" not in _read("main.py")
    evaluator = _read("services/evaluator.py")
    occurrences = [
        line.strip()
        for line in evaluator.splitlines()
        if "self.network_plan_resolver is None" in line
    ]
    # 唯一允许的存在形式是 fail-closed 守卫（缺 resolver 直接报错，而非回退 legacy）
    assert len(occurrences) == 1
    guard_line = next(
        line
        for line in evaluator.splitlines()
        if "self.network_plan_resolver is None" in line
    )
    assert "if" in guard_line
    assert "raise RuntimeError" in evaluator


def test_llm_review_dead_code_and_facade_are_removed() -> None:
    # The interim app.services.llm_client facade is deleted outright: the
    # package under services/llm/ is the only LLM boundary.
    assert not (APP_ROOT / "services" / "llm_client.py").exists()
    schemas = _read("schemas.py")
    assert "LlmReviewResponse" not in schemas
    assert "LlmReviewItem" not in schemas


def test_offline_provider_does_not_masquerade_object_type_as_usage_code() -> None:
    provider = _read("services/network_plan_client.py")
    assert '"usageCode": entry.object_type' not in provider
    assert '"usageCode": None' in provider


def test_resolved_segment_shim_properties_are_removed() -> None:
    resolver = _read("services/network_plan_resolver.py")
    shims = (
        "    def network(self)",
        "    def original(self)",
        "    def matches(self)",
        "    def entry(self)",
    )
    for shim in shims:
        assert shim not in resolver
    # V4-P3：legacy_entry 第二事实通道已删除，显式目录分类走 typed provider
    # 通道（app/services/network_fact_provider.py）。
    assert "legacy_entry" not in resolver
    assert "offline_catalog" not in resolver
    assert "NetworkCatalog" not in resolver
    assert "NetworkEntry" not in resolver
