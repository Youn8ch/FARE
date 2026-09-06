# FARE 主链二次收口修复与后续优化计划 V4

> 基准版本：`a70ea45`（本地与 `origin/main` 同步，515 个测试全绿，Python 3.13）
> 计划性质：主链二次收口 + 修复完成后的持续优化方向
> 前置文档：`FARE-主链二次收口修复与后续优化计划-V3.md`（V4 是 V3 的修订版，不是重写）
> 核心目标：允许调整主项目处理逻辑，但不扩展产品业务范围；优先保证主链正确、单一、可验证、可维护。
> 验收原则：**所有修复阶段的业务验收必须使用可重复的模拟输入（mock / versioned fixture / fake client），通过限制输入条件验证精确输出，不以真实外部服务作为阶段验收前提。**

---

## 0. V4 相对 V3 的变更摘要

V3 的总体方向、七个问题判断、验收原则全部保留。V4 做了以下修订（依据 2026-08-30 对 `a70ea45` 真实代码的逐条核对）：

| # | 变更 | 理由 |
|---|---|---|
| C1 | 新增「执行前决策记录」（第 2 节），D1~D6 全部落定 | V3 留白的决策点在开工前必须冻结，避免执行中反复 |
| C2 | 原 P1（类型契约）与 P2（Rules 前置）合并为新 P1 | P1 单独存在时没有行为变化也没有消费者；`RuleStageResult` 的第一个消费者就是 P2 的 ACL gating，合并后边界干净 |
| C3 | 原 P6（Evaluator 拆分）拆为两步提交：先纯搬移、再引入 stages/ 目录 | 1543 行文件一次性搬迁不利于 review 与二分定位 |
| C4 | 原 P7 的 post-decision 并行（`asyncio.gather`）改为**保持串行**，并行推迟到 O12 | 并行只省 LLM 延迟，代价是 `exceptions` 顺序与 timing 指标失去确定性；行为冻结优先 |
| C5 | P4（Provider 收口）明确：**保留 offline 字段伪装**（zone→area/areaId/regionName、environment→platformName），只新增 classification 通道 | 清理伪装会把 offline 模式下 `platform_name` 变为 None，改变 condition 带 `platform_name` 的规则匹配结果，属于行为变更 |
| C6 | P3（单次 reduce）补齐隐藏依赖：`llm_added_pending_count` 指标推导、语义 `candidate_rule_ids` 管线、`DecisionTrace.semantic_finding_ids` 契约、item_id 铸造点提前、AC-05 反转理由记录 | 这些是 V3 遗漏的管线，漏掉任何一条都会导致 baseline 破裂或指标断裂 |
| C7 | fixture 策略改为**复用现有 case 体系**（`tests/case_schema.py` + `tests/cases/`），`tests/fixtures/v3/` 只放无法归入现有结构的新 mock/LLM fixture | 仓库已有 59 个 requirement case + 6 个 evaluation case 的版本化体系，另起炉灶会产生第二套测试语义 |
| C8 | 验收矩阵新增 V3-31~V3-33（离线多命中、exceptions/顺序冻结、audit findings 完整性） | 核对中发现的未冻结行为 |
| C9 | ProviderNetworkFact 设计改为 `NetworkPlanFact + Optional[ExplicitNetworkClassification]`，避免第四个近似重复的事实模型 | 现有 `NetworkPlanFact`（schemas.py）已是规范化 provider fact；再加一个逐字段等价模型只会增加 P4 等价性事故面 |
| C10 | 明确 `validate_network_plan_response` 的搬迁归属与 audit `raw_records` 冻结 | P4 把校验从 Resolver 挪进 provider 适配层时，必须钉死校验函数去处与审计原始记录结构 |
| C11 | 新增 ACL-PATH-001 归属规则、"仅 primary 才进 matched_rules" 的钉死条款、脱敏覆盖新字段、`PolicyBundle.match` 的 `total_combinations` 进 stage contract | 均为核对中发现的易错点 |

V3 → V4 阶段映射：P0→P0；P1+P2→P1；P3→P2；P4→P3；P5→P4；P6→P5（拆两步）；P7→P6（去并行）；P8→P7；P9→P8；P10→P9。

---

## 1. 背景与当前判断（含代码核实结果）

`a70ea45` 已完成第一轮架构收口，以下成果作为本轮基线保留：

- HTTP API、批量 CLI 等入口共享 `Runtime.evaluate_request`。
- `NetworkPlanResolver` 已成为网络规划主流程中的统一 Resolver。
- `CanonicalNetworkFact` / `CanonicalAddressSegment` 已成为 RuleEngine 的主要事实模型。
- `usage_code -> object_type`、`platform_name -> environment` 等隐式推断已被禁止（`canonical.py` 模块文档明确）。
- RuleEngine 已不直接依赖 `NetworkCatalog`（`test_legacy_cleanup.py` 静态检查固定）。
- Finding / DecisionReducer 已建立，Network / Rule / ACL / Semantic 以 Finding 形式参与裁决。
- LLM 不能把已有 `待定` 提升为 `合规`。
- ACL deterministic short-circuit、semantic guard、LLM shadow 均有完整测试。

以下七项判断已逐条对照真实代码核实，**全部属实**：

