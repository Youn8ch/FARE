from __future__ import annotations

from evals.llm.run_real_semantic_acceptance import (
    SemanticBusinessSuite,
    evaluate_thresholds,
    load_audit_runs,
    score_runs,
)


def _suite() -> SemanticBusinessSuite:
    return SemanticBusinessSuite.model_validate(
        {
            "schema_version": "fare-real-semantic-eval/v1",
            "dataset_version": "test",
            "review_status": "candidate",
            "cases": [
                {
                    "id": "positive-a",
                    "category": "required_downgrade",
                    "request_description": "a",
                    "expected_effect": "downgraded",
                    "acceptable_effects": ["downgraded"],
                    "expected_gap_types": [],
                    "expected_missing_fields": [],
                },
                {
                    "id": "positive-b",
                    "category": "required_downgrade",
                    "request_description": "b",
                    "expected_effect": "downgraded",
                    "acceptable_effects": ["downgraded"],
                    "expected_gap_types": [],
                    "expected_missing_fields": [],
                },
                {
                    "id": "question",
                    "category": "question_only",
                    "request_description": "c",
                    "expected_effect": "question_only",
                    "acceptable_effects": ["question_only"],
                    "expected_gap_types": [],
                    "expected_missing_fields": [],
                },
                {
                    "id": "normal",
                    "category": "no_downgrade",
                    "request_description": "d",
                    "expected_effect": "unchanged",
                    "acceptable_effects": ["unchanged"],
                    "expected_gap_types": [],
                    "expected_missing_fields": [],
                },
            ],
        }
    )


def _run(case_id: str, category: str, expected: str, effect: str) -> dict:
    return {
        "case_id": case_id,
        "category": category,
        "expected_effect": expected,
        "acceptable_effects": [expected],
        "expected_gap_types": [],
        "expected_missing_fields": [],
        "observed_gap_types": [],
        "observed_missing_fields": [],
        "semantic_effect": effect,
        "semantic_schema_passed": True,
        "semantic_guard_passed": True,
        "semantic_fabricated_rule_rejected": False,
        "explanation_fabricated_rule_rejected": False,
        "unlocatable_evidence_rejected": False,
        "deterministic_decision": "合规",
        "final_decision": "待定" if effect == "downgraded" else "合规",
    }


def test_acceptance_scoring_separates_recall_false_positive_and_consistency() -> None:
    runs = [
        _run("positive-a", "required_downgrade", "downgraded", "downgraded"),
        _run("positive-a", "required_downgrade", "downgraded", "unchanged"),
        _run("positive-b", "required_downgrade", "downgraded", "downgraded"),
        _run("positive-b", "required_downgrade", "downgraded", "downgraded"),
        _run("question", "question_only", "question_only", "question_only"),
        _run("question", "question_only", "question_only", "question_only"),
        _run("normal", "no_downgrade", "unchanged", "downgraded"),
        _run("normal", "no_downgrade", "unchanged", "unchanged"),
    ]

    metrics = score_runs(_suite(), runs)

    assert metrics["required_downgrade_recall"] == 0.75
    assert metrics["compliant_wrong_downgrade_rate"] == 0.25
    assert metrics["expected_effect_match_rate"] == 0.75
    assert metrics["repeat_consistency_rate"] == 0.75


def test_threshold_failures_report_direction_threshold_and_actual() -> None:
    failures = evaluate_thresholds(
        {"recall": 0.8, "false_positive": 0.2},
        {
            "minimum": {"recall": 0.9},
            "maximum": {"false_positive": 0.01},
        },
    )

    assert failures == [
        {"metric": "recall", "operator": ">=", "threshold": 0.9, "actual": 0.8},
        {
            "metric": "false_positive",
            "operator": "<=",
            "threshold": 0.01,
            "actual": 0.2,
        },
    ]


def test_missing_field_expectation_is_contains_not_exact_match() -> None:
    run = _run("question", "question_only", "question_only", "question_only")
    run["expected_missing_fields"] = ["approval_reference"]
    run["observed_missing_fields"] = ["approval_reference", "business_owner"]

    metrics = score_runs(
        _suite().model_copy(
            update={"cases": [_suite().cases[2]]}
        ),
        [run],
    )

    assert metrics["expected_missing_field_match_rate"] == 1.0
    assert metrics["required_downgrade_recall"] == 1.0


def test_audit_runs_can_be_loaded_for_resume(tmp_path) -> None:
    response = {
        "items": [
            {
                "decision_trace": {
                    "deterministic_decision": "合规",
                    "semantic_effect": "downgraded",
                    "semantic_finding_ids": ["gap-1"],
                    "final_decision": "待定",
                    "final_reason_code": "SEMANTIC_POLICY_GAP",
                }
            }
        ],
        "semantic_analysis": {"policy_gaps": [], "missing_information": []},
    }
    record = {
        "request_id": "real-semantic-stamp-positive-a-r2",
        "final_response": response,
        "model_raw": {
            "stages": {
                "semantic": {"status": "passed", "completion_status": "passed"}
            }
        },
        "exceptions": [],
    }
    (tmp_path / "fare-audit-2026-08-13.jsonl").write_text(
        __import__("json").dumps(record, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    runs = load_audit_runs(_suite(), tmp_path)

    assert [(run["case_id"], run["repeat"]) for run in runs] == [("positive-a", 2)]
