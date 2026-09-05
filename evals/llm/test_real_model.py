from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from app.services.llm import LlmClient
from evals.llm.case_schema import load_manifest, load_suite

pytestmark = pytest.mark.llm_eval
ROOT = Path(__file__).resolve().parent


def test_real_model_quality_requires_all_approval_gates() -> None:
    if os.getenv("RUN_REAL_LLM_EVAL") != "1":
        pytest.skip("RUN_REAL_LLM_EVAL=1 is required")

    manifest = load_manifest(ROOT / "datasets" / "manifest.json")
    if manifest.dataset_status != "gold" or manifest.approval_status != "approved":
        pytest.skip("an approved gold dataset manifest is required")
    if not os.getenv("LLM_BASE_URL") or not os.getenv("LLM_MODEL"):
        pytest.skip("LLM_BASE_URL and LLM_MODEL are required from the secure environment")

    suite = load_suite(ROOT / "datasets" / "provisional" / "cases.json")
    client = LlmClient(
        mode="http",
        base_url=os.environ["LLM_BASE_URL"],
        model=os.environ["LLM_MODEL"],
        api_key=os.getenv("LLM_API_KEY"),
        mock_file=None,
        semantic_timeout=float(os.getenv("LLM_SEMANTIC_TIMEOUT_SECONDS", "30")),
        explanation_timeout=float(
            os.getenv("LLM_EXPLANATION_TIMEOUT_SECONDS", "30")
        ),
        max_correction_retries=1,
    )

    async def run() -> None:
        for case in suite.cases:
            items = [
                {
                    "item_id": item.item_id,
                    "request_description": item.sources.get(
                        "request_description", ""
                    ),
                    "source_description": item.sources.get(
                        "source_description", ""
                    ),
                    "destination_description": item.sources.get(
                        "destination_description", ""
                    ),
                    "authoritative_facts": {},
                }
                for item in case.items
            ]
            response, _ = await client.analyze(
                {"request_id": case.id, "items": items, "rules": []}
            )
            assert response.analyzed_item_ids == [item.item_id for item in case.items]

    asyncio.run(run())