| # | 问题 | 核实 | 代码证据（`a70ea45`） |
|---|---|---|---|
| 1 | 每 item 两次 `DecisionReducer.reduce()` | ✅ | `evaluator.py:574`（`_build_deterministic_stage` 确定性裁决）+ `evaluator.py:911`（`_reduce_items` 最终裁决） |
| 2 | RuleEngine 实际执行时机与阶段名不一致 | ✅ | `evaluator.py:518`（ACL gating 内调 `policies.match`）+ `evaluator.py:570`（rules 阶段再次匹配）；真实顺序为 `plan → network → acl → rules → semantic → reduce → explain → assemble` |
| 3 | offline_catalog 隐藏第二事实通道 | ✅ | `network_plan_resolver.py:80/91` 持有 `offline_catalog`；`:547` 经 `_legacy_entry()`（`:577-583`）二次查目录；`canonical.py:98` 消费 `legacy_entry` |
| 4 | Evaluator 承担过多业务职责 | ✅ | 1543 行；内联两个 shadow 阶段（`:219-375`）、解释（`:377-421`）、指标（`:1499-1543`）、文案映射（`_finding_text`/`_CATALOG_ERROR_TEXT`/`_ACL_ERROR_TEXT`）、ACL verification 判定（`:1438-1454`）、语义 effect 分桶（`:1052-1174`） |
| 5 | secondary findings 未进入最终输出 | ✅ | `Decision.findings` 计算后被丢弃；`EvaluationItem` 无 findings 字段；audit 只存最终 response + 原始记录——secondary finding **在任何地方不可审计** |
| 6 | 阶段命名与真实链不符 | ✅ | `docs/ARCHITECTURE.md:80` 仍写"八阶段编排"；shadow 阶段内联在 `evaluate()` 中 |
| 7 | request-level decision 内联 | ✅ | `evaluator.py:434` 三元表达式 |

附带发现：`_apply_semantic_failure`（`evaluator.py:773`）已是死代码（全仓无调用），可直接删除。

本轮不是重做 FARE，而是在 `a70ea45` 之上完成最后一轮主链收口。

---

## 2. 执行前决策记录（已落定，执行中不得重开）

| # | 决策 | 结论 |
|---|---|---|
| D1 | 每 item 单次正式 reduce，推翻 AC-05 钉死的"两次 reduce" | **确认执行**。`tests/test_evaluator_orchestration.py:103-107` 以注释解释并断言 `len(reducer.calls) == 2 * items`，这是 AC-05 的有意决策；P2 阶段须在 `docs/v3-baseline.md` 记录反转理由（单次 reduce 内部同时产出 deterministic 快照与 final，信息不丢失且消除双裁决），并同步更新该测试 |
| D2 | offline 字段伪装（`network_plan_client.py:324-327`：zone→area/areaId/regionName、environment→platformName） | **V4 收口期间保留伪装、行为零变化**；只新增 explicit classification 通道。伪装清理归入 O1（Provider Contract 正式化），届时需独立行为变更评审 |
| D3 | `decision_findings` 为 additive API 变更 | **确认接受**。`matched_rules` 语义本轮不动，schema 向后兼容 |
| D4 | post-decision 三阶段（explanation / ACL candidate shadow / request findings shadow）是否并行 | **V4 轮保持串行**（与现状一致），并行列入 O12 性能优化项 |
| D5 | Evaluator 拆分深度 | **最小职责拆分**，以"职责清晰"为准，不为目录美观搬文件 |
| D6 | V3/V4 测试数据体系 | **复用 `tests/case_schema.py` + `tests/cases/` 现有模式**扩展主链场景；`tests/fixtures/v3/` 仅容纳无法归入现有结构的新 mock / LLM fixture |

---

## 3. 本轮总体目标

本轮结束后，主链应为：

```text
EvaluationRequest
    │
    ▼
Runtime / Admission
    ├─ query limit
    ├─ raw item upper-bound limit
    └─ idempotency claim
    │
    ▼
NetworkFactProvider（mock / http / offline_catalog 三实现，同一 typed result）
    │
    ▼
NetworkPlanResolver（只接触 ProviderNetworkFact；不再持有 NetworkCatalog）
    │
    ▼
Canonicalizer（只复制显式事实，不做业务推断）
    │
    ▼
RuleStage
    ├─ 每 item 规则匹配恰好一次（含 total_combinations 上下文）
    └─ 只生成 Rule Findings / matched rules；item_id 在此之前已铸造
    │
    ▼
AclStage
    ├─ 消费 Network Findings + RuleStageResult 决定 skip/analyze
    └─ 只生成 ACL Facts / ACL Findings（含 ACL-PATH-001）
    │
    ▼
SemanticStage
    └─ 只生成 Semantic Findings / questions / observations
    │
    ▼
ItemFindingSet（network / rules / acl / semantic 四分区）
    │
    ▼
DecisionReducer（每 item 恰好一次正式 reduce；内部同时产出 deterministic 快照与 final）
    │
    ▼
ItemAssembler
    │
    ▼
RequestDecisionAggregator（request 级纯聚合）
    │
    ▼
PostDecisionAnalysis（串行：explanation → acl candidate shadow → request findings shadow）
    │
    ▼
ResponseAssembler
    │
    ▼
AuditStore
```

---

## 4. 必须满足的最终架构不变量

以下不变量是本轮最终验收的硬条件。

### 4.1 事实不变量

1. RuleEngine 只能读取 Canonical 模型。
2. Resolver 不得直接读取 `NetworkCatalog` / `NetworkEntry`。
3. offline/mock/http 三种 provider 必须通过同一种 typed provider result 进入 Resolver。
4. Canonical 只复制显式事实，不进行业务推断。
5. 同一事实不得从两个并行来源在 Resolver 中拼接。
6. 规则匹配的 zone→area_id 回退（`rule_loader.py:155-158`）是现有业务语义，**原样保留**：HTTP provider 无 zone 时 zone 类条件回退用 `area_id` 匹配。

### 4.2 规则不变量

1. 每个 item 的正式规则匹配只执行一次。
2. ACL gating 只能消费 RuleStage 已产生的结果，不能重新调用 RuleEngine / `PolicyBundle.match()`。
3. RuleStage 不产生 decision。
4. RuleStage 不调用 ACL / LLM。
5. `PolicyBundle.match(combination, total_combinations)` 的 `total_combinations`（`combination_count` 规则所需）必须进入 RuleStage 的输入契约。

