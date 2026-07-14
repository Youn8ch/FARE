from __future__ import annotations

import asyncio

import pytest

from app.schemas import LlmAclExtractionItem, LlmAclExtractionResponse
from app.services.llm_client import LlmClient, LlmDependencyError


def _client() -> LlmClient:
    return LlmClient(
        mode="http",
        base_url="http://model.invalid/v1",
        model="test-model",
        api_key=None,
        mock_file=None,
        semantic_timeout=10,
        explanation_timeout=6,
        max_correction_retries=1,
    )


def test_llm_acl_extraction_requires_locatable_evidence(monkeypatch):
    client = _client()

    async def fake_complete(messages, schema, timeout=None):
        parsed = LlmAclExtractionResponse(
            items=[
                LlmAclExtractionItem(
                    item_id="item-1",
                    firewalls=["FW-FROM-MODEL"],
                    evidence=["途经设备 FW-FROM-MODEL"],
                )
            ]
        )
        return parsed, parsed.model_dump(mode="json")

    monkeypatch.setattr(client, "_complete", fake_complete)
    result = asyncio.run(
        client.extract_acl_facts(
            [
                {
                    "item_id": "item-1",
                    "analysis": "候选路径途经设备 FW-FROM-MODEL。",
                    "config": "",
                }
            ]
        )
    )
    assert result["item-1"].firewalls == ["FW-FROM-MODEL"]


def test_llm_acl_extraction_rejects_fabricated_evidence(monkeypatch):
    client = _client()

    async def fake_complete(messages, schema, timeout=None):
        parsed = LlmAclExtractionResponse(
            items=[
                LlmAclExtractionItem(
                    item_id="item-1",
                    firewalls=["FABRICATED"],
                    evidence=["不存在于原文的证据"],
                )
            ]
        )
        return parsed, parsed.model_dump(mode="json")

    monkeypatch.setattr(client, "_complete", fake_complete)
    with pytest.raises(LlmDependencyError, match="cannot be located"):
        asyncio.run(
            client.extract_acl_facts(
                [{"item_id": "item-1", "analysis": "无固定格式内容", "config": ""}]
            )
        )
