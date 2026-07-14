from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from app.schemas import (
    Access,
    AclAnalysis,
    AclRawResponse,
    EvaluationItem,
    EvaluationRequest,
    EvaluationResponse,
    ExtractedFacts,
    MatchedRule,
    ModelInfo,
    SemanticAnalysis,
)
from app.services.acl_client import AclClient, AclDependencyError
from app.services.acl_extract import AclFactExtractor
from app.services.catalog import NetworkCatalog
from app.services.llm_client import LlmClient, LlmDependencyError
from app.services.output_guard import (
    SemanticGuardError,
    failed_semantic_analysis,
    guard_semantic_output,
)
from app.services.rule_loader import PolicyBundle, Rule
from app.services.splitter import AccessCombination, split_request


@dataclass(slots=True)
class EvaluationResult:
    response: EvaluationResponse
    acl_raw: list[dict[str, Any]]
    model_raw: dict[str, Any]
    exceptions: list[str]


@dataclass(slots=True)
class _AclRecord:
    item_id: str
    combination: AccessCombination
    raw: AclRawResponse | None
    facts: ExtractedFacts
    dependency_error: str | None


class Evaluator:
    def __init__(
        self,
        *,
        catalog: NetworkCatalog,
        policies: PolicyBundle,
        acl_client: AclClient,
        extractor: AclFactExtractor,
        llm_client: LlmClient,
    ) -> None:
        self.catalog = catalog
        self.policies = policies
        self.acl_client = acl_client
        self.extractor = extractor
        self.llm_client = llm_client

    async def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        combinations = split_request(request, self.catalog)
        records: list[_AclRecord] = []
        raw_records: list[dict[str, Any]] = []
        exceptions: list[str] = []
        model_raw: dict[str, Any] = {}

        for index, combination in enumerate(combinations, start=1):
            item_id = f"{request.request_id}-{index:03d}"
            raw: AclRawResponse | None = None
            dependency_error: str | None = None
            try:
                raw = await self.acl_client.analyze(combination)
                facts = self.extractor.extract(raw)
                raw_records.append(
                    {"item_id": item_id, "response": raw.model_dump(mode="json")}
                )
            except AclDependencyError as exc:
                dependency_error = str(exc)
                exceptions.append(f"ACL dependency: {dependency_error}")
                facts = ExtractedFacts()
                raw_records.append({"item_id": item_id, "error": dependency_error})
            records.append(
                _AclRecord(item_id, combination, raw, facts, dependency_error)
            )

        items = [
            self._evaluate_item(
                record.item_id,
                record.combination,
                record.facts,
                record.dependency_error,
                len(combinations),
            )
            for record in records
        ]
        analyses = [
            AclAnalysis(
                raw_analysis=record.raw.analysis if record.raw else "",
                raw_config=record.raw.config if record.raw else "",
                extracted_facts=record.facts,
            )
            for record in records
        ]

        semantic_payload = self._semantic_payload(request, records)
        model_raw["semantic_input"] = semantic_payload
        try:
            raw_semantic, semantic_raw = await self.llm_client.analyze(semantic_payload)
            model_raw["semantic"] = semantic_raw
            semantic = guard_semantic_output(
                raw_semantic,
                evidence_sources=_evidence_sources(records),
                authoritative_facts=_authoritative_facts(records),
                valid_rule_ids=self.policies.rule_ids,
            )
            deterministic_candidates = {
                rule.id for item in items for rule in item.matched_rules
            }
            semantic = semantic.model_copy(
                update={
                    "candidate_rule_ids": sorted(
                        deterministic_candidates | set(semantic.candidate_rule_ids)
                    )
                }
            )
            items = _apply_semantic_findings(items, semantic)
            semantic_succeeded = True
        except (LlmDependencyError, SemanticGuardError) as exc:
            detail = str(exc)
            exceptions.append(f"LLM semantic analysis: {detail}")
            model_raw.setdefault("semantic", {"error": detail})
            semantic = failed_semantic_analysis(
                [record.item_id for record in records], detail
            )
            items = [
                _semantic_failed(item) if item.decision == "合规" else item
                for item in items
            ]
            semantic_succeeded = False

        if semantic_succeeded:
            try:
                explanation_payload = {
                    "request_id": request.request_id,
                    "policy_version": self.policies.version,
                    "items": [item.model_dump(mode="json") for item in items],
                }
                model_raw["explanation_input"] = explanation_payload
                explanation, explanation_raw = await self.llm_client.explain(
                    explanation_payload
                )
                model_raw["explanation"] = explanation_raw
                expected = {item.item_id for item in items}
                actual = [item.item_id for item in explanation.items]
                if len(actual) != len(set(actual)) or set(actual) != expected:
                    raise LlmDependencyError(
                        "LLM explanation item set does not match request"
                    )
                explained = {item.item_id: item for item in explanation.items}
                items = [
                    item.model_copy(
                        update={
                            "reason": explained[item.item_id].explanation,
                            "recommendation": explained[item.item_id].recommendation,
                            "explanation_source": "llm",
                        }
                    )
                    for item in items
                ]
            except LlmDependencyError as exc:
                exceptions.append(f"LLM explanation: {exc}")
                model_raw["explanation"] = {"error": str(exc), "template_fallback": True}

        response = EvaluationResponse(
            request_id=request.request_id,
            decision="待定" if any(item.decision == "待定" for item in items) else "合规",
            policy_version=self.policies.version,
            model=ModelInfo(
                name=(
                    "mock-llm" if self.llm_client.mode == "mock" else "internal-openai-compatible"
                ),
                version=self.llm_client.model_name,
            ),
            semantic_analysis=semantic,
            items=items,
            acl_analysis=_aggregate_analyses(analyses),
            audit_id=str(uuid4()),
        )
        return EvaluationResult(
            response=response,
            acl_raw=raw_records,
            model_raw=model_raw,
            exceptions=exceptions,
        )

    def _semantic_payload(
        self, request: EvaluationRequest, records: list[_AclRecord]
    ) -> dict[str, Any]:
        return {
            "request_id": request.request_id,
            "request_description": request.request_description,
            "items": [
                {
                    "item_id": record.item_id,
                    "access": {
                        "source": record.combination.source_text,
                        "destination": record.combination.destination_text,
                        "protocol": record.combination.protocol,
                        "port": record.combination.port.model_dump(mode="json"),
                    },
                    "source_description": record.combination.source_description,
                    "destination_description": record.combination.destination_description,
                    "request_description": record.combination.request_description,
                    "authoritative_facts": _authoritative_fact(record),
                    "acl_analysis": record.raw.analysis if record.raw else "",
                    "acl_config": record.raw.config if record.raw else "",
                }
                for record in records
            ],
            "rules": [rule.semantic_summary() for rule in self.policies.rules],
        }

    def _evaluate_item(
        self,
        item_id: str,
        combination: AccessCombination,
        facts: ExtractedFacts,
        dependency_error: str | None,
        total_combinations: int,
    ) -> EvaluationItem:
        access = Access(
            source=combination.source_text,
            destination=combination.destination_text,
            protocol=combination.protocol,
            port=combination.port,
        )
        matched = self.policies.match(combination, total_combinations)
        evidence = _catalog_evidence(combination) + facts.evidence
        if matched:
            return _pending(item_id, access, matched[0], matched, evidence)
        catalog_error = combination.source.error_code or combination.destination.error_code
        if catalog_error:
            reason_type = "fact_conflict" if catalog_error == "ZONE_CONFLICT" else "fact_incomplete"
            messages = {
                "ADDRESS_ANY": "地址使用 any，且未命中已批准的最小开放规则。",
                "ADDRESS_INVALID": "地址无法解析；前置校验契约未满足。",
                "ZONE_UNRESOLVED": "地址子范围未命中唯一的权威网络目录。",
                "ZONE_CONFLICT": "地址子范围同时命中多个权威网络目录项。",
            }
            return EvaluationItem(
                item_id=item_id,
                access=access,
                decision="待定",
                reason_type=reason_type,
                reason_code=catalog_error,
                evidence=evidence,
                reason=messages[catalog_error],
                recommendation="补充或修正权威网络目录，并确保每个地址子范围唯一归属。",
            )
        if dependency_error:
            return EvaluationItem(
                item_id=item_id,
                access=access,
                decision="待定",
                reason_type="dependency_failure",
                reason_code="ACL_DEPENDENCY_FAILURE",
                evidence=evidence,
                reason="ACL 分析依赖调用失败，无法形成完整的候选路径事实。",
                recommendation="检查评估依赖并提交人工复核；如流程允许，创建新的评估版本。",
            )
        if facts.explicit_no_path:
            rule = self.policies.acl_no_path_rule
            return _pending(item_id, access, rule, [rule], evidence)
        if facts.ambiguous:
            return EvaluationItem(
                item_id=item_id,
                access=access,
                decision="待定",
                reason_type="fact_conflict",
                reason_code="ACL_FACT_AMBIGUOUS",
                evidence=evidence,
                reason="ACL 候选路径或拟配置分析存在歧义或冲突。",
                recommendation="由网络团队核实 ACL 分析原文并补充无歧义事实。",
            )
        if facts.observed_ports and not any(
            combination.port.start <= port <= combination.port.end
            for port in facts.observed_ports
        ):
            return EvaluationItem(
                item_id=item_id,
                access=access,
                decision="待定",
                reason_type="fact_conflict",
                reason_code="ACL_PORT_MISMATCH",
                evidence=evidence,
                reason="ACL 候选分析中明确出现的端口与申请端口不一致。",
                recommendation="核对申请端口和 ACL 分析输入后重新评估。",
            )
        if not facts.firewalls:
            return EvaluationItem(
                item_id=item_id,
                access=access,
                decision="待定",
                reason_type="fact_incomplete",
                reason_code="ACL_FIREWALL_UNRESOLVED",
                evidence=evidence,
                reason="ACL 分析未能确认候选路径中的防火墙；这不等同于明确无路径。",
                recommendation="补充可解析的候选防火墙路径事实。",
            )
        return EvaluationItem(
            item_id=item_id,
            access=access,
            decision="合规",
            evidence=evidence,
            reason="权威网络事实完整，且未命中规则包中的拒绝规则。",
            recommendation="按既有审批流程继续处理。",
        )


