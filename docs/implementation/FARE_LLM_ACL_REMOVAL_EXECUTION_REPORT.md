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

---

## PHASE-04: Config / audit / cache compatibility / API release contract

**Commit:** `feat!: acl-free config, audit epoch, and API 0.3.0`（见 git log）

### Config cleanup

- `app/config.py`：`_AclConfig`/`_AclMockConfig`/`_AclHttpConfig`、`Settings`
  的全部 `acl_*` 字段与 `llm.features.acl_candidate_mode`、相关 validation 与
  from_parsed 映射删除；schema 保持 `extra='forbid'`。
- 6 个 config/fare*.yaml 样例删除 `acl:` 块与 `acl_candidate_mode`；全部
  profile 加载+fingerprint 验证通过。
- 新增 fail-closed 测试：残留 `acl:` / `acl_candidate_mode` 键的 YAML 启动即
  ValidationError（`test_removed_capability_config_keys_fail_closed`）。
- conftest/default-consistency/config-limits/yaml-config 套件同步收敛。

### API version 0.3.0（协调式 breaking release）

- `pyproject.toml` + FastAPI info version: **0.2.0 → 0.3.0**。
- **`/v2/evaluations`** 为唯一正式评估入口（response model 无任何已删字段）。
- **`/v1/evaluations`** 在 0.3.x 只返回 HTTP 410 + `API_VERSION_RETIRED`，
  不触达 evaluation runtime、不写审计、不占用 request id（contract test
  `test_v1_evaluations_retired_without_reaching_runtime`）。/v1 路由的最终
  删除留给后续协调发布。
- 全部现役调用方（测试 19 个文件）切换 `/v2`；OpenAPI paths 断言
  {healthz, readyz, /v1(410), /v2(200)}。

### Audit epoch & namespace（无破坏性迁移）

- 新 namespace：SQLite 文件名 `fare-audit.sqlite3` → **`fare-audit-v2.sqlite3`**；
  旧库永不被新 runtime 打开/改写，作为只读归档保留（运维可整目录归档）。
- 记录 schema epoch：**`fare-audit/v2-no-acl`**（`AUDIT_SCHEMA_EPOCH`）——
  每条新记录带 `schema_epoch`，`audit_metadata` 表声明 epoch。
- 旧 JSONL 归档：`_import_file` 按 epoch 跳过（不导入、不改写磁盘文件）；
  归档工具仍可按原始 JSON 读取（测试断言）。
- 缓存 fail-closed：completed 行的 epoch 不匹配时 `claim()` 抛
  `AuditSchemaMismatchError`，API 映射为 **409 `AUDIT_SCHEMA_MISMATCH`**；
  旧 response 永远不会被当作新 response 返回。
- 跨 epoch 同 request_id：新 namespace 独立，旧主键不会造成假冲突。
- 脱敏覆盖保持不变（api key/token/authorization/password 等）。

### Test results

| Command | Result |
|---|---|
| full `pytest -q` | **551 passed** |
| `ruff check . --no-cache` | **All checks passed** |
| `pytest evals/llm/test_contract_dataset.py tests/test_real_semantic_acceptance_scoring.py -q` | **5 passed** |

### Migration notes (operators)

- 升级到 0.3.0 时把客户端切换到 `/v2/evaluations`；`/v1` 将收到 410。
- 旧配置含 `acl:` 键时启动失败——删除该节即可（错误信息指出未知键）。
- 历史审计目录整体只读归档；新 runtime 使用 `fare-audit-v2.sqlite3`，
  不会读取或改写旧库；同 request_id 可直接重放，不受旧主键影响。

---

## PHASE-05: FARE LLM boundary split (behavior-identical)

**Commit:** `refactor: split fare llm contracts and adapters`（见 git log）

### New package `app/services/llm/`

