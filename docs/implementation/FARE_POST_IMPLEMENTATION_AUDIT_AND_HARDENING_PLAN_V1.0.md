# FARE ACL-free / LLM 重构实施后独立审计与发布前硬化方案 V1.0

> 状态：待交付 zcode 新会话执行  
> 制定日期：2026-09-06  
> 审计对象分支：`codex/llm-infrastructure-acl-removal-v1`  
> 审计对象 HEAD：`c8adb6850468b890832f7637e0bc95f9ebf9efcb`  
> 基线祖先：`45805c69f33cde682ae40953955ee83b084a1bc4`  
> 推荐硬化分支：`codex/llm-infrastructure-acl-removal-v1-hardening`  
> 远程权限：不包含 Push、PR、Merge  

---

## 0. 下一步结论

当前实施不应直接 Push 或创建 PR。下一步应在新会话中完成一次“先独立审计、再进行限定范围硬化、最后重新验收”的任务。

已在 2026-09-06 做过第一轮独立复验：

- 当前分支和 HEAD 与交付报告一致。
- 9 个 PHASE 提交连续继承自 `45805c6`。
- 工作树在复验前后均干净。
- `schemas_baseline_tmp.py` 不在现役树，也未在当前可见分支历史中发现。
- 全量测试：`572 passed`，1 个既有 Starlette/httpx deprecation warning。
- ruff：All checks passed。
- ACL 生产能力已经移除；宽泛扫描会被 `dataclass`、`aclose` 等子串污染，必须使用精确扫描和允许清单。

但存在三个发布前必须闭环的问题，以及两个需要明确收口的治理问题：

| ID | 等级 | 问题 | 当前证据 | 目标 |
|---|---:|---|---|---|
| H-01 | P1 | no-auth 配置实际发送 `Authorization: Bearer no-auth` | `app/services/llm/provider.py` 使用占位 key；测试把该差异直接标为 approved | no-auth 请求完全不含 Authorization，同时不读取环境 key |
| H-02 | P1 | `/v2` OpenAPI 响应 schema 退化为空 properties | runtime OpenAPI 中 `EvaluationResponse`、`EvaluationItem` 均为 0 properties | OpenAPI 正确公开全部目标响应字段和 required 信息 |
| H-03 | P1 | 构建/依赖结果不可复现且当前 editable 元数据陈旧 | `pip show fare` 仍显示 0.2.0，Requires 无 Instructor/OpenAI；仓库无 lock/constraints | 干净环境安装得到 fare 0.3.0、完整依赖和可重复版本集合 |
| H-04 | P2 | ACL 清零报告口径容易误读 | 宽泛模式产生 496 行；报告依赖人工排除子串和迁移资产 | 使用精确正则、目录分类和机器可执行 allowlist |
| H-05 | P2 | 旧 `app.services.llm_client` 兼容 facade 仍被 eval/tests 使用，且无退场版本 | 生产主链已使用新包，但现役测试/real-model eval 仍 import facade | 新代码全部使用新路径；明确 facade 删除版本或本次直接删除 |

本任务不得改写原 9 个阶段提交。所有修复必须以新提交追加到新的 hardening 分支。

---

## 1. 执行分支与权限边界

### 1.1 新会话启动位置

新会话先选择：

```text
codex/llm-infrastructure-acl-removal-v1
```

并验证：

```text
HEAD == c8adb6850468b890832f7637e0bc95f9ebf9efcb
git status --short == empty，或只包含本文档这一项未跟踪文件
```

完成只读审计 checkpoint 后，创建：

```text
codex/llm-infrastructure-acl-removal-v1-hardening
```

所有硬化提交只进入该分支。

本文制定时尚未提交，因此同一工作目录中预期可能看到：

```text
?? docs/implementation/FARE_POST_IMPLEMENTATION_AUDIT_AND_HARDENING_PLAN_V1.0.md
```