def _apply_semantic_findings(
    items: list[EvaluationItem], semantic: SemanticAnalysis
) -> list[EvaluationItem]:
    conflicts = {
        claim.scope for claim in semantic.claims if claim.status == "conflict"
    } | {
        contradiction.scope
        for contradiction in semantic.contradictions
        if contradiction.status == "verified"
    }
    gaps = {
        gap.scope for gap in semantic.policy_gaps if gap.status == "verified"
    }
    result: list[EvaluationItem] = []
    for item in items:
        if item.decision == "待定":
            result.append(item)
        elif item.item_id in conflicts:
            result.append(
                item.model_copy(
                    update={
                        "decision": "待定",
                        "reason_type": "fact_conflict",
                        "reason_code": "SEMANTIC_FACT_CONFLICT",
                        "reason": "通过证据守卫的候选语义与权威事实或其他原文证据冲突。",
                        "recommendation": "核对冲突字段并补充无歧义的权威事实。",
                    }
                )
            )
        elif item.item_id in gaps:
            result.append(
                item.model_copy(
                    update={
                        "decision": "待定",
                        "reason_type": "risk_uncertain",
                        "reason_code": "SEMANTIC_POLICY_GAP",
                        "reason": "存在有原文证据但当前正式规则无法确定处理的风险疑点。",
                        "recommendation": "提交人工复核，并由规则责任人评估是否补充正式规则。",
                    }
                )
            )
        else:
            result.append(item)
    return result


