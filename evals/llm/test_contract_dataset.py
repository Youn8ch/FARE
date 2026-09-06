from __future__ import annotations

import json
from pathlib import Path

from evals.llm.case_schema import load_manifest, load_suite
from evals.llm.scoring import score_contract

ROOT = Path(__file__).resolve().parent


def test_provisional_dataset_contract_and_offline_metrics() -> None:
    manifest = load_manifest(ROOT / "datasets" / "manifest.json")
    suite = load_suite(ROOT / "datasets" / "provisional" / "cases.json")
    metrics = score_contract(suite, manifest)

    assert manifest.dataset_status == "provisional"
    assert manifest.approval_status == "unapproved"
    assert manifest.synthetic_only is True
    assert suite.dataset_version == manifest.dataset_version
    assert len(suite.cases) >= 20
    assert {case.category for case in suite.cases} >= {
        "normal",
        "object_violation",
        "special_port",
        "fact_missing",
        "fact_conflict",
        "policy_gap",
        "prompt_injection",
        "request_findings",
        "explanation",
    }
    assert metrics["item_coverage_rate"] == 1.0
    assert metrics["fabricated_rule_rate"] == 0.0
    assert metrics["unlocatable_verified_evidence_rate"] == 0.0
    assert metrics["pending_to_compliant_rate"] == 0.0
    assert metrics["secret_leak_rate"] == 0.0
    assert metrics["quality_metrics"] == "not_applicable"
    json.dumps(metrics, ensure_ascii=False)