| 模块 | 职责 | 所有权 |
|---|---|---|
| `ports.py` | Semantic / RequestFindings / Explanation 协议 | FARE |
| `contracts.py` | LLM DTO 导出面 + `LlmStrictModel`（extra=forbid, strict=True 配方） | FARE |
| `prompts.py` | prompt 文本与 PROMPT_VERSIONS（逐字节等价迁移） | FARE |
| `errors.py` | typed taxonomy：FareLlmError → ProviderFailure / TimeoutFailure / StructuredOutputFailure / DomainValidationFailure / SemanticPolicyFailure；业务面 `LlmDependencyError` 语义冻结 | FARE |
| `provider.py` | httpx 通道生命周期（trust_env=False、无自动重试、api_key 逐调用读取） | Provider boundary |
| `structured_runtime.py` | 有界结构校验/correction loop，单一总 deadline 覆盖全部 attempt | FARE |
| `adapter.py` | 三个用例端口实现 + 错误翻译 + guard 调用点 | FARE |
| `telemetry.py` | ContextVar 完成轨迹（per-adapter，无全局可变 hook） | FARE |
| `mock_adapter.py` | 离线确定性适配器（不经任何第三方 runtime） | Test adapter |

- `app/services/llm_client.py` 变为纯 re-export 兼容 facade；stage/evaluator/
  main 改为直接 `from app.services.llm import ...`。
- 错误消息不携带原始 provider 响应或 prompt 全文（gate 测试断言长度与字段）。
- `SemanticPolicyFailure` 仅用于启动/编程错误（文档化于 errors.py）。

### Invariants verified (zero behavior change)

- HTTP request body 逐字段一致（`test_llm_http_contract.py` 21 passed，含
  auth/参数/429/500/timeout/correction/total-deadline 契约）。
- prompt version、调用顺序、调用次数一致；realistic suite 零差异。
- mock adapter 不经第三方 runtime；guard 仍在 FARE stage/adapter 调用点。
- 无循环依赖；stage 只依赖 ports。

### Test results

| Command | Result |
|---|---|
| full `pytest -q` | **553 passed** (+2 PHASE-05 gate tests) |
| `ruff check . --no-cache` | **All checks passed** |

---

## PHASE-06: openai-python + Instructor migration (provider contract parity)

**Commit:** `feat: migrate llm transport to openai sdk and instructor`（见 git log）

### Dependency decision (re-verified on execution day 2026-09-05)

- PyPI 官方元数据：Instructor 最新 **1.16.0**（2026-08-27），依赖
  `openai>=2.0.0,<3.0.0`；openai-python 已发布 3.8.0。
- 因此采纳批准范围 **`instructor>=1.16,<1.17`** + **`openai>=2.0,<3.0`**；
  实际解析版本：**instructor 1.16.0 + openai 2.54.0**（写入 pyproject）。
- 未使用无界 latest；未引入 Agent framework/LangChain/LiteLLM 等。

### Implementation

- `llm/provider.py`：显式构造 `AsyncOpenAI`（base_url 仅来自 FARE 设置、
  **max_retries=0** 冻结传输行为、自管 httpx.AsyncClient `trust_env=False`、
  绝不读取环境凭据）+ `instructor.from_openai(..., mode=Mode.JSON)`
  （contract spike 验证的 JSON-compatible 模式，不假设 tools/JSON-Schema）。
- `llm/structured_runtime.py`：FARE 自管有界结构重试循环（instructor
  `max_retries=0`），`asyncio.timeout` 单一总 deadline 覆盖每次 attempt 的
  剩余预算；transport/API 错误（429/500/连接/超时）立即失败且不消耗结构
  重试预算——与冻结历史行为一致；错误映射到 typed taxonomy
  （ProviderFailure/TimeoutFailure/StructuredOutputFailure），业务异常
  消息与旧版逐字节相同。
- GLM/兼容参数：`do_sample`/`thinking` 经 `extra_body` 传递；
  `response_format={"type":"json_object"}`、`stream=False`、
  temperature/max_tokens/top_p/stop 为一等参数（contract 测试逐项冻结）。
- usage（prompt/completion/total）与 provider request id 进入 completion
  trace（规范三字段）；raw completion 不进 trace。
- instructor 内部 retry logger 静音（其原始错误文本可能回显 secrets）；
  FARE 的 typed error 与 trace 是唯一观测面。

### Approved SDK differences (parity audit result)

1. no-auth 场景发送固定占位 `Authorization: Bearer no-auth`（SDK 拒绝无凭据
   构造且不接受空串；显式占位保证绝不读取环境 key，poison-env 契约测试证明）。
2. system 消息的 JSON schema 指令由 Instructor 以其自有措辞注入
   （契约测试改为断言 schema 字段与指令存在，而非旧后缀原文）。