def _semantic_failed(item: EvaluationItem) -> EvaluationItem:
    return item.model_copy(
        update={
            "decision": "待定",
            "reason_type": "dependency_failure",
            "reason_code": "LLM_SEMANTIC_ANALYSIS_FAILURE",
            "reason": "必要的全申请语义分析未通过依赖或输出守卫，未据此输出合规结论。",
            "recommendation": "检查模型服务和输出契约后提交人工复核。",
        }
    )


def _pending(
    item_id: str,
    access: Access,
    primary: Rule,
    matched: list[Rule],
    evidence: list[str],
) -> EvaluationItem:
    return EvaluationItem(
        item_id=item_id,
        access=access,
        decision="待定",
        reason_type=primary.reason_type,
        reason_code=primary.id,
        matched_rules=[
            MatchedRule(id=rule.id, name=rule.name, category=rule.category)
            for rule in matched
        ],
        evidence=evidence,
        reason=primary.reason_template,
        recommendation=primary.recommendation,
    )


def _catalog_evidence(item: AccessCombination) -> list[str]:
    evidence: list[str] = []
    if item.source.entry:
        evidence.append(f"源地址命中 {item.source.entry.id}（区域 {item.source.entry.zone}）")
    if item.destination.entry:
        evidence.append(
            f"目的地址命中 {item.destination.entry.id}（区域 {item.destination.entry.zone}）"
        )
    return evidence


