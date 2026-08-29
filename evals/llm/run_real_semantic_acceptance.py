from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.config import FareConfig
from app.main import build_runtime
from app.schemas import EvaluationRequest
from app.services.audit import request_hash

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REAL_CONFIG = PROJECT_ROOT / "config/fare.yaml"
DEFAULT_DATASET = (
    PROJECT_ROOT / "evals/llm/datasets/semantic_business_cases.v1.json"
)
DEFAULT_THRESHOLDS = PROJECT_ROOT / "evals/llm/semantic_thresholds.yaml"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SemanticBusinessCase(StrictModel):
    id: str = Field(min_length=1)
    category: Literal["required_downgrade", "question_only", "no_downgrade"]
    request_description: str = Field(min_length=1)
    expected_effect: Literal["downgraded", "question_only", "unchanged"]
    acceptable_effects: list[
        Literal["downgraded", "question_only", "observation_only", "unchanged"]
    ] = Field(min_length=1)
    expected_gap_types: list[str]
    expected_missing_fields: list[str]


class SemanticBusinessSuite(StrictModel):
    schema_version: Literal["fare-real-semantic-eval/v1"]
    dataset_version: str
    review_status: str
    cases: list[SemanticBusinessCase] = Field(min_length=1)


def load_suite(path: Path) -> SemanticBusinessSuite:
    return SemanticBusinessSuite.model_validate_json(path.read_text(encoding="utf-8"))