这是唯一允许的启动时差异。创建 hardening 分支后，将本文作为该分支的第一份
计划工件提交；如果出现任何其他修改，必须先停止并确认归属。

### 1.2 授权范围

允许：

- 读取和审查原 9 个提交。
- 运行离线测试、mock transport、schema/build/install 验证。
- 修复 H-01～H-05。
- 新增或修改相应测试、文档和本地执行报告。
- 在 hardening 分支形成阶段性本地提交。

不允许：

- Push、创建 PR、Merge。
- amend、rebase 或 force-update 原 9 个提交。
- 恢复任何 ACL 能力或旧 ACL 响应字段。
- 访问真实 LLM、真实内网、真实网段规划或真实凭据。
- 删除历史审计归档。
- 实现未来 Verification pipeline。
- 处理与 H-01～H-05 无关的大规模重构。

---

<!-- CHECKPOINT-A-START -->
## 2. Checkpoint A：新会话独立只读审计

**位置：创建 hardening 分支和修改文件之前。**

### 2.1 仓库和提交证据

执行并记录：

```powershell
git branch --show-current
git rev-parse HEAD
git status --short
git merge-base --is-ancestor 45805c69f33cde682ae40953955ee83b084a1bc4 HEAD
git log --oneline --decorate -12
git diff --stat 45805c69f33cde682ae40953955ee83b084a1bc4..HEAD
git log --all --oneline -- schemas_baseline_tmp.py
```

检查：

- [ ] 9 个提交顺序与执行报告一致。
- [ ] `schemas_baseline_tmp.py` 不存在于现役树和目标分支历史。
- [ ] PHASE-08 使用“本提交”而非自引用 SHA，报告不再因 amend 自相矛盾。
- [ ] 除本文档这一项预期未跟踪文件外，没有不属于任务的工作树修改；用户文件没有被改动或删除。
- [ ] “每阶段可独立 revert”的表述是否真实；若阶段存在依赖，应改成“阶段提交可识别，按逆序 revert”，不得宣称任意单独回退都可运行。

### 2.2 架构不变量审计

逐项阅读而非只信测试名称：

- `app/services/evaluator.py`
- `app/services/stages/semantic_stage.py`
- `app/services/stages/reduce_stage.py`
- `app/services/stages/post_decision_stage.py`
- `app/services/decision_reducer.py`
- `app/services/llm/`
- `app/services/audit.py`
- `app/main.py`
- `app/schemas.py`
- `app/config.py`

确认：

- [ ] `DecisionReducer.reduce_item()` 仍是 item 决策唯一正式入口。
- [ ] request-level 聚合只聚合 item decision，不成为第二套裁决引擎。
- [ ] LLM 不能生成 canonical facts、修改规则或提升确定性待定。
- [ ] semantic guard 和 explanation guard 均在 Instructor 之后、业务效果之前。
- [ ] request findings 不改变 locked decision。
- [ ] SDK 与 Instructor retry 均为 0，FARE correction loop 有界并共享总 deadline。
- [ ] provider-specific 参数只存在 provider/adapter 边界。
- [ ] AuditStore 仍是权威审计；telemetry 不替代 persist。
- [ ] `/v1` 410 不 claim request id、不触发 network/LLM runtime。
- [ ] 新 audit namespace 不导入或改写旧 epoch 数据。

