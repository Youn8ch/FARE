from __future__ import annotations

import pytest

from app.schemas import LlmSemanticResponse
from app.services.output_guard import SemanticGuardError, guard_semantic_output

ITEM = "REQ-001-001"
SOURCE_FACT = "NPF-10C90100"
DESTINATION_FACT = "NPF-10DC1000"


def _bindings():
    return {
        ITEM: {
            "source": {
                SOURCE_FACT: {
                    "area_id": "鳌峰科创生产区",
                    "area": "鳌峰科创生产区",
                    "region_name": "鳌峰生产区",
                    "platform_name": "鲲鹏应用",
                    "network": "16.201.0.0/22",
                    "subnet": "16.201.1.0/24",
                    "usage_code": None,
                    "description": "虚拟机",
                }
            },
            "destination": {
                DESTINATION_FACT: {
                    "area_id": "核心生产区",
                    "area": "核心生产区",
                    "region_name": "数据库区",
                    "platform_name": "数据库平台",
                    "network": "16.220.16.0/22",
                    "subnet": "16.220.16.0/24",
                    "usage_code": "PROD_DATABASE",
                    "description": "生产数据库",
                }
            },
        }
    }


def _output(reference: dict, **claim_updates) -> LlmSemanticResponse:
    claim = {
        "claim_id": "network-claim-1",
        "claim_type": "network_fact_reference",
        "scope": ITEM,
        "fact_references": [reference],
        "source": "network_plan_fact",
        "evidence": "structured reference",
    }
    claim.update(claim_updates)
    return LlmSemanticResponse.model_validate(
        {"analyzed_item_ids": [ITEM], "network_claims": [claim]}
    )


def _guard(output: LlmSemanticResponse):
    return guard_semantic_output(
        output,
        evidence_sources={
            ITEM: {
                "request_description": "申请用途与生产数据库用途矛盾",
                "source_description": "应用",
                "destination_description": "数据库",
            }
        },
        authoritative_facts={ITEM: {}},
        valid_rule_ids=set(),
        network_facts=_bindings(),
    )


def test_exact_structured_network_fact_reference_is_verified() -> None:
    output = _output(
        {
            "item_id": ITEM,
            "role": "source",
            "fact_id": SOURCE_FACT,
            "field": "area_id",
            "value": "鳌峰科创生产区",
        }
    )
    assert _guard(output).network_claims[0].status == "verified"


def test_unknown_or_cross_role_fact_is_batch_rejected() -> None:
    reference = {
        "item_id": ITEM,
        "role": "source",
        "fact_id": DESTINATION_FACT,
        "field": "area_id",
        "value": "核心生产区",
    }
    with pytest.raises(SemanticGuardError, match="cross-role"):
        _guard(_output(reference))


def test_fabricated_value_rejects_only_the_claim() -> None:
    output = _output(
        {
            "item_id": ITEM,
            "role": "source",
            "fact_id": SOURCE_FACT,
            "field": "area_id",
            "value": "虚构核心区",
        }
    )
    result = _guard(output)
    assert result.network_claims[0].status == "rejected"
    assert result.guard_results[-1].status == "rejected"


def test_locatable_request_conflict_is_preserved_as_conflict() -> None:
    output = _output(
        {
            "item_id": ITEM,
            "role": "destination",
            "fact_id": DESTINATION_FACT,
            "field": "usage_code",
            "value": "PROD_DATABASE",
        },
        claim_type="request_fact_conflict",
        source="request_description",
        evidence="用途矛盾",
    )
    assert _guard(output).network_claims[0].status == "conflict"
