"""FARE LLM boundary package.

Ownership map (see docs/ARCHITECTURE.md):

- ``ports``            — use-case protocols stages depend on (FARE)
- ``contracts``        — LLM I/O DTO surface + strict candidate model (FARE)
- ``prompts``          — prompt texts and versions (FARE)
- ``errors``           — FARE error taxonomy (FARE)
- ``adapter``          — use-case adapter: deadline, error translation (FARE)
- ``structured_runtime`` — bounded structure validation/re-ask loop
- ``provider``         — provider transport channel lifecycle
- ``telemetry``        — per-call completion traces
- ``mock_adapter``     — offline deterministic test adapter

Instructor / the OpenAI SDK (PHASE-06) live strictly below ``adapter``: they
never see FARE stages, guards, findings, or decisions.
"""

from app.services.llm.adapter import LlmClient
from app.services.llm.contracts import LlmStrictModel
from app.services.llm.errors import (
    DomainValidationFailure,
    FareLlmError,
    LlmDependencyError,
    ProviderFailure,
    SemanticPolicyFailure,
    StructuredOutputFailure,
    TimeoutFailure,
)
from app.services.llm.ports import (
    LlmClientProtocol,
    LlmRequestFindingsClientProtocol,
)
from app.services.llm.prompts import PROMPT_VERSIONS

__all__ = [
    "DomainValidationFailure",
    "FareLlmError",
    "LlmClient",
    "LlmClientProtocol",
    "LlmDependencyError",
    "LlmRequestFindingsClientProtocol",
    "LlmStrictModel",
    "PROMPT_VERSIONS",
    "ProviderFailure",
    "SemanticPolicyFailure",
    "StructuredOutputFailure",
    "TimeoutFailure",
]
