"""AC-09 final acceptance: full-scenario regression.

Runs the complete CLI batch chain (requirement_runner entry point) over ALL
versioned area-relation batches and compares every request conclusion with the
versioned expected files; verifies the supplementary failure scenarios are
covered by named tests; and asserts the final architecture checklist.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.config import FareConfig
from app.main import build_runtime
from app.requirement_runner import evaluate_requirements
from app.services.requirement_source import RequirementBatch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASE_ROOT = PROJECT_ROOT / "tests/cases/network_requirements"
CONFIG_FILE = PROJECT_ROOT / "config/fare.test-area-relations.yaml"


def _expected() -> dict[str, dict]:
    paths = [
        CASE_ROOT / "area_relation_expected.json",
        *sorted(CASE_ROOT.glob("area_relation_*.expected.json")),
    ]
    merged: dict[str, dict] = {}
    for path in paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        merged.update(document["requests"])
    return merged


@pytest.mark.parametrize(
    "batch_path",
    sorted(CASE_ROOT.glob("area_relation_*.batch.json")),
    ids=lambda path: path.name.removesuffix(".batch.json"),
)
def test_final_cli_batch_regression_matches_versioned_expectations(
    batch_path: Path, tmp_path: Path
) -> None:
    config = FareConfig.load(CONFIG_FILE)
    configured = replace(
        config,
        settings=replace(
            config.settings,
            audit_log_dir=tmp_path / "audit",
            config_id=f"{config.config_id}-ac09",
        ),
    )
    batch = RequirementBatch.model_validate_json(
        batch_path.read_text(encoding="utf-8")
    )
    expected_by_id = _expected()

    async def run():
        runtime = build_runtime(configured.settings)
        try:
            return await evaluate_requirements(
                runtime,
                batch.requests,
                max_concurrency=configured.settings.max_concurrent_evaluations,
            )
        finally:
            await runtime.aclose()

    results = asyncio.run(run())
    assert len(results) == len(batch.requests)
    for result in results:
        assert result["http_status"] == 200, result["request_id"]
        body = result["response"]
        expected = expected_by_id[result["request_id"]]
        assert body["decision"] == expected["decision"], result["request_id"]
        assert len(body["items"]) == expected["item_count"]
        for item, expected_item in zip(body["items"], expected["items"], strict=True):
            assert item["decision"] == expected_item["decision"]
            assert item["reason_code"] == expected_item["reason_code"]
            assert [rule["id"] for rule in item["matched_rules"]] == (
                expected_item["matched_rule_ids"]
            )
            assert item["source_network_fact_status"] == expected_item["source_status"]
            assert item["destination_network_fact_status"] == (
                expected_item["destination_status"]
            )
            assert item["acl_verification_status"] == expected_item["acl_status"]


def test_final_all_batches_summary_distribution(tmp_path: Path) -> None:
    """全部 batch 的结论分布与版本化期望一致（159 条需求 / 261 items）。"""

    config = FareConfig.load(CONFIG_FILE)
    configured = replace(
        config,
        settings=replace(
            config.settings,
            audit_log_dir=tmp_path / "audit",
            config_id=f"{config.config_id}-ac09-summary",
        ),
    )
    expected_by_id = _expected()
    requests = [
        request
        for path in sorted(CASE_ROOT.glob("area_relation_*.batch.json"))
        for request in RequirementBatch.model_validate_json(
            path.read_text(encoding="utf-8")
        ).requests
    ]
    assert len(requests) == 159

    async def run():
        runtime = build_runtime(configured.settings)
        try:
            return await evaluate_requirements(
                runtime,
                requests,
                max_concurrency=configured.settings.max_concurrent_evaluations,
            )
        finally:
            await runtime.aclose()

    results = asyncio.run(run())
    assert all(result["http_status"] == 200 for result in results)
    decisions = {result["request_id"]: result["response"]["decision"] for result in results}
    assert decisions == {rid: exp["decision"] for rid, exp in expected_by_id.items()}
    total_items = sum(
        len(result["response"]["items"]) for result in results
    )
    assert total_items == 261
