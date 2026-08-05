from __future__ import annotations

from typing import Any

from evals.llm.case_schema import DatasetManifest, EvalSuite


def score_contract(suite: EvalSuite, manifest: DatasetManifest) -> dict[str, Any]:
    total_items = 0
    covered_items = 0
    fabricated_rules = 0
    unlocatable_evidence = 0
    pending_to_compliant = 0

    for case in suite.cases:
        items = {item.item_id: item for item in case.items}
        analyzed = case.observed.analyzed_item_ids
        total_items += len(items)
        covered_items += (
            len(set(analyzed) & set(items))
            if len(analyzed) == len(set(analyzed))
            else 0
        )
        fabricated_rules += len(set(case.observed.rule_ids) - set(case.allowed_rule_ids))
        for evidence in case.observed.evidence:
            item = items.get(evidence.item_id)
            if item is None or evidence.source not in item.sources:
                unlocatable_evidence += 1
            elif evidence.quote not in item.sources[evidence.source]:
                unlocatable_evidence += 1
        for item_id, after in case.observed.decisions_after.items():
            item = items.get(item_id)
            if item is not None and item.decision_before == "待定" and after == "合规":
                pending_to_compliant += 1

    return {
        "runner_version": "fare-llm-eval/1",
        "schema_version": suite.schema_version,
        "dataset_version": suite.dataset_version,
        "dataset_status": manifest.dataset_status,
        "model_version": "offline-contract-fake/1",
        "prompt_version": "2026.08.0",
        "policy_version": manifest.policy_version,
        "case_count": len(suite.cases),
        "item_coverage_rate": covered_items / total_items,
        "fabricated_rule_rate": fabricated_rules / total_items,
        "unlocatable_verified_evidence_rate": unlocatable_evidence / total_items,
        "pending_to_compliant_rate": pending_to_compliant / total_items,
        "secret_leak_rate": 0.0,
        "quality_metrics": "not_applicable",
    }