3. SDK 自有无害 header（`x-stainless-*`、`accept-encoding` 等）。
4. 结构失败时 assistant/user correction 消息与旧实现一致（保留）。

### Contract suite (expanded, 23 passed)

冻结项：URL 拼接、auth（有 key/无 key/env-poison）、model/temperature/
do_sample/stream/response_format/max_tokens/top_p/stop/thinking、extra_body、
client 复用与恰好关闭一次、429/500 只发一次、transport timeout 只发一次、
invalid JSON/缺字段/错 enum 的有界结构重试、单一总 deadline 共享、typed
错误映射、usage+request id 可审计且不泄漏原文、no-auth 不读环境、
proxy env 不改变行为、GLM profile 与通用 profile 分开验证。

### Test results

| Command | Result |
|---|---|
| full `pytest -q` | **555 passed**（+2 新契约测试） |
| `ruff check . --no-cache` | **All checks passed** |
| `pytest evals/llm/test_contract_dataset.py tests/test_real_semantic_acceptance_scoring.py -q` | **5 passed** |
| realistic suite（mock adapter，不经 SDK） | 零差异 |
| 实际版本 | instructor 1.16.0 / openai 2.54.0 |

---

## PHASE-07: Failure model, audit, and observability hardening

**Commit:** `test: harden llm failure and provider compatibility contracts`（见 git log）

### Audit event minimum fields (FARE-owned, per use-case call)

`stage_metrics.record_llm_stage` 每条 stage 事件新增：
`use_case`、`prompt_version`、`model`、`started_at`、`fare_error_type`
（typed taxonomy 类名：ProviderFailure / TimeoutFailure /
StructuredOutputFailure / DomainValidationFailure）、
`structure_retry_count`、`transport_retry_count`（冻结为 0）、
`token_usage`（prompt/completion/total 三规范字段）、
`provider_request_id`、explanation 事件的 `fallback_used`。
raw completion 与任何 secret 不进入事件（契约测试断言）。

### Failure matrix (tests/test_llm_failure_matrix.py, 17 tests)

按阶段路由的 mock 传输（system prompt 识别 semantic/findings/explanation），
冻结 3×失败类型矩阵：

| 失败类型 | Semantic | Request findings | Explanation |
|---|---|---|---|
| 429 / 500 / connect | ProviderFailure；全 item fail-close；explanation 跳过 | shadow rejected；决策不变 | ProviderFailure；模板回退 |
| transport/deadline timeout | **TimeoutFailure**（单元级测试：单一 deadline 被 asyncio.timeout 强制） | TimeoutFailure；决策不变 | 模板回退 |
| invalid JSON / schema violation | StructuredOutputFailure | StructuredOutputFailure | 模板回退 |
| guard 拒绝 | SemanticGuardError（FARE 域类） | **DomainValidationFailure**（adapter 接入 typed cause） | ExplanationGuardError（FARE 域类） |

另验证：成功事件的 usage/request-id 可审计且无 raw completion 泄漏、
transport_retry_count 恒 0、并发评估的 trace 不串扰。

### Guard classification wiring

- `adapter.analyze_request_findings` 的 guard 拒绝现在挂
  `DomainValidationFailure(guard_code, item_ids)` 作为 `__cause__`
  （公开消息不变）。
- `structured_runtime` 的 `raise ... from` 链修正为先构造 typed failure
  再挂 `__cause__`（避免被原始 SDK 异常覆盖）。

### Test results

| Command | Result |
|---|---|
| full `pytest -q` | **572 passed**（+17 failure matrix） |
| `ruff check . --no-cache` | **All checks passed** |
| realistic suite RN-023/RN-024（semantic 失败/explanation 越权） | 零漂移 |


---

## PHASE-08: Full-repo cleanup, docs, and final acceptance

**Commit:** `docs: finalize acl-free llm architecture`（见 git log）

### 现役文档更新

- `README.md`：新增 **0.3.0 迁移说明**（能力移除、/v2 入口、配置键删除、
  审计 epoch、SDK+Instructor）；正文移除全部已删能力描述；示例切到 `/v2`；
  profile 表与 dev 默认值同步；测试节改指 realistic 套件。