### 2.3 测试复验

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m ruff check . --no-cache
.\.venv\Scripts\python.exe -m pytest evals\llm\test_contract_dataset.py tests\test_real_semantic_acceptance_scoring.py -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m pytest tests\test_realistic_network_requests.py -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m pytest tests\test_llm_http_contract.py tests\test_llm_failure_matrix.py -q -p no:cacheprovider
git status --short
```

### 2.4 审计 checkpoint 输出

修改前先在会话中输出：

```text
audit_baseline_verified: true|false
target_head: <full SHA>
worktree_clean: true|false
full_tests: N passed / N failed
known_findings_confirmed: H-01,H-02,...
additional_findings: [] | [finding ids]
safe_to_create_hardening_branch: true|false
```

如果出现额外 P0/P1、基线不一致、测试失败或工作树不明修改，停止并报告；不要进入修复。
<!-- CHECKPOINT-A-END -->

---

<!-- HARDENING-01-START -->
## 3. Hardening 01：恢复真正的 no-auth HTTP 契约

### 3.1 问题

当前 `ProviderChannel` 用 `api_key="no-auth"` 构造 SDK。当业务配置没有 key 时，`auth_headers()` 返回空 dict，导致 SDK 构造时的占位 key 最终作为真实 HTTP header 发出：

```text
Authorization: Bearer no-auth
```

这不是无害的严格等价：

- 某些无认证 endpoint 会拒绝任何 Authorization header。
- 网关或日志会把它当作一次错误凭据。
- 它违反迁移前“api_key=None 时不发 Authorization”的冻结契约。
- 测试不能通过把实现偏差注释为 approved 来替代外部兼容证明。

本地 `openai 2.54.0` 已验证公开导出的 `openai.omit` 可以在 per-call `extra_headers` 中省略 Authorization，同时仍用占位 key 构造 SDK。因此存在不依赖私有 API的修复路径。

### 3.2 修改位置

- `app/services/llm/provider.py`
- `app/services/llm/structured_runtime.py`（仅在类型/透传需要时）
- `tests/test_llm_http_contract.py`
- `tests/test_provider_unification.py`
- `docs/CONFIGURATION.md`
- 执行报告的 approved SDK differences 与未关闭风险

### 3.3 实施要求

- [ ] 保留 SDK 构造所需的内部占位 key，但保证它永不离开进程。
- [ ] `api_key is None` 时 per-call 明确传递 Authorization omission。
- [ ] 有真实 key 时准确发送 `Bearer <configured-key>`。
- [ ] 不读取 `OPENAI_API_KEY`、代理或其他环境配置。
- [ ] 不使用 `_enforce_credentials=False` 等下划线私有/半私有构造参数。
- [ ] 不通过 request hook 做静默字符串删除，优先使用 SDK 公开 `omit` 契约。
- [ ] 类型声明允许 SDK header omission 类型，但不扩散到业务层。

### 3.4 测试

必须把原测试从“期望 Bearer no-auth”改成：

```text
api_key=None       -> Authorization header 完全不存在
api_key=sentinel   -> Authorization == Bearer sentinel
poison env key     -> 绝不出现在 header
```

同时覆盖 semantic、request findings、explanation 三个 use case，确保每条路径都通过同一修复。

### 3.5 门禁

- [ ] no-auth outbound request 没有 Authorization。
- [ ] 占位字符串不出现在 request、trace、audit、exception、logs。
- [ ] provider contract 和 failure matrix 全通过。
- [ ] GLM 有 key profile 行为不变。

建议提交：

```text
fix: preserve true no-auth provider contract
```
<!-- HARDENING-01-END -->

---

<!-- HARDENING-02-START -->
## 4. Hardening 02：修复 `/v2` OpenAPI 响应 schema

### 4.1 问题

当前运行结果：

```text
EvaluationRequest  OpenAPI properties = 6
EvaluationResponse OpenAPI properties = 0
EvaluationItem     OpenAPI properties = 0
```

Pydantic validation schema 包含 `EvaluationResponse` 12 个字段和 `EvaluationItem` 18 个字段，但 serialization schema 因 wrap serializer 返回 `dict[str, Any]` 而退化为空对象。即使 0.2.0 已存在该缺陷，新的 `/v2` API 也不能以不完整 OpenAPI 作为发布契约。

### 4.2 修改位置

- `app/schemas.py`
- `app/main.py`（仅在 response model serialization 配置需要时）
- `tests/test_api.py`
- `tests/test_v4_invariants.py`
- `docs/implementation/openapi-current-v0.3.0.json`
- `docs/implementation/openapi-breaking-change-diff.md`
- `docs/implementation/FARE_LLM_ACL_REMOVAL_EXECUTION_REPORT.md`

### 4.3 修复原则

- [ ] 不通过手工伪造 OpenAPI schema 掩盖 model schema 错误。
- [ ] 优先移除会破坏 serialization schema 的 wrap serializers。
- [ ] 对 optional 字段使用 Pydantic/FastAPI 公开、可生成 schema 的排除机制，例如字段级 `exclude_if` 或经过验证的 `response_model_exclude_none`。
- [ ] 保持现有 JSON 响应行为：禁用的 `network_analysis`、`request_findings`、空 LLM text、空 decision trace、空 error details 继续按现契约省略。
- [ ] validation schema 与 serialization schema 都必须完整。
- [ ] 不重新引入已删除 ACL 字段。

### 4.4 新测试必须做正向断言

现有测试只验证 banned ACL 字符串不存在，无法发现整个 schema 为空。增加：

- [ ] `EvaluationResponse` properties 精确包含目标 12 个字段。
- [ ] `EvaluationItem` properties 精确包含目标 18 个字段。
- [ ] required 字段集合正确。
- [ ] `/v2/evaluations` 200 response 引用完整 `EvaluationResponse`。
- [ ] `/v1` 只声明 410 `ErrorResponse`。
- [ ] OpenAPI 中不存在 ACL DTO/字段/source/reason。
- [ ] 实际 response 仍省略 disabled optional fields。
- [ ] `model_json_schema(mode="serialization")` 不是空对象。

### 4.5 Artifact 更新规则

- 重新生成 `openapi-current-v0.3.0.json`。
- 不修改历史 `openapi-baseline-v0.2.0.json`。
- 更新 diff，明确本硬化修复使 `/v2` response schema 从空壳变为完整契约。
- 删除“以 Pydantic schema 代替 OpenAPI 即可”的豁免措辞。

### 4.6 门禁

- [ ] runtime `/openapi.json` 与 current artifact 一致。
- [ ] response schemas 非空且字段完整。
- [ ] response body serialization 与修复前业务 JSON 等价。
- [ ] 全量 API、audit replay、CLI/runner 测试通过。

建议提交：

```text
fix: publish complete v2 response schemas
```
<!-- HARDENING-02-END -->

---

<!-- HARDENING-03-START -->
## 5. Hardening 03：构建、安装和依赖可复现性

### 5.1 问题

当前工作 `.venv` 中：

```text
pyproject version = 0.3.0
pip show fare version = 0.2.0
pip show fare Requires = fastapi,httpx,pydantic,pyyaml,uvicorn
installed instructor = 1.16.0
installed openai = 2.54.0
repository lock/constraints file = none
```

这说明测试是在一个经过增量安装的环境中通过，尚未证明从仓库元数据进行全新安装可以得到报告所声称的 0.3.0 artifact 和依赖集合。

### 5.2 修改位置

- `pyproject.toml`
- 新的依赖锁定/constraints artifact（具体形式需与仓库既有工具链一致）
- `README.md`
- `docs/CONFIGURATION.md` 或 `docs/INTEGRATION.md`
- 新增 build/install smoke 脚本或测试
- 执行报告

### 5.3 实施要求

- [ ] 不以当前 `.venv` 的偶然状态作为 release 证据。
- [ ] 在仓库内定义可重复的生产依赖集合。
- [ ] 至少精确锁定 `instructor==1.16.0`、`openai==2.54.0`，并保证其余关键 transitive 依赖可重现；若使用范围依赖，必须另有 lock/constraints 控制最终解析版本。
- [ ] 不无界升级到 openai 3.x。
- [ ] 不顺手更换包管理器；若引入新 lock 工具需要单独说明理由。
- [ ] clean install 后 `fare==0.3.0`，metadata Requires 包含 Instructor/OpenAI。
- [ ] 构建 wheel，并从 wheel 而不是 editable source 运行最小 import/startup/OpenAPI smoke。
- [ ] 测试 production package 不意外打包 tests、audit logs、真实 fixture 或 secret。
- [ ] 记录构建命令、artifact hash 和解析后的完整版本集合。

### 5.4 干净环境验证

必须使用新建的临时目录/虚拟环境，不能删除或重用当前 `.venv`。流程应覆盖：

1. 从仓库构建 sdist/wheel。
2. 按 lock/constraints 安装 wheel。
3. `import app` / `create_app()`。
4. 检查 `importlib.metadata.version("fare") == "0.3.0"`。
5. 检查 distribution requirements 含 Instructor/OpenAI。
6. 生成 OpenAPI 并检查完整 response schema。
7. 运行最小 mock `/v2/evaluations`。
8. 验证 no-auth header 不存在。

### 5.5 门禁

- [ ] clean build/install 成功。
- [ ] artifact metadata 正确。
- [ ] 依赖解析可重复。
- [ ] wheel smoke 通过。
- [ ] 当前开发 `.venv` 重新安装项目后也显示 0.3.0。
- [ ] 未提交构建产物、临时 venv 或缓存。

建议提交：

```text
build: lock and verify the 0.3.0 runtime
```
<!-- HARDENING-03-END -->

---

<!-- HARDENING-04-START -->
## 6. Hardening 04：清零口径、兼容 facade 与报告修正

### 6.1 ACL 扫描口径

避免使用会把 `dataclass` 和 `aclose` 误判为 ACL 的表达式。建议区分：

```powershell
rg -n '(?i:\bacl\b|acl_)|Acl[A-Z]|ACL-' app config policies evals README.md docs pyproject.toml
```

然后用机器可读 allowlist 分类：

- active runtime：原则上只允许 audit epoch 的历史标识。
- migration docs：允许说明已删除字段。
- negative tests：允许断言旧名称不存在。
- legacy datasets：允许 legacy/target 对比。
- 其他匹配：失败。

新增测试或脚本时，不要简单以“总行数 0”为目标；应验证没有可执行 ACL symbol、配置键、schema component、finding source、rule 或 runtime import。

### 6.2 LLM compatibility facade

当前 `app/services/llm_client.py` 已缩为 re-export facade，但多个现役 tests/evals 仍主动依赖旧入口。

执行以下二选一并记录决定：

**推荐 A：本次删除 facade。**

- 将 tests、helpers、evals 全部迁到 `app.services.llm`。
- 删除 `app/services/llm_client.py`。
- 添加负向架构测试禁止新旧入口并存。

**可接受 B：保留一个明确期限。**

- 所有仓库内现役代码先迁到新入口。
- facade 仅为外部调用方保留。
- 发出 deprecation warning 或写清兼容政策。
- 明确在 0.4.0 删除，并新增跟踪项。

不得维持“仓库自己仍依赖旧入口、但没有退场版本”的现状。

### 6.3 报告修正

- [ ] no-auth approved difference 改为已修复的严格 parity。
- [ ] OpenAPI 风险改为已修复并记录正向 schema 断言。
- [ ] dependency/build 记录加入 clean install 证据。
- [ ] ACL 扫描写出精确模式和 allowlist，而不是只给人工解释后的数字。
- [ ] “各阶段可独立 revert”改为符合实际的逆序回退说明。
- [ ] 未解决风险只保留真正未关闭项。
- [ ] 不 amend PHASE-08；报告修正随 hardening commit 追加。

### 6.4 门禁

- [ ] 活跃代码无 ACL 能力。
- [ ] facade 已删除或有明确版本期限。
- [ ] 报告与最终代码/测试数字一致。
- [ ] 文档没有把已修复问题继续写成已知豁免。

建议提交：

```text
docs: close post-implementation audit findings
```
<!-- HARDENING-04-END -->

---

<!-- FINAL-GATE-START -->
## 7. Final Gate：硬化后的完整验收

### 7.1 必跑测试

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
.\.venv\Scripts\python.exe -m ruff check . --no-cache
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m pytest evals\llm\test_contract_dataset.py tests\test_real_semantic_acceptance_scoring.py -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m pytest tests\test_realistic_network_requests.py -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m pytest tests\test_llm_http_contract.py tests\test_llm_failure_matrix.py tests\test_llm_observability.py -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m pytest tests\test_api.py tests\test_audit_sqlite.py tests\test_runtime_lifecycle.py -q -p no:cacheprovider
git status --short
```

