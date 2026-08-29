from __future__ import annotations

import ipaddress
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

Decision = Literal["合规", "待定"]
ReasonType = Literal[
    "policy_violation",
    "fact_incomplete",
    "fact_conflict",
    "acl_no_path",
    "dependency_failure",
    "risk_uncertain",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderNetworkPlanData(BaseModel):
    """Provider DTO kept separate from FARE's internal network facts."""

    model_config = ConfigDict(
        extra="ignore",
        strict=True,
        populate_by_name=True,
    )

    area: str
    area_id: str = Field(validation_alias="areaId")
    region_name: str = Field(validation_alias="regionName")
    platform_name: str = Field(validation_alias="platformName")
    network: str
    gateway: str | None
    subnet: str
    vlan_id: str | None = Field(validation_alias="vlanId")
    usage_code: str | None = Field(validation_alias="usageCode")
    description: str | None

    @field_validator("area", "area_id", "region_name", "platform_name")
    @classmethod
    def require_non_blank_network_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("network planning names must not be blank")
        return value


class ProviderNetworkPlanResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    code: int
    msg: str
    data: ProviderNetworkPlanData | None
    success: bool


class NetworkPlanTransportResponse(StrictModel):
    http_status: int = Field(ge=100, le=599)
    body: object | None = None
    raw_text: str | None = None


class NetworkPlanFact(StrictModel):
    fact_id: str
    query_subnet: str
    area: str
    area_id: str
    region_name: str
    platform_name: str
    network: str
    gateway: str | None
    subnet: str
    vlan_id: str | None
    usage_code: str | None
    description: str | None


NetworkPlanLookupStatus = Literal[
    "resolved",
    "not_found",
    "dependency_failure",
    "invalid_response",
    "conflict",
]


class NetworkPlanLookup(StrictModel):
    lookup_id: str
    query_subnet: str
    status: NetworkPlanLookupStatus
    fact_id: str | None = None
    data: NetworkPlanFact | None = None
    error_code: str | None = None
    error_message: str | None = None


NetworkFactStatus = Literal[
    "complete",
    "not_found",
    "dependency_failure",
    "invalid_response",
    "conflict",
    "not_applicable",
]


class NetworkRegion(StrictModel):
    role: Literal["source", "destination"]
    original_index: int
    original_address: str
    access_network: str
    network_fact_ids: list[str] = Field(default_factory=list)
    status: NetworkFactStatus
    area_id: str | None = None
    region_name: str | None = None
    platform_name: str | None = None
    usage_code: str | None = None
    error_code: str | None = None


class NetworkAnalysis(StrictModel):
    classification: Literal["权威网段规划事实"] = "权威网段规划事实"
    lookups: list[NetworkPlanLookup] = Field(default_factory=list)
    source_regions: list[NetworkRegion] = Field(default_factory=list)
    destination_regions: list[NetworkRegion] = Field(default_factory=list)


class EvaluationCardinality(StrictModel):
    unique_query_subnet_count: int = Field(ge=0)
    source_segment_upper_bound: int = Field(ge=0)
    destination_segment_upper_bound: int = Field(ge=0)
    port_range_count: int = Field(ge=0)
    item_upper_bound: int = Field(ge=0)


class AddressInput(StrictModel):
    address: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)

    @field_validator("address", "description")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("address")
    @classmethod
    def validate_address_contract(cls, value: str) -> str:
        if value.lower() == "any":
            return value
        try:
            ipaddress.ip_network(value, strict=False)
        except ValueError as exc:
            raise ValueError("address must be an IP, CIDR, or 'any'") from exc
        return value


class PortRange(StrictModel):
    start: int = Field(ge=0, le=65535)
    end: int = Field(ge=0, le=65535)

    @model_validator(mode="after")
    def ordered(self) -> PortRange:
        if self.end < self.start:
            raise ValueError("port end must be greater than or equal to start")
        return self


class EvaluationRequest(StrictModel):
    request_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")
    sources: list[AddressInput] = Field(min_length=1, max_length=100)
    destinations: list[AddressInput] = Field(min_length=1, max_length=100)
    protocol: str = Field(min_length=1, max_length=32)
    ports: list[PortRange] = Field(min_length=1, max_length=100)
    request_description: str = Field(default="", max_length=10000)

    @field_validator("request_id", "protocol", "request_description")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("protocol")
    @classmethod
    def normalize_protocol(cls, value: str) -> str:
        return value.lower()


class Access(StrictModel):
    source: str
    destination: str
    protocol: str
    port: PortRange


