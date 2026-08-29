"""V4 invariant static checks (grow with each phase).

Each check pins one architectural invariant from the V4 plan §4 so that a
regression fails fast instead of drifting silently.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = PROJECT_ROOT / "app"


def _read(relative: str) -> str:
    return (APP_ROOT / relative).read_text(encoding="utf-8")


def test_rule_match_is_called_exactly_once_per_item_path() -> None:
    """Invariant 4.2.1/4.2.2: the formal PolicyBundle.match() call lives only
    in the rule stage; ACL gating consumes RuleStageResult."""

    evaluator = _read("services/evaluator.py")
    assert evaluator.count("self.policies.match(") == 1


def test_stage_contracts_are_frozen_dataclasses() -> None:
    """Invariant: stage boundaries pass frozen dataclasses, not bare dicts."""

    types = _read("services/evaluation_types.py")
    assert "@dataclass(frozen=True, slots=True)" in types
    assert "class RuleStageResult" in types
    assert "class AclStageResult" in types