### 4.3 裁决不变量

1. 每个 item 只允许一次正式 `DecisionReducer.reduce()`。
2. Network / Rule / ACL / Semantic 都只能先产生 Finding。
3. Reducer 是 item-level 唯一正式裁决入口。
4. request-level decision 只能由独立 `RequestDecisionAggregator` 从 final items 纯聚合。
5. PostDecisionAnalysis 不允许再修改 `decision` / `reason_code` / `reason_type` / primary finding。
6. Semantic / LLM 不允许 `待定 -> 合规`。
7. ACL-PATH-001 的归属固定为：AclStage 依据 `explicit_no_path` 事实生成该 finding（`PolicyBundle.match` 永远不返回它，`rule_loader.py:143`）；**仅当它恰为 primary 时**才追加进 `matched_rules`（保持 `evaluator.py:738-747` 的现有兼容语义）。

### 4.4 输出不变量

1. primary finding 决定 item 的 `reason_code/reason_type`。
2. secondary findings 必须可审计（进入 API `decision_findings` 与 audit）。
3. `decision_trace` 必须由同一次 reducer 调用产生或由 reducer 返回的结果派生；`DecisionTrace` 的 schema 契约不动，**`semantic_finding_ids` 字段保留**（`schemas.py:210`，Literal 枚举 `unchanged/observation_only/question_only/downgraded/semantic_failure` 不变）。
4. shadow LLM 失败不得改变业务结论。
5. explanation 失败只能触发模板 fallback。
6. 新增字段（`decision_findings.detail` 等）必须被 `redact_evaluation_response` 脱敏链路覆盖（`main.py:82`）。
7. `exceptions` 列表的追加顺序保持确定性（candidates → request findings → explanation），进 audit 可断言。

---

## 5. 测试总原则：必须使用模拟输入验证精确输出

（与 V3 第 4 节一致，全部保留。）

### 5.1 模拟输入要求

阶段验收默认只允许：

- `MockNetworkPlanClient` / versioned network fixture；
- `MockAclClient` / versioned ACL fixture；
- `RecordingLlmClient` / versioned LLM fixture；
- fake / recording reducer、rule engine、provider；
- FastAPI `TestClient`；
- 纯函数单元测试。

不得把以下内容作为阶段 PASS 前提：真实网段规划 API、真实 ACL API、真实内网 LLM、外网模型、人工观察输出"看起来正常"。

真实接口联调可以存在，但只能作为非阻塞的 integration / UAT 项。仓库现状已满足此原则（`tests/conftest.py` 的 `block_real_network` 守卫禁止一切非回环连接），本轮不得削弱该守卫。

### 5.2 精确断言要求

每个模拟场景至少断言其中适用的字段：

```text
HTTP status / request decision / item count / item decision
reason_code / reason_type / primary finding / all findings
matched_rules / network fact status / ACL verification status
decision_trace / provider call count / rule match call count
ACL call count / semantic call count / DecisionReducer call count
shadow call count / audit record
```

禁止只断言 `assert response.status_code == 200` 或 `assert body["decision"] in {...}`。

---

## 6. 测试数据体系（D6：复用现有 case 体系）

**主链业务场景**（P0 characterization、各阶段验收、P8 最终矩阵）优先落盘为现有版本化 case 格式：

```text
tests/cases/…            # 复用 tests/case_schema.py 的 schema_version 体系扩展
tests/fixtures/v3/       # 仅放无法归入现有结构的新 fixture
├─ network/              # 依赖失败、invalid_response、subnet_mismatch、multi_region、provider_equivalence 等
├─ acl/                  # explicit_no_path、ambiguous、port_mismatch、missing_firewall、dependency_failure 等
└─ llm/                  # semantic_* / fabricated_rule / explanation_* 等
```

所有 fixture 必须带 `fixture_version` / `purpose` / `responses`（`MockNetworkPlanClient` 已强制此契约）。**不新建平行的 case 描述格式**；`main_chain_cases` 以现有 case schema 的扩展字段实现。

---

## 7. 阶段 P0：冻结 `a70ea45` 行为基线

### 7.1 目标

在改主链前，把当前可观察行为完整冻结。本阶段不改变业务代码。

### 7.2 工作内容

1. 从 `a70ea45` 创建独立修复分支 `codex/fare-main-chain-v4`。
2. 记录 Python 版本、pytest 数量（当前 515 全绿）、ruff 状态、CASE-01~CASE-12 当前输出、当前 stage 顺序、当前 provider/rule/ACL/reducer/LLM 调用次数。
3. **额外冻结以下核对中确认的隐性行为**（进 `docs/v3-baseline.md`）：
   - `model_raw["metrics"]` 的 `llm_added_pending_count` 推导方式（P2 将改变其来源，需先记录当前值）；
   - `exceptions` 列表顺序（candidates → request findings → explanation）；
   - offline 模式的伪装字段观察值（`area/area_id/region_name = zone`、`platform_name = environment`）；
   - offline 目录多命中时 item 落到 `NETWORK_PLAN_INVALID_RESPONSE`、`_legacy_entry` 静默返回 None 的现状；
   - `matched_rules` 中 ACL-PATH-001 仅在 primary 时出现。
4. 建立本轮 characterization tests（复用 §6 的 case 体系）。
5. 明确哪些输出属于：必须保持 / 允许修正 / 明确计划变更（D1 的反转记录写在此处）。

### 7.3 模拟验收场景

（与 V3 P0-C01~C05 一致：普通 HTTPS 合规、规划不存在、Telnet skip、ACL explicit no path、semantic conflict。冻结字段与预期不变。）

### 7.4 PASS 条件

- 全部旧测试通过（515 基线）；
- 新 characterization tests 通过；
- 生成 `docs/v3-baseline.md`；
- 无业务代码修改。

