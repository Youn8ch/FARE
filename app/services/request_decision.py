"""V4-P5a/P7: the request-level decision aggregation, extracted verbatim.

``DecisionReducer`` is the item-level adjudication entry; this module is the
request-level counterpart: a pure aggregation over final items.
"""

from __future__ import annotations

from app.schemas import EvaluationItem


def aggregate_request_decision(items: list[EvaluationItem]) -> str:
    """任一 item 待定 -> request 待定；全部 item 合规 -> request 合规。"""

    return "待定" if any(item.decision == "待定" for item in items) else "合规"
