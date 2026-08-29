"""V4-P5b: the Evaluator is an orchestrator over named stages.

Every stage is a separately owned step (app/services/stages/, app/services/
finding_factory.py, item_assembler.py, request_decision.py,
response_assembler.py); the Evaluator only expresses stage order, dependency
wiring, and error propagation. Finding construction, text mapping, item
mutation, metrics details, and shadow-stage flow live outside.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from app.schemas import (
    EvaluationRequest,
    EvaluationResponse,
    ModelInfo,
)
from app.services.acl_client import AclClient
from app.services.acl_extract import AclFactExtractor
from app.services.decision_reducer import DecisionReducer
from app.services.llm_client import LlmClientProtocol
from app.services.network_plan_resolver import (
    EvaluationItemLimitError,
    NetworkPlanResolution,
    NetworkPlanResolver,
)
from app.services.request_decision import aggregate_request_decision
from app.services.response_assembler import aggregate_analyses
from app.services.rule_loader import PolicyBundle
from app.services.splitter import split_resolved_request
from app.services.stage_metrics import llm_metadata, llm_metrics
from app.services.stages import (
    acl_stage,
    post_decision_stage,
    reduce_stage,
    rule_stage,
    semantic_stage,
)


@dataclass(slots=True)
class EvaluationResult:
    response: EvaluationResponse
    acl_raw: list[dict[str, Any]]
    model_raw: dict[str, Any]
    exceptions: list[str]
    network_plan_raw: list[dict[str, object]]


@dataclass(slots=True)
class _ResolutionStage:
    resolution: NetworkPlanResolution
    combinations: list
    raw_records: list[dict[str, object]]


class Evaluator:
    def __init__(
        self,
        *,
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
        stage_observer: Callable[[str], None] | None = None,
    ) -> None:
        if llm_request_findings_mode not in {"off", "shadow"}:
            raise ValueError(
                "request findings guarded mode is not approved; use off or shadow"
            )
        if acl_deterministic_pending_mode not in {"skip", "analyze"}:
            raise ValueError(
                "ACL deterministic pending mode must be 'skip' or 'analyze'"
            )
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
        self.decision_reducer = DecisionReducer()
        self._stage_observer = stage_observer
        self.config_id = config_id
        self.environment = environment
        self.config_fingerprint = config_fingerprint

    def _stage(self, name: str) -> None:
        if self._stage_observer is not None:
            self._stage_observer(name)

    def _guard_item_limit(self, request: EvaluationRequest) -> None:
        raw_count = (
            len(request.sources) * len(request.destinations) * len(request.ports)
        )
        if raw_count > self.max_evaluation_items:
            raise EvaluationItemLimitError(
                raw_count, self.max_evaluation_items
            )

    async def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        # Orchestration only: stage order is observable through the injected
        # stage observer; every step delegates to an owned stage module.
        self._stage("plan")
        self._guard_item_limit(request)

        self._stage("network")
        resolution_stage = await self._resolve_request_stage(request)
        combinations = resolution_stage.combinations

        self._stage("rules")
        rule_results = rule_stage.run(
            self.policies, request.request_id, combinations
        )

        self._stage("acl")
        acl_output = await acl_stage.run(
            acl_client=self.acl_client,
            extractor=self.extractor,
            decision_mode=self.acl_decision_mode,
            pending_mode=self.acl_deterministic_pending_mode,
            no_path_rule=self.policies.acl_no_path_rule,
            max_concurrency=self.acl_max_concurrency,
            rule_results=rule_results,
        )
        records = acl_output.records
        raw_records = acl_output.raw_records
        exceptions = acl_output.exceptions
        model_raw: dict[str, Any] = {
            "metadata": llm_metadata(self.llm_client, self.policies.version),
            "stages": {},
        }

        # 确定性装配：分区收集 findings，不产生 decision；reduce 阶段统一裁决。
        outcomes, analyses = reduce_stage.assemble_outcomes(
            self.policies, self.decision_reducer, rule_results, records
        )
        deterministic_candidates = {
            rule.id
            for outcome in outcomes.values()
            for rule in outcome.item_matched_rules
        }

        self._stage("semantic")
        semantic_result = await semantic_stage.run(
            llm_client=self.llm_client,
            policies=self.policies,
            semantic_effects=self.semantic_effects,
            request=request,
            records=records,
            model_raw=model_raw,
            exceptions=exceptions,
            deterministic_candidates=deterministic_candidates,
        )

        self._stage("reduce")
        items, llm_added_pending_count = reduce_stage.reduce_items(
            self.policies,
            self.decision_reducer,
            records,
            outcomes,
            semantic_result,
        )

        # Post-decision analysis (serial, D4): explanation / shadows may only
        # observe and explain; they cannot change the business conclusion.
        self._stage("post_decision")
        items, acl_candidate_analysis, request_findings = (
            await post_decision_stage.run(
                llm_client=self.llm_client,
                policies=self.policies,
                request=request,
                items=items,
                records=records,
                semantic_succeeded=semantic_result.succeeded,
                semantic_payload_items=semantic_result.payload_items,
                acl_candidate_mode=self.llm_acl_candidate_mode,
                request_findings_mode=self.llm_request_findings_mode,
                model_raw=model_raw,
                exceptions=exceptions,
            )
        )
        model_raw["metrics"] = llm_metrics(
            model_raw["stages"],
            llm_added_pending_count=llm_added_pending_count,
            items=items,
        )

        self._stage("assemble")
        response = EvaluationResponse(
            request_id=request.request_id,
            config_id=self.config_id,
            environment=self.environment,
            config_fingerprint=self.config_fingerprint,
            decision=aggregate_request_decision(items),
            policy_version=self.policies.version,
            model=ModelInfo(
                name=(
                    "mock-llm"
                    if self.llm_client.mode == "mock"
                    else "internal-openai-compatible"
                ),
                version=self.llm_client.model_name,
            ),
            semantic_analysis=semantic_result.semantic,
            items=items,
            acl_analysis=aggregate_analyses(
                analyses,
                [record.verification_status for record in records],
            ),
            audit_id=str(uuid4()),
            network_analysis=resolution_stage.resolution.analysis,
            acl_candidate_analysis=acl_candidate_analysis,
            request_findings=request_findings,
        )
        return EvaluationResult(
            response=response,
            acl_raw=raw_records,
            model_raw=model_raw,
            exceptions=exceptions,
            network_plan_raw=resolution_stage.raw_records,
        )

    async def _resolve_request_stage(
        self, request: EvaluationRequest
    ) -> _ResolutionStage:
        if self.network_plan_resolver is None:
            raise RuntimeError("an evaluator requires a network plan resolver")
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
