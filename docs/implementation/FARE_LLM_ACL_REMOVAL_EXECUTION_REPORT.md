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

---

## PHASE-03: ACL capability fully removed (breaking change)

**Commit:** `feat!: remove acl assessment capability`（见 git log）

### Deleted production files

- `app/services/acl_client.py`（Mock/Http ACL 客户端）
- `app/services/acl_extract.py`（确定性 ACL 抽取器）
- `app/services/acl_candidate_merge.py`（LLM candidate merge）
- `app/services/stages/acl_stage.py`（ACL stage）
- `app/services/response_assembler.py`（纯 ACL 的 aggregate_analyses）

### Deleted test assets

- `tests/test_acl_deterministic_short_circuit.py`、`tests/test_acl_llm_gating.py`、
  `tests/test_llm_guard.py`（整文件为 ACL candidate guard）
- `tests/test_catalog_and_acl.py` → 非 ACL 用例迁移至新 `tests/test_catalog.py`
- `tests/fixtures/acl/**`、`tests/fixtures/llm/acl_candidates/**`、
  `tests/cases/evaluations/acl_candidates.v2.json`
- core.v2.json 的 4 个 ACL 用例（14→10）

### Production changes

- **主链收敛**：`plan → network → rules → semantic → reduce → post_decision → assemble`。
- `decision_reducer.py`：`FindingSource = network|rule|semantic`；`PRIORITY_ACL`、
  `ItemFindingSet.acl` 删除；确定性快照 = network+rules+catalog。
- `finding_factory.py`：`acl_findings()`、`ACL_ERROR_TEXT`、`acl_no_path_rule`
  参数删除；`finding_text(primary, matched)` 两参。
- `rule_loader.py`：ACL-PATH-001 强制校验、`acl_no_path_rule` 属性、match 特殊
  排除、`explicit_no_path` 条件、`acl_no_path` reason type 全部删除（旧策略包
  因 unsupported condition 自然加载失败）。
- `schemas.py`：`AclAnalysis`/`AclCandidateAnalysis`/`LlmAclExtraction*`/
  `ExtractedFacts`/`AclRawResponse` DTO 删除；`EvaluationItem.acl_verification_status`、
  `EvaluationResponse.acl_analysis/acl_candidate_analysis` 删除；
  `DecisionFinding.source` 收敛为 network|rule|semantic；`acl_no_path` reason
  type 删除；evidence source 联合类型删除 acl_analysis/acl_config。
- `item_assembler.py`：`acl_verification_status` 与 ACL 文案参数删除。
- `llm_client.py`：`extract_acl_facts`、`LlmAclCandidateClientProtocol`、
  `guard_acl_candidates`、ACL prompt/fixture loader 删除；semantic/request
  findings prompt 重写——防火墙路径与访问控制状态明确列为不可推导事实。
- `post_decision_stage.py`：ACL candidate shadow 删除；顺序收敛为
  request findings → explanation。
- `semantic_stage.py`：payload/evidence 的 acl_analysis/acl_config 通道删除。
- `audit.py`：`persist()` 不再接收/写入 `acl_raw`；新审计记录无 ACL 数据。
- `main.py`：ACL 客户端构造/生命周期/aclose 通道删除；`__main__.py` CLI 状态
  输出删除 ACL mode。
- 策略包：`policies/compliance_rules.yaml` 与 22 个 fixture 策略包删除
  ACL-PATH-001；根策略版本 2026.08.0 → **2026.09.0**（manifest + catalog 同步）。
- 修正 PHASE-02 遗留：main.py 中 redaction 调用曾被误缩进 except 块（本阶段
  修复，live 响应脱敏恢复与基线一致）。

### Test changes

- realistic 套件 `ACTIVE_CONTRACT` 翻转为 **`acl_free`**：`target_expected`
  成为唯一门禁；RN-029..035 的批准差异全部按阶段 1 清单落地（4 例待定→合规、
  1 例 advisory 无决策漂移、shadow 能力整体删除）；legacy_expected 保留为
  只读取证数据，运行时不再断言。
- realistic schema/dataset 删除 acl_fixture/acl_decision_mode/acl_candidate_*
  字段；`tests/fixtures/acl/realistic/` 与 realistic acl_candidates fixture 删除。
- v2 case schema/数据删除 ACL 面；`RecordingLlmClient` 删除 ACL surface。
- `test_v4_invariants.py` 新增负向门禁：OpenAPI+response 无验证字段、finding
  factory 无 legacy code、semantic payload/prompt versions/stage metrics 无
  已删 stage；静态检查 ACL stage/客户端/DTO 全不存在。
- characterization / main-chain / architecture-baseline / orchestration 套件
  按 ACL-free 行为更新（stage 顺序、metrics 键序、exceptions 序、V3-21/22/28/32/33）。
- area_relation expected 数据集（29 个文件）与 rule-packages 数据集删除
  acl_status/acl_call_count 字段。

### Test results

| Command | Result |
|---|---|
| full `pytest -q` | **552 passed**（基线 577 中删除 68 个 ACL 专用断言 + 新增/改写 43 个） |
| `ruff check . --no-cache` | **All checks passed** |
| `pytest evals/llm/test_contract_dataset.py tests/test_real_semantic_acceptance_scoring.py -q` | **5 passed** |
| realistic suite (acl_free contract) | **38 passed**（35 场景 + 3 元测试） |
| app/ ACL 引用扫描（不含 config.py，PHASE-04 清理） | **0** |

### Approved differences realized (per PHASE-01 list)

- RN-030/031/032/033/034：`待定` → `合规`（无其他 finding 时）。
- RN-029：决策不变，仅审计/验证表面消失。
- RN-035：ACL candidate shadow LLM 调用 1→0，`acl_candidate_analysis` 字段消失。
- 无清单之外的行为漂移（realistic suite 全绿即为门禁）。

### Notes

- `app/config.py` 的 ACL 配置 schema 按方案留待 PHASE-04 删除（运行时已不消费）。
- `explanation_guard.py` 的越权断言模式改写为等价的
  `live/production firewall|access control ...` 形式，保护语义不变。