### 7.2 特殊发布门禁

- [ ] no-auth outbound header 完全没有 Authorization。
- [ ] `/v2` OpenAPI `EvaluationResponse` 和 `EvaluationItem` properties/required 完整。
- [ ] wheel clean-install smoke 通过，metadata 为 0.3.0。
- [ ] 依赖有可重复解析机制。
- [ ] 35/35 realistic scenarios 全通过且没有未批准 decision 漂移。
- [ ] ACL 精确扫描只剩 allowlist 中的历史/负向文本。
- [ ] 临时文件、build artifact、temp venv、cache 未进入 Git。
- [ ] 原 9 个提交 SHA 未被改写。
- [ ] hardening 分支工作树干净。

### 7.3 最终输出格式

```text
audit_result: ACCEPT | ACCEPT_WITH_CHANGES | REJECT
base_branch: codex/llm-infrastructure-acl-removal-v1
base_head: c8adb6850468b890832f7637e0bc95f9ebf9efcb
hardening_branch: codex/llm-infrastructure-acl-removal-v1-hardening
hardening_commits:
  - <sha> <subject>
full_tests: N passed, N warnings
ruff: PASS|FAIL
explicit_eval: N passed
realistic_cases: 35/35
no_auth_header_absent: true|false
openapi_response_schema_complete: true|false
clean_wheel_install: true|false
installed_fare_version: 0.3.0|other
dependencies_reproducible: true|false
acl_active_capabilities: 0|N
unresolved_findings: []|[...]
worktree_clean: true|false
push_authorized: false
pr_created: false
merge_authorized: false
```

