from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from pathlib import Path

from app.schemas import AclRawResponse
from app.services.splitter import AccessCombination


class AclDependencyError(RuntimeError):
    pass


class AclClient(ABC):
    @abstractmethod
    async def analyze(self, combination: AccessCombination) -> AclRawResponse:
        raise NotImplementedError


class MockAclClient(AclClient):
    def __init__(self, fixture_path: Path | None = None) -> None:
        self.fixture_path = fixture_path
        self._fixture = self._load(fixture_path) if fixture_path else None
        self.calls: list[str] = []

    async def analyze(self, combination: AccessCombination) -> AclRawResponse:
        await asyncio.sleep(0)
        key = (
            f"{combination.source_text}|{combination.destination_text}|"
            f"{combination.protocol}|{combination.port.start}-{combination.port.end}"
        )
        self.calls.append(key)
        if not self._fixture:
            return AclRawResponse(
                analysis="候选路径经过防火墙 MOCK-FW-01。",
                config=(
                    f"access-list FARE-CANDIDATE object-group SRC object-group DST "
                    f"port {combination.port.start}"
                ),
                metadata={"mock": True},
            )
        raw = self._fixture.get("responses", {}).get(key, self._fixture.get("default"))
        if raw is None:
            raise AclDependencyError(f"mock ACL fixture has no response for combination {key}")
        if isinstance(raw, dict) and raw.get("error"):
            raise AclDependencyError(str(raw["error"]))
        try:
            return AclRawResponse.model_validate(raw)
        except Exception as exc:
            raise AclDependencyError("mock ACL response cannot be parsed") from exc

    @staticmethod
    def _load(path: Path) -> dict:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid ACL mock fixture: {path}") from exc
        if not isinstance(value, dict):
            raise ValueError("ACL mock fixture root must be an object")
        return value


class HttpAclClient(AclClient):
    """Stable boundary for the real ACL adapter.

    Deliberately fails closed until the provider's request/response contract and the
    machine-readable no-path expression are supplied and covered by contract tests.
    """

    def __init__(self, url: str, timeout: float) -> None:
        self.url = url
        self.timeout = timeout

    async def analyze(self, combination: AccessCombination) -> AclRawResponse:
        raise AclDependencyError(
            "HTTP ACL adapter is disabled until the real ACL API contract is implemented"
        )
