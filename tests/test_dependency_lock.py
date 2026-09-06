"""Dependency-lock regression tests for constraints.txt / constraints-ci.txt."""

from __future__ import annotations

import sys
from pathlib import Path

from packaging.markers import Marker, default_environment
from packaging.requirements import Requirement

REPO_ROOT = Path(__file__).resolve().parents[1]
CONSTRAINTS = REPO_ROOT / "constraints.txt"
CONSTRAINTS_CI = REPO_ROOT / "constraints-ci.txt"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _constraint_lines() -> list[str]:
    return [
        line.strip()
        for line in CONSTRAINTS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_uvloop_is_pinned_with_posix_only_marker() -> None:
    uvloop_lines = [
        line for line in _constraint_lines() if line.split(";")[0].startswith("uvloop")
    ]
    assert len(uvloop_lines) == 1, uvloop_lines
    requirement = Requirement(uvloop_lines[0])
    assert requirement.name == "uvloop"
    assert requirement.specifier == "==0.22.1"
    assert requirement.marker is not None
    assert Marker('sys_platform != "win32"') == requirement.marker


def test_uvloop_marker_tracks_current_platform_installability() -> None:
    marker = Marker('sys_platform != "win32"')
    expected = sys.platform != "win32"
    assert marker.evaluate(default_environment()) is expected


def test_existing_production_pins_are_unchanged() -> None:
    pins = {
        Requirement(line).name.lower(): str(Requirement(line).specifier)
        for line in _constraint_lines()
    }
    assert pins["openai"] == "==2.54.0"
    assert pins["instructor"] == "==1.16.0"
    assert pins["uvicorn"] == "==0.52.4"


def test_build_backend_hatchling_pin_is_unchanged() -> None:
    text = PYPROJECT.read_text(encoding="utf-8")
    assert 'requires = ["hatchling==1.32.0"]' in text


def test_constraints_ci_inherits_production_lock() -> None:
    content = [
        line.strip()
        for line in CONSTRAINTS_CI.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert content[0] == "-c constraints.txt"