def _authoritative_fact(record: _AclRecord) -> dict[str, str]:
    source = record.combination.source.entry
    destination = record.combination.destination.entry
    facts: dict[str, str] = {}
    if source:
        facts.update(
            source_zone=source.zone,
            source_environment=source.environment,
            source_object_type=source.object_type,
        )
    if destination:
        facts.update(
            destination_zone=destination.zone,
            destination_environment=destination.environment,
            destination_object_type=destination.object_type,
        )
    return facts


def _authoritative_facts(records: list[_AclRecord]) -> dict[str, dict[str, str]]:
    return {record.item_id: _authoritative_fact(record) for record in records}


def _evidence_sources(records: list[_AclRecord]) -> dict[str, dict[str, str]]:
    return {
        record.item_id: {
            "request_description": record.combination.request_description,
            "source_description": record.combination.source_description,
            "destination_description": record.combination.destination_description,
            "acl_analysis": record.raw.analysis if record.raw else "",
            "acl_config": record.raw.config if record.raw else "",
        }
        for record in records
    }


def _aggregate_analyses(analyses: list[AclAnalysis]) -> AclAnalysis:
    facts = ExtractedFacts(
        firewalls=list(
            dict.fromkeys(
                name for analysis in analyses for name in analysis.extracted_facts.firewalls
            )
        ),
        explicit_no_path=any(
            analysis.extracted_facts.explicit_no_path for analysis in analyses
        ),
        candidate_acls=list(
            dict.fromkeys(
                name
                for analysis in analyses
                for name in analysis.extracted_facts.candidate_acls
            )
        ),
        address_objects=list(
            dict.fromkeys(
                name
                for analysis in analyses
                for name in analysis.extracted_facts.address_objects
            )
        ),
        observed_ports=sorted(
            {port for analysis in analyses for port in analysis.extracted_facts.observed_ports}
        ),
        evidence=list(
            dict.fromkeys(
                value for analysis in analyses for value in analysis.extracted_facts.evidence
            )
        ),
        ambiguous=any(analysis.extracted_facts.ambiguous for analysis in analyses),
    )
    return AclAnalysis(
        raw_analysis="\n\n".join(
            f"[组合 {index}]\n{analysis.raw_analysis}"
            for index, analysis in enumerate(analyses, 1)
        ),
        raw_config="\n\n".join(
            f"[组合 {index}]\n{analysis.raw_config}"
            for index, analysis in enumerate(analyses, 1)
        ),
        extracted_facts=facts,
    )