---

## 8. 阶段 P1：Stage Contract + Rules 前置（每 item 只匹配一次）

> 合并 V3 的 P1（类型契约）与 P2（Rules 前置）：类型契约的第一个消费者就是本阶段的 ACL gating 改造，合并后无"只有类型没有用户"的中间态。

### 8.1 目标

把真实顺序修正为 `network → canonical → rules → ACL`；ACL gating 消费 `RuleStageResult`，不再自行调用 `PolicyBundle.match()`；阶段间数据全部走 typed contract。

### 8.2 修改方向

```text
当前：ACL stage 内 PolicyBundle.match()（evaluator.py:518）
      + Rules stage 再次 PolicyBundle.match()（evaluator.py:570）
目标：RuleStage 内 PolicyBundle.match() 恰好一次
      AclStage 消费 RuleStageResult
```

新增/调整的类型（建议 `app/services/evaluation_types.py` + `app/services/stages/`，全部 frozen dataclass）：

```python
@dataclass(frozen=True)
class RuleStageResult:
    matched_rules: tuple[Rule, ...]          # 每 item，含 total_combinations 上下文的匹配结果
    findings: tuple[Finding, ...]

@dataclass(frozen=True)
class AclStageResult:
    facts: ExtractedFacts
    findings: tuple[Finding, ...]            # 含 ACL-PATH-001（自本阶段起归属 AclStage）
    verification_status: str
    raw: ...

@dataclass(frozen=True)
class SemanticStageResult:
    findings: tuple[Finding, ...]
    review_ids / question_ids / observation_ids
    semantic: SemanticAnalysis

@dataclass(frozen=True)
class ItemFindingSet:
    network: tuple[Finding, ...]
    rules: tuple[Finding, ...]
    acl: tuple[Finding, ...]
    semantic: tuple[Finding, ...]
```

**item_id 铸造点提前**：现于 `_run_acl_stage`（`evaluator.py:485`）以 `f"{request_id}-{index:03d}"` 铸造；本阶段将其提前到 splitter / 组合枚举处，**编号规则不得变**（audit 与 case 断言依赖）。

**semantic 管线衔接**：`_run_semantic_stage` 现从中间 items 的 `item.matched_rules` 取 `deterministic_candidates`（`evaluator.py:835-837`）；本阶段起改从 `RuleStageResult` 取数，`candidate_rule_ids` 合并结果保持不变。

### 8.3 模拟验收

（沿用 V3 P2-C01~C05：）

- **P1-C01 正常合规项**：tcp/443 无规则命中 → `rule match calls = 1/item`，`ACL calls = 1/item`。
- **P1-C02 Telnet + skip**：tcp/23，`deterministic_pending_mode=skip` → `rule match calls = 1`，`matched rule = PORT-001`，`ACL calls = 0`，decision 保持现有行为。
- **P1-C03 Telnet + analyze**：tcp/23，`analyze`，ACL verified → `rule match calls = 1`，`ACL calls = 1`，`primary = PORT-001`。
- **P1-C04 network failure**：source `NETWORK_PLAN_NOT_FOUND` → `ACL calls = 0`；RuleStage 可执行纯规则检查（special_port/least_privilege 类规则在无网络事实时仍可命中，现状如此），但不得覆盖 network primary；final reason = `NETWORK_PLAN_NOT_FOUND`。
- **P1-C05 多 item**：2×2×1 = 4 items → `rule match calls = 4`，不得 > 4。

### 8.4 PASS 条件

- `PolicyBundle.match()` 每 item 恰好一次；
- ACL 模块中不存在 RuleEngine / `PolicyBundle.match` 调用（静态检查）；
- stage 顺序测试改为 `plan → network → rules → acl → semantic → reduce → post_decision → assemble`（或等价命名），`test_evaluator_orchestration.py` 的 `EXPECTED_STAGES` 同步更新；
- 所有 P0 输出保持；
- 不允许 `dict[str, Any]` 作为主业务阶段之间的唯一数据契约。

---

## 9. 阶段 P2：DecisionReducer 改为每 item 一次正式裁决

### 9.1 目标

彻底消除 `deterministic reduce → final reduce` 双裁决，改为每 item：

```text
ItemFindingSet → DecisionReducer.reduce(...) → Final Decision + DecisionTrace（一次完成）
```

**此为对 AC-05 已记录决策的有意反转（D1），反转理由写入 `docs/v3-baseline.md`。**

### 9.2 推荐设计

不另实现一套 deterministic decision 算法。Reducer 在一次调用内同时计算：

```text
deterministic 快照 = network + rules + acl 分区 findings（排除 semantic）
final              = 全部 findings
```

返回契约（**必须完整覆盖现有 DecisionTrace schema**）：

```python
Decision(
    decision=..., primary_finding=..., findings=...,
    matched_rules=...,
    trace=DecisionTrace(
        deterministic_decision=...,      # 快照裁决
        semantic_effect=...,             # Literal 五值不变（schemas.py:210）
        semantic_finding_ids=[...],      # 保留，不得删除
        final_decision=...,
        final_reason_code=...,
    ),
)
```

### 9.3 禁止方案

- 不允许 `preview = reduce(deterministic); final = reduce(all)`（即使改名为 preview 仍是两次完整裁决）；
- 不允许在 Evaluator / ItemAssembler 中复制 Reducer 的 priority 算法；
- 不允许删除/改变 `DecisionTrace` 的任何现有字段或 Literal 枚举。

### 9.4 隐藏依赖清单（本阶段必须同步处理，漏一项即 baseline 破裂）

