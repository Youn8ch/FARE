from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import DEFAULT_CONFIG_PATH, FareConfig, Settings
from app.schemas import ErrorDetail, ErrorResponse, EvaluationRequest, EvaluationResponse
from app.services.acl_client import HttpAclClient, MockAclClient
from app.services.acl_extract import AclFactExtractor
from app.services.audit import AuditStore, redact_evaluation_response, request_hash
from app.services.catalog import NetworkCatalog
from app.services.evaluator import Evaluator
from app.services.llm_client import LlmClient
from app.services.network_plan_client import (
    HttpNetworkPlanClient,
    MockNetworkPlanClient,
    OfflineCatalogNetworkPlanClient,
    TtlNetworkPlanClient,
)
from app.services.network_plan_resolver import (
    EvaluationItemLimitError,
    NetworkPlanQueryLimitError,
    NetworkPlanResolver,
)
from app.services.rule_loader import PolicyBundle


@dataclass(slots=True)
class Runtime:
    settings: Settings
    evaluator: Evaluator
    audit: AuditStore
    semaphore: asyncio.Semaphore
    network_plan_resolver: NetworkPlanResolver

    async def evaluate_request(self, payload: EvaluationRequest) -> EvaluationResponse:
        try:
            # Every provider mode shares the same resolver main path, so the
            # query limit is enforced identically for mock/http/offline_catalog.
            self.network_plan_resolver.ensure_query_limit(payload)
        except NetworkPlanQueryLimitError as exc:
            raise EvaluationServiceError(
                422,
                "NETWORK_PLAN_QUERY_LIMIT_EXCEEDED",
                str(exc),
                {"actual": exc.actual, "limit": exc.limit},
            ) from exc

        digest = request_hash(payload)
        status, cached = await self._claim_audit(payload.request_id, digest)
        if status == "cached":
            if cached is None:
                raise RuntimeError("cached audit claim did not include a response")
            return cached
        if status == "in_progress":
            raise EvaluationServiceError(
                409, "evaluation_in_progress", "evaluation is still in progress"
            )
        if status == "conflict":
            raise EvaluationServiceError(
                409,
                "idempotency_conflict",
                "request_id was already used with different normalized input",
            )

        try:
            async with self.semaphore:
                try:
                    result = await self.evaluator.evaluate(payload)
                except EvaluationItemLimitError as exc:
                    raise EvaluationServiceError(
                        422,
                        "EVALUATION_ITEM_LIMIT_EXCEEDED",
                        str(exc),
                        {"actual": exc.actual, "limit": exc.limit},
                    ) from exc
                result.response = redact_evaluation_response(result.response)
                await self.audit.persist(
                    request=payload,
                    input_hash=digest,
                    response=result.response,
                    acl_raw=result.acl_raw,
                    model_raw=result.model_raw,
                    exceptions=result.exceptions,
                    network_plan_raw=result.network_plan_raw,
                )
            return result.response
        except asyncio.CancelledError:
            await self._abandon_claim(payload.request_id)
            raise
        except Exception:
            await self._abandon_claim(payload.request_id)
            raise

    async def _claim_audit(
        self, request_id: str, digest: str
    ) -> tuple[str, EvaluationResponse | None]:
        """Finish a threaded claim before propagating caller cancellation.

        ``asyncio.to_thread`` cannot stop work that has already entered SQLite. If
        the caller is cancelled while ``claim`` is running, wait for that claim to
        settle and release it when this runtime became the owner.
        """

        claim_task = asyncio.create_task(self.audit.claim(request_id, digest))
        try:
            return await asyncio.shield(claim_task)
        except asyncio.CancelledError:
            try:
                status, _ = await claim_task
            except Exception:
                pass
            else:
                if status == "owner":
                    await self._abandon_claim(request_id)
            raise

    async def _abandon_claim(self, request_id: str) -> None:
        abandon_task = asyncio.create_task(self.audit.abandon(request_id))
        try:
            await asyncio.shield(abandon_task)
        except asyncio.CancelledError:
            await abandon_task
            raise

    async def aclose(self) -> None:
        resources = [
            self.evaluator.llm_client,
            self.evaluator.acl_client,
            self.network_plan_resolver.client,
        ]
        seen: set[int] = set()
        errors: list[BaseException] = []
        for resource in resources:
            if resource is None or id(resource) in seen:
                continue
            seen.add(id(resource))
            close = getattr(resource, "aclose", None)
            if not callable(close):
                continue
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except BaseException as exc:
                errors.append(exc)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("runtime resource close failures", errors)


