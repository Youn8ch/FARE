from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import FareConfig
from app.requirement_runner import run_requirement_batch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REAL_CONFIG = PROJECT_ROOT / "config/fare.yaml"
AREA_CONFIG = PROJECT_ROOT / "config/fare.test-area-relations.yaml"
DEFAULT_PATTERN = "area_relation_generated_05.batch.json"


def build_config(pattern: str) -> FareConfig:
    real = FareConfig.load(REAL_CONFIG)
    area = FareConfig.load(AREA_CONFIG)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    audit_dir = PROJECT_ROOT / "audit_logs" / f"real_area_relations_{stamp}"
    output_file = PROJECT_ROOT / "run_results" / f"real_area_relations_{stamp}.json"

    settings = replace(
        area.settings,
        audit_log_dir=audit_dir,
        llm_client_mode=real.settings.llm_client_mode,
        llm_base_url=real.settings.llm_base_url,
        llm_model=real.settings.llm_model,
        llm_api_key=real.settings.llm_api_key,
        llm_mock_file=None,
        llm_semantic_timeout_seconds=real.settings.llm_semantic_timeout_seconds,
        llm_explanation_timeout_seconds=real.settings.llm_explanation_timeout_seconds,
        llm_max_correction_retries=real.settings.llm_max_correction_retries,
        llm_request_findings_mode="shadow",
        llm_temperature=0,
        llm_max_tokens=real.settings.llm_max_tokens,
        llm_top_p=real.settings.llm_top_p,
        llm_stop=real.settings.llm_stop,
        llm_thinking=real.settings.llm_thinking,
    )
    settings.validate()
    if settings.llm_client_mode != "http":
        raise ValueError("config/fare.yaml must configure llm.mode=http")

    return replace(
        area,
        config_id=f"fare-real-area-relations-{stamp}",
        environment="real-model-eval",
        requirement_source=replace(
            area.requirement_source,
            local_pattern=pattern,
            output_file=output_file,
        ),
        settings=settings,
    )


def summarize(config: FareConfig, document: dict[str, Any]) -> dict[str, Any]:
    stage_counts: dict[str, Counter[str]] = {}
    audit_files = sorted(config.settings.audit_log_dir.glob("fare-audit-*.jsonl"))
    for path in audit_files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            for stage, value in record.get("model_raw", {}).get("stages", {}).items():
                stage_counts.setdefault(stage, Counter())[value.get("status", "missing")] += 1

    responses = [entry["response"] for entry in document["results"]]
    return {
        "config_id": config.config_id,
        "input_pattern": config.requirement_source.local_pattern,
        "request_count": len(responses),
        "output_file": str(config.requirement_source.output_file),
        "audit_directory": str(config.settings.audit_log_dir),
        "models": sorted(
            {
                f"{response['model']['name']}:{response['model']['version']}"
                for response in responses
            }
        ),
        "decision_counts": dict(Counter(response["decision"] for response in responses)),
        "stage_status_counts": {
            stage: dict(counts) for stage, counts in sorted(stage_counts.items())
        },
        "http_failure_count": sum(
            entry["http_status"] != 200 for entry in document["results"]
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run area-relation requirements through the real HTTP model."
    )
    parser.add_argument(
        "--pattern",
        default=DEFAULT_PATTERN,
        help=(
            "Requirement filename/glob. Use area_relation_*.batch.json for all "
            "159 requests."
        ),
    )
    args = parser.parse_args()
    config = build_config(args.pattern)
    document = run_requirement_batch(config)
    summary = summarize(config, document)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["http_failure_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
