"""V4-P7 acceptance: RequestDecisionAggregator is the formal request-level
component (plan §14).

DecisionReducer = item-level; RequestDecisionAggregator = request-level.
The Evaluator must not contain an inline request-decision conditional
(static check) and the aggregation rules are fixed:

- P7-C01/C02: all compliant -> request 合规
- P7-C03: any pending item -> request 待定
- P7-C04: all pending -> request 待定
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.config import Settings
from app.main import build_runtime
from app.schemas import EvaluationRequest
from app.services.request_decision import aggregate_request_decision
from tests.test_architecture_baseline import _mock_chain, _payload

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_aggregator_is_pure() -> None:
    assert aggregate_request_decision([]) == "合规"


def _request(request_id: str, sources: list[dict]) -> EvaluationRequest:
    return EvaluationRequest.model_validate(
        _payload(request_id, sources=sources)
    )


def _decisions(settings: Settings, sources: list[dict]) -> tuple[str, list[str]]:
    runtime = build_runtime(_mock_chain(settings))
    try:
        result = asyncio.run(
            runtime.evaluator.evaluate(_request("v4-p7", sources))
        )
        return (
            result.response.decision,
            [item.decision for item in result.response.items],
        )
    finally:
        asyncio.run(runtime.aclose())


def test_p7c01_p7c02_all_compliant_items_yield_compliant_request(
    settings: Settings,
) -> None:
    decision, item_decisions = _decisions(
        settings, [{"address": "16.201.1.10", "description": "应用"}]
    )
    assert item_decisions == ["合规"]
    assert decision == "合规"


def test_p7c03_any_pending_item_yields_pending_request(settings: Settings) -> None:
    # 第二个源地址不在 fixture 中 -> 该 item 待定
    decision, item_decisions = _decisions(
        settings,
        [
            {"address": "16.201.1.10", "description": "应用"},
            {"address": "16.201.3.10", "description": "应用 2"},
        ],
    )
    assert item_decisions == ["合规", "待定"]
    assert decision == "待定"


def test_p7c04_all_pending_items_yield_pending_request(settings: Settings) -> None:
    decision, item_decisions = _decisions(
        settings, [{"address": "16.201.3.10", "description": "应用"}]
    )
    assert item_decisions == ["待定"]
    assert decision == "待定"


def test_evaluator_has_no_inline_request_decision_conditional() -> None:
    evaluator = (
        (PROJECT_ROOT / "app" / "services" / "evaluator.py")
        .read_text(encoding="utf-8")
    )
    assert '"待定" if any(' not in evaluator
    assert "aggregate_request_decision(items)" in evaluator
