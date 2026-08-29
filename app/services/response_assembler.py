"""V4-P5a: response-level ACL analysis aggregation, extracted verbatim from
``evaluator.py`` with zero behavior change (mechanical move).
"""

from __future__ import annotations

from app.schemas import AclAnalysis, ExtractedFacts


def aggregate_analyses(
    analyses: list[AclAnalysis], verification_statuses: list[str]
) -> AclAnalysis:
    facts = ExtractedFacts(
        firewalls=list(
            dict.fromkeys(
                name for analysis in analyses for name in analysis.extracted_facts.firewalls
            )
        ),
        explicit_no_path=any(
            analysis.extracted_facts.explicit_no_path for analysis in analyses
        ),
        candidate_acls=list(
            dict.fromkeys(
                name
                for analysis in analyses
                for name in analysis.extracted_facts.candidate_acls
            )
        ),
        address_objects=list(
            dict.fromkeys(
                name
                for analysis in analyses
                for name in analysis.extracted_facts.address_objects
            )
        ),
        observed_ports=sorted(
            {port for analysis in analyses for port in analysis.extracted_facts.observed_ports}
        ),
        evidence=list(
            dict.fromkeys(
                value for analysis in analyses for value in analysis.extracted_facts.evidence
            )
        ),
        ambiguous=any(analysis.extracted_facts.ambiguous for analysis in analyses),
    )
    return AclAnalysis(
        raw_analysis="\n\n".join(
            f"[组合 {index}]\n{analysis.raw_analysis}"
            for index, analysis in enumerate(analyses, 1)
        ),
        raw_config="\n\n".join(
            f"[组合 {index}]\n{analysis.raw_config}"
            for index, analysis in enumerate(analyses, 1)
        ),
        extracted_facts=facts,
        verification_summary={
            status: sum(verification == status for verification in verification_statuses)
            for status in ("verified", "unverified", "review_required", "skipped")
        },
    )