只有 `audit_result=ACCEPT`、所有特殊门禁为 true、未解决 findings 为空时，才可以向用户申请单独的 Push 授权。
<!-- FINAL-GATE-END -->

---

## 8. 回退策略

推荐按 hardening 提交逆序回退：

```text
docs/report cleanup
build/lock
OpenAPI schema fix
no-auth fix
```

规则：

- 使用 `git revert`，不使用 `git reset --hard`。
- 不回写或删除旧审计归档。
- 不改写 `c8adb68` 之前的 9 个阶段提交。
- 若 OpenAPI 修复改变实际 response JSON，必须整体回退该提交并重新设计，不能只放宽测试。
- 若依赖锁定导致 provider contract 失败，停止并报告，不无界升级第三方依赖。

---

## 9. 完成定义

本次下一步任务完成必须同时满足：

1. 新会话独立审计确认原交付基线和架构不变量。
2. no-auth 恢复为真正无 Authorization header。
3. `/v2` OpenAPI 响应 schema 完整可供客户端生成器使用。
4. 从干净环境构建和安装得到 `fare 0.3.0` 与完整、可重复依赖。
5. ACL 清零扫描使用精确口径和机器 allowlist。
6. LLM compatibility facade 已删除或有明确退场版本。
7. 原 9 个提交不被改写，所有修复作为新提交追加。
8. 全量测试、realistic suite、provider contract、failure matrix、显式 eval、ruff 全部通过。
9. 工作树干净，执行报告与事实一致。
10. Push、PR、Merge 仍未执行，等待用户分别授权。
