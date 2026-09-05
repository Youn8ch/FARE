"""Precise ACL-removal scan with a machine-executable allowlist.

The naive ``grep -i acl`` is unusable: ``dataclass``, ``aclose``, and
``metadata``-style substrings pollute the result. This scanner uses a word
precise pattern, classifies every hit into exactly one category, and fails on
anything the allowlist does not cover:

- ``scan_tooling``    — the scanner, its allowlist, and its precision test;
                        they quote ACL identifiers to detect them.
- ``legacy_dataset``  — frozen request datasets comparing legacy/target
                        contract behavior (tests/cases, evals datasets).
- ``negative_test``   — tests/evals asserting the removed names are absent.
- ``migration_doc``   — docs/README explaining the removal and history.
- ``active_code``     — app/, config/, policies/, pyproject.toml,
                        constraints.txt, scripts/: every hit must match one
                        of the allowed patterns (the versioned audit epoch
                        identifiers). Anything else is a violation.

Usage (from the repository root)::

    python scripts/acl_scan.py [--json]

Exit code 0 means: no ACL capability, config key, schema component, finding
source, rule, or runtime import outside the allowlisted categories.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_PATH = (
    PROJECT_ROOT / "docs/implementation/acl-scan-allowlist.json"
)

SCAN_EXTENSIONS = {
    ".py",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".txt",
}


def load_allowlist() -> dict:
    return json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))


def acl_pattern(allowlist: dict) -> re.Pattern[str]:
    """The compiled word-precise ACL pattern from the allowlist."""

    return re.compile(allowlist["pattern"])


def iter_scanned_files(roots: list[str]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        root_path = PROJECT_ROOT / root
        if root_path.is_file():
            files.append(root_path)
            continue
        for path in sorted(root_path.rglob("*")):
            if path.is_file() and path.suffix in SCAN_EXTENSIONS:
                files.append(path)
    return files


def match_path(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def classify(path: str, line: str, allowlist: dict) -> tuple[str | None, str | None]:
    """Return ``(category, None)`` when the hit is allowlisted, or
    ``(None, reason)`` when it is a violation."""

    tooling = allowlist["scan_tooling"]
    if match_path(path, tooling["paths"]):
        return "scan_tooling", None

    for category in allowlist["categories"]:
        if match_path(path, category["paths"]):
            return category["id"], None

    if match_path(path, allowlist["active_code"]["paths"]):
        for pattern in allowlist["active_code"]["allowed_line_patterns"]:
            if re.search(pattern, line):
                return "active_code", None
        return None, (
            "ACL reference in active code outside the audit-epoch allowlist"
        )
    return None, "path is not covered by any allowlist category"


def scan(allowlist: dict) -> tuple[list[dict], list[dict]]:
    pattern = acl_pattern(allowlist)
    hits: list[dict] = []
    violations: list[dict] = []
    for path in iter_scanned_files(allowlist["scan_roots"]):
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if not pattern.search(line):
                continue
            category, reason = classify(relative, line, allowlist)
            record = {"path": relative, "line": number, "text": line.strip()}
            if category is None:
                violations.append({**record, "reason": reason})
            else:
                hits.append({**record, "category": category})
    return hits, violations


def check_allowed_patterns_are_live(allowlist: dict, hits: list[dict]) -> list[str]:
    """Every allowed active-code pattern must still match at least one line,
    so the allowlist cannot silently outlive the code it describes."""

    problems = []
    matched = {record["text"] for record in hits if record["category"] == "active_code"}
    for pattern in allowlist["active_code"]["allowed_line_patterns"]:
        if not any(re.search(pattern, text) for text in matched):
            problems.append(f"allowed pattern matches nothing: {pattern!r}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON output")
    args = parser.parse_args()

    allowlist = load_allowlist()
    hits, violations = scan(allowlist)
    stale = check_allowed_patterns_are_live(allowlist, hits)

    counts: dict[str, int] = {}
    for record in hits:
        counts[record["category"]] = counts.get(record["category"], 0) + 1

    if args.json:
        print(
            json.dumps(
                {
                    "counts": counts,
                    "hits": hits,
                    "violations": violations,
                    "stale_allowlist": stale,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print("ACL precise scan (allowlist: docs/implementation/acl-scan-allowlist.json)")
        for category in sorted(counts):
            print(f"  {category}: {counts[category]} allowlisted hit(s)")
        for record in hits:
            if record["category"] == "active_code":
                print(f"  active_code: {record['path']}:{record['line']}: {record['text']}")
        for record in violations:
            print(
                f"  VIOLATION: {record['path']}:{record['line']}: "
                f"{record['text']} ({record['reason']})"
            )
        for problem in stale:
            print(f"  STALE ALLOWLIST: {problem}")

    if violations or stale:
        print("acl_scan: FAIL")
        return 1
    print("acl_scan: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