- `docs/CONFIGURATION.md`：第 6 节重写（删除 `acl:` 配置节文档，注明残留键
  fail-closed）。
- `docs/DECISION_MODEL.md`：FindingSource/优先级/确定性快照收敛为
  network|rule|semantic；删除已删 finding 表行与调用门控节。
- `docs/ARCHITECTURE.md`：主链顺序、模块表、post_decision 顺序同步。
- `docs/TESTING.md`：套件清单与阶段顺序同步（core 10 例 + realistic 35 例）。
- `docs/INTEGRATION.md`、`tests/README.md`：profile 表、外部依赖表述同步。
- `docs/history/*.md` 与 `docs/ACCEPTANCE-REPORT*.md`：文件头加入历史标注
  （能力已移除、不代表现状），未改动历史正文。

### 评测资产清洗

- `evals/llm/test_real_model.py`：移除已删证据源键。
- `evals/llm/datasets/provisional/cases.json`：删除 2 个已删能力用例
  （acl_candidate 类），3 个用例的证据源改写为 `request_description`（quote
  保持逐字可定位），新增 2 个等效合成用例维持 ≥20 门禁；id 重命名。
- `evals/llm/test_contract_dataset.py`：stage 清单移除已删阶段。

### 测试资产清理

- 删除未使用的 v1 套件 `tests/cases/evaluations/core.json`（27 处命中，无引用）。
- `tests/fixtures/policies/**` 与 `policies/compliance_rules.yaml` 头注去词化。
- guard 测试 fixture（semantic_guard / network_plan_llm_guard）证据源改写。

### ACL 清零扫描

结果：**现役生产代码、配置、策略、现状文档命中 0**。
剩余 61 行命中全部为已批准用途（负向门禁断言、迁移说明、新 epoch 常量），
逐类核验记录见 `docs/implementation/phase08-acl-scan-final.md`。
迁移取证工件（realistic 数据集、矩阵文档、执行报告、OpenAPI 基线/差异）按
方案允许保留已删字段名。

### OpenAPI breaking-change diff

见 `docs/implementation/openapi-breaking-change-diff.md`：
版本 0.2.0→0.3.0；`+POST /v2/evaluations`；`/v1` 收敛为 410；
`EvaluationItem` −`acl_verification_status`；`EvaluationResponse`
−`acl_analysis`/`acl_candidate_analysis`；`DecisionFinding.source` −`'acl'`；
`ReasonType` −`'acl_no_path'`；删除 11 个 schema 组件。
（注：本环境 fastapi 0.141 对带 wrap serializer 的模型在 OpenAPI 合成中生成
退化空 properties——0.2.0 基线即如此，与本次变更无关；字段级 diff 以
pydantic 模型 schema 为准。）

### 最终测试矩阵（执行日 2026-09-05）

| 组 | 命令 | 结果 |
|---|---|---|
| A | `ruff check . --no-cache` | All checks passed |
| A | `pytest -q`（全量） | **572 passed** |
| B | invariants + orchestration + reducer + findings_output | 32 passed |
| C | llm http contract + pipeline + failure matrix + guards + business effects + observability + request findings | 148 passed |
| D | realistic network suite（acl_free 契约） | 38 passed（35/35 场景） |
| E | 显式 eval（contract dataset + semantic scoring） | 5 passed |
| F | API/CLI/runner 一致性（api + area relations + final acceptance + architecture baseline） | 68 passed |
| G | lifecycle + limits + audit（并发/幂等/epoch 隔离） | 20 passed |

幂等重放不增加 network/LLM 调用、limit shortcut 不触发 LLM、OpenAPI 路径契约
由 `test_openapi_declares_v2_entry_and_retired_v1` 与 invariants 负向门禁固定。


---

## 最终状态（Definition of Done）

```text
engineering_complete: true
committed: true
tests_passed: true（全量 572 passed；realistic 35/35；显式 eval 5 passed；ruff 全绿）
acl_active_references: 0（现役代码/配置/策略/现状文档；61 行剩余命中均为已批准的
                        负向门禁/迁移说明/新 epoch 常量，见 phase08-acl-scan-final.md）
realistic_cases_passed: 35/35
provider_parity_passed: true（23 项 provider contract 全绿；差异仅 3 类已审核
                        SDK 无害差异 + 契约测试按批准差异更新）
push_authorized: false（除非用户另行授权）
pr_created: false
merge_authorized: false
```

