from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvalItem(StrictModel):
    item_id: str = Field(min_length=1)
    sources: dict[str, str]
    decision_before: Literal["合规", "待定"]


class Evidence(StrictModel):
    item_id: str
    source: str
    quote: str = Field(min_length=1)


class ObservedOutput(StrictModel):
    analyzed_item_ids: list[str]
    rule_ids: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    decisions_after: dict[str, Literal["合规", "待定"]]


class EvalCase(StrictModel):
    id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    label_status: Literal["provisional"]
    generated_by_role: str = Field(min_length=1)
    reviewed_by_role: str = Field(min_length=1)
    labeled_at: date
    policy_version: str = Field(min_length=1)
    allowed_rule_ids: list[str]
    items: list[EvalItem] = Field(min_length=1, max_length=4)
    observed: ObservedOutput

    @model_validator(mode="after")
    def unique_ids(self) -> EvalCase:
        item_ids = [item.item_id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("case item IDs must be unique")
        return self


class EvalSuite(StrictModel):
    schema_version: Literal["llm-eval-case/v1"]
    dataset_version: str
    cases: list[EvalCase] = Field(min_length=20)

    @model_validator(mode="after")
    def unique_case_ids(self) -> EvalSuite:
        case_ids = [case.id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("evaluation case IDs must be unique")
        return self


class DatasetManifest(StrictModel):
    dataset_version: str
    dataset_status: Literal["provisional", "gold"]
    approval_status: Literal["unapproved", "approved"]
    synthetic_only: bool
    schema_version: Literal["llm-eval-case/v1"]
    generated_by_role: str
    reviewed_by_role: str
    policy_version: str


def load_suite(path: Path) -> EvalSuite:
    return EvalSuite.model_validate(json.loads(path.read_text(encoding="utf-8")))


def load_manifest(path: Path) -> DatasetManifest:
    return DatasetManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
