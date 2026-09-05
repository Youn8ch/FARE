# FARE LLM Infrastructure Refactor & ACL Removal — Execution Report

> Status: IN PROGRESS
> Executor: zcode
> Plan: `docs/FARE_LLM_INFRASTRUCTURE_AND_ACL_REMOVAL_IMPLEMENTATION_PLAN_V1.0.md`
> Authorization: local implementation + local commits only. Push / PR / merge NOT authorized.

## PHASE-00: Execution baseline and change guardrails

- Baseline branch: `codex/fare-main-chain-v4`
- Baseline HEAD: `45805c69f33cde682ae40953955ee83b084a1bc4`
- Implementation branch: `codex/llm-infrastructure-acl-removal-v1` (created from baseline HEAD)
- Working tree before start: clean except the plan document itself (untracked, user file, preserved as-is).

### Environment

- Python: 3.13.7 (`.venv`, satisfies `>=3.13.7,<3.14`)
- ruff: 0.16.4, pytest: 8.4.2, pydantic: 2.13.4, fastapi: 0.141.1, httpx: 0.28.1
- `instructor`: NOT installed (as expected)
- `openai`: NOT installed (as expected)

### Baseline test results (re-run, not quoted from plan)

| Command | Result |
|---|---|
| `pytest -q -p no:cacheprovider` | **577 passed**, 1 warning (Starlette/httpx TestClient deprecation), 33.05s |
| `ruff check . --no-cache` | **All checks passed** |
| `pytest evals/llm/test_contract_dataset.py tests/test_real_semantic_acceptance_scoring.py -q` | **5 passed**, same warning |

### Current stage order (observed from `app/services/evaluator.py`)

```
plan -> network -> rules -> acl -> semantic -> reduce -> post_decision -> assemble
```

post_decision (serial, D4): explanation -> ACL candidate shadow -> request findings
(return order in code: `items, acl_candidate_analysis, request_findings`).

### Current LLM use-case surface (4 types; target keeps 3)

| Use case | Stage | Current mode flag | Target |
|---|---|---|---|
| Semantic analysis | semantic | always-on via policies | KEEP |
| ACL candidate shadow | post_decision | `llm.features.acl_candidate_mode` | REMOVE |
| Request findings | post_decision | `llm.features.request_findings_mode` | KEEP |
| Explanation | post_decision | always-on when semantic succeeded | KEEP |

### ACL impact surface snapshot (`rg -l '(?i)acl' ...`)

148 files matched. Full list archived in `docs/implementation/phase00-acl-impact-files.txt`.

Categories:
- Production code: 25 files under `app/` (incl. `acl_client.py`, `acl_extract.py`, `acl_candidate_merge.py`, `stages/acl_stage.py`)
- Config: 6 files under `config/`
- Policies: `policies/compliance_rules.yaml` + 20 fixture policy packages
- Tests: 40+ files incl. dedicated ACL suites (`test_acl_deterministic_short_circuit.py`, `test_acl_llm_gating.py`, `test_llm_acl_candidates.py`, `test_catalog_and_acl.py`)
- Fixtures/cases: `tests/fixtures/acl/*` (3), `tests/fixtures/llm/acl_candidates/*`, `tests/cases/evaluations/acl_candidates.v2.json`
- Docs: current docs + `docs/history/` (historical, read-only archive)

### OpenAPI baseline

- Saved to `docs/implementation/openapi-baseline-v0.2.0.json`
- Paths: `/healthz`, `/readyz`, `/v1/evaluations`
- App version: 0.2.0

### PHASE-00 gate

- [x] User files identified and untouched (only untracked file = plan doc itself)
- [x] Baseline tests reproduced (577 passed / ruff clean / eval 5 passed)
- [x] OpenAPI, ACL impact list archived
