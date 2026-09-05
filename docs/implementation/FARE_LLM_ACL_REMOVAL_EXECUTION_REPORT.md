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

---

## PHASE-01: Realistic network request inputs designed and frozen first

**Commit:** `test: add realistic network request scenarios`（见 git log）

### What was added (test assets only; zero production behavior change)

- `tests/cases/evaluations/realistic_network_requests.v1.json` — **35 frozen cases**
  (RN-001..RN-024 core matrix + RN-025/026/027/028 failure/limit extensions +
  RN-029..RN-035 ACL migration evidence cases), each with `legacy_expected`,
  `target_expected`, `approved_differences[]`, `invariants[]`.
- `tests/case_schema.py` — realistic.v1 model family appended (v2 models untouched).
- `tests/helpers/case_loader.py` — `load_realistic_suite` / `prepare_realistic_llm_fixtures`
  / `realistic_item_ids` (reuses v2 helpers; no second evaluation runner — the
  suite drives the production runtime through the HTTP API).
- `tests/test_realistic_network_requests.py` — runner with `ACTIVE_CONTRACT`
  constant (`legacy` now; flips to `acl_free` in PHASE-03).
- `tests/fixtures/network_plan/realistic/*.v1.json` — 9 mock profiles (7 zones +
  5 failure shapes). **Documented deviation from the plan's /26 topology:** the
  frozen transport queries at /24 granularity, so zone→/24 binding lives per
  profile; rationale recorded in `docs/testing/REALISTIC_NETWORK_REQUEST_MATRIX.md` §2.
- `tests/fixtures/policies/realistic_network/` — manifest 2026.09.0 + rules
  (incl. loader-mandated ACL-PATH-001 placeholder, removed in PHASE-03) + catalog.
- `tests/fixtures/acl/realistic/*.v1.json` — 6 ACL mock fixtures (legacy evidence only).
- `tests/fixtures/llm/realistic_network/{semantic,request_findings,explanation,acl_candidates}/` — 10 LLM fixtures.
- `docs/testing/REALISTIC_NETWORK_REQUEST_MATRIX.md` — full matrix + data-safety rules.

### Test results

| Command | Result |
|---|---|
| `pytest tests/test_realistic_network_requests.py -q` | **38 passed** (35 case runs + 3 metadata/safety tests) |
| `pytest tests/test_evaluation_cases.py tests/test_v4_main_chain_acceptance.py -q` | **68 passed** |
| full `pytest -q` | **615 passed** (577 baseline + 38), same deprecation warning |
| `ruff check . --no-cache` | **All checks passed** |

### Phase gate

- [x] ≥24 scenarios in dataset (35)
- [x] Every ACL-related historical scenario has explicit `approved_differences` (RN-029..035; enforced by test)
- [x] `legacy_expected` evidence ran green against the pre-removal runtime
- [x] Target assertions use no real ACL / real network; conftest blocks non-loopback sockets
- [x] All mock catalog addresses are RFC 5737 documentation addresses (enforced by test)
- [x] Case loader is the only data-driven entry; no duplicated evaluation logic

### Notes / deviations

- /26 zone topology → per-profile zone binding (frozen /24 query contract); documented.
- RN-030 legacy evidence captured the real frozen behavior: required-mode ACL
  dependency failure emits TWO findings (dependency + unresolved firewall).

---

## PHASE-02: ACL-free neutral item context (ACL still present)

**Commit:** `refactor: introduce acl-free evaluation context`（见 git log）

### Changes

- `app/services/evaluation_types.py` — added frozen `EvaluationItemContext`
  (`item_id`, `combination`, `rule_result`); carries NO ACL-named field.
- `app/services/stages/acl_stage.py` — added `AclCompatOutcome` adapter +
  `compat_outcomes()`: the only channel ACL material takes toward the
  downstream stages (dies with the ACL stage in PHASE-03).
- `app/services/stages/semantic_stage.py` — consumes `contexts` +
  generic `extra_evidence` mapping (the ACL evidence texts ride this
  explicit adapter parameter until removal); no `AclRecord` import.
- `app/services/stages/reduce_stage.py` — consumes `contexts` +
  `acl_outcomes` compat channel; no `AclRecord` import.
- `app/services/stages/post_decision_stage.py` — consumes `contexts` +
  `acl_outcomes` + `extra_evidence`; no `AclRecord` import.
- `app/services/evaluator.py` — builds contexts from rule results; wires the
  adapter outputs; stage order unchanged (plan→network→rules→acl→semantic→
  reduce→post_decision→assemble).
- `tests/test_evaluator_orchestration.py` — added two PHASE-02 gate tests
  (stage modules no longer reference `AclRecord`; neutral context has no
  ACL-named field).

### Invariants verified

- `PolicyBundle.match()` once per item; `DecisionReducer.reduce_item()` once
  per item (existing orchestration tests green, unchanged).
- post_decision order unchanged (acl candidates → request findings → explanation).
- Business responses byte-equivalent: full suite incl. characterization,
  invariants, main-chain acceptance, and the PHASE-01 realistic suite's
  `legacy_expected` evidence all pass unchanged.

### Test results

| Command | Result |
|---|---|
| full `pytest -q` | **617 passed** (615 + 2 new gate tests) |
| `ruff check . --no-cache` | **All checks passed** |
| `rg -n 'AclRecord' app/services/stages/{semantic,reduce,post_decision}_stage.py` | **no matches** |
