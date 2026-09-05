"""Precision tests for the ACL-removal scan (H-04 hardening).

The scan must be word-precise (no ``dataclass``/``aclose`` false positives),
machine-executable, and it must actually catch a live ACL symbol in active
code instead of only counting lines.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import acl_scan  # noqa: E402 - deliberate import of the scan tooling


def _classify(path: str, line: str) -> tuple[str | None, str | None]:
    return acl_scan.classify(path, line, acl_scan.load_allowlist())


def test_word_precise_pattern_rejects_substring_pollution() -> None:
    allowlist = acl_scan.load_allowlist()
    for innocent in (
        "@dataclass(frozen=True, slots=True)",
        "await channel.aclose()",
    ):
        assert not acl_scan.acl_pattern(allowlist).search(innocent), innocent
    for real in (
        "acl_analysis",
        "from app.services.stages import acl_stage",
        "ACL_NO_PATH",
        "AclCandidateAnalysis",
        "the acl assessment capability",
    ):
        assert acl_scan.acl_pattern(allowlist).search(real), real


def test_classifier_categories() -> None:
    category, reason = _classify(
        "app/services/audit.py",
        'AUDIT_SCHEMA_EPOCH = "fare-audit/v2-no-acl"',
    )
    assert (category, reason) == ("active_code", None)

    category, reason = _classify(
        "app/config.py", 'acl: {"mode": "mock"}'
    )
    assert category is None
    assert "active code" in (reason or "")

    category, _ = _classify(
        "tests/test_x.py", 'assert "acl_analysis" not in body'
    )
    assert category == "negative_test"

    category, _ = _classify(
        "tests/cases/evaluations/realistic_network_requests.v1.json",
        '"acl_verification_status": "verified"',
    )
    assert category == "legacy_dataset"

    category, _ = _classify(
        "evals/llm/datasets/manifest.json", "legacy acl contract"
    )
    assert category == "legacy_dataset"

    category, _ = _classify(
        "docs/v3-baseline.md", "the removed ACL assessment capability"
    )
    assert category == "migration_doc"

    category, reason = _classify(
        "policies/rules.yaml", "acl_fact_conflict"
    )
    assert category is None
    assert "active code" in (reason or "")

    category, reason = _classify("unknown/thing.py", "acl_analysis")
    assert category is None
    assert "not covered" in (reason or "")


def test_scan_of_current_tree_passes() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "acl_scan.py"),
        ],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "acl_scan: PASS" in result.stdout
    # The one legitimate active-code hit is the versioned audit epoch.
    assert 'app/services/audit.py' in result.stdout
    assert 'AUDIT_SCHEMA_EPOCH = "fare-audit/v2-no-acl"' in result.stdout
