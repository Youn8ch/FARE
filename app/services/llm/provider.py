"""Provider transport boundary (hand-written HTTP until PHASE-06).

The channel owns the async HTTP client lifecycle: base URL comes only from
FARE settings, no automatic transport retries, ``trust_env=False`` so
environment proxies or keys can never alter the "settings-only" contract.
The resource is closed exactly once by the runtime lifecycle owner.
"""

from __future__ import annotations

from typing import Any

import httpx


class ProviderChannel:
    """Lifecycle-owned OpenAI-compatible HTTP channel."""

    def __init__(
        self,
        *,
        base_url: str | None,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if transport is not None and client is not None:
            raise ValueError("provide either an HTTP client or transport, not both")
        self.base_url = base_url.rstrip("/") if base_url else None
        self._owns_http_client = client is None
        self._http_client = client
        if self._owns_http_client:
            self._http_client = httpx.AsyncClient(
                transport=transport,
                trust_env=False,
            )

    @property
    def available(self) -> bool:
        return self._http_client is not None

    def headers(self, api_key: str | None) -> dict[str, str]:
        # The credential is read per call so tests and runtime rotation can
        # update it after construction without rebuilding the channel.
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    async def post_chat_completions(
        self,
        json_body: dict[str, Any],
        *,
        timeout: float,
        api_key: str | None,
    ) -> httpx.Response:
        assert self._http_client is not None  # noqa: S101 - guarded by available()
        return await self._http_client.post(
            f"{self.base_url}/chat/completions",
            headers=self.headers(api_key),
            json=json_body,
            timeout=timeout,
        )

    async def aclose(self) -> None:
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()
