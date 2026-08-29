from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.config import FareConfig
from app.main import EvaluationServiceError, Runtime, build_runtime
from app.schemas import ErrorDetail, ErrorResponse, EvaluationRequest
from app.services.requirement_source import RequirementSource, build_requirement_source


async def fetch_requirements(config: FareConfig):
    source = build_requirement_source(config.requirement_source)
    try:
        return await source.fetch()
    finally:
        await _close_resources(source)


async def evaluate_requirements(
    runtime: Runtime,
    requests: Sequence[EvaluationRequest],
    *,
    max_concurrency: int,
) -> list[dict[str, Any]]:
    if max_concurrency < 1:
        raise ValueError("requirement evaluation concurrency must be positive")
    if not requests:
        return []

    queue: asyncio.Queue[tuple[int, EvaluationRequest]] = asyncio.Queue()
    for index, request in enumerate(requests):
        queue.put_nowait((index, request))
    results: list[dict[str, Any] | None] = [None] * len(requests)

    async def worker() -> None:
        while True:
            try:
                index, request = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                try:
                    response = await runtime.evaluate_request(request)
                    status = 200
                    body = response.model_dump(mode="json")
                except EvaluationServiceError as exc:
                    status = exc.status
                    body = ErrorResponse(
                        error=ErrorDetail(
                            code=exc.code,
                            message=exc.message,
                            details=exc.details,
                        )
                    ).model_dump(mode="json")
                results[index] = {
                    "request_id": request.request_id,
                    "http_status": status,
                    "response": body,
                }
            finally:
                queue.task_done()

    async with asyncio.TaskGroup() as group:
        for _ in range(min(max_concurrency, len(requests))):
            group.create_task(worker())

    if any(result is None for result in results):
        raise RuntimeError("requirement batch completed without all results")
    return [result for result in results if result is not None]


async def _run_batch_with_resources(
    config: FareConfig,
    source: RequirementSource,
    runtime: Runtime,
) -> dict[str, Any]:
    requests = await source.fetch()
    results = await evaluate_requirements(
        runtime,
        requests,
        max_concurrency=config.settings.max_concurrent_evaluations,
    )
    document = {
        "schema_version": "fare-requirement-run/v1",
        "config_id": config.config_id,
        "requirement_source_mode": config.requirement_source.mode,
        "results": results,
    }
    await asyncio.to_thread(
        _write_result, config.requirement_source.output_file, document
    )
    return document


async def run_requirement_batch_async(config: FareConfig) -> dict[str, Any]:
    source = build_requirement_source(config.requirement_source)
    runtime: Runtime | None = None
    try:
        runtime = build_runtime(config.settings)
        return await _run_batch_with_resources(config, source, runtime)
    finally:
        await _close_resources(source, runtime)


def run_requirement_batch(config: FareConfig) -> dict[str, Any]:
    return asyncio.run(run_requirement_batch_async(config))


async def _run_requirement_source_async(config: FareConfig) -> int:
    source = build_requirement_source(config.requirement_source)
    runtime: Runtime | None = None
    try:
        runtime = build_runtime(config.settings)
        while True:
            document = await _run_batch_with_resources(config, source, runtime)
            if any(result["http_status"] != 200 for result in document["results"]):
                return 1
            if config.requirement_source.execution_mode == "once":
                return 0
            await asyncio.sleep(config.requirement_source.poll_interval_seconds)
    finally:
        await _close_resources(source, runtime)


def run_requirement_source(config: FareConfig) -> int:
    try:
        return asyncio.run(_run_requirement_source_async(config))
    except KeyboardInterrupt:
        return 0


async def _close_resource(resource: object) -> None:
    close = getattr(resource, "aclose", None)
    if not callable(close):
        return
    result = close()
    if inspect.isawaitable(result):
        await result


async def _close_resources(*resources: object | None) -> None:
    errors: list[BaseException] = []
    seen: set[int] = set()
    for resource in resources:
        if resource is None or id(resource) in seen:
            continue
        seen.add(id(resource))
        try:
            await _close_resource(resource)
        except BaseException as exc:
            errors.append(exc)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("requirement resource close failures", errors)


def _write_result(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