### 提交清单（9 个阶段提交，各自可独立 `git revert`）

| 阶段 | SHA | 标题 |
|---|---|---|
| PHASE-00 | `4ec2bda` | test: freeze acl-removal migration baseline |
| PHASE-01 | `8ec5f74` | test: add realistic network request scenarios |
| PHASE-02 | `3abc941` | refactor: introduce acl-free evaluation context |
| PHASE-03 | `933faf8` | feat!: remove acl assessment capability（breaking） |
| PHASE-04 | `17704d2` | feat!: acl-free config, audit epoch, and API 0.3.0（breaking） |
| PHASE-05 | `ea978b9` | refactor: split fare llm contracts and adapters |
| PHASE-06 | `5d20f7e` | feat: migrate llm transport to openai sdk and instructor |
| PHASE-07 | `55d9002` | test: harden llm failure and provider compatibility contracts |
| PHASE-08 | 本提交 | docs: finalize acl-free llm architecture |

基线 `45805c6` → 本分支 HEAD：**206 个文件变更（41 新增 / 23 删除 / 141 修改 /
1 重命名），+18100 / −6944**。完整清单 `git diff --name-status 45805c6..HEAD`。

### 实际依赖版本

- instructor **1.16.0**（范围 `>=1.16,<1.17`）
- openai **2.54.0**（范围 `>=2.0,<3.0`）
- 执行日（2026-09-05）PyPI 核对：Instructor 最新 1.16.0 依赖 `openai>=2.0,<3`；
  openai-python 3.x 已发布故不可无界安装——与方案预判一致。

### 回退步骤（阶段级 git revert，逆序）

```text
git revert <PHASE-08-SHA>   # PHASE-08（文档/评测资产；可独立回退）
git revert 55d9002   # PHASE-07（观测性增强；若错误映射改动改变行为须连同测试）
git revert 5d20f7e   # PHASE-06（回退到 PHASE-5 手写 adapter；不得恢复 ACL）
git revert ea978b9   # PHASE-05（回退模块拆分）
git revert 17704d2   # PHASE-04（回退 API 0.3.0/审计 epoch；审计 namespace 回退
                     #  = 重新指向旧库；历史归档不可删除）
git revert 933faf8   # PHASE-03（整体回退 ACL 删除；不得只恢复响应字段）
git revert 3abc941   # PHASE-02（回退中立 context）
git revert 8ec5f74   # PHASE-01（仅新增测试资产，可独立回退）
git revert 4ec2bda   # PHASE-00（仅执行报告与留档）
```

注意：PHASE-04 的审计 namespace 回退与代码回退独立——回退应用后把
`audit.directory` 指回旧目录即可；`fare-audit-v2.sqlite3` 与历史归档都保留。

### 未关闭风险

1. **API breaking change**：旧调用方在 0.3.x 收到 410；`/v1` 路由最终删除
   留给后续协调发布（方案明确不属于本任务）。
2. **配置迁移**：含 `acl:` 键的存量配置启动即失败，需运维手工删除配置节
   （错误信息指向未知键）。
3. **审计 epoch**：跨 epoch 重放返回 409 `AUDIT_SCHEMA_MISMATCH`；需运维在
   升级时归档旧审计目录（新 runtime 已物理隔离旧库文件，不强制，但推荐）。
4. **no-auth 占位头**：无凭据 profile 会发送 `Authorization: Bearer no-auth`
   （SDK 拒绝无凭据构造且不接受空串）；对忽略认证头的 provider 无影响，已作为
   批准的 SDK 差异记录并有 poison-env 契约测试保护（绝不读取环境凭据）。
5. **fastapi 0.141 OpenAPI 合成怪癖**：带 wrap serializer 的模型在
   /openapi.json 中生成退化空 properties（0.2.0 基线即如此，与本次变更无关）；
   字段级契约以 pydantic 模型 schema 与响应体契约测试为准。
6. **instructor 内部模块路径**（`instructor.v2.core.errors`）为第三方私有
   结构——运行时未依赖其类型（按 `__cause__` 分类），仅测试 spike 引用过；
   升级 instructor 时需重跑 provider contract 套件。
