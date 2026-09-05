"""Compatibility import facade for the FARE LLM boundary.

The implementation moved to :mod:`app.services.llm` (ports / contracts /
prompts / errors / adapter / structured_runtime / provider / telemetry /
mock_adapter). This module only re-exports the previous names so existing
imports keep working; new code imports from ``app.services.llm`` directly.
"""

from __future__ import annotations

from app.services.llm import (  # noqa: F401
    PROMPT_VERSIONS,
    LlmClient,
    LlmClientProtocol,
    LlmDependencyError,
    LlmRequestFindingsClientProtocol,
)
from app.services.llm.telemetry import (
    validation_error_summary as _validation_error_summary,  # noqa: F401
)
