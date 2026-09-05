"""LLM I/O contracts (FARE-owned).

The wire DTOs stay defined in :mod:`app.schemas` next to the public API
models they mirror; this module is the single import surface for the LLM
boundary and owns the strict candidate model used for structured output.

``LlmStrictModel`` adds ``strict=True`` on top of ``extra='forbid'``: LLM
candidates must arrive with real JSON types, not string coercions.
"""

from __future__ import annotations

from pydantic import ConfigDict

from app.schemas import (
    LlmExplanationItem,
    LlmExplanationResponse,
    LlmMissingInformation,
    LlmPolicyGap,
    LlmRequestFinding,
    LlmRequestFindingsResponse,
    LlmSemanticClaim,
    LlmSemanticContradiction,
    LlmSemanticResponse,
)

__all__ = [
    "LlmExplanationItem",
    "LlmExplanationResponse",
    "LlmMissingInformation",
    "LlmPolicyGap",
    "LlmRequestFinding",
    "LlmRequestFindingsResponse",
    "LlmSemanticClaim",
    "LlmSemanticContradiction",
    "LlmSemanticResponse",
    "LlmStrictModel",
]


class LlmStrictModel:
    """Mixin base marking a candidate contract as type-strict.

    Implemented as a configuration recipe so schemas can adopt it without
    changing their inheritance tree: ``model_config = LlmStrictModel.config``.
    """

    config = ConfigDict(extra="forbid", strict=True)
