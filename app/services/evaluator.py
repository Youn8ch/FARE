from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import perf_counter
from typing import Any
from uuid import uuid4

from app.schemas import (
    Access,
    AclAnalysis,
    AclCandidateAnalysis,
    AclRawResponse,
    DecisionTrace,
    EvaluationItem,
    EvaluationRequest,
    EvaluationResponse,
    ExtractedFacts,
    MatchedRule,
    ModelInfo,
    RequestFindingsAnalysis,
    SemanticAnalysis,
)
from app.services.acl_candidate_merge import merge_acl_candidate
from app.services.acl_client import AclClient, AclDependencyError
from app.services.acl_extract import AclFactExtractor
from app.services.catalog import NetworkCatalog
from app.services.explanation_guard import ExplanationGuardError, guard_explanation_output
from app.services.llm_client import (
    LlmAclCandidateClientProtocol,
    LlmClientProtocol,
    LlmDependencyError,
    LlmRequestFindingsClientProtocol,
    guard_acl_candidates,
)
from app.services.network_plan_resolver import (
    EvaluationItemLimitError,
    NetworkPlanResolution,
    NetworkPlanResolver,
)
from app.services.output_guard import (
    RequestFindingGuardError,
    SemanticGuardError,
    failed_semantic_analysis,
    guard_request_findings,
    guard_semantic_output,
)
from app.services.rule_loader import PolicyBundle, Rule
from app.services.splitter import AccessCombination, split_request, split_resolved_request


@dataclass(slots=True)
class EvaluationResult:
    response: EvaluationResponse
    acl_raw: list[dict[str, Any]]
    model_raw: dict[str, Any]
    exceptions: list[str]
    network_plan_raw: list[dict[str, object]]


@dataclass(slots=True)
class _AclRecord:
    item_id: str
    combination: AccessCombination
    raw: AclRawResponse | None
    facts: ExtractedFacts
    dependency_error: str | None
    verification_status: str


@dataclass(slots=True)
class _ResolutionStage:
    resolution: NetworkPlanResolution | None
    combinations: list[AccessCombination]
    raw_records: list[dict[str, object]]


@dataclass(slots=True)
class _AclStage:
    records: list[_AclRecord]
    raw_records: list[dict[str, Any]]
    exceptions: list[str]


@dataclass(slots=True)
class _SemanticStage:
    semantic: SemanticAnalysis
    items: list[EvaluationItem]
    succeeded: bool
    added_pending_count: int