1. **`llm_added_pending_count` 指标**（`evaluator.py:905-921`，进 audit `model_raw["metrics"]`）：现靠"中间 items 待定数 vs 最终待定数"差值计算；本阶段起改由 reducer 返回的 `deterministic_decision` 快照推导，指标值必须与 P0 冻结值逐场景一致。
2. **降级判定逻辑**（`_materialize_final_item`，`evaluator.py:924-1014`）：`downgraded` 判定现依赖"确定性 decision vs 最终 decision"比较与 `primary.source == "semantic"`；单次 reduce 后由快照与 final 比较复现，语义（含 `semantic_failure` 分支、文案映射 `_SEMANTIC_FAILURE_TEXT`/fact_conflict 文案）逐字保持。
3. **`pending_requires_reason` 校验**：`EvaluationItem` 模型校验器要求合规 item 不得带 reason 字段；Reducer/ItemAssembler 必须保证。
4. **死代码删除**：`_apply_semantic_failure`（`evaluator.py:773`，全仓无调用）直接删除。
5. **测试同步**：`test_evaluator_orchestration.py:107` 的 `len(reducer.calls) == 2 * items` 断言改为 `== items`，注释一并改写。

### 9.5 模拟验收

新增 `RecordingDecisionReducer`（已有雏形）。场景沿用 V3 P3-C01~C07：

- **P2-C01 全合规**：reduce calls = 1，deterministic = 合规，final = 合规。
- **P2-C02 Rule pending + semantic conflict**：deterministic = 待定，final = 待定，primary = PORT-001，semantic 不覆盖 rule。
- **P2-C03 ACL pending + semantic conflict**：primary = ACL finding，final = 待定。
- **P2-C04 仅 semantic conflict**：deterministic = 合规，final = 待定，primary = SEMANTIC_FACT_CONFLICT。
- **P2-C05 semantic question_only**：final = 合规，`semantic_effect = question_only`。
- **P2-C06 semantic failure**：deterministic 合规 + LLM semantic failure fixture → final = 待定，reason = `LLM_SEMANTIC_ANALYSIS_FAILURE`。
- **P2-C07 deterministic pending + semantic failure**：final primary 仍为 deterministic primary，不得被 semantic failure 覆盖。

每例同时断言 `llm_added_pending_count`、`decision_trace.semantic_effect`、`semantic_finding_ids` 与 P0 冻结值一致。

### 9.6 PASS 条件

- 硬断言 `assert len(reducer.calls) == len(result.items)`；
- 静态检查：`app/` 中 `DecisionReducer.reduce(` 只允许出现在 reduce stage / reducer-owned workflow；
- `_build_deterministic_stage` 中的 decision 生成与 `_apply_semantic_failure` 已删除；
- P0 全部输出保持（含 metrics）。

---

## 10. 阶段 P3：统一 Provider 事实通道，删除 `legacy_entry`

### 10.1 目标

Resolver 不再认识 `NetworkCatalog` / `NetworkEntry` / `legacy_entry` / `offline_catalog`；offline_catalog 只能作为 Provider 实现存在。

### 10.2 目标模型（C9：不引入第四个事实模型）

```python
@dataclass(frozen=True)
class ExplicitNetworkClassification:
    catalog_entry_id: str | None
    zone: str | None
    environment: str | None
    object_type: str | None
    labels: frozenset[str]

# ProviderNetworkFact = 现有 NetworkPlanFact（schemas.py，已规范化）+ 可选显式分类
@dataclass(frozen=True)
class ProviderNetworkFact:
    fact: NetworkPlanFact                    # 逐字保留现有字段
    classification: ExplicitNetworkClassification | None
    source: str                              # "http" / "mock" / "offline_catalog"
```

三种 Provider：

- **HTTP**：外部 API DTO → adapter validation → ProviderNetworkFact；`classification = None`（除非真实合约明确提供）。
- **Mock**：fixture 直接构造与 HTTP/offline 等价的 ProviderNetworkFact。
- **Offline Catalog**：`NetworkCatalog → OfflineCatalogProvider → ProviderNetworkFact`；`classification = 目录显式字段`。

### 10.3 冻结清单（本阶段不得改变的可观察行为）

1. **伪装保留（D2/C5）**：`OfflineCatalogNetworkPlanClient` 的 `zone→area/areaId/regionName`、`environment→platformName` 映射原样保留（`network_plan_client.py:324-327`）；classification 通道是**新增并行通道**，不替换伪装值。伪装清理归 O1。
2. **离线多命中**：provider 返回 code 409 → 校验后落到 `NETWORK_PLAN_INVALID_RESPONSE` 的现状冻结；`_legacy_entry` 静默返回 None 的旧行为随 legacy_entry 一并消失，但最终 reason_code 不变（仍为 `NETWORK_PLAN_INVALID_RESPONSE`）。
3. **zone→area_id 规则回退**：`rule_loader.py:155-158` 原样保留（见 4.1.6）。
4. **audit `raw_records` 结构冻结**：transport 级 `http_status`/`body`/`raw_text`/`validation` 记录结构不变。
5. **`usageCode: None`**：offline provider 不得把 object_type 伪装成 usage_code（现有静态测试 `test_legacy_cleanup.py::test_offline_provider_does_not_masquerade_object_type_as_usage_code` 继续有效）。

### 10.4 校验搬迁归属（C10）

`validate_network_plan_response`（`network_plan_resolver.py:244-359`）整体迁入 provider 适配层（HTTP 与 Mock 共用同一校验；Offline provider 走自身实现）。Resolver 只保留：查询计划、并发、超时、limit。搬迁后 P0 的全部网络错误码路径（404/401/403/5xx/408/429/invalid/mismatch）输出逐字段一致。

### 10.5 模拟验收

（沿用 V3 P4-C01~C05，并收紧：）

