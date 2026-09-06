from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.main import Runtime
from app.schemas import EvaluationRequest
from tests.conftest import payload


def _request(request_id: str) -> EvaluationRequest:
    return EvaluationRequest.model_validate(payload(request_id=request_id))


class _FakeResolver:
    """Query-limit boundary double for runtime-level lifecycle tests."""

    def ensure_query_limit(self, request: EvaluationRequest) -> None:
        return None


def _runtime(*, evaluator: object, audit: object, resolver: object | None = None) -> Runtime:
    return Runtime(
        settings=None,  # type: ignore[arg-type]
        evaluator=evaluator,  # type: ignore[arg-type]
        audit=audit,  # type: ignore[arg-type]
        semaphore=asyncio.Semaphore(1),
        network_plan_resolver=resolver or _FakeResolver(),  # type: ignore[arg-type]
    )


def test_cancellation_during_threaded_claim_releases_owner() -> None:
    class DelayedAudit:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.abandoned: list[str] = []

        async def claim(self, request_id: str, digest: str):
            self.started.set()
            await self.release.wait()
            return "owner", None

        async def abandon(self, request_id: str) -> None:
            self.abandoned.append(request_id)

    class Evaluator:
        async def evaluate(self, request: EvaluationRequest):
            raise AssertionError("evaluation must not start after claim cancellation")

    async def scenario() -> None:
        audit = DelayedAudit()
        runtime = _runtime(evaluator=Evaluator(), audit=audit)
        task = asyncio.create_task(runtime.evaluate_request(_request("cancel-claim")))
        await audit.started.wait()
        task.cancel()
        audit.release.set()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert audit.abandoned == ["cancel-claim"]

    asyncio.run(scenario())


def test_cancellation_during_evaluation_releases_owner() -> None:
    class Audit:
        def __init__(self) -> None:
            self.abandoned: list[str] = []

        async def claim(self, request_id: str, digest: str):
            return "owner", None

        async def abandon(self, request_id: str) -> None:
            self.abandoned.append(request_id)

    class Evaluator:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def evaluate(self, request: EvaluationRequest):
            self.started.set()
            await asyncio.Future()

    async def scenario() -> None:
        audit = Audit()
        evaluator = Evaluator()
        runtime = _runtime(evaluator=evaluator, audit=audit)
        task = asyncio.create_task(runtime.evaluate_request(_request("cancel-evaluation")))
        await evaluator.started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert audit.abandoned == ["cancel-evaluation"]

    asyncio.run(scenario())


def test_runtime_close_attempts_every_resource_after_failure() -> None:
    closed: list[str] = []

    class Resource:
        def __init__(self, name: str, *, fail: bool = False) -> None:
            self.name = name
            self.fail = fail

        async def aclose(self) -> None:
            closed.append(self.name)
            if self.fail:
                raise RuntimeError(f"{self.name} close failed")

    llm = Resource("llm", fail=True)
    network = Resource("network")
    evaluator = SimpleNamespace(llm_client=llm)
    resolver = SimpleNamespace(provider=network)
    runtime = _runtime(evaluator=evaluator, audit=object(), resolver=resolver)

    with pytest.raises(RuntimeError, match="llm close failed"):
        asyncio.run(runtime.aclose())

    assert closed == ["llm", "network"]
