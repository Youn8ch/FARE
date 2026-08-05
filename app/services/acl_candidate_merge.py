from __future__ import annotations

from app.schemas import (
    AclCandidateComparison,
    AclCandidateMergeStatus,
    ExtractedFacts,
    LlmAclExtractionItem,
)


def merge_acl_candidate(
    *,
    item_id: str,
    deterministic: ExtractedFacts,
    llm_candidate: LlmAclExtractionItem | None = None,
    rejection_reason: str | None = None,
) -> AclCandidateComparison:
    """Keep deterministic and model layers separate and classify their relationship."""
    candidate = llm_candidate or LlmAclExtractionItem(item_id=item_id)
    status = _merge_status(deterministic, candidate, rejected=bool(rejection_reason))
    return AclCandidateComparison(
        item_id=item_id,
        status=status,
        deterministic=deterministic.model_copy(deep=True),
        llm_candidate=candidate.model_copy(deep=True),
        rejection_reason=rejection_reason,
    )


def _merge_status(
    deterministic: ExtractedFacts,
    candidate: LlmAclExtractionItem,
    *,
    rejected: bool,
) -> AclCandidateMergeStatus:
    if rejected:
        return "rejected"

    deterministic_signature = _deterministic_signature(deterministic)
    candidate_signature = _candidate_signature(candidate)
    deterministic_present = any(deterministic_signature) or (
        deterministic.explicit_no_path or deterministic.ambiguous
    )
    candidate_present = any(candidate_signature)
    if not deterministic_present and not candidate_present:
        return "empty"
    if deterministic_present and not candidate_present:
        return "deterministic_only"
    if candidate_present and not deterministic_present:
        return "llm_only"
    if (
        not deterministic.explicit_no_path
        and not deterministic.ambiguous
        and deterministic_signature == candidate_signature
    ):
        return "agree"
    return "conflict"


def _deterministic_signature(
    facts: ExtractedFacts,
) -> tuple[frozenset[str], frozenset[str], frozenset[str], frozenset[int]]:
    return (
        _normalized_strings(facts.firewalls),
        _normalized_strings(facts.candidate_acls),
        _normalized_strings(facts.address_objects),
        frozenset(facts.observed_ports),
    )


def _candidate_signature(
    facts: LlmAclExtractionItem,
) -> tuple[frozenset[str], frozenset[str], frozenset[str], frozenset[int]]:
    structured_firewalls = [
        str(fact.value) for fact in facts.facts if fact.type == "firewall"
    ]
    structured_acls = [
        str(fact.value) for fact in facts.facts if fact.type == "candidate_acl"
    ]
    structured_objects = [
        str(fact.value) for fact in facts.facts if fact.type == "address_object"
    ]
    structured_ports = [
        int(fact.value) for fact in facts.facts if fact.type == "observed_port"
    ]
    return (
        _normalized_strings([*facts.firewalls, *structured_firewalls]),
        _normalized_strings([*facts.candidate_acls, *structured_acls]),
        _normalized_strings([*facts.address_objects, *structured_objects]),
        frozenset([*facts.observed_ports, *structured_ports]),
    )


def _normalized_strings(values: list[str]) -> frozenset[str]:
    return frozenset(value.strip().casefold() for value in values)