- **P3-C01 mock/offline 等价**：等价输入下两 provider 产生完整 Canonical equality（access_network、area、area_id、region_name、platform_name、network、subnet、usage_code、catalog_entry_id、zone、environment、object_type、labels、status、error_code 全部比较，不只比 decision）。
- **P3-C02 HTTP 无 classification**：zone/environment/object_type/labels 不得从 usage_code/platform_name/description 推断。
- **P3-C03 offline 显式分类**：目录明确 `environment=production`、`object_type=database` → Canonical 逐字保留。
- **P3-C04 offline 多命中**：provider result = conflict（409）→ 最终 `NETWORK_PLAN_INVALID_RESPONSE`（冻结现状，见 10.3.2），Resolver 不自行重查目录。
- **P3-C05 query limit 三模式一致**：`network_plan_max_subnets=1`、请求跨 2 个唯一 /24 → 三模式均 `422 / NETWORK_PLAN_QUERY_LIMIT_EXCEEDED`，`provider calls = 0`。

### 10.6 静态 PASS 条件

以下必须为 0：

```text
NetworkPlanResolver import NetworkCatalog / NetworkEntry
ResolvedAddressSegment.legacy_entry
_resolve_address(... offline_catalog ...)
_legacy_entry(...)
```

**测试锚点同步翻转**：`test_legacy_cleanup.py:88-89`（现断言 `legacy_entry` 保留）改为断言其不存在；该文件 docstring 的"兼容边界允许"表述同步修订。

---

## 11. 阶段 P4：Finding 成为正式可审计业务中间结果

### 11.1 目标

最终 item 必须能看到全部影响判断的 Finding，而不是只留 primary。

### 11.2 输出设计

给 `EvaluationItem` 增加 additive 字段：

```python
decision_findings: list[DecisionFinding]

class DecisionFinding:
    code: str
    source: str
    reason_type: str | None
    affects_decision: bool
    detail: str | None
    is_primary: bool
```

优先级留在内部，不对 API 暴露。

### 11.3 规则

- `reason_code/reason_type` 仍映射 primary；
- `decision_findings` 包含全部 findings，与 Reducer 返回的 findings 一一对应；
- `matched_rules` 保持现有兼容语义**完全不动**——特别是 4.3.7 的钉死条款：ACL-PATH-001 仅在恰为 primary 时出现在 `matched_rules`，但作为 finding 始终进入 `decision_findings`；
- audit 必须保留完整 findings；
- `decision_findings` 及其 `detail` 必须纳入 `redact_evaluation_response` 脱敏覆盖（4.4.6）。

### 11.4 模拟验收

（沿用 V3 P5-C01~C03：）

- **P4-C01 PORT-001 + ACL-PATH-001**（analyze 模式）：decision = 待定，primary = PORT-001；`decision_findings` 同时含两者，ACL-PATH 不消失；`matched_rules` 仅含 PORT-001（ACL-PATH 非 primary，钉死条款）。
- **P4-C02 四类 finding 并存**：primary = network，四类全部出现在 `decision_findings`。
- **P4-C03 信息性 semantic finding**：`affects_decision=false`，decision 可保持合规，finding 仍在输出与 audit 中。

### 11.5 PASS 条件

- Reducer 返回 findings 与 API `decision_findings` 一一对应；
- audit 中不丢 secondary finding；
- primary 只有一个；`reason_code == primary.code`。

---

## 12. 阶段 P5：拆分 Evaluator，使其真正只负责编排（两步提交）

> 拆为两个独立提交（C3）：P5a 纯搬移、P5b 引入 stages/ 编排。每步均以 P0/P1 全量输出一致为 PASS 前提。

### 12.1 P5a：纯搬移（行为零变化）

从 `evaluator.py` 搬出，模块归属建议：

```text
app/services/
├─ finding_factory.py      # _collect_deterministic_findings、semantic findings 构造、文案映射（_finding_text 及各 *_TEXT 表）
├─ item_assembler.py       # _build_item、_materialize_final_item、_network_item_fields、_catalog_evidence
├─ request_decision.py     # request 级聚合（先内联函数，P7 正式组件化）
└─ response_assembler.py   # _aggregate_analyses、EvaluationResponse 组装
```

搬移 = 移动代码 + 改调用点，**不改任何逻辑、不改任何输出**。

### 12.2 P5b：引入 stages/ 与编排重写

```text
app/services/stages/
├─ network_stage.py
├─ rule_stage.py
├─ acl_stage.py
├─ semantic_stage.py
├─ reduce_stage.py
└─ post_decision_stage.py
```

`Evaluator.evaluate()` 最终应接近：

```python
async def evaluate(request):
    plan = planner.plan(request)                       # 含 item 上限守卫
    network = await network_stage.run(plan)
    canonical = canonicalizer.build(network)
    rules = rule_stage.run(canonical)                  # 含 total_combinations
    acl = await acl_stage.run(canonical, rules)
    semantic = await semantic_stage.run(...)
    decisions = reduce_stage.run(...)
    items = item_assembler.build(...)
    request_decision = request_aggregator.aggregate(items)
    post = await post_decision.run(...)                # 串行（D4）
    return response_assembler.build(...)
```

Evaluator 不再自行实现：Finding 文案映射、semantic effect bucket、ACL verification 判断、response item mutation、metrics 细节、shadow stage 具体流程。metrics 统一收敛为一个 stage metrics 收集器（为 O5 铺路），`model_raw["metrics"]` 输出保持不变。

### 12.3 模拟验收

（沿用 V3 P6-C01~C04：stage recorder 精确顺序、network 失败链路、item limit 在任何依赖调用前 422、query limit 时 provider/ACL/LLM 全 0。）

### 12.4 PASS 条件

- Evaluator 不再直接构造 Network/Rule/ACL/Semantic Finding；
- Evaluator 不再直接修改 final item decision；
- Evaluator 主方法只体现阶段顺序和错误传播；
- 旧测试全部迁移到新 stage contract；
- P5a 与 P5b 各自独立提交，各自全量测试绿。

