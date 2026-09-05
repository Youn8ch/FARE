"""Clean-wheel install smoke for the FARE 0.3.0 release artifact.

Run inside a freshly created virtual environment where the 0.3.0 wheel was
installed from the wheel file itself (never editable source), ideally with
``-c constraints.txt``. Verifies distribution metadata, the runtime app
surface, the /v2 mock contract, and the no-auth HTTP header contract:

    python scripts/wheel_smoke_check.py

Exits non-zero on the first violated expectation.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from importlib import metadata
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

REQUIRED_FARE_VERSION = "0.3.0"
REQUIRED_PINNED = {"instructor": "1.16.0", "openai": "2.54.0"}


def check(condition: bool, message: str) -> None:
    if not condition:
        print(f"FAIL: {message}")
        sys.exit(1)
    print(f"ok: {message}")


def main() -> None:
    # 1. Distribution metadata.
    version = metadata.version("fare")
    check(version == REQUIRED_FARE_VERSION, f"fare version == {REQUIRED_FARE_VERSION}")
    requires = metadata.requires("fare") or []
    names = {
        _canonical_dependency_name(name) for name in requires
    }
    check("instructor" in names, "Requires-Dist contains instructor")
    check("openai" in names, "Requires-Dist contains openai")
    for dist_name, expected in REQUIRED_PINNED.items():
        actual = metadata.version(dist_name)
        check(actual == expected, f"{dist_name} == {expected} (got {actual})")

    # 2. No tests, fixtures, audit data, or local secrets are packaged.
    file_names = {
        str(file).split("/")[0].split("\\")[0]
        for file in metadata.files("fare")
        if file is not None
    }
    packaged_roots = sorted(file_names)
    check("app" in packaged_roots, "wheel installs the app package")
    forbidden_roots = {"tests", "evals", "config", "audit_logs", "policies"}
    leaked = forbidden_roots & set(packaged_roots)
    check(not leaked, f"wheel packages no {sorted(leaked) or 'forbidden'} content")

    # 3. Runtime app surface: create_app + complete /v2 response schemas.
    from app.config import Settings
    from app.main import create_app

    settings = Settings(
        policy_dir=Path("policies").resolve(),
        audit_log_dir=Path(tempfile.mkdtemp(prefix="fare-wheel-smoke-")) / "audit",
        audit_log_retention_days=30,
        llm_client_mode="mock",
        llm_base_url=None,
        llm_model=None,
        llm_api_key=None,
        llm_mock_file=None,
        llm_semantic_timeout_seconds=10,
        llm_explanation_timeout_seconds=6,
        llm_max_correction_retries=1,
        max_concurrent_evaluations=4,
        llm_request_findings_mode="off",
        network_plan_client_mode="mock",
        network_plan_mock_file=_core_plan_fixture(),
    )
    app = create_app()
    spec = app.openapi()
    components = spec["components"]["schemas"]
    check(
        len(components["EvaluationResponse"]["properties"]) == 12,
        "OpenAPI EvaluationResponse has 12 properties",
    )
    check(
        len(components["EvaluationItem"]["properties"]) == 18,
        "OpenAPI EvaluationItem has 18 properties",
    )

    # 4. Mock /v2 evaluation through the TestClient surface.
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v2/evaluations",
            json={
                "request_id": "wheel-smoke-001",
                "sources": [{"address": "16.1.30.10", "description": "生产应用 A"}],
                "destinations": [
                    {"address": "16.1.30.20", "description": "生产应用 B"}
                ],
                "protocol": "tcp",
                "ports": [{"start": 443, "end": 443}],
                "request_description": "生产应用 HTTPS 访问",
            },
        )
        check(response.status_code == 200, "mock /v2/evaluations returns 200")
        body = response.json()
        check("request_findings" not in body, "disabled shadow stage stays omitted")
        check(body["decision"] in {"合规", "待定"}, "mock decision present")

    # 5. no-auth HTTP contract: no Authorization header ever leaves the
    # process when no key is configured.
    captured: dict[str, str | None] = {}
    completion = {
        "id": "chatcmpl-wheel-smoke",
        "choices": [
            {
                "message": {
                    "content": json.dumps({"analyzed_item_ids": ["wheel-smoke-001"]})
                }
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("Authorization")
        return httpx.Response(200, json=completion)

    from app.schemas import LlmSemanticResponse
    from app.services.llm.provider import ProviderChannel
    from app.services.llm.structured_runtime import complete_structured
    from app.services.llm.telemetry import CompletionTraceRecorder

    channel = ProviderChannel(
        base_url="http://model.invalid/v1",
        transport=httpx.MockTransport(handler),
    )

    async def call() -> None:
        await complete_structured(
            channel=channel,
            traces=CompletionTraceRecorder(id(channel)),
            schema=LlmSemanticResponse,
            messages=[{"role": "user", "content": "smoke"}],
            model="smoke-model",
            api_key=None,
            temperature=0,
            max_tokens=None,
            top_p=None,
            stop=None,
            thinking=None,
            max_correction_retries=0,
            total_timeout=5,
        )

    asyncio.run(call())
    check(
        captured["authorization"] is None,
        "no-auth outbound request carries no Authorization header",
    )
    check(
        "no-auth" not in json.dumps(captured),
        "internal placeholder key never reaches the wire",
    )
    asyncio.run(channel.aclose())

    print("wheel smoke: ALL CHECKS PASSED")


def _canonical_dependency_name(requirement: str) -> str:
    """The lowercase distribution name of one ``Requires-Dist`` entry."""

    name = requirement.split(";")[0]
    for separator in (" ", ">", "<", "=", "["):
        name = name.split(separator)[0]
    return name.strip().lower()


def _core_plan_fixture() -> Path:
    """Locate the core network plan fixture next to the checkout.

    The fixture is a data asset, deliberately not packaged in the wheel; the
    smoke runs from the repository root so policies/fixtures are resolvable.
    """

    candidates = sorted(
        Path("tests/fixtures/network_plan").glob("*.json")
    )
    if not candidates:
        raise SystemExit("FAIL: core network plan fixture not found")
    return candidates[0].resolve()


if __name__ == "__main__":
    main()