class MatchedRule(StrictModel):
    id: str
    name: str
    category: str


class DecisionTrace(StrictModel):
    deterministic_decision: Decision
    semantic_effect: Literal[
        "unchanged",
        "observation_only",
        "question_only",
        "downgraded",
        "semantic_failure",
    ]
    semantic_finding_ids: list[str] = Field(default_factory=list)
    final_decision: Decision
    final_reason_code: str | None = None


class DecisionFinding(StrictModel):
    """One adjudication finding exposed on the item (V4-P4, additive).

    Mirrors the reducer's ``Finding`` in frozen insertion order; the internal
    priority is not exposed. ``is_primary`` marks the single finding that
    determined ``reason_code`` / ``reason_type``.
    """

    code: str
    source: Literal["network", "rule", "acl", "semantic"]
    reason_type: ReasonType | None = None
    affects_decision: bool = True
    detail: str | None = None
    is_primary: bool = False


class EvaluationItem(StrictModel):
    item_id: str
    access: Access
    decision: Decision
    reason_type: ReasonType | None = None
    reason_code: str | None = None
    matched_rules: list[MatchedRule] = Field(default_factory=list)
    decision_findings: list[DecisionFinding] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    reason: str
    recommendation: str
    explanation_source: Literal["llm", "template"] = "template"
    llm_explanation: str | None = Field(default=None, max_length=4000)
    llm_recommendation: str | None = Field(default=None, max_length=4000)
    source_network_fact_ids: list[str] = Field(default_factory=list)
    destination_network_fact_ids: list[str] = Field(default_factory=list)
    source_network_fact_status: NetworkFactStatus = "not_applicable"
    destination_network_fact_status: NetworkFactStatus = "not_applicable"
    acl_verification_status: Literal[
        "verified", "unverified", "review_required", "skipped"
    ] = "unverified"
    decision_trace: DecisionTrace | None = None

    @model_validator(mode="after")
    def pending_requires_reason(self) -> EvaluationItem:
        if self.decision == "待定" and (not self.reason_type or not self.reason_code):
            raise ValueError("pending items require reason_type and reason_code")
        if self.decision == "合规" and (self.reason_type or self.reason_code):
            raise ValueError("compliant items cannot contain pending reason fields")
        return self

    @model_serializer(mode="wrap")
    def omit_empty_llm_text(self, handler: Any) -> dict[str, Any]:
        serialized = handler(self)
        if self.llm_explanation is None:
            serialized.pop("llm_explanation", None)
        if self.llm_recommendation is None:
            serialized.pop("llm_recommendation", None)
        if self.decision_trace is None:
            serialized.pop("decision_trace", None)
        return serialized


class ExtractedFacts(StrictModel):
    firewalls: list[str] = Field(default_factory=list)
    explicit_no_path: bool = False
    candidate_acls: list[str] = Field(default_factory=list)
    address_objects: list[str] = Field(default_factory=list)
    observed_ports: list[int] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    ambiguous: bool = False


class AclAnalysis(StrictModel):
    classification: Literal["候选路径与拟新增策略分析，非现网 ACL 状态"] = (
        "候选路径与拟新增策略分析，非现网 ACL 状态"
    )
    raw_analysis: str
    raw_config: str
    extracted_facts: ExtractedFacts
    verification_summary: dict[
        Literal["verified", "unverified", "review_required", "skipped"], int
    ] = Field(default_factory=dict)


AclCandidateFactType = Literal[
    "firewall",
    "candidate_acl",
    "address_object",
    "observed_port",
]
AclCandidateEvidenceSource = Literal["acl_analysis", "acl_config"]
AclCandidateMergeStatus = Literal[
    "agree",
    "llm_only",
    "deterministic_only",
    "conflict",
    "empty",
    "rejected",
]