---

## 13. 阶段 P6：统一 PostDecisionAnalysis（保持串行）

### 13.1 目标

把 Explanation、ACL Candidate Shadow、Request Findings Shadow 三个非权威阶段明确放到正式裁决之后：

```text
reduce → post_decision_analysis → assemble
```

**执行顺序固定为串行（D4/C4）：explanation → acl candidate shadow → request findings shadow**（与现状一致）。并行化列入 O12，本轮不做——理由：并行会破坏 `exceptions` 顺序确定性与 stage timing 可比性，收益仅为 LLM 延迟。

Explanation 输入必须是 final items（现状已满足）。

### 13.2 模拟验收

（沿用 V3 P7-C01~C05：全成功、ACL shadow 失败、request findings shadow 失败、explanation 失败 template fallback、三者全失败 HTTP 仍按正式业务结果输出。）

### 13.3 硬性 PASS

测试在 reduce 后保存 business core 快照：

```python
business_core_before = [decision, reason_type, reason_code, matched_rules,
                        decision_findings, decision_trace.final_decision]
...
assert business_core_after == business_core_before
```

post 阶段后逐一比较；另断言 `exceptions` 列表顺序与 P0 冻结顺序一致（4.4.7）。

---

## 14. 阶段 P7：显式 RequestDecisionAggregator

### 14.1 目标

把 `evaluator.py:434` 的三元表达式移出，成为正式纯函数组件（P5a 已落 `request_decision.py`，本阶段正式命名与测试固定）。

### 14.2 业务规则

保持：任一 item 待定 → request 待定；全部合规 → request 合规。

### 14.3 模拟验收

（沿用 V3 P8-C01~C04 四例：单合规、全合规、混合、全待定。）

### 14.4 PASS 条件

- Evaluator 不直接包含 request decision 条件表达式；
- 文档明确 DecisionReducer = item-level、RequestDecisionAggregator = request-level。

---

## 15. 阶段 P8：最终主链模拟验收矩阵

集中测试 `tests/test_v4_main_chain_acceptance.py`，全部使用模拟依赖。V3-01~V3-30 全部保留（见 V3 第 15 节表格），新增：

| Case | 模拟限制 | 预期 item / Primary | 关键断言 |
|---|---|---|---|
| V3-31 | offline 目录多命中（2 entries 覆盖同一 /24） | 待定 / NETWORK_PLAN_INVALID_RESPONSE | 冻结现状；无 ZONE_CONFLICT；Resolver 不重查目录 |
| V3-32 | 正常请求 + 一次 ACL shadow 失败 | 正式 decision 不变 | `exceptions` 顺序 = P0 冻结顺序；stage metrics 顺序字段一致 |
| V3-33 | PORT-001 + semantic conflict | 待定 / PORT-001 | audit 记录中 `decision_findings` 完整含两类 finding，primary 唯一 |

---

## 16. 阶段 P9：最终代码与文档收口

### 16.1 删除历史结构

确认删除：

- deterministic first reduce（`_build_deterministic_stage` 中 decision 生成）；
- ACL 内部 rule pre-match；
- Resolver `legacy_entry` / `offline_catalog` / `_legacy_entry()`；
- `_apply_semantic_failure` 等死代码；
- 旧 stage comment / 旧"八阶段"描述；
- 失效的测试 double。

### 16.2 文档更新

至少更新：`README.md`、`docs/ARCHITECTURE.md`（含"八阶段"表述修正）、`docs/DECISION_MODEL.md`（补 D1 反转记录与单次 reduce 语义）、`docs/TESTING.md`、`docs/ACCEPTANCE-REPORT-V4.md`。架构图必须与真实代码一致。

### 16.3 最终自动验收

```bash
pytest -p no:cacheprovider -q
ruff check . --no-cache
pytest -q tests/test_v4_main_chain_acceptance.py
pytest -q tests/test_decision_reducer.py
pytest -q tests/test_provider_unification.py
pytest -q tests/test_evaluator_orchestration.py
pytest -q tests/test_legacy_cleanup.py   # 已按 10.6 翻转
```

最终 PASS 要求：

1. baseline 测试全部通过或有明确、已批准的行为变更记录（D1 记录在案）；
2. V4 新测试全部通过；
3. Ruff 全绿；
4. 不要求真实外部 API；
5. `DecisionReducer.reduce()` 调用计数 = item 数；
6. Rule match 调用计数 = item 数；
7. Resolver 不存在 catalog 第二事实通道；
8. post-decision 阶段无法修改业务裁决；
9. offline 伪装行为与 audit raw_records 结构与 `a70ea45` 完全一致。

---

## 17. Git 执行纪律

从 `a70ea45` 创建独立分支 `codex/fare-main-chain-v4`。每个阶段独立提交：

```text
V4-P0 baseline-freeze
V4-P1 stage-contracts-and-rule-before-acl
V4-P2 single-reduce
V4-P3 provider-fact-unification
V4-P4 findings-output
V4-P5a evaluator-mechanical-extraction
V4-P5b stage-decomposition
V4-P6 post-decision-stage
V4-P7 request-aggregation
V4-P8 acceptance-matrix
V4-P9 docs-cleanup
```

每一阶段：修改前检查 `git status`；只做本阶段内容；先跑专项测试再跑全量；PASS 后 commit；记录阶段 SHA；不把后续优化夹进修复阶段；最终验收前不做大规模格式化/无关重命名。

---

## 18. 本轮明确不扩展的产品范围

（与 V3 第 18 节一致，全部保留。）

不新增：NAT 分析、路由路径计算、策略自动下发、防火墙配置写入、新审批系统、新数据库架构、新 UI、新权限模型、新产品模块。

真实 ACL 合约、生产网段规划合约、生产规则审批属于外部集成事项，不应阻塞模拟主链验收。