def load_thresholds(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if document.get("schema_version") != "fare-real-semantic-thresholds/v1":
        raise ValueError("unsupported semantic threshold schema")
    return document


def _rate(numerator: int, denominator: int, *, empty: float = 0.0) -> float:
    return round(numerator / denominator, 6) if denominator else empty


def score_runs(
    suite: SemanticBusinessSuite, runs: list[dict[str, Any]]
) -> dict[str, float]:
    total = len(runs)
    required = [run for run in runs if run["category"] == "required_downgrade"]
    non_downgrade = [run for run in runs if run["category"] != "required_downgrade"]
    case_effects: dict[str, list[str]] = {case.id: [] for case in suite.cases}
    for run in runs:
        case_effects[run["case_id"]].append(run["semantic_effect"])
    consistent = sum(
        max(Counter(effects).values(), default=0) for effects in case_effects.values()
    )
    consistency_denominator = sum(len(effects) for effects in case_effects.values())
    return {
        "semantic_schema_pass_rate": _rate(
            sum(run["semantic_schema_passed"] for run in runs), total
        ),
        "semantic_evidence_guard_pass_rate": _rate(
            sum(run["semantic_guard_passed"] for run in runs), total
        ),
        "required_downgrade_recall": _rate(
            sum(run["semantic_effect"] == "downgraded" for run in required),
            len(required),
            empty=1.0,
        ),
        "expected_effect_match_rate": _rate(
            sum(run["semantic_effect"] in run["acceptable_effects"] for run in runs),
            total,
        ),
        "expected_gap_type_match_rate": _rate(
            sum(
                set(run["expected_gap_types"]) <= set(run["observed_gap_types"])
                for run in runs
            ),
            total,
        ),
        "expected_missing_field_match_rate": _rate(
            sum(
                set(run["expected_missing_fields"])
                <= set(run["observed_missing_fields"])
                for run in runs
            ),
            total,
        ),
        "repeat_consistency_rate": _rate(consistent, consistency_denominator),
        "compliant_wrong_downgrade_rate": _rate(
            sum(run["semantic_effect"] == "downgraded" for run in non_downgrade),
            len(non_downgrade),
        ),
        "semantic_fabricated_rule_rate": _rate(
            sum(run["semantic_fabricated_rule_rejected"] for run in runs), total
        ),
        "explanation_fabricated_rule_rate": _rate(
            sum(run["explanation_fabricated_rule_rejected"] for run in runs), total
        ),
        "excess_missing_field_rate": _rate(
            sum(
                len(
                    set(run["observed_missing_fields"])
                    - set(run["expected_missing_fields"])
                )
                for run in runs
            ),
            sum(len(set(run["observed_missing_fields"])) for run in runs),
        ),
        "unlocatable_verified_evidence_rate": _rate(
            sum(run["unlocatable_evidence_rejected"] for run in runs), total
        ),
        "pending_to_compliant_rate": _rate(
            sum(
                run["deterministic_decision"] == "待定"
                and run["final_decision"] == "合规"
                for run in runs
            ),
            total,
        ),
    }


def evaluate_thresholds(
    metrics: dict[str, float], thresholds: dict[str, Any]
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for metric, threshold in thresholds.get("minimum", {}).items():
        actual = metrics[metric]
        if actual < float(threshold):
            failures.append(
                {"metric": metric, "operator": ">=", "threshold": threshold, "actual": actual}
            )
    for metric, threshold in thresholds.get("maximum", {}).items():
        actual = metrics[metric]
        if actual > float(threshold):
            failures.append(
                {"metric": metric, "operator": "<=", "threshold": threshold, "actual": actual}
            )
    return failures


def build_settings(
    stamp: str, *, include_shadows: bool, audit_directory: Path | None = None
):
    real = FareConfig.load(REAL_CONFIG).settings
    settings = replace(
        real,
        audit_log_dir=(
            audit_directory
            if audit_directory is not None
            else PROJECT_ROOT / "audit_logs" / f"real_semantic_{stamp}"
        ),
        network_plan_client_mode="offline_catalog",
        network_plan_mock_file=None,
        acl_client_mode="mock",
        acl_mock_file=None,
        acl_decision_mode="advisory",
        llm_acl_candidate_mode="shadow" if include_shadows else "off",
        llm_request_findings_mode="shadow" if include_shadows else "off",
        llm_temperature=0,
    )
    settings.validate()
    if settings.llm_client_mode != "http":
        raise ValueError("config/fare.yaml must configure llm.mode=http")
    return settings


def _run_record(
    case: SemanticBusinessCase,
    repeat: int,
    *,
    response: dict[str, Any],
    model_raw: dict[str, Any],
    exceptions: list[str],
) -> dict[str, Any]:
    item = response["items"][0]
    trace = item["decision_trace"]
    stage = model_raw["stages"]["semantic"]
    exception_text = " ".join(exceptions).casefold()
    semantic = response["semantic_analysis"]
    return {
        "case_id": case.id,
        "category": case.category,
        "repeat": repeat,
        "expected_effect": case.expected_effect,
        "acceptable_effects": case.acceptable_effects,
        "expected_gap_types": case.expected_gap_types,
        "expected_missing_fields": case.expected_missing_fields,
        "semantic_schema_passed": stage.get("completion_status") == "passed",
        "semantic_guard_passed": stage["status"] == "passed",
        "semantic_stage": stage,
        "deterministic_decision": trace["deterministic_decision"],
        "semantic_effect": trace["semantic_effect"],
        "semantic_finding_ids": trace["semantic_finding_ids"],
        "final_decision": trace["final_decision"],
        "final_reason_code": trace["final_reason_code"],
        "observed_gap_types": [
            gap["gap_type"]
            for gap in semantic["policy_gaps"]
            if gap["status"] == "verified"
        ],
        "observed_missing_fields": [
            missing["field"] for missing in semantic["missing_information"]
        ],
        "semantic_fabricated_rule_rejected": (
            "llm semantic analysis:" in exception_text
            and "nonexistent rule" in exception_text
        ),
        "explanation_fabricated_rule_rejected": (
            "llm explanation:" in exception_text
            and "nonexistent rule" in exception_text
        ),
        "unlocatable_evidence_rejected": "cannot be located" in exception_text,
    }


def load_audit_runs(
    suite: SemanticBusinessSuite, audit_directory: Path
) -> list[dict[str, Any]]:
    cases = {case.id: case for case in suite.cases}
    runs: list[dict[str, Any]] = []
    for path in sorted(audit_directory.glob("fare-audit-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            request_id = record["request_id"]
            match = next(
                (
                    (case, found)
                    for case in cases.values()
                    if (
                        found := re.search(
                            rf"-{re.escape(case.id)}-r(?P<repeat>\d+)$", request_id
                        )
                    )
                ),
                None,
            )
            if match is None:
                continue
            case, repeat_match = match
            runs.append(
                _run_record(
                    case,
                    int(repeat_match.group("repeat")),
                    response=record["final_response"],
                    model_raw=record["model_raw"],
                    exceptions=record.get("exceptions", []),
                )
            )
    return runs


async def run_suite(
    suite: SemanticBusinessSuite,
    *,
    repeats: int,
    include_shadows: bool,
    resume_audit_directory: Path | None = None,
) -> dict[str, Any]:
    stamp = (
        resume_audit_directory.name.removeprefix("real_semantic_")
        if resume_audit_directory is not None
        else datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    )
    settings = build_settings(
        stamp,
        include_shadows=include_shadows,
        audit_directory=resume_audit_directory,
    )
    runtime = build_runtime(settings)
    runs = (
        load_audit_runs(suite, settings.audit_log_dir)
        if resume_audit_directory is not None
        else []
    )
    existing = {(run["case_id"], run["repeat"]) for run in runs}
    for repeat in range(1, repeats + 1):
        for case in suite.cases:
            if (case.id, repeat) in existing:
                continue
            request = EvaluationRequest.model_validate(
                {
                    "request_id": f"real-semantic-{stamp}-{case.id}-r{repeat}",
                    "sources": [
                        {"address": "16.1.30.10", "description": "生产应用 A"}
                    ],
                    "destinations": [
                        {"address": "16.1.30.20", "description": "生产应用 B"}
                    ],
                    "protocol": "tcp",
                    "ports": [{"start": 443, "end": 443}],
                    "request_description": case.request_description,
                }
            )
            result = await runtime.evaluator.evaluate(request)
            await runtime.audit.persist(
                request=request,
                input_hash=request_hash(request),
                response=result.response,
                acl_raw=result.acl_raw,
                model_raw=result.model_raw,
                exceptions=result.exceptions,
                network_plan_raw=result.network_plan_raw,
            )
            run = _run_record(
                case,
                repeat,
                response=result.response.model_dump(mode="json"),
                model_raw=result.model_raw,
                exceptions=result.exceptions,
            )
            runs.append(run)
            trace = result.response.items[0].decision_trace
            stage = result.model_raw["stages"]["semantic"]
            print(
                f"[{len(runs)}/{repeats * len(suite.cases)}] "
                f"{case.id} repeat={repeat} effect={trace.semantic_effect} "
                f"semantic={stage['status']}",
                flush=True,
            )

    thresholds = load_thresholds(DEFAULT_THRESHOLDS)
    metrics = score_runs(suite, runs)
    failures = evaluate_thresholds(metrics, thresholds)
    return {
        "runner_version": "fare-real-semantic/1",
        "generated_at": datetime.now(UTC).isoformat(),
        "dataset_version": suite.dataset_version,
        "dataset_review_status": suite.review_status,
        "model": settings.llm_model,
        "repeats": repeats,
        "shadow_stages_enabled": include_shadows,
        "audit_directory": str(settings.audit_log_dir),
        "metrics": metrics,
        "thresholds": thresholds,
        "threshold_failures": failures,
        "passed": not failures,
        "runs": runs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run repeated real-model semantic business acceptance tests."
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument(
        "--case-id",
        action="append",
        help="Run only the named case; may be supplied more than once.",
    )
    parser.add_argument(
        "--include-shadows",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Also call ACL-candidate and request-finding shadow model stages.",
    )
    parser.add_argument(
        "--resume-audit-directory",
        type=Path,
        help="Resume missing case/repeat pairs from an existing audit directory.",
    )
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    suite = load_suite(args.dataset)
    if args.case_id:
        selected = [case for case in suite.cases if case.id in set(args.case_id)]
        missing = set(args.case_id) - {case.id for case in selected}
        if missing:
            parser.error(f"unknown --case-id values: {sorted(missing)}")
        suite = suite.model_copy(update={"cases": selected})
    report = asyncio.run(
        run_suite(
            suite,
            repeats=args.repeats,
            include_shadows=args.include_shadows,
            resume_audit_directory=args.resume_audit_directory,
        )
    )
    output = (
        PROJECT_ROOT
        / "run_results"
        / f"real_semantic_acceptance_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = {key: value for key, value in report.items() if key != "runs"}
    summary["output_file"] = str(output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