class Evaluator:
    def __init__(
        self,
        *,
        catalog: NetworkCatalog | None,
        policies: PolicyBundle,
        acl_client: AclClient,
        extractor: AclFactExtractor,
        llm_client: LlmClientProtocol,
        llm_acl_candidate_mode: str = "off",
        llm_request_findings_mode: str = "off",
        semantic_effects: dict[str, str] | None = None,
        network_plan_resolver: NetworkPlanResolver | None = None,
        max_evaluation_items: int = 256,
        acl_max_concurrency: int = 8,
        acl_decision_mode: str = "required",
        acl_deterministic_pending_mode: str = "analyze",
        config_id: str | None = None,
        environment: str | None = None,
        config_fingerprint: str | None = None,
    ) -> None:
        if llm_request_findings_mode not in {"off", "shadow"}:
            raise ValueError(
                "request findings guarded mode is not approved; use off or shadow"
            )
        if acl_deterministic_pending_mode not in {"skip", "analyze"}:
            raise ValueError(
                "ACL deterministic pending mode must be 'skip' or 'analyze'"
            )
        self.catalog = catalog
        self.policies = policies
        self.acl_client = acl_client
        self.extractor = extractor
        self.llm_client = llm_client
        self.llm_acl_candidate_mode = llm_acl_candidate_mode
        self.llm_request_findings_mode = llm_request_findings_mode
        self.semantic_effects = dict(semantic_effects or {})
        self.network_plan_resolver = network_plan_resolver
        self.max_evaluation_items = max_evaluation_items
        self.acl_max_concurrency = acl_max_concurrency
        self.acl_decision_mode = acl_decision_mode
        self.acl_deterministic_pending_mode = acl_deterministic_pending_mode
        self.config_id = config_id
        self.environment = environment
        self.config_fingerprint = config_fingerprint

    async def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        resolution_stage = await self._resolve_request_stage(request)
        resolution = resolution_stage.resolution
        combinations = resolution_stage.combinations
        network_plan_raw = resolution_stage.raw_records

        acl_stage = await self._run_acl_stage(request.request_id, combinations)
        records = acl_stage.records
        raw_records = acl_stage.raw_records
        exceptions = acl_stage.exceptions
        model_raw: dict[str, Any] = {
            "metadata": _llm_metadata(self.llm_client, self.policies.version),
            "stages": {},
        }

        items, analyses = self._build_deterministic_stage(records, len(combinations))

        semantic_payload = self._semantic_payload(request, records)
        semantic_stage = await self._run_semantic_stage(
            records=records,
            items=items,
            payload=semantic_payload,
            model_raw=model_raw,
            exceptions=exceptions,
        )
        semantic = semantic_stage.semantic
        items = semantic_stage.items
        semantic_succeeded = semantic_stage.succeeded
        llm_added_pending_count = semantic_stage.added_pending_count

        acl_candidate_analysis: AclCandidateAnalysis | None = None
        candidate_started = perf_counter()
        candidate_status = "off"
        candidate_error: Exception | None = None
        if self.llm_acl_candidate_mode == "shadow":
            candidate_status = "passed"
            candidate_records = list(records)
            candidate_inputs = [
                {
                    "item_id": record.item_id,
                    "analysis": record.raw.analysis if record.raw else "",
                    "config": record.raw.config if record.raw else "",
                }
                for record in candidate_records
            ]
            if candidate_inputs:
                model_raw["acl_candidates_input"] = candidate_inputs
                try:
                    if not isinstance(
                        self.llm_client, LlmAclCandidateClientProtocol
                    ):
                        raise LlmDependencyError(
                            "LLM client does not support ACL candidate extraction"
                        )
                    untrusted_candidates = await self.llm_client.extract_acl_facts(
                        candidate_inputs,
                        request_id=request.request_id,
                    )
                    candidates = guard_acl_candidates(
                        untrusted_candidates, candidate_inputs
                    )
                    expected_ids = {record.item_id for record in candidate_records}
                    if set(candidates) != expected_ids:
                        raise LlmDependencyError(
                            "LLM ACL candidate item set does not match shadow input"
                        )
                    acl_candidate_analysis = AclCandidateAnalysis(
                        items=[
                            merge_acl_candidate(
                                item_id=record.item_id,
                                deterministic=record.facts,
                                llm_candidate=candidates[record.item_id],
                            )
                            for record in candidate_records
                        ]
                    )
                    model_raw["acl_candidates"] = acl_candidate_analysis.model_dump(
                        mode="json"
                    )
                except LlmDependencyError as exc:
                    candidate_status = "rejected"
                    candidate_error = exc
                    detail = str(exc)
                    public_detail = "ACL candidate shadow output was rejected"
                    exceptions.append(f"LLM ACL candidates: {detail}")
                    acl_candidate_analysis = AclCandidateAnalysis(
                        items=[
                            merge_acl_candidate(
                                item_id=record.item_id,
                                deterministic=record.facts,
                                rejection_reason=public_detail,
                            )
                            for record in candidate_records
                        ]
                    )
                    model_raw["acl_candidates"] = {
                        "error": detail,
                        "result": acl_candidate_analysis.model_dump(mode="json"),
                    }
            else:
                acl_candidate_analysis = AclCandidateAnalysis()
                model_raw["acl_candidates"] = acl_candidate_analysis.model_dump(
                    mode="json"
                )
        _record_llm_stage(
            model_raw,
            "acl_candidates",
            candidate_started,
            candidate_status,
            candidate_error,
            self.llm_client,
        )

        request_findings: RequestFindingsAnalysis | None = None
        findings_started = perf_counter()
        findings_status = "off"
        findings_error: Exception | None = None
        if self.llm_request_findings_mode == "shadow":
            findings_status = "passed"
            finding_inputs = list(semantic_payload["items"])
            item_ids = [record.item_id for record in records]
            model_raw["request_findings_input"] = {
                "request_id": request.request_id,
                "items": finding_inputs,
            }
            try:
                if not isinstance(
                    self.llm_client, LlmRequestFindingsClientProtocol
                ):
                    raise LlmDependencyError(
                        "LLM client does not support request findings analysis"
                    )
                untrusted_findings = await self.llm_client.analyze_request_findings(
                    finding_inputs,
                    request_id=request.request_id,
                )
                guarded_findings = guard_request_findings(
                    untrusted_findings,
                    evidence_sources=_evidence_sources(records),
                )
                request_findings = RequestFindingsAnalysis(
                    status="completed",
                    analyzed_item_ids=guarded_findings.analyzed_item_ids,
                    findings=guarded_findings.findings,
                )
                model_raw["request_findings"] = request_findings.model_dump(
                    mode="json"
                )
                model_raw["request_findings_stats"] = {
                    "status": "completed",
                    "finding_count": len(guarded_findings.findings),
                    "affected_item_count": len(
                        {
                            item_id
                            for finding in guarded_findings.findings
                            for item_id in finding.affected_item_ids
                        }
                    ),
                }
            except (LlmDependencyError, RequestFindingGuardError) as exc:
                findings_status = "rejected"
                findings_error = exc
                detail = str(exc)
                public_detail = "Request findings shadow output was rejected"
                exceptions.append(f"LLM request findings: {detail}")
                request_findings = RequestFindingsAnalysis(
                    status="rejected",
                    analyzed_item_ids=item_ids,
                    rejection_reason=public_detail,
                )
                model_raw["request_findings"] = {
                    "error": detail,
                    "result": request_findings.model_dump(mode="json"),
                }
                model_raw["request_findings_stats"] = {
                    "status": "rejected",
                    "finding_count": 0,
                    "affected_item_count": 0,
                }
        _record_llm_stage(
            model_raw,
            "request_findings",
            findings_started,
            findings_status,
            findings_error,
            self.llm_client,
        )

        explanation_started = perf_counter()
        explanation_status = "skipped"
        explanation_error: Exception | None = None
        if semantic_succeeded:
            explanation_status = "passed"
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
                explained = guard_explanation_output(
                    explanation,
                    items=items,
                    valid_rule_ids=self.policies.rule_ids,
                )
                items = [
                    item.model_copy(
                        update={
                            "llm_explanation": explained[item.item_id].explanation,
                            "llm_recommendation": explained[item.item_id].recommendation,
                            "explanation_source": "llm",
                        }
                    )
                    for item in items
                ]
            except (LlmDependencyError, ExplanationGuardError) as exc:
                explanation_status = "rejected"
                explanation_error = exc
                exceptions.append(f"LLM explanation: {exc}")
                model_raw["explanation"] = {"error": str(exc), "template_fallback": True}
        _record_llm_stage(
            model_raw,
            "explanation",
            explanation_started,
            explanation_status,
            explanation_error,
            self.llm_client,
        )
        model_raw["metrics"] = _llm_metrics(
            model_raw["stages"],
            llm_added_pending_count=llm_added_pending_count,
            items=items,
        )

        response = EvaluationResponse(
            request_id=request.request_id,
            config_id=self.config_id,
            environment=self.environment,
            config_fingerprint=self.config_fingerprint,
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
            acl_analysis=_aggregate_analyses(analyses, records),
            audit_id=str(uuid4()),
            network_analysis=resolution.analysis if resolution else None,
            acl_candidate_analysis=acl_candidate_analysis,
            request_findings=request_findings,
        )
        return EvaluationResult(
            response=response,
            acl_raw=raw_records,
            model_raw=model_raw,
            exceptions=exceptions,
            network_plan_raw=network_plan_raw,
        )

    async def _resolve_request_stage(
        self, request: EvaluationRequest
    ) -> _ResolutionStage:
        if self.network_plan_resolver is not None:
            resolution = await self.network_plan_resolver.resolve(request)
            item_count = len(resolution.sources) * len(resolution.destinations) * len(
                request.ports
            )
            if item_count > self.max_evaluation_items:
                raise EvaluationItemLimitError(item_count, self.max_evaluation_items)
            return _ResolutionStage(
                resolution=resolution,
                combinations=split_resolved_request(request, resolution),
                raw_records=list(resolution.raw_records),
            )

        if self.catalog is None:
            raise RuntimeError("an evaluator requires a network plan resolver")
        combinations = split_request(request, self.catalog)
        if len(combinations) > self.max_evaluation_items:
            raise EvaluationItemLimitError(
                len(combinations), self.max_evaluation_items
            )
        return _ResolutionStage(
            resolution=None,
            combinations=combinations,
            raw_records=[],
        )

    async def _run_acl_stage(
        self,
        request_id: str,
        combinations: list[AccessCombination],
    ) -> _AclStage:
        semaphore = asyncio.Semaphore(self.acl_max_concurrency)
        total_combinations = len(combinations)
        analyzed = await asyncio.gather(
            *(
                self._analyze_acl_combination(
                    item_id=f"{request_id}-{index:03d}",
                    combination=combination,
                    total_combinations=total_combinations,
                    semaphore=semaphore,
                )
                for index, combination in enumerate(combinations, start=1)
            )
        )
        records = [record for record, _ in analyzed]
        return _AclStage(
            records=records,
            raw_records=[raw_record for _, raw_record in analyzed],
            exceptions=[
                f"ACL dependency: {record.dependency_error}"
                for record in records
                if record.dependency_error
            ],
        )

    async def _analyze_acl_combination(
        self,
        *,
        item_id: str,
        combination: AccessCombination,
        total_combinations: int,
        semaphore: asyncio.Semaphore,
    ) -> tuple[_AclRecord, dict[str, Any]]:
        if _network_fact_blocks_acl(combination):
            return (
                _skipped_acl_record(item_id, combination),
                {"item_id": item_id, "skipped_due_to_network_fact": True},
            )

        matched_rules = self.policies.match(combination, total_combinations)
        if self.acl_deterministic_pending_mode == "skip" and matched_rules:
            return (
                _skipped_acl_record(item_id, combination),
                {
                    "item_id": item_id,
                    "skipped": True,
                    "skip_reason": "deterministic_pending_rule",
                    "matched_rule_ids": [rule.id for rule in matched_rules],
                },
            )

        raw: AclRawResponse | None = None
        dependency_error: str | None = None
        try:
            async with semaphore:
                raw = await self.acl_client.analyze(combination)
            facts = self.extractor.extract(raw)
            raw_record = {
                "item_id": item_id,
                "response": raw.model_dump(mode="json"),
            }
            verification_status = _acl_verification_status(combination, facts, None)
        except AclDependencyError as exc:
            dependency_error = str(exc)
            facts = ExtractedFacts()
            raw_record = {"item_id": item_id, "error": dependency_error}
            verification_status = "unverified"
        return (
            _AclRecord(
                item_id,
                combination,
                raw,
                facts,
                dependency_error,
                verification_status,
            ),
            raw_record,
        )

    def _build_deterministic_stage(
        self,
        records: list[_AclRecord],
        total_combinations: int,
    ) -> tuple[list[EvaluationItem], list[AclAnalysis]]:
        items = [
            self._evaluate_item(
                record.item_id,
                record.combination,
                record.facts,
                record.dependency_error,
                total_combinations,
            ).model_copy(
                update=_network_item_fields(
                    record.combination, record.verification_status
                )
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
        return items, analyses

    async def _run_semantic_stage(
        self,
        *,
        records: list[_AclRecord],
        items: list[EvaluationItem],
        payload: dict[str, Any],
        model_raw: dict[str, Any],
        exceptions: list[str],
    ) -> _SemanticStage:
        deterministic_items = list(items)
        deterministic_pending_count = sum(
            item.decision == "待定" for item in deterministic_items
        )
        model_raw["semantic_input"] = payload
        started = perf_counter()
        error: Exception | None = None
        try:
            raw_semantic, semantic_raw = await self.llm_client.analyze(payload)
            model_raw["semantic"] = semantic_raw
            semantic = guard_semantic_output(
                raw_semantic,
                evidence_sources=_evidence_sources(records),
                authoritative_facts=_authoritative_facts(records),
                valid_rule_ids=self.policies.rule_ids,
                network_facts=_network_fact_bindings(records),
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
            semantic = _apply_configured_semantic_effects(
                semantic, self.semantic_effects
            )
            items = _apply_semantic_findings(
                items,
                semantic,
                fact_conflict_effect=self.semantic_effects.get(
                    "fact_conflict", "review_required"
                ),
            )
            succeeded = True
        except (LlmDependencyError, SemanticGuardError) as exc:
            error = exc
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
            items = [
                item.model_copy(
                    update={
                        "decision_trace": DecisionTrace(
                            deterministic_decision=deterministic.decision,
                            semantic_effect="semantic_failure",
                            semantic_finding_ids=[],
                            final_decision=item.decision,
                            final_reason_code=item.reason_code,
                        )
                    }
                )
                for deterministic, item in zip(
                    deterministic_items, items, strict=True
                )
            ]
            succeeded = False
        _record_llm_stage(
            model_raw,
            "semantic",
            started,
            "passed" if succeeded else "rejected",
            error,
            self.llm_client,
        )
        return _SemanticStage(
            semantic=semantic,
            items=items,
            succeeded=succeeded,
            added_pending_count=max(
                0,
                sum(item.decision == "待定" for item in items)
                - deterministic_pending_count,
            ),
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
                    "source_network_facts": _network_facts(record.combination.source),
                    "destination_network_facts": _network_facts(
                        record.combination.destination
                    ),
                    "network_plan_status": {
                        "source": _segment_status(record.combination.source),
                        "destination": _segment_status(record.combination.destination),
                    },
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
        network_error = _primary_network_error(combination)
        if network_error is not None:
            reason_type = (
                "fact_conflict"
                if network_error in {
                    "NETWORK_PLAN_FACT_CONFLICT",
                    "NETWORK_PLAN_SUBNET_MISMATCH",
                    "NETWORK_PLAN_NETWORK_MISMATCH",
                }
                else (
                    "dependency_failure"
                    if network_error
                    in {
                        "NETWORK_PLAN_DEPENDENCY_FAILURE",
                        "NETWORK_PLAN_AUTH_FAILURE",
                    }
                    else "fact_incomplete"
                )
            )
            return EvaluationItem(
                item_id=item_id,
                access=access,
                decision="待定",
                reason_type=reason_type,
                reason_code=network_error,
                matched_rules=[
                    MatchedRule(id=rule.id, name=rule.name, category=rule.category)
                    for rule in matched
                ],
                evidence=evidence,
                reason="网段规划权威事实未完整解析，无法形成确定性合规结论。",
                recommendation="核实网段规划数据或依赖状态后重新评估。",
            )
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
        if dependency_error and self.acl_decision_mode == "required":
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
        if not facts.firewalls and self.acl_decision_mode == "required":
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


def _apply_configured_semantic_effects(
    semantic: SemanticAnalysis, effects: dict[str, str]
) -> SemanticAnalysis:
    contradiction_effect = effects.get("contradiction", "review_required")
    contradictions = [
        contradiction.model_copy(
            update={
                "applied_effect": (
                    contradiction_effect
                    if contradiction.status == "verified"
                    else "observe_only"
                )
            }
        )
        for contradiction in semantic.contradictions
    ]
    gaps = [
        gap.model_copy(
            update={
                "applied_effect": (
                    effects.get(gap.gap_type, "observe_only")
                    if gap.status == "verified"
                    else "observe_only"
                )
            }
        )
        for gap in semantic.policy_gaps
    ]
    return semantic.model_copy(
        update={"contradictions": contradictions, "policy_gaps": gaps}
    )


def _apply_semantic_findings(
    items: list[EvaluationItem],
    semantic: SemanticAnalysis,
    *,
    fact_conflict_effect: str = "review_required",
) -> list[EvaluationItem]:
    review_ids: dict[str, list[str]] = {item.item_id: [] for item in items}
    observation_ids: dict[str, list[str]] = {item.item_id: [] for item in items}
    question_ids: dict[str, list[str]] = {item.item_id: [] for item in items}
    fact_conflict_ids: set[str] = set()

    for claim in (*semantic.claims, *semantic.network_claims):
        if claim.status == "conflict":
            fact_conflict_ids.add(claim.claim_id)
            target = (
                review_ids
                if fact_conflict_effect == "review_required"
                else question_ids
                if fact_conflict_effect == "question_only"
                else observation_ids
            )
            target[claim.scope].append(claim.claim_id)
    for contradiction in semantic.contradictions:
        if contradiction.status != "verified":
            continue
        fact_conflict_ids.add(contradiction.contradiction_id)
        target = (
            review_ids
            if contradiction.applied_effect == "review_required"
            else question_ids
            if contradiction.applied_effect == "question_only"
            else observation_ids
        )
        target[contradiction.scope].append(contradiction.contradiction_id)
    for gap in semantic.policy_gaps:
        if gap.status != "verified":
            continue
        target = (
            review_ids
            if gap.applied_effect == "review_required"
            else question_ids
            if gap.applied_effect == "question_only"
            else observation_ids
        )
        target[gap.scope].append(gap.gap_id)
    for missing in semantic.missing_information:
        question_ids[missing.item_id].append(missing.missing_id)

    result: list[EvaluationItem] = []
    for item in items:
        item_review_ids = review_ids[item.item_id]
        item_question_ids = question_ids[item.item_id]
        item_observation_ids = observation_ids[item.item_id]
        if item.decision == "待定":
            final = item
            effect = "unchanged"
            finding_ids = (
                item_review_ids + item_question_ids + item_observation_ids
            )
        elif item_review_ids:
            has_fact_conflict = bool(set(item_review_ids) & fact_conflict_ids)
            final = item.model_copy(
                update={
                    "decision": "待定",
                    "reason_type": (
                        "fact_conflict" if has_fact_conflict else "risk_uncertain"
                    ),
                    "reason_code": (
                        "SEMANTIC_FACT_CONFLICT"
                        if has_fact_conflict
                        else "SEMANTIC_POLICY_GAP"
                    ),
                    "reason": (
                        "通过证据守卫的申请语义与权威事实或其他原文证据冲突。"
                        if has_fact_conflict
                        else "存在经证据验证且被服务端影响策略列为人工复核的规则缺口。"
                    ),
                    "recommendation": (
                        "核对冲突字段并补充无歧义的权威事实。"
                        if has_fact_conflict
                        else "提交人工复核，并由规则责任人评估是否补充正式规则。"
                    ),
                }
            )
            effect = "downgraded"
            finding_ids = item_review_ids
        elif item_question_ids:
            final = item
            effect = "question_only"
            finding_ids = item_question_ids
        elif item_observation_ids:
            final = item
            effect = "observation_only"
            finding_ids = item_observation_ids
        else:
            final = item
            effect = "unchanged"
            finding_ids = []
        result.append(
            final.model_copy(
                update={
                    "decision_trace": DecisionTrace(
                        deterministic_decision=item.decision,
                        semantic_effect=effect,
                        semantic_finding_ids=finding_ids,
                        final_decision=final.decision,
                        final_reason_code=final.reason_code,
                    )
                }
            )
        )
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
    for role, segment in (("源", item.source), ("目的", item.destination)):
        facts = _network_facts(segment)
        for fact in facts:
            evidence.append(
                f"{role}地址引用网段事实 {fact['fact_id']}（区域 {fact['area_id']}）"
            )
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
    source_fact = getattr(record.combination.source, "primary_fact", None)
    destination_fact = getattr(record.combination.destination, "primary_fact", None)
    if source_fact is not None:
        facts.setdefault("source_zone", source_fact.area_id)
    if destination_fact is not None:
        facts.setdefault("destination_zone", destination_fact.area_id)
    return facts


def _authoritative_facts(records: list[_AclRecord]) -> dict[str, dict[str, str]]:
    return {record.item_id: _authoritative_fact(record) for record in records}


def _network_fact_bindings(
    records: list[_AclRecord],
) -> dict[str, dict[str, dict[str, dict[str, str | None]]]]:
    return {
        record.item_id: {
            role: {
                fact.fact_id: {
                    field: getattr(fact, field)
                    for field in (
                        "area_id",
                        "area",
                        "region_name",
                        "platform_name",
                        "network",
                        "subnet",
                        "usage_code",
                        "description",
                    )
                }
                for fact in getattr(segment, "network_facts", ())
            }
            for role, segment in (
                ("source", record.combination.source),
                ("destination", record.combination.destination),
            )
        }
        for record in records
    }


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


def _aggregate_analyses(
    analyses: list[AclAnalysis], records: list[_AclRecord]
) -> AclAnalysis:
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
        verification_summary={
            status: sum(record.verification_status == status for record in records)
            for status in ("verified", "unverified", "review_required", "skipped")
        },
    )


def _segment_status(segment: object) -> str:
    return str(getattr(segment, "network_fact_status", "complete"))


def _network_facts(segment: object) -> list[dict[str, Any]]:
    return [
        fact.model_dump(mode="json")
        for fact in getattr(segment, "network_facts", ())
    ]


def _primary_network_error(combination: AccessCombination) -> str | None:
    for segment in (combination.source, combination.destination):
        status = _segment_status(segment)
        if status not in {"complete", "not_applicable"}:
            return str(getattr(segment, "error_code", None) or "NETWORK_PLAN_INVALID_RESPONSE")
    return None


def _network_fact_blocks_acl(combination: AccessCombination) -> bool:
    return _primary_network_error(combination) is not None


def _skipped_acl_record(
    item_id: str, combination: AccessCombination
) -> _AclRecord:
    return _AclRecord(
        item_id=item_id,
        combination=combination,
        raw=None,
        facts=ExtractedFacts(),
        dependency_error=None,
        verification_status="skipped",
    )


def _network_item_fields(
    combination: AccessCombination, verification_status: str
) -> dict[str, Any]:
    return {
        "source_network_fact_ids": list(
            getattr(combination.source, "network_fact_ids", ())
        ),
        "destination_network_fact_ids": list(
            getattr(combination.destination, "network_fact_ids", ())
        ),
        "source_network_fact_status": _segment_status(combination.source),
        "destination_network_fact_status": _segment_status(combination.destination),
        "acl_verification_status": verification_status,
    }


def _acl_verification_status(
    combination: AccessCombination,
    facts: ExtractedFacts,
    dependency_error: str | None,
) -> str:
    if _network_fact_blocks_acl(combination):
        return "skipped"
    if facts.explicit_no_path or facts.ambiguous:
        return "review_required"
    if facts.observed_ports and not any(
        combination.port.start <= port <= combination.port.end
        for port in facts.observed_ports
    ):
        return "review_required"
    if dependency_error or not facts.firewalls:
        return "unverified"
    return "verified"


def _llm_metadata(client: LlmClientProtocol, policy_version: str) -> dict[str, Any]:
    return {
        "client_mode": client.mode,
        "model_version": client.model_name,
        "policy_version": policy_version,
        "prompt_versions": dict(getattr(client, "prompt_versions", {}) or {}),
        "fixture_version": getattr(client, "fixture_version", None),
    }


def _record_llm_stage(
    model_raw: dict[str, Any],
    stage: str,
    started: float,
    status: str,
    error: Exception | None,
    client: LlmClientProtocol,
) -> None:
    trace = None
    consume = getattr(client, "consume_completion_trace", None)
    if callable(consume):
        trace = consume()
    record: dict[str, Any] = {
        "status": status,
        "duration_ms": round((perf_counter() - started) * 1000, 3),
        "error_type": type(error).__name__ if error is not None else None,
        "attempts": 0,
        "corrections": 0,
    }
    if isinstance(trace, dict):
        record["attempts"] = int(trace.get("attempts", 0))
        record["corrections"] = int(trace.get("corrections", 0))
        record["provider_duration_ms"] = trace.get("duration_ms")
        record["completion_schema"] = trace.get("schema")
        record["completion_status"] = trace.get("status")
        record["completion_error_type"] = trace.get("error_type")
        record["completion_error_detail"] = trace.get("error_detail")
        if record["error_type"] is None and trace.get("error_type"):
            record["error_type"] = str(trace["error_type"])
    model_raw["stages"][stage] = record


def _llm_metrics(
    stages: dict[str, dict[str, Any]],
    *,
    llm_added_pending_count: int,
    items: list[EvaluationItem],
) -> dict[str, Any]:
    attempts = sum(int(stage.get("attempts", 0)) for stage in stages.values())
    corrections = sum(int(stage.get("corrections", 0)) for stage in stages.values())
    rejected = sum(stage.get("status") == "rejected" for stage in stages.values())
    schema_rejected = sum(
        stage.get("completion_status") == "failed" for stage in stages.values()
    )
    output_guard_rejected = sum(
        stage.get("status") == "rejected"
        and stage.get("completion_status") == "passed"
        for stage in stages.values()
    )
    dependency_failed = sum(
        stage.get("status") == "rejected"
        and stage.get("completion_status") not in {"passed", "failed"}
        for stage in stages.values()
    )
    traces = [item.decision_trace for item in items if item.decision_trace is not None]
    return {
        "schema_attempt_count": attempts,
        "schema_correction_count": corrections,
        "schema_correction_rate": round(corrections / attempts, 6) if attempts else 0.0,
        "guard_rejection_count": rejected,
        "model_schema_rejection_count": schema_rejected,
        "model_output_guard_rejection_count": output_guard_rejected,
        "model_dependency_failure_count": dependency_failed,
        "explanation_fallback_count": int(
            stages.get("explanation", {}).get("status") != "passed"
        ),
        "llm_added_pending_count": llm_added_pending_count,
        "model_business_downgrade_count": sum(
            trace.semantic_effect == "downgraded" for trace in traces
        ),
        "model_observation_only_count": sum(
            trace.semantic_effect == "observation_only" for trace in traces
        ),
        "model_question_only_count": sum(
            trace.semantic_effect == "question_only" for trace in traces
        ),
    }