---

## 19. 修复阶段完成后的项目优化方向

（O1~O12 沿用 V3 第 19 节，以下仅列 V4 调整与新增点。）

### O1. Provider Contract 正式化（吸收 D2 遗留）

TransportAdapter → ProviderResponseValidator → NetworkFactProvider → ProviderNetworkFact → Resolver。**offline 字段伪装（zone→area/areaId/regionName、environment→platformName）的清理在此阶段执行**：需独立行为变更评审（影响 `platform_name` 相关规则在 offline 模式下的匹配），配套 fixture 与验收 case 正式记录前后差异。

### O5. 统一 Stage Metrics（承接 P5b 的收集器）

统一 duration_ms / call_count / input_item_count / output_item_count / skip_count / failure_count / dependency_status。metrics 不参与业务 decision。

### O7. Property-Based / Metamorphic Test

四条性质（低优先级 finding 不改变 primary；乱序不影响 final primary；`affects_decision=False` 不改变 decision；post-decision 任意失败不改变 decision）——V4 完成后这些性质有了清晰的单元边界（Reducer / post_decision_stage），建议优先补齐。

### O12. 性能优化顺序（吸收 D4）

1. 消除重复 Rule match（V4-P1 已完成）；
2. 消除重复正式 reduce（V4-P2 已完成）；
3. **post-decision 三阶段并行化（V4 明确推迟至此）**——需先建立 `exceptions` 顺序无关的断言策略；
4. provider cache；5. ACL concurrency；6. semantic batch；7. 最后才考虑 RuleEngine 索引。

每一项性能优化必须保证 simulated golden output 100% 相同（O12.3 并行化还需保证 `exceptions` 内容集合不变）。

---

## 20. 最终完成定义（Definition of Done）

- [ ] `a70ea45` 基线已冻结（`docs/v3-baseline.md`，含 D1 反转记录与隐性输出冻结清单）；
- [ ] Rules 在 ACL 前执行；每 item RuleEngine.match 恰好一次；ACL gating 不再调用 RuleEngine；
- [ ] 每 item DecisionReducer.reduce 恰好一次；deterministic 快照与 final decision 在同一次 reducer 调用中形成；
- [ ] `llm_added_pending_count` 等全部 metrics 与 P0 冻结值逐场景一致；
- [ ] `DecisionTrace` schema 完全不变（含 `semantic_finding_ids`）；
- [ ] Resolver 不 import / 持有 NetworkCatalog；`legacy_entry` 完全删除；
- [ ] mock/http/offline 共享 typed provider fact contract；`ProviderNetworkFact = NetworkPlanFact + Optional classification`（无第四个重复事实模型）；
- [ ] offline 伪装字段与离线多命中行为冻结不变（D2）；
- [ ] Canonical 不做隐式推断；zone→area_id 回退保留；
- [ ] secondary findings 进入 API `decision_findings` 与 audit；脱敏覆盖新字段；
- [ ] `matched_rules` 兼容语义不变（ACL-PATH-001 仅 primary 时出现）；
- [ ] Evaluator 只负责编排（P5a/P5b 两步完成）；RequestDecisionAggregator 独立；
- [ ] Explanation / shadow 全部处于 post-decision 且**串行**执行（D4）；post-decision 无权修改 decision；
- [ ] `exceptions` 顺序确定性保持；
- [ ] V3-01~V3-33 模拟验收全绿；全量 pytest 全绿；Ruff 全绿；
- [ ] README / ARCHITECTURE / DECISION_MODEL 与真实代码一致；
- [ ] 最终验收不依赖任何真实外部 API。

---

## 21. 最终目标状态

V4 完成后，FARE 主链应具备一个非常清晰的原则：

> **外部依赖只提供事实；各业务阶段只生成 Finding；每个 item 的全部 Finding 最终只进入一次正式 DecisionReducer；裁决之后的任何 LLM 或解释能力都只能解释和观察，不能再次改变业务结论。**

这应作为后续所有功能、规则和外部接口扩展的架构约束。

---

## 附录 A：代码证据索引（`a70ea45`）

| 主题 | 位置 |
|---|---|
| 两次 reduce | `app/services/evaluator.py:574`、`:911`；AC-05 钉死断言 `tests/test_evaluator_orchestration.py:103-107` |
| ACL 内规则预匹配 | `app/services/evaluator.py:518`（gating）、`:570`（rules 阶段） |
| 第二事实通道 | `app/services/network_plan_resolver.py:21,80,91,547,577-583`；`app/services/canonical.py:98` |
| offline 伪装 | `app/services/network_plan_client.py:324-327`；usage_code 非伪装静态测试 `tests/test_legacy_cleanup.py:72-75` |
| 离线多命中 → 409 → INVALID_RESPONSE | `app/services/network_plan_client.py:308-316` + `app/services/network_plan_resolver.py:298-304` |
| zone→area_id 回退 | `app/services/rule_loader.py:155-158` |
| ACL-PATH-001 排除与注入 | `app/services/rule_loader.py:143`；`app/services/evaluator.py:677-685`、`:738-747` |
| item_id 铸造 | `app/services/evaluator.py:485` |
| llm_added_pending_count | `app/services/evaluator.py:905-921` |
| 语义 candidate_rule_ids 管线 | `app/services/evaluator.py:835-837` |
| 死代码 `_apply_semantic_failure` | `app/services/evaluator.py:773`（全仓无调用） |
| request 聚合内联 | `app/services/evaluator.py:434` |
| DecisionTrace 契约 | `app/schemas.py:210` |
| legacy_entry 测试锚点 | `tests/test_legacy_cleanup.py:88-89` |
| 脱敏链路 | `app/main.py:82` |
| 禁网守卫 | `tests/conftest.py::block_real_network` |
