from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import RequirementSourceSettings
from app.schemas import EvaluationRequest


class RequirementSourceError(RuntimeError):
    pass


class RequirementBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["fare-requirement-batch/v1"]
    requests: list[EvaluationRequest] = Field(min_length=1)


class RequirementSource:
    async def fetch(self) -> list[EvaluationRequest]:
        raise NotImplementedError

    async def aclose(self) -> None:
        """Release source resources; local sources intentionally do nothing."""


class LocalRequirementSource(RequirementSource):
    def __init__(self, directory: Path, pattern: str, recursive: bool) -> None:
        self.directory = directory
        self.pattern = pattern
        self.recursive = recursive

    async def fetch(self) -> list[EvaluationRequest]:
        if not self.directory.is_dir():
            raise RequirementSourceError(
                f"local requirement directory does not exist: {self.directory}"
            )
        iterator = (
            self.directory.rglob(self.pattern)
            if self.recursive
            else self.directory.glob(self.pattern)
        )
        paths = sorted(path for path in iterator if path.is_file())
        if not paths:
            raise RequirementSourceError(
                f"no local requirement files match {self.pattern!r} in {self.directory}"
            )
        requests: list[EvaluationRequest] = []
        for path in paths:
            try:
                batch = RequirementBatch.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                raise RequirementSourceError(
                    f"invalid local requirement batch: {path}"
                ) from exc
            requests.extend(batch.requests)
        _ensure_unique_request_ids(requests)
        return requests


class ApiRequirementSource(RequirementSource):
    def __init__(
        self,
        settings: RequirementSourceSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not settings.api_url:
            raise ValueError("API requirement source URL is required")
        if transport is not None and client is not None:
            raise ValueError("provide either an HTTP client or transport, not both")
        self.settings = settings
        self.transport = transport
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=self.settings.api_timeout_seconds,
            transport=self.transport,
            trust_env=False,
        )

    async def fetch(self) -> list[EvaluationRequest]:
        headers = {}
        if self.settings.api_token:
            headers["Authorization"] = f"Bearer {self.settings.api_token}"
        try:
            if self.settings.api_method == "POST":
                response = await self._client.post(
                    self.settings.api_url or "",
                    headers=headers,
                    json={"batch_size": self.settings.batch_size},
                    timeout=self.settings.api_timeout_seconds,
                )
            else:
                response = await self._client.get(
                    self.settings.api_url or "",
                    headers=headers,
                    params={"batch_size": self.settings.batch_size},
                    timeout=self.settings.api_timeout_seconds,
                )
            response.raise_for_status()
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
            raise RequirementSourceError(
                f"network requirement API request failed: {type(exc).__name__}"
            ) from exc
        try:
            batch = RequirementBatch.model_validate(response.json())
        except (ValueError, json.JSONDecodeError) as exc:
            raise RequirementSourceError(
                "network requirement API returned an invalid requirement batch"
            ) from exc
        if len(batch.requests) > self.settings.batch_size:
            raise RequirementSourceError(
                "network requirement API exceeded configured batch_size"
            )
        requests = list(batch.requests)
        _ensure_unique_request_ids(requests)
        return requests

    async def aclose(self) -> None:
        if self._owns_client and not self._client.is_closed:
            await self._client.aclose()


def build_requirement_source(
    settings: RequirementSourceSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    client: httpx.AsyncClient | None = None,
) -> RequirementSource:
    if settings.mode == "local":
        if settings.local_directory is None:
            raise ValueError("local requirement source directory is required")
        return LocalRequirementSource(
            settings.local_directory,
            settings.local_pattern,
            settings.local_recursive,
        )
    if settings.mode == "api":
        return ApiRequirementSource(settings, transport=transport, client=client)
    raise ValueError(f"unsupported requirement source mode: {settings.mode}")


def _ensure_unique_request_ids(requests: list[EvaluationRequest]) -> None:
    ids = [request.request_id for request in requests]
    if len(ids) != len(set(ids)):
        raise RequirementSourceError("network requirement request_id values must be unique")
