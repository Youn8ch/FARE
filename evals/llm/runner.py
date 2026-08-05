from __future__ import annotations

import json
from pathlib import Path

from evals.llm.case_schema import load_manifest, load_suite
from evals.llm.scoring import score_contract

ROOT = Path(__file__).resolve().parent


def run_offline() -> dict:
    manifest = load_manifest(ROOT / "datasets" / "manifest.json")
    suite = load_suite(ROOT / "datasets" / "provisional" / "cases.json")
    return score_contract(suite, manifest)


if __name__ == "__main__":
    print(json.dumps(run_offline(), ensure_ascii=False, sort_keys=True))