class LlmAclCandidateFact(StrictModel):
    type: AclCandidateFactType
    value: str | int
    source: AclCandidateEvidenceSource
    evidence: str = Field(min_length=1, max_length=4000)
    confidence: float = Field(ge=0, le=1)

    @field_validator("value", mode="before")
    @classmethod
    def reject_boolean_value(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("ACL candidate fact value must not be boolean")
        return value

    @field_validator("evidence")
    @classmethod
    def strip_evidence(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("ACL candidate evidence must not be blank")
        return value

    @model_validator(mode="after")
    def validate_value_type(self) -> LlmAclCandidateFact:
        if self.type == "observed_port":
            if (
                not isinstance(self.value, int)
                or isinstance(self.value, bool)
                or not 0 <= self.value <= 65535
            ):
                raise ValueError("observed_port value must be an integer from 0 to 65535")
            return self
        if not isinstance(self.value, str) or not self.value.strip():
            raise ValueError(f"{self.type} value must be a non-blank string")
        self.value = self.value.strip()
        return self


class LlmAclExtractionItem(StrictModel):
    item_id: str = Field(min_length=1, max_length=300)
    facts: list[LlmAclCandidateFact] = Field(default_factory=list)
    firewalls: list[str] = Field(default_factory=list)
    candidate_acls: list[str] = Field(default_factory=list)
    address_objects: list[str] = Field(default_factory=list)
    observed_ports: list[int] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)

    @field_validator("firewalls", "candidate_acls", "address_objects")
    @classmethod
    def validate_flat_string_facts(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("flat ACL candidate values must not be blank")
        return normalized

    @field_validator("observed_ports", mode="before")
    @classmethod
    def reject_boolean_ports(cls, values: Any) -> Any:
        if isinstance(values, list) and any(isinstance(value, bool) for value in values):
            raise ValueError("observed ports must be integers, not booleans")
        return values

    @field_validator("observed_ports")
    @classmethod
    def validate_flat_ports(cls, values: list[int]) -> list[int]:
        if any(not 0 <= value <= 65535 for value in values):
            raise ValueError("observed ports must be from 0 to 65535")
        return values

    @field_validator("evidence")
    @classmethod
    def validate_flat_evidence(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("ACL candidate evidence must not be blank")
        return normalized


class LlmAclExtractionResponse(StrictModel):
    items: list[LlmAclExtractionItem]


class AclCandidateComparison(StrictModel):
    item_id: str
    status: AclCandidateMergeStatus
    deterministic: ExtractedFacts
    llm_candidate: LlmAclExtractionItem
    rejection_reason: str | None = None


class AclCandidateAnalysis(StrictModel):
    mode: Literal["shadow"] = "shadow"
    classification: Literal["LLM 候选事实影子分析，不作为合规依据"] = (
        "LLM 候选事实影子分析，不作为合规依据"
    )
    items: list[AclCandidateComparison] = Field(default_factory=list)


class ModelInfo(StrictModel):
    name: str
    version: str


ClaimStatus = Literal["verified", "candidate", "conflict", "rejected"]
SemanticClaimType = Literal[
    "request_context",
    "access_purpose",
    "temporary_access",
    "system_role",
    "maintenance_method",
    "approval_reference",
    "business_owner",
    "requested_duration",
    "source_zone",
    "destination_zone",
    "source_environment",
    "destination_environment",
    "source_object_type",
    "destination_object_type",
]
SemanticEvidenceSource = Literal[
    "request_description",
    "source_description",
    "destination_description",
    "acl_analysis",
    "acl_config",
    "network_plan_fact",
]
RequestFindingType = Literal[
    "mixed_business_context",
    "inconsistent_purpose",
    "unsupported_combination",
    "temporary_scope_mismatch",
    "missing_approval_context",
]
SemanticEffect = Literal["observe_only", "question_only", "review_required"]
PolicyGapType = Literal[
    "temporary_permanent_conflict",
    "purpose_target_mismatch",
    "mixed_business_context",
    "approval_scope_mismatch",
    "unclassified_privileged_access",
]
MissingInformationField = Literal[
    "access_purpose",
    "temporary_access",
    "maintenance_method",
    "approval_reference",
    "business_owner",
    "requested_duration",
]
VerbatimEvidenceSource = Literal[
    "request_description",
    "source_description",
    "destination_description",
    "acl_analysis",
    "acl_config",
]


class SemanticEvidence(StrictModel):
    item_id: str = Field(min_length=1, max_length=300)
    source: VerbatimEvidenceSource
    quote: str = Field(min_length=1, max_length=4000)

    @field_validator("item_id", "quote")
    @classmethod
    def strip_semantic_evidence(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("semantic evidence fields must not be blank")
        return value


class RequestFindingEvidence(StrictModel):
    item_id: str = Field(min_length=1, max_length=300)
    source: SemanticEvidenceSource
    quote: str = Field(min_length=1, max_length=4000)

    @field_validator("item_id", "quote")
    @classmethod
    def strip_finding_evidence(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("request finding evidence fields must not be blank")
        return value


class LlmRequestFinding(StrictModel):
    finding_id: str = Field(min_length=1, max_length=300)
    type: RequestFindingType
    finding_type: RequestFindingType
    affected_item_ids: list[str] = Field(min_length=1)
    description: str = Field(min_length=1, max_length=4000)
    evidence: list[RequestFindingEvidence] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    status: Literal["candidate"]
    question: str = Field(min_length=1, max_length=4000)
    question_for_requester: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="before")
    @classmethod
    def synchronize_finding_compatibility_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        finding_type = _normalize_semantic_identifier(normalized.get("finding_type"))
        short_type = _normalize_semantic_identifier(normalized.get("type"))
        if finding_type is None and short_type is None:
            raise ValueError("request finding requires type or finding_type")
        if finding_type is None:
            finding_type = short_type
        if short_type is None:
            short_type = finding_type
        if finding_type != short_type:
            raise ValueError("request finding type fields must match")
        question = normalized.get("question")
        requester_question = normalized.get("question_for_requester")
        if question is None and requester_question is None:
            raise ValueError("request finding requires a requester question")
        if question is None:
            question = requester_question
        if requester_question is None:
            requester_question = question
        if question != requester_question:
            raise ValueError("request finding question fields must match")
        normalized["type"] = short_type
        normalized["finding_type"] = finding_type
        normalized["question"] = question
        normalized["question_for_requester"] = requester_question
        return normalized

    @field_validator(
        "finding_id",
        "description",
        "question",
        "question_for_requester",
    )
    @classmethod
    def strip_finding_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("request finding text fields must not be blank")
        return value

    @field_validator("affected_item_ids")
    @classmethod
    def strip_affected_item_ids(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("affected item IDs must not be blank")
        return normalized


class LlmRequestFindingsResponse(StrictModel):
    analyzed_item_ids: list[str]
    findings: list[LlmRequestFinding] = Field(default_factory=list)


class RequestFindingsAnalysis(StrictModel):
    mode: Literal["shadow"] = "shadow"
    classification: Literal["LLM 申请级发现影子分析，不作为业务裁决依据"] = (
        "LLM 申请级发现影子分析，不作为业务裁决依据"
    )
    status: Literal["completed", "rejected"]
    analyzed_item_ids: list[str]
    findings: list[LlmRequestFinding] = Field(default_factory=list)
    rejection_reason: str | None = None

    @model_validator(mode="after")
    def validate_rejection_reason(self) -> RequestFindingsAnalysis:
        if self.status == "rejected" and not self.rejection_reason:
            raise ValueError("rejected request findings require a public reason")
        if self.status == "completed" and self.rejection_reason is not None:
            raise ValueError("completed request findings cannot have a rejection reason")
        return self


class _SemanticClaimBase(StrictModel):
    claim_id: str
    scope: str
    field: SemanticClaimType
    claim_type: SemanticClaimType
    value: str
    source: SemanticEvidenceSource
    evidence: str
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="before")
    @classmethod
    def synchronize_claim_type(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        field = _normalize_semantic_identifier(normalized.get("field"))
        claim_type = _normalize_semantic_identifier(normalized.get("claim_type"))
        if field is None and claim_type is None:
            raise ValueError("semantic claim requires field or claim_type")
        if field is None:
            field = claim_type
        if claim_type is None:
            claim_type = field
        if field != claim_type:
            raise ValueError("semantic claim field and claim_type must match")
        normalized["field"] = field
        normalized["claim_type"] = claim_type
        normalized["source"] = _normalize_semantic_identifier(normalized.get("source"))
        return normalized


class SemanticClaim(_SemanticClaimBase):
    status: ClaimStatus


class SemanticContradiction(StrictModel):
    contradiction_id: str
    scope: str
    description: str
    evidence: list[SemanticEvidence] = Field(min_length=2)
    status: ClaimStatus
    applied_effect: SemanticEffect = "observe_only"


class PolicyGap(StrictModel):
    gap_id: str
    scope: str
    gap_type: PolicyGapType
    description: str
    evidence: list[SemanticEvidence] = Field(min_length=1)
    affected_fields: list[SemanticClaimType] = Field(min_length=1)
    question_for_requester: str = Field(min_length=1, max_length=4000)
    suggested_effect: SemanticEffect
    status: ClaimStatus
    applied_effect: SemanticEffect = "observe_only"


class MissingInformation(StrictModel):
    missing_id: str = Field(min_length=1, max_length=300)
    item_id: str = Field(min_length=1, max_length=300)
    field: MissingInformationField
    question: str = Field(min_length=1, max_length=4000)
    impact: Literal["question_only"] = "question_only"


class GuardResult(StrictModel):
    code: str
    status: Literal["passed", "rejected"]
    detail: str
    related_id: str | None = None


NetworkFactReferenceField = Literal[
    "area_id",
    "area",
    "region_name",
    "platform_name",
    "network",
    "subnet",
    "usage_code",
    "description",
]


class LlmNetworkFactReference(StrictModel):
    item_id: str
    role: Literal["source", "destination"]
    fact_id: str
    field: NetworkFactReferenceField
    value: str | None


class LlmNetworkSemanticClaim(StrictModel):
    claim_id: str
    claim_type: Literal[
        "network_fact_reference",
        "request_fact_conflict",
        "business_purpose",
        "system_role",
        "temporary_access",
    ]
    scope: str
    fact_references: list[LlmNetworkFactReference] = Field(min_length=1)
    source: SemanticEvidenceSource
    evidence: str


class NetworkSemanticClaim(LlmNetworkSemanticClaim):
    status: ClaimStatus


class SemanticAnalysis(StrictModel):
    analyzed_item_ids: list[str]
    claims: list[SemanticClaim] = Field(default_factory=list)
    contradictions: list[SemanticContradiction] = Field(default_factory=list)
    candidate_rule_ids: list[str] = Field(default_factory=list)
    policy_gaps: list[PolicyGap] = Field(default_factory=list)
    questions_for_requester: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    guard_results: list[GuardResult] = Field(default_factory=list)
    network_claims: list[NetworkSemanticClaim] = Field(default_factory=list)
    missing_information: list[MissingInformation] = Field(default_factory=list)


class EvaluationResponse(StrictModel):
    request_id: str
    config_id: str | None = None
    environment: str | None = None
    config_fingerprint: str | None = None
    decision: Decision
    policy_version: str
    model: ModelInfo
    semantic_analysis: SemanticAnalysis
    items: list[EvaluationItem]
    acl_analysis: AclAnalysis
    audit_id: str
    network_analysis: NetworkAnalysis | None = None
    acl_candidate_analysis: AclCandidateAnalysis | None = None
    request_findings: RequestFindingsAnalysis | None = None

    @model_serializer(mode="wrap")
    def omit_disabled_shadow_stages(self, handler: Any) -> dict[str, Any]:
        serialized = handler(self)
        if self.acl_candidate_analysis is None:
            serialized.pop("acl_candidate_analysis", None)
        if self.request_findings is None:
            serialized.pop("request_findings", None)
        if self.network_analysis is None:
            serialized.pop("network_analysis", None)
        return serialized


class ErrorDetail(StrictModel):
    code: str
    message: str
    details: dict[str, int] | None = None

    @model_serializer(mode="wrap")
    def omit_empty_details(self, handler: Any) -> dict[str, Any]:
        serialized = handler(self)
        if self.details is None:
            serialized.pop("details", None)
        return serialized


class ErrorResponse(StrictModel):
    error: ErrorDetail


class AclRawResponse(StrictModel):
    analysis: str = ""
    config: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class LlmSemanticClaim(_SemanticClaimBase):
    pass


class LlmSemanticContradiction(StrictModel):
    contradiction_id: str
    scope: str
    description: str
    evidence: list[SemanticEvidence] = Field(min_length=2)


class LlmPolicyGap(StrictModel):
    gap_id: str
    scope: str
    gap_type: PolicyGapType
    description: str
    evidence: list[SemanticEvidence] = Field(min_length=1)
    affected_fields: list[SemanticClaimType] = Field(min_length=1)
    question_for_requester: str = Field(min_length=1, max_length=4000)
    suggested_effect: SemanticEffect


class LlmMissingInformation(MissingInformation):
    pass


class LlmSemanticResponse(StrictModel):
    analyzed_item_ids: list[str]
    claims: list[LlmSemanticClaim] = Field(default_factory=list)
    contradictions: list[LlmSemanticContradiction] = Field(default_factory=list)
    candidate_rule_ids: list[str] = Field(default_factory=list)
    policy_gaps: list[LlmPolicyGap] = Field(default_factory=list)
    questions_for_requester: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    network_claims: list[LlmNetworkSemanticClaim] = Field(default_factory=list)
    missing_information: list[LlmMissingInformation] = Field(default_factory=list)


class LlmExplanationItem(StrictModel):
    item_id: str = Field(min_length=1, max_length=300)
    explanation: str = Field(max_length=4000)
    recommendation: str = Field(max_length=4000)
    referenced_rule_ids: list[str] = Field(default_factory=list)


class LlmExplanationResponse(StrictModel):
    items: list[LlmExplanationItem]


def _normalize_semantic_identifier(value: Any) -> Any:
    return value.strip().lower() if isinstance(value, str) else value
