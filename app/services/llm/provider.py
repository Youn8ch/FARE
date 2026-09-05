"""Provider transport boundary: OpenAI SDK + Instructor.

The channel owns the ``AsyncOpenAI`` construction and the lifecycle-owned
``httpx.AsyncClient``:

- ``base_url`` comes only from FARE settings;
- ``max_retries=0`` freezes provider/transport behavior — no automatic SDK
  retry on connection errors, 408/409/429/5xx;
- the HTTP client is built with ``trust_env=False`` so environment proxies
  and environment keys can never alter the settings-only contract;
- ``api_key`` is supplied per call through ``extra_headers``: a configured
  key is sent as ``Authorization: Bearer <key>``, and no-auth deployments
  explicitly omit the header with the SDK's public ``omit`` contract, so no
  ``Authorization`` header ever leaves the process without a configured key
  (the SDK client itself is constructed with an internal placeholder key
  that never reaches the wire);
- Instructor runs in the JSON-compatible mode (``Mode.JSON``) verified by the
  provider contract spike: schema-constrained JSON generation without
  assuming tools/JSON-Schema support from OpenAI-compatible providers.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import instructor
from openai import AsyncOpenAI, Omit, omit

# FARE owns observability: instructor's internal retry logger emits raw
# provider error bodies (which may echo secrets such as reflected keys).
# The FARE typed errors and completion traces replace that surface entirely.
logging.getLogger("instructor").setLevel(logging.CRITICAL)

# The OpenAI SDK refuses to construct without a credential and would fall
# back to reading OPENAI_API_KEY from the environment; this internal
# placeholder keeps no-auth profiles environment-free. It never reaches the
# wire: every call overrides Authorization through ``auth_headers()``, which
# omits the header entirely when no key is configured.
NO_AUTH_PLACEHOLDER_KEY = "no-auth"

AuthHeader = str | Omit


class ProviderChannel:
    """Lifecycle-owned provider channel around ``AsyncOpenAI``."""

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
        self._sdk = AsyncOpenAI(
            base_url=self.base_url,
            api_key=NO_AUTH_PLACEHOLDER_KEY,
            http_client=self._http_client,
            max_retries=0,
        )
        self.structured = instructor.from_openai(self._sdk, mode=instructor.Mode.JSON)

    @property
    def available(self) -> bool:
        return self._http_client is not None

    def auth_headers(self, api_key: str | None) -> dict[str, AuthHeader]:
        """Per-call credential override; the credential is read at call time.

        ``openai.omit`` (public SDK contract) removes the ``Authorization``
        header entirely for no-auth profiles instead of letting the internal
        placeholder key leak onto the wire.
        """

        if api_key:
            return {"Authorization": f"Bearer {api_key}"}
        return {"Authorization": omit}

    async def aclose(self) -> None:
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()

    def structured_create(self) -> Any:
        return self.structured.chat.completions.create_with_completion