class EvaluationServiceError(RuntimeError):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: dict[str, int] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def build_runtime(settings: Settings) -> Runtime:
    settings.validate()
    catalog = None
    if settings.network_plan_client_mode == "offline_catalog":
        catalog = NetworkCatalog.load(settings.policy_dir / "network_catalog.yaml")
    policies = PolicyBundle.load(
        settings.policy_dir, catalog.version if catalog is not None else None
    )
    audit = AuditStore(
        settings.audit_log_dir,
        settings.audit_log_retention_days,
        config_id=settings.config_id,
        environment=settings.environment,
        config_fingerprint=settings.config_fingerprint,
    )
    audit.initialize()
    if settings.acl_client_mode == "mock":
        acl_client = MockAclClient(settings.acl_mock_file)
    else:
        acl_client = HttpAclClient(
            settings.acl_api_url or "",
            settings.acl_timeout_seconds,
            token=settings.acl_api_token,
        )
    llm_client = LlmClient(
        mode=settings.llm_client_mode,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        mock_file=settings.llm_mock_file,
        semantic_timeout=settings.llm_semantic_timeout_seconds,
        explanation_timeout=settings.llm_explanation_timeout_seconds,
        max_correction_retries=settings.llm_max_correction_retries,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        top_p=settings.llm_top_p,
        stop=settings.llm_stop,
        thinking=settings.llm_thinking,
    )
    # Every provider mode goes through the same resolver main path; the
    # offline catalog acts as an explicit compatibility provider.
    if settings.network_plan_client_mode == "mock":
        network_plan_client = MockNetworkPlanClient(settings.network_plan_mock_file)
    elif settings.network_plan_client_mode == "http":
        network_plan_client = HttpNetworkPlanClient(
            settings.network_plan_api_url or "",
            settings.network_plan_timeout_seconds,
            query_parameter=settings.network_plan_http_query_parameter,
            token=settings.network_plan_api_token,
        )
    else:
        network_plan_client = OfflineCatalogNetworkPlanClient(catalog)
    if settings.network_plan_cache_ttl_seconds > 0:
        network_plan_client = TtlNetworkPlanClient(
            network_plan_client,
            settings.network_plan_cache_ttl_seconds,
            settings.network_plan_cache_max_entries,
        )
    network_plan_resolver = NetworkPlanResolver(
        network_plan_client,
        max_subnets=settings.network_plan_max_subnets_per_request,
        max_concurrency=settings.network_plan_max_concurrency,
        lookup_timeout=settings.network_plan_timeout_seconds,
        batch_timeout=settings.network_plan_batch_timeout_seconds,
        offline_catalog=catalog,
    )
    evaluator = Evaluator(
        policies=policies,
        acl_client=acl_client,
        extractor=AclFactExtractor(),
        llm_client=llm_client,
        llm_acl_candidate_mode=settings.llm_acl_candidate_mode,
        llm_request_findings_mode=settings.llm_request_findings_mode,
        semantic_effects=settings.semantic_effects,
        network_plan_resolver=network_plan_resolver,
        max_evaluation_items=settings.max_evaluation_items,
        acl_max_concurrency=settings.acl_max_concurrency,
        acl_decision_mode=settings.acl_decision_mode,
        acl_deterministic_pending_mode=settings.acl_deterministic_pending_mode,
        config_id=settings.config_id,
        environment=settings.environment,
        config_fingerprint=settings.config_fingerprint,
    )
    return Runtime(
        settings=settings,
        evaluator=evaluator,
        audit=audit,
        semaphore=asyncio.Semaphore(settings.max_concurrent_evaluations),
        network_plan_resolver=network_plan_resolver,
    )


def create_app(
    settings: Settings | None = None,
    *,
    config_path=DEFAULT_CONFIG_PATH,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configured = settings or FareConfig.load(config_path).settings
        runtime = build_runtime(configured)
        app.state.runtime = runtime
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False
            await runtime.aclose()

    app = FastAPI(
        title="FARE",
        version="0.2.0",
        description="Firewall Access Request Evaluator",
        lifespan=lifespan,
    )

    @app.get("/healthz", tags=["operations"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", tags=["operations"])
    async def readyz(request: Request) -> JSONResponse:
        ready = bool(getattr(request.app.state, "ready", False))
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ready" if ready else "not_ready"},
        )

    @app.post(
        "/v1/evaluations",
        response_model=EvaluationResponse,
        responses={409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
        tags=["evaluations"],
    )
    async def evaluate(payload: EvaluationRequest, request: Request):
        runtime: Runtime = request.app.state.runtime
        try:
            return await runtime.evaluate_request(payload)
        except EvaluationServiceError as exc:
            return _error(
                exc.status,
                exc.code,
                exc.message,
                exc.details,
            )

    return app


def _error(
    status: int,
    code: str,
    message: str,
    details: dict[str, int] | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorDetail(code=code, message=message, details=details)
    )
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"))


app = create_app()
