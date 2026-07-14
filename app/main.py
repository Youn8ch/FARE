from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import Settings
from app.schemas import ErrorDetail, ErrorResponse, EvaluationRequest, EvaluationResponse
from app.services.acl_client import HttpAclClient, MockAclClient
from app.services.acl_extract import AclFactExtractor
from app.services.audit import AuditStore, request_hash
from app.services.catalog import NetworkCatalog
from app.services.evaluator import Evaluator
from app.services.llm_client import LlmClient
from app.services.rule_loader import PolicyBundle


@dataclass(slots=True)
class Runtime:
    settings: Settings
    evaluator: Evaluator
    audit: AuditStore
    semaphore: asyncio.Semaphore


def build_runtime(settings: Settings) -> Runtime:
    settings.validate()
    catalog = NetworkCatalog.load(settings.policy_dir / "network_catalog.yaml")
    policies = PolicyBundle.load(settings.policy_dir, catalog.version)
    audit = AuditStore(settings.audit_log_dir, settings.audit_log_retention_days)
    audit.initialize()
    if settings.acl_client_mode == "mock":
        acl_client = MockAclClient(settings.acl_mock_file)
    else:
        acl_client = HttpAclClient(settings.acl_api_url or "", settings.acl_timeout_seconds)
    llm_client = LlmClient(
        mode=settings.llm_client_mode,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        mock_file=settings.llm_mock_file,
        semantic_timeout=settings.llm_semantic_timeout_seconds,
        explanation_timeout=settings.llm_explanation_timeout_seconds,
        max_correction_retries=settings.llm_max_correction_retries,
    )
    evaluator = Evaluator(
        catalog=catalog,
        policies=policies,
        acl_client=acl_client,
        extractor=AclFactExtractor(),
        llm_client=llm_client,
    )
    return Runtime(
        settings=settings,
        evaluator=evaluator,
        audit=audit,
        semaphore=asyncio.Semaphore(settings.max_concurrent_evaluations),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.runtime = build_runtime(settings or Settings.from_env())
        app.state.ready = True
        yield
        app.state.ready = False

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
        responses={409: {"model": ErrorResponse}},
        tags=["evaluations"],
    )
    async def evaluate(payload: EvaluationRequest, request: Request):
        runtime: Runtime = request.app.state.runtime
        digest = request_hash(payload)
        status, cached = await runtime.audit.claim(payload.request_id, digest)
        if status == "cached":
            return cached
        if status == "in_progress":
            return _error(409, "evaluation_in_progress", "evaluation is still in progress")
        if status == "conflict":
            return _error(
                409,
                "idempotency_conflict",
                "request_id was already used with different normalized input",
            )
        try:
            async with runtime.semaphore:
                result = await runtime.evaluator.evaluate(payload)
                await runtime.audit.persist(
                    request=payload,
                    input_hash=digest,
                    response=result.response,
                    acl_raw=result.acl_raw,
                    model_raw=result.model_raw,
                    exceptions=result.exceptions,
                )
            return result.response
        except Exception:
            await runtime.audit.abandon(payload.request_id)
            raise

    return app


def _error(status: int, code: str, message: str) -> JSONResponse:
    body = ErrorResponse(error=ErrorDetail(code=code, message=message))
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"))


app = create_app()
