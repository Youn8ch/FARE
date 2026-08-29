# FARE 架构收口修复计划 V2.1（校准版）

> 版本：V2.1（校准版，2026-08-30）  
> 目的：在不新增业务功能的前提下，将 FARE 收口为 **唯一事实模型、唯一处理主链路、唯一裁决入口、唯一默认运行方式**。  
> 适用环境：测试 / 开发环境。  
> 本版新增硬性要求：**每一个阶段都必须有模拟测试输入、阶段限制规则、预期输出和输出断言；不能只以“pytest 通过”作为验收。**  
> V2.1 变更：基于当前工作区代码审计校准——配置语境修正（YAML 单配置，`from_env` 已删除）、补入 CLI/evals 入口、修正 limit 现状描述、新增执行前置与 CASE 挂靠映射。详见第 0 章。

---

# 0. V2.1 校准说明与执行前置

## 0.1 相对 V2 的修正（依据 2026-08-30 代码审计）

| # | V2 原文 | 修正 | 依据 |
|---|---|---|---|
| C1 | AC-07 围绕 `Settings.from_env()` 与环境变量默认值展开 | 工作区已删除 `from_env()`，改为单 YAML 配置（`app/config.py` 无任何 `os.getenv`；`config/fare.yaml` + `python -m app serve --config`）。AC-07 全章重写为 YAML 语境 | app/config.py、README |
| C2 | 默认值矛盾表述为「Settings() vs from_env()」 | 矛盾现形态：`Settings.acl_decision_mode` 默认 `"required"`（config.py:290）vs YAML schema `_AclConfig.decision_mode` 默认 `"advisory"`（config.py:132）；conftest 未传 `network_plan_client_mode`，落到 dataclass 默认 `offline_catalog`（config.py:278） | app/config.py、tests/conftest.py |
| C3 | 只覆盖 HTTP API 入口 | 补入第二/第三入口：`python -m app requirements`（app/requirement_runner.py:104,118 直接 `build_runtime` + `runtime.evaluate_request`）与 `evals/llm/run_real_*.py`。三者共享同一 Runtime，所有触及裁决的阶段的回归矩阵必须覆盖 CLI 批量链路 | app/requirement_runner.py、evals/llm/ |
| C4 | 「offline 绕过 query/item limit」 | 现状：item limit 两条链路均已强制（evaluator.py:414-415 与 425-428）；仅 query limit 被 offline 绕过（main.py:42-43）。AC-02 只需收口 query limit | app/main.py、app/services/evaluator.py |
| C5 | CASE-01~12 独立命名，未提现有测试资产 | 现有 `tests/test_evaluation_cases.py` + `tests/cases/evaluations/*.v2.json` 已是数据驱动表征套件（core 14 例）。新增 3.13 挂靠映射，AC-00 起复用，禁止另建并行基线 | tests/cases/evaluations/ |
| C6 | 回退点「main SHA」 | 工作区为未提交的大重构中间态（仅 3 个 commit，59 个文件变更）。新增 0.2 节执行前置：先固化 `PRE_CONVERGENCE_SHA`，否则全部回退点失效 | git status / log |
| C7 | 未提密钥 | 五份 config/*.yaml 均内嵌明文 LLM `api_key`；config/ 当前未跟踪——尚未泄漏，但若按原样首次提交即入库。新增 0.2 节步骤 A，先于首次提交处理 | config/fare.yaml:70 等 |
| C8 | CASE-11（policy_gap 降级）按目标态直接写 | 现状多数 policy_gap 类型默认 `review_required`（会执行 合规→待定，evaluator.py:899-925；config.py:182-203）。这是业务行为变更，AC-06 实施前需业务方书面确认 | app/config.py、app/services/evaluator.py |
| C9 | CASE-06 断言 `source network status = not_applicable` | 仅 resolver（mock/http）链路成立；offline_catalog 的 CatalogSegment 无事实状态概念（`_segment_status` 兜底 `"complete"`，evaluator.py:1131-1132）。已在 CASE-06 加注 | app/services/evaluator.py |
| C10 | — | 新增 AC-08 清理项：`LlmClient.review` 死代码（llm_client.py:420，全仓无调用方）、offline client 将 `entry.object_type` 冒充 `usageCode` 的 shim（network_plan_client.py:332）、README 对已删除方案文档的悬空引用、ResolvedAddressSegment 上的 legacy shim 属性 | 各处 |

## 0.2 执行前置（必须先于 AC-00 完成）

### 步骤 A：密钥出库（先于首次提交）

```text
A1. 确认密钥从未入库：git log --all -- config/ 应为空（当前 config/ 未跟踪）。
A2. 五份 config/*.yaml 的 llm.http.api_key 全部置空（schema 允许 None，
    config.py:152；mock 模式不受影响）。
    真实密钥移入 config/fare.local.yaml（复制对应 profile 后填写），
    并在 .gitignore 增加 config/fare.local.yaml。
A3. README 增加说明：http 模式部署时从本地未跟踪副本读取密钥。
A4. 由于密钥从未提交，无需轮换；但 A1 必须留档确认。
```

### 步骤 B：固化工作区

```text
B1. git add -A && git commit（不含 fare.local.yaml），
    消息注明“V2.1 校准前工作区固化”。
B2. 记录该 SHA 为 PRE_CONVERGENCE_SHA。
B3. 之后所有 AC 阶段的“回退到上一 stable SHA”均以本系列提交为准；
    AC-00 的回退点 = PRE_CONVERGENCE_SHA。
```

步骤 A/B 完成并验证 `pytest`、`ruff` 全绿后，才允许开始 AC-00。

---

# 1. 本轮最终目标

目标主链路：

```text
EvaluationRequest
        │
        ▼
Normalize / Cardinality
        │
        ▼
NetworkFactProvider
        │
        ▼
NetworkPlanResolver
        │
        ▼
CanonicalNetworkFact
        │
        ▼
CanonicalAddressSegment
        │
        ▼
AccessCombination
        │
        ├───────────────┐
        ▼               ▼
    RuleEngine       AclEnricher
        │               │
        └───────┬───────┘
                ▼
             Findings
                │
                ▼
        SemanticReviewer
                │
                ▼
          SemanticGuard
                │
                ▼
          DecisionReducer
                │
                ▼
       合规 / 待定 + 主原因
                │
                ▼
      Optional Explanation
                │
                ▼
       ResponseAssembler
                │
                ▼
            AuditStore
```

最终必须消除四类双轨状态：

```text
NetworkCatalog / NetworkPlan 双事实模型
                ↓
offline / API 两套处理主链路
                ↓
network / rule / ACL / LLM 多裁决入口
                ↓
Settings dataclass / YAML schema / tests / README 多套默认行为
```

入口一致性（V2.1 补充）：HTTP API、`python -m app requirements` CLI 批量、
evals 脚本三条入口共享同一 Runtime（`runtime.evaluate_request`）。
收口后三条入口行为必须同源，回归矩阵必须覆盖三条入口。

---

# 2. 全阶段统一测试纪律

从 AC-00 开始，任何阶段都必须同时满足以下四项。

## 2.1 模拟测试输入

每阶段必须明确：

```text
输入 Request
Network Plan 模拟响应
ACL 模拟响应
LLM 模拟响应
阶段配置
```

不能只写：

```text
“测试 network plan”
“测试 ACL”
“测试 LLM”
```

必须给出能够复现的具体输入。

---

## 2.2 阶段限制规则

每阶段必须声明：

```text
本阶段允许改变什么
本阶段禁止改变什么
哪些业务结论必须保持不变
哪些字段必须保持兼容
哪些调用次数不得增加
```

如果实际修改突破限制规则：

```text
阶段测试即使全绿也不能提交。
```

---

## 2.3 预期输出

至少验证：

```text
HTTP status
decision
reason_type
reason_code
matched_rules
access.source
access.destination
network fact status
network fact ids
acl_verification_status
依赖调用次数
```

涉及 Runtime 行为的阶段（AC-02 / AC-04 / AC-05 / AC-06），还必须对
CLI 批量链路（requirement_runner + tests/cases/network_requirements 抽样）
输出结论分布对比。

涉及内部架构阶段时，还必须验证：

```text
实际对象类型
实际调用链
Finding 列表
DecisionReducer 主 finding
旧符号是否仍被主路径引用
```

---

## 2.4 输出断言

禁止仅使用：

```python
assert response.status_code == 200
```

至少应增加关键字段精确断言，例如：

```python
assert item["decision"] == "待定"
assert item["reason_code"] == "NETWORK_PLAN_NOT_FOUND"
assert item["source_network_fact_status"] == "not_found"
assert item["acl_verification_status"] == "skipped"
```

若是结构性阶段：

```python
assert isinstance(segment, CanonicalAddressSegment)
assert provider.calls == ["16.201.1.0/24"]
assert decision.primary_finding.code == "PORT-001"
```

---

# 3. 统一模拟数据集

后续所有阶段尽量复用同一批 fixture，避免每阶段重新发明样例。

当前 `EvaluationRequest` 示例采用：

```json
{
  "request_id": "ac-case-001",
  "sources": [
    {
      "address": "16.201.1.10",
      "description": "应用"
    }
  ],
  "destinations": [
    {
      "address": "16.220.16.20",
      "description": "数据库"
    }
  ],
  "protocol": "tcp",
  "ports": [
    {
      "start": 443,
      "end": 443
    }
  ],
  "request_description": "HTTPS 访问"
}
```

以下案例作为架构收口的统一测试集。

---

## CASE-01：单 IP 正常访问

### Request

```json
{
  "request_id": "ac-case-01",
  "sources": [{"address": "16.201.1.10", "description": "应用"}],
  "destinations": [{"address": "16.220.16.20", "description": "数据库"}],
  "protocol": "tcp",
  "ports": [{"start": 443, "end": 443}],
  "request_description": "HTTPS 访问"
}
```

### Network Plan

```text
16.201.1.0/24 -> resolved
areaId = 鳌峰科创生产区
regionName = 鳌峰生产区（科创鲲鹏应用）
network = 16.201.0.0/22

16.220.16.0/24 -> resolved
areaId = 核心生产区
regionName = 核心生产数据库区
usageCode = PROD_DATABASE
```

### 最低预期

```text
HTTP = 200
source access = 16.201.1.10/32
source fact status = complete
destination fact status = complete
Network Plan lookup = 2
真实访问范围不得扩大为 /24
```

---

## CASE-02：Network Plan 未规划

### Request

```text
source = 16.201.3.10
destination = 16.220.16.20
tcp/443
```

### Network Plan

```text
16.201.3.0/24
provider body code = 404
success = false
```

### 预期

```text
HTTP = 200
decision = 待定
reason_code = NETWORK_PLAN_NOT_FOUND
source_network_fact_status = not_found
acl_verification_status = skipped
ACL call = 0
```

---

## CASE-03：查询上限

配置：

```text
network_plan_max_subnets_per_request = 1
```

输入：

```text
source 16.201.1.10
destination 16.220.16.20
```

实际唯一 `/24`：

```text
2
```

预期：

```text
HTTP = 422
error.code = NETWORK_PLAN_QUERY_LIMIT_EXCEEDED
actual = 2
limit = 1
Network Plan call = 0
ACL call = 0
LLM call = 0
audit claim = 0
```

---

## CASE-04：Item 上限

配置：

```text
max_evaluation_items = 3
```

Request：

```text
sources:
- 16.201.1.10
- 16.201.1.20

destinations:
- 16.220.16.20
- 16.220.16.30

ports:
- tcp/443
```

组合：

```text
2 × 2 × 1 = 4
```

预期：

```text
HTTP = 422
error.code = EVALUATION_ITEM_LIMIT_EXCEEDED
ACL call = 0
semantic call = 0
重复提交不得卡在 evaluation_in_progress
```

---

## CASE-05：正式规则 PORT-001

Request：

```json
{
  "request_id": "ac-case-05",
  "sources": [{"address": "16.201.1.10", "description": "应用"}],
  "destinations": [{"address": "16.220.16.20", "description": "设备"}],
  "protocol": "tcp",
  "ports": [{"start": 23, "end": 23}],
  "request_description": "Telnet 管理"
}
```

预期：

```text
decision = 待定
reason_type = policy_violation
reason_code / primary rule = PORT-001
matched_rules 包含 PORT-001
```

---

## CASE-06：any 地址

Request：

```text
source = any
destination = 16.220.16.20
tcp/443
```

预期：

```text
decision = 待定
matched_rules 包含 LEAST-ANY-001
source network status = not_applicable
不得把 any 发送给 Network Plan
```

注意（V2.1）：`not_applicable` 仅在 resolver（mock/http）链路成立；
offline_catalog 模式下 CatalogSegment 无事实状态概念（`_segment_status`
兜底 `"complete"`）。AC-00 表征时按实际链路记录，不得混写。

ACL 是否调用：

```text
AC-00 保留当前行为并记录
AC-06 根据门控策略明确最终行为
```

---

## CASE-07：端口范围过大

Request：

```text
tcp/1-101
```

预期：

```text
decision = 待定
matched_rules 包含 LEAST-PORT-001
```

---

## CASE-08：ACL advisory / required

模拟 ACL：

```text
ACL client dependency failure
```

### advisory

预期：

```text
acl_verification_status = unverified
如果无其他问题 -> decision = 合规
```

### required

预期：

```text
decision = 待定
reason_code = ACL_DEPENDENCY_FAILURE
```

---

## CASE-09：ACL 明确无路径

ACL mock：

```text
explicit_no_path = true
```

预期：

```text
decision = 待定
matched_rules 包含 ACL-PATH-001
reason_type = acl_no_path
```

---

## CASE-10：Semantic 权威事实冲突

Network fact：

```text
source area_id = 鳌峰科创生产区
```

LLM claim：

```text
source_zone / authoritative semantic claim 与权威事实冲突
```

且 evidence 可验证。

预期：

```text
semantic finding = conflict
若原 deterministic decision 为合规：
最终 decision = 待定
```

---

## CASE-11：Semantic policy_gap

LLM：

```text
返回 evidence-bound policy_gap
```

V2 收口后的目标预期：

```text
policy_gap 保留为 candidate / informational finding
不得单独执行 合规 -> 待定
```

【V2.1 业务确认点】现状多数 policy_gap 类型默认 `review_required`
（会执行 合规 -> 待定）。本条目标态是一次业务行为变更，AC-06 实施前
必须取得业务方书面确认；未确认前此预期冻结，不得实施。

---

## CASE-12：跨区域 CIDR

Request：

```text
source = 一个跨两个 /24 且 Network Plan 属性不同的 CIDR
destination = 16.220.16.20
tcp/443
```

预期：

```text
查询粒度 = /24
访问范围仅按真实申请范围拆分
不同区域必须拆 segment
同事实连续区域才允许聚合
不得扩大到整个查询 /24
```

---

## 3.13 CASE 与现有测试资产映射（V2.1 新增）

现有 `tests/test_evaluation_cases.py` + `tests/cases/evaluations/*.v2.json`
已是数据驱动表征套件（core 14 例 + explanation / llm_pipeline /
acl_candidates / request_findings 各套件）。CASE-01~12 必须挂靠该体系，
禁止另建并行基线。

| CASE | 现有挂靠点 | 缺口动作 |
|---|---|---|
| CASE-01 | `compliant_https_v2`（offline 链路）；`tests/test_network_plan_evaluation.py`（mock 链路 16.201.1.10，已断言 NPF-10C90100） | 无需新建，AC-00 直接表征 |
| CASE-02 | `unknown_catalog_address_v2`（offline 等价）；test_network_plan_evaluation.py 404 分支（resolver 链路） | 无需新建 |
| CASE-03 | 错误码断言在 main.py:44-50；核对 tests/test_config_limits.py 覆盖度 | 不足则补入 v2 套件 |
| CASE-04 | main.py:73-79；`two_by_two_cartesian_product_v2` 是组合展开表征（非超限） | 超限用例若缺则补 |
| CASE-05 | `tcp_23_is_rejected_v2`；near-miss：`udp_23_does_not_match_tcp_rule_v2` | 无需新建 |
| CASE-06 | `any_address_v2` | ACL 调用与否按 AC-00 表征记录 |
| CASE-07 | `port_span_101_is_rejected_v2`（命中）/ `port_span_100_is_allowed_v2`（near-miss） | 无需新建 |
| CASE-08 | `acl_firewall_unresolved_v2` 覆盖 required 依赖失败路径 | advisory 分支表征若缺则补 |
| CASE-09 | `acl_explicit_no_path_v2` | 无需新建 |
| CASE-10 | `llm_pipeline.v2.json` | 无需新建 |
| CASE-11 | 无 | 新增（挂入 llm_pipeline 套件） |
| CASE-12 | fixture `tests/fixtures/network_plan/multi_region.v1.json` 已具备跨区数据 | 断言用例若缺则补 |
| OBJECT-001 | `object_office_to_production_database_v2`（仅在 offline_catalog 模式可达，正是 7.4 收口决策的现状佐证） | AC-03 按 7.4 处置 |

---

# 4. AC-00：锁定当前行为基线

## 4.1 目标

只记录现状，不改变业务逻辑。

---

## 4.2 修改范围

允许：

```text
tests/
tests/fixtures/
docs/architecture-baseline.md
```

禁止：

```text
app/ 全部（含 evaluator.py、rule_loader.py、network_plan_resolver.py、
        main.py、requirement_runner.py、config.py）
policies/ 全部
config/ 全部
```

---

## 4.3 本阶段模拟测试输入

必须执行：

```text
CASE-01
CASE-02
CASE-03
CASE-04
CASE-05
CASE-06
CASE-08
CASE-09
CASE-10
CASE-11
```

此外增加：

```text
重复相同 request_id + 相同 payload
相同 request_id + 不同 payload
```

CLI 批量链路基线（V2.1 新增）：

```text
requirement_runner + tests/cases/network_requirements 抽样（≥2 个 batch），
记录结论分布与输出文件摘要，作为 AC-05 / AC-06 的对照基线。
```

---

## 4.4 本阶段限制规则

```text
1. 不修改任何现有 decision 逻辑。
2. 不修改 rule priority。
3. 不修改 ACL 调用条件。
4. 不修改 LLM guard。
5. 不修改 Settings 默认值。
6. 发现异常只记录为 characterization test。
```

---

## 4.5 预期输出验证

建立基线快照：

```text
CASE
HTTP
item count
decision
reason_type
reason_code
matched_rules
network status
ACL status
Network calls
ACL calls
Semantic calls
Explanation calls
CLI 批量结论分布（V2.1 新增）
```

例如 CASE-02 必须锁定：

```python
assert item["decision"] == "待定"
assert item["reason_code"] == "NETWORK_PLAN_NOT_FOUND"
assert item["source_network_fact_status"] == "not_found"
assert item["acl_verification_status"] == "skipped"
```

---

## 4.6 测试命令

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q
.\.venv\Scripts\python.exe -m ruff check . --no-cache
```

---

## 4.7 验收条件

```text
所有当前主行为都已有 characterization test。
已知问题用显式测试记录。
本阶段无业务代码变化。
```

---

## 4.8 回退点

```text
回退到 AC-00 开始前 PRE_CONVERGENCE_SHA（第 0.2 节固化提交）。
```

---

# 5. AC-01：建立 Canonical Network Fact

## 5.1 目标

引入：

```text
CanonicalNetworkFact
CanonicalAddressSegment
```

使后续代码不再同时理解：

```text
CatalogSegment
ResolvedAddressSegment
```

---

## 5.2 本阶段模拟测试输入

### 输入 A：完整 Network Plan

使用 CASE-01 Network Plan 数据。

预期 CanonicalNetworkFact：

```text
area_id = 鳌峰科创生产区
region_name = 鳌峰生产区（科创鲲鹏应用）
platform_name = 鳌峰生产区（科创鲲鹏应用）
network = 16.201.0.0/22
usage_code = null
```

### 输入 B：usageCode = PROD_DATABASE

使用：

```text
16.220.16.0/24
usageCode = PROD_DATABASE
description = 生产数据库服务器
```

### 输入 C：usageCode 为空

使用：

```text
16.201.1.0/24
usageCode = null
```

---

## 5.3 本阶段限制规则

```text
1. Canonical Fact 只做规范化，不做业务推断。
2. 禁止：
   platform_name -> environment
3. 禁止：
   usage_code -> object_type
   除非存在正式显式 mapping。
4. 禁止从 description 推断 object_type/environment。
5. 不改变当前最终 decision。
6. 不修改 LLM 行为。
7. 不删除 NetworkCatalog。
```

---

## 5.4 预期输出

输入 B：

```text
CanonicalNetworkFact.usage_code = PROD_DATABASE
CanonicalNetworkFact.object_type = None
CanonicalNetworkFact.environment = None
```

在没有正式映射前，禁止出现：

```text
object_type = database
environment = production
```

仅因为字符串中包含 `PROD_DATABASE` 或“生产数据库”。

输入 CASE-01：

```text
access.source = 16.201.1.10/32
```

不得变成：

```text
16.201.1.0/24
```

---

## 5.5 输出断言

```python
assert fact.usage_code == "PROD_DATABASE"
assert fact.object_type is None
assert fact.environment is None

assert segment.access_network == "16.201.1.10/32"
assert segment.network_fact_ids == ["NPF-10C90100"]
```

并增加类型断言：

```python
assert isinstance(combination.source, CanonicalAddressSegment)
assert isinstance(combination.destination, CanonicalAddressSegment)
```

---

## 5.6 回归测试

执行：

```text
CASE-01
CASE-02
CASE-12
```

确保决策输出与 AC-00 基线一致。

---

## 5.7 回退点

```text
AC-00 stable SHA
```

如果 Canonical Fact 需要猜测字段才能兼容旧规则：

```text
立即停止 AC-01，不进入 AC-02。
```

---

# 6. AC-02：统一 NetworkFactProvider 主链路

## 6.1 目标

将：

```text
mock/http -> resolver
offline_catalog -> legacy split
```

统一为：

```text
provider -> resolver -> canonical segment
```

---

## 6.2 本阶段模拟测试输入

为同一个 CASE-01 构造两种 provider：

### Mock provider

```text
16.201.1.0/24 -> API-shaped response
16.220.16.0/24 -> API-shaped response
```

### Offline provider

从 legacy catalog 构造等价事实。

同一个 EvaluationRequest：

```text
CASE-01
```

---

## 6.3 本阶段限制规则

```text
1. provider 可以不同。
2. resolver 必须唯一。
3. segmentation 必须唯一。
4. RuleEngine 不得根据 provider 类型分支。
5. offline compatibility 不得绕过 query limit。
   （V2.1 现状修正：item limit 两条链路均已强制——evaluator.py:414-415
   与 425-428；仅 query limit 被 offline 绕过——main.py:42-43。
   本阶段随统一主链路自然收口 query limit，item limit 保持即可。）
6. 不改变正式规则。
7. 不改 DecisionReducer；当前尚未引入。
```

---

## 6.4 预期输出

Mock 与 Offline 对同一等价事实：

```text
access.source 相同
access.destination 相同
network status 相同
segment count 相同
decision 相同
reason_code 相同
```

允许差异：

```text
provider-specific fact_id
provider metadata
```

不允许：

```text
offline_catalog 产生一套 segment
mock 产生另一套 segment
```

---

## 6.5 输出断言

```python
mock_result.items[0].access == offline_result.items[0].access
mock_result.items[0].decision == offline_result.items[0].decision
```

内部：

```python
assert runtime.network_plan_resolver is not None
```

三种 mode 都成立：

```text
mock
http
offline_catalog
```

代码验收：

```text
正式 runtime 不再通过 resolver=None 进入 legacy split。
```

---

## 6.6 限制测试

执行：

```text
CASE-03 query limit
CASE-04 item limit
```

对 mock/offline 均验证。

---

## 6.7 回退点

```text
AC-01 stable SHA
```

如果 offline 无法映射：

```text
允许 Provider 输出 optional None；
禁止恢复 RuleEngine 直接读取 catalog。
```

---

# 7. AC-03：RuleEngine 收口与规则可达性

## 7.1 目标

RuleEngine 只认：

```text
Canonical facts
AccessCombination
```

同时处理 `OBJECT-001` 事实来源问题。

---

## 7.2 本阶段模拟测试输入

必须执行：

```text
CASE-05 PORT-001
CASE-06 LEAST-ANY-001
CASE-07 LEAST-PORT-001
CASE-09 ACL-PATH-001
```

再加入：

### OBJECT-001 模拟输入 1：无正式 object mapping

Network facts：

```text
source usageCode = OFFICE_CLIENT
destination usageCode = PROD_DATABASE
```

但：

```text
object_type = None
environment = None
```

预期：

```text
OBJECT-001 不得命中。
```

### OBJECT-001 模拟输入 2：仅 fixture 中显式提供 canonical fields

```text
source.object_type = endpoint
destination.object_type = database
destination.environment = production
```

预期：

```text
OBJECT-001 命中。
```

---

## 7.3 本阶段限制规则

```text
1. 禁止从 description 猜规则字段。
2. 禁止隐式 usageCode 字符串匹配 object_type。
3. 禁止 platformName 自动映射 environment。
4. 默认正式 rule package 中的启用规则必须具备主路径事实来源。
5. RuleEngine 不得访问 .entry。
6. RuleEngine 不得 import NetworkCatalog。
```

---

## 7.4 OBJECT-001 收口决策

若当前无正式映射字典：

```text
默认正式规则包暂时禁用 OBJECT-001。
```

V2.1 现状注：代码中不存在任何 usageCode / platformName / description 到
object_type / environment 的推断逻辑；object_type 唯一来源是 catalog YAML。
offline client 将 entry.object_type 冒充为 usageCode（network_plan_client.py:332），
该 shim 属兼容层，列入 AC-08 清理。object_office_to_production_database_v2
仅在 offline_catalog 模式可达，正是本决策的现状佐证。

但保留 fixture 测试 RuleEngine 的 `object_relation` 能力。

只有取得正式数据字典后，才允许启用显式 mapping。

---

## 7.5 预期输出

CASE-05：

```text
decision = 待定
matched_rules contains PORT-001
```

CASE-06：

```text
decision = 待定
matched_rules contains LEAST-ANY-001
```

OBJECT-001 无映射：

```text
matched_rules not contains OBJECT-001
```

OBJECT-001 显式 canonical facts：

```text
matched_rules contains OBJECT-001
```

---

## 7.6 规则可达性测试

新增：

```text
default active rule -> 至少一个 positive main-path case
default active rule -> 至少一个 near-miss case
```

例如 PORT-001：

```text
tcp/23 -> match
tcp/22 -> no match
```

LEAST-PORT：

```text
1-101 -> match
1-100 -> no match
```

---

## 7.7 回退点

```text
AC-02 stable SHA
```

如果默认规则存在无事实来源项：

```text
不能以“测试暂时跳过”通过阶段验收。
必须禁用该规则或补充正式事实来源。
```

---

# 8. AC-04：Finding + DecisionReducer

## 8.1 目标

network / rule / ACL / semantic 都只生成 Finding。

最终：

```text
DecisionReducer
```

是唯一正式裁决入口。

---

## 8.2 模拟 Finding 输入

直接构造内部测试，不依赖 HTTP。

### F-01

```text
network = NETWORK_PLAN_NOT_FOUND
rule = PORT-001
acl = ACL-PATH-001
semantic = conflict
```

预期主原因：

```text
NETWORK_PLAN_NOT_FOUND
```

### F-02

```text
network = OK
rule = PORT-001
acl = ACL-PATH-001
semantic = conflict
```

预期：

```text
PORT-001
```

### F-03

```text
network = OK
rule = none
acl = ACL-PATH-001
semantic = conflict
```

预期：

```text
ACL-PATH-001 / acl_no_path
```

### F-04

```text
network = OK
rule = none
acl = OK
semantic = authoritative conflict
```

预期：

```text
semantic conflict
decision = 待定
```

### F-05

```text
only semantic policy_gap candidate
```

预期目标：

```text
decision = 合规
```

---

## 8.3 本阶段限制规则

```text
1. 只有 DecisionReducer 可以决定 decision。
2. secondary finding 不得覆盖 higher-priority primary finding。
3. matched_rules 必须完整保留。
4. finding 顺序必须稳定。
5. 本阶段不拆 Evaluator 大结构。
6. 不修改 Provider。
```

---

## 8.4 预期输出

DecisionReducer 输出至少包括：

```text
decision
primary_finding
reason_type
reason_code
matched_rules
all_findings
```

示例 F-01：

```text
decision = 待定
primary_finding.code = NETWORK_PLAN_NOT_FOUND
all_findings contains:
- NETWORK_PLAN_NOT_FOUND
- PORT-001
- ACL-PATH-001
- SEMANTIC_FACT_CONFLICT
```

---

## 8.5 输出断言

```python
assert result.decision == "待定"
assert result.primary_finding.code == "NETWORK_PLAN_NOT_FOUND"
assert {f.code for f in result.findings} >= {
    "NETWORK_PLAN_NOT_FOUND",
    "PORT-001",
}
```

然后用真实 API 再执行：

```text
CASE-02
CASE-05
CASE-08
CASE-09
CASE-10
CASE-11
```

---

## 8.6 回退点

```text
AC-03 stable SHA
```

本阶段必须单独提交，禁止与 AC-05 同时完成。

---

# 9. AC-05：Evaluator 瘦身为编排器

## 9.1 目标

Evaluator 只表达：

```text
plan
resolve
enrich
rules
semantic
reduce
explain
assemble
```

---

## 9.2 本阶段模拟测试输入

复用：

```text
CASE-01 正常
CASE-02 network fail
CASE-04 item limit
CASE-05 deterministic rule
CASE-08 ACL failure
CASE-10 semantic conflict
```

另外使用 Recording Fake：

```text
RecordingNetworkProvider
RecordingAclClient
RecordingLlmClient
RecordingDecisionReducer
```

---

## 9.3 本阶段限制规则

```text
1. 不改变 AC-04 已冻结的 DecisionReducer 优先级。
2. 不改变正式 response schema。
3. 不改变 rule result。
4. 不增加默认 LLM 调用次数。
5. 不扩大 ACL 调用次数。
6. 仅做职责迁移。
```

---

## 9.4 预期调用链输出

CASE-01：

```text
network.resolve = 1 batch
ACL = 每 applicable item 最多 1
semantic = 1 batch
reducer = 每 item 1
explanation = semantic 成功后按当前设计
```

CASE-02：

```text
network -> finding -> reducer
ACL = 0
```

CASE-04：

```text
cardinality -> 422
ACL = 0
LLM = 0
```

---

## 9.5 输出断言

Recording calls：

```python
assert calls == [
    "plan",
    "network",
    "acl",
    "rules",
    "semantic",
    "reduce",
    "explain",
    "assemble",
]
```

对于失败路径允许提前终止，但必须有明确测试，例如：

```python
assert "acl" not in calls
assert "semantic" not in calls
```

---

## 9.6 API 输出验证

AC-00 的所有 characterization cases 必须重新执行。

除前面明确批准的行为变更外：

```text
JSON 关键字段不得变化。
CLI 批量链路结论分布与 AC-00 基线一致。（V2.1 新增）
```

---

## 9.7 回退点

```text
AC-04 stable SHA
```

如果拆分导致大量互相 mock 私有实现：

```text
优先合并过细模块，而不是增加更多 facade。
```

---

# 10. AC-06：ACL 与 LLM 职责收口

## 10.1 目标

收紧：

```text
ACL 是否值得调用
LLM 哪些结果允许影响 decision
```

---

## 10.2 本阶段模拟测试输入

### ACL-01

```text
CASE-02 network not found
```

预期：

```text
ACL call = 0
```

### ACL-02

```text
CASE-06 any
```

如果最终批准：

```text
LEAST-ANY-001 已足够决定待定
且 ACL 不作为必要证据
```

则预期：

```text
ACL call = 0
```

若业务仍要求收集候选路径：

```text
明确保留 call = 1
```

计划执行时必须二选一并测试固定，禁止含糊。

### LLM-01

```text
CASE-10 semantic authoritative conflict
```

预期：

```text
affects_decision = true
合规 -> 待定
```

### LLM-02

```text
CASE-11 policy_gap
```

预期：

```text
affects_decision = false
decision 保持 deterministic/ACL 结果
```

### LLM-03

fabricated rule：

```text
candidate_rule_ids = ["NON-EXISTENT-999"]
```

预期：

```text
semantic batch rejected / failure handling
不能将虚构规则写入 matched_rules
```

---

## 10.3 本阶段限制规则

```text
1. LLM 永远不能执行 待定 -> 合规。
2. policy_gap 默认不能独立执行 合规 -> 待定。
3. authoritative structured conflict 可以影响 decision。
4. ACL candidate 保持 shadow。
5. request findings 保持 shadow。
6. 本轮不得新增第五个 LLM 阶段。
```

---

## 10.4 预期输出

CASE-11：

```text
decision = 合规
semantic finding = policy_gap candidate
reason_code = null
```

【业务确认点】本预期改变现有 policy_gap 默认行为（review_required），
实施前必须取得业务方书面确认（见第 3 章 CASE-11 注与第 0.1 节 C8）。

前提：

```text
没有其他 rule/network/ACL finding。
```

CASE-10：

```text
decision = 待定
reason_type = fact_conflict
semantic conflict retained
```

---

## 10.5 调用次数断言

默认：

```text
ACL candidate mode=off -> 0 LLM candidate call
request findings mode=off -> 0 request-finding call
```

Shadow：

```text
对应 stage = 1 batch
正式 decision 不变化
```

---

## 10.6 回退点

```text
AC-05 stable SHA
```

policy_gap 行为修改必须独立可 revert。

---

# 11. AC-07：统一 Settings、Runtime、Tests 默认行为

## 11.1 目标

（V2.1 重写：工作区已删除 `Settings.from_env()` 与全部环境变量配置，
改为单 YAML 配置 `config/*.yaml` + `python -m app serve --config`。
本阶段验收对象从“环境变量默认”改为以下四个默认入口。）

四个必须一致的默认入口：

```text
1. Settings dataclass 默认（直接构造 Settings(...)）
2. YAML schema 默认（profile 中省略可省键时落到的 pydantic 默认）
3. tests/conftest.py settings fixture
4. config/fare.yaml（dev profile）+ README 启动命令
```

dev/test 默认目标值（YAML 键名）：

```text
network_plan.mode = mock（并显式绑定 versioned fixture）
acl.mode = mock
acl.decision_mode = advisory
llm.mode = mock
llm.features.acl_candidate_mode = off
llm.features.request_findings_mode = off
```

offline_catalog 与 acl.decision_mode=required 必须在各 profile 中显式指定，
不得作为任何隐式默认存在。

若最终项目决定采用其他值：四个入口仍必须相同。

已知不一致（本阶段消除）：

```text
Settings.acl_decision_mode 默认 "required"（config.py:290）
  vs YAML schema 默认 "advisory"（config.py:132）
conftest 未传 network_plan_client_mode，落到 dataclass 默认
  "offline_catalog"（config.py:278）
```

---

## 11.2 本阶段模拟测试输入

入口一，直接构造：

```text
Settings(...)
```

入口二，YAML schema 默认：

```text
构造省略可省键的最小 profile，经 FareConfig.load() 解析，
核对落到的 pydantic 默认值
```

入口三，pytest fixture：

```text
tests/conftest.py settings fixture（改为显式声明全部 mode 键）
```

入口四，README 启动方式：

```text
.\.venv\Scripts\python.exe -m app serve --config config/fare.yaml
```

再执行：

```text
CASE-01
```

---

## 11.3 本阶段限制规则

```text
1. 禁止 Settings dataclass 默认与 YAML schema 默认不一致。
2. conftest fixture 必须显式声明全部 mode / decision_mode / features 键，
   禁止依赖 dataclass 默认值。
3. offline_catalog 必须显式指定。
4. acl.decision_mode=required 必须显式指定。
5. README 示例不能依赖隐藏本地配置（含未跟踪的 fare.local.yaml 密钥副本）。
6. 配置一致性测试必须覆盖全部五份 config/*.yaml profile。
```

---

## 11.4 预期输出

四个默认入口：

```text
network plan mode 相同
acl mode 相同
acl decision mode 相同
llm mode 相同
features off/off 相同
```

README 启动验收：

```text
按 README 命令在干净环境启动，文档示例请求可复现，
不依赖未提交的本地密钥 / fixture。
```

因此 dev 默认必须：

```text
明确绑定 versioned network_plan fixture
```

或者 README 明确要求设置该 fixture。

---

## 11.5 输出断言

```python
assert settings_direct.acl_decision_mode == yaml_schema_default.acl_decision_mode
assert settings_direct.network_plan_client_mode == "mock"   # dev 默认，或最终决议值
assert conftest_settings.acl_decision_mode == "advisory"
```

核心 E2E fixture（dev 默认 mock 模式下）：

```python
assert client.app.state.runtime.network_plan_resolver is not None
```

注意（V2.1）：offline_catalog 模式下 `runtime.network_plan_resolver` 为 None
是设计内行为（main.py:210-223 compatibility mode），该断言只在 mock/http
默认下适用；AC-02 完成后三种 mode 均有 resolver，断言改为全模式成立。

---

## 11.6 回退点

```text
AC-06 stable SHA
```

历史测试若失败：

```text
改测试为显式 compatibility mode；
禁止重新制造双默认。
```

---

# 12. AC-08：删除 Legacy 与文档收口

## 12.1 目标

在替代路径全部稳定后删除：

```text
legacy split（split_request 主路径引用）
旧 runtime branch（resolver=None compatibility，main.py:210-223）
未使用兼容符号
失效文档
```

V2.1 增补清理项：

```text
LlmClient.review 死代码（llm_client.py:420，全仓无调用方）
offline client 的 usageCode 冒充 shim（network_plan_client.py:332）
  —— AC-03 完成事实来源收口后删除
README 对已删除方案文档的悬空引用
ResolvedAddressSegment 上的 legacy shim 属性
  （original/network/matches/entry，network_plan_resolver.py:57-75，
  AC-03 后按实际残留清理）
```

---

## 12.2 本阶段模拟测试输入

仍必须运行真实业务输入：

```text
CASE-01
CASE-02
CASE-03
CASE-05
CASE-08
CASE-10
```

此外增加静态模拟验证：

### STATIC-01

搜索：

```text
split_request(
```

预期：

```text
正式 app runtime = 0 引用
```

### STATIC-02

搜索：

```text
CatalogSegment
```

预期：

```text
正式 RuleEngine / Evaluator = 0 运行时依赖
```

### STATIC-03

搜索：

```text
network_plan_resolver is None
```

预期：

```text
正式主链路 = 0 compatibility branch
```

---

## 12.3 本阶段限制规则

```text
1. 只能删除已有替代路径覆盖的 legacy。
2. 删除前必须有 AC-00~AC-07 对应测试。
3. 不借 cleanup 修改业务规则。
4. 不借文档整理改接口。
5. 历史方案优先移动到 history，不直接丢失。
```

---

## 12.4 预期输出

业务 CASE 输出：

```text
与 AC-07 一致。
```

静态输出：

```text
Legacy runtime reference = 0
```

文档结构：

```text
README.md
docs/ARCHITECTURE.md
docs/DECISION_MODEL.md
docs/INTEGRATION.md
docs/TESTING.md
docs/history/*
```

---

## 12.5 输出断言

可通过测试或脚本：

```python
assert not runtime_imports("CatalogSegment")
assert not runtime_calls("split_request")
```

也可以在 Codex 执行记录中提供：

```text
rg "split_request\(" app/
rg "CatalogSegment" app/
```

并解释剩余命中是否只属于 compatibility/provider/history。

---

## 12.6 回退点

```text
AC-07 stable SHA
```

删除动作单独 commit，优先独立 revert，不连带回退前面架构。

---

# 13. AC-09：最终架构验收与全场景回归

## 13.1 目标

不新增代码设计，只证明收口完成。

---

## 13.2 本阶段模拟测试输入

必须一次性执行：

```text
CASE-01 单 IP
CASE-02 Network Plan 404
CASE-03 query limit
CASE-04 item limit
CASE-05 Telnet
CASE-06 any
CASE-07 large port span
CASE-08 ACL advisory/required
CASE-09 ACL no path
CASE-10 semantic conflict
CASE-11 policy gap
CASE-12 cross-region
```

补充：

```text
IPv6 unsupported
provider invalid response
provider dependency failure
ACL ambiguous
ACL observed port mismatch
semantic invalid schema
semantic fabricated rule
explanation failure
idempotency replay
same request_id different payload
CLI 批量链路回归（V2.1 新增）：requirement_runner +
tests/cases/network_requirements 全部 batch，
结论分布与 AC-00 基线一致
```

---

## 13.3 本阶段限制规则

```text
1. AC-09 原则上不再进行架构重构。
2. 测试失败必须回到责任阶段修复。
3. 禁止为了“全绿”放宽断言。
4. 禁止新增 xfail 覆盖新回归。
5. 禁止删测试换取通过。
```

---

## 13.4 最终输出矩阵

最终生成一份测试报告：

| Case | HTTP | Decision | Primary Reason | Rules | Net Status | ACL Status | Calls | Result |
|---|---:|---|---|---|---|---|---|---|
| CASE-01 | 200 | 合规/按规则 | - | - | complete | verified/unverified | 正常 | PASS |
| CASE-02 | 200 | 待定 | NETWORK_PLAN_NOT_FOUND | - | not_found | skipped | ACL=0 | PASS |
| CASE-03 | 422 | - | QUERY_LIMIT | - | - | - | deps=0 | PASS |
| CASE-04 | 422 | - | ITEM_LIMIT | - | - | - | ACL/LLM=0 | PASS |
| CASE-05 | 200 | 待定 | PORT-001 | PORT-001 | complete | ... | ... | PASS |
| CASE-06 | 200 | 待定 | LEAST-ANY-001 | LEAST-ANY-001 | N/A | ... | ... | PASS |
| CASE-07 | 200 | 待定 | LEAST-PORT-001 | LEAST-PORT-001 | complete | ... | ... | PASS |
| CASE-08-A | 200 | 合规 | - | - | complete | unverified | ... | PASS |
| CASE-08-B | 200 | 待定 | ACL_DEPENDENCY_FAILURE | - | complete | unverified | ... | PASS |
| CASE-09 | 200 | 待定 | ACL-PATH-001 | ACL-PATH-001 | complete | ... | ... | PASS |
| CASE-10 | 200 | 待定 | SEMANTIC_FACT_CONFLICT | - | complete | ... | semantic=1 | PASS |
| CASE-11 | 200 | 合规 | - | - | complete | ... | semantic=1 | PASS |

具体字段必须以实际代码 reason_code 为准；计划执行时不得写模糊的“类似”。

---

## 13.5 全量测试命令

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q
.\.venv\Scripts\python.exe -m ruff check . --no-cache
```

若 marker 存在，再执行对应分组：

```powershell
.\.venv\Scripts\python.exe -m pytest -m llm_guard -q
.\.venv\Scripts\python.exe -m pytest -m llm_pipeline -q
```

---

## 13.6 最终架构断言

全部必须成立：

- [ ] 所有 network mode 使用同一个 Resolver。
- [ ] 所有 segment 使用 CanonicalAddressSegment。
- [ ] RuleEngine 不直接读取 NetworkCatalog。
- [ ] RuleEngine 不使用 `.entry`。
- [ ] 默认正式规则全部可达。
- [ ] `decision/reason_code` 只有 DecisionReducer 产生。
- [ ] Network/Rule/ACL/Semantic 先生成 Finding。
- [ ] policy_gap 默认不独立改变 decision。
- [ ] LLM 不能执行 `待定 -> 合规`。
- [ ] ACL 调用门控有单独测试。
- [ ] Evaluator 仅负责编排。
- [ ] Settings dataclass / YAML schema / conftest / README 默认一致。（V2.1 语境）
- [ ] offline_catalog 只是 Provider compatibility mode。
- [ ] legacy runtime branch 已删除。
- [ ] HTTP API / CLI 批量 / evals 三入口行为同源验证通过。（V2.1）
- [ ] config/*.yaml 无明文密钥。（V2.1）
- [ ] pytest 全绿。
- [ ] Ruff 全绿。
- [ ] CASE-01~CASE-12 全部有精确输出断言。

---

## 13.7 最终回退点

保留：

```text
PRE_CONVERGENCE_SHA
AC03_STABLE_SHA
AC04_DECISION_SHA
AC07_PIPELINE_SHA
AC08_CLEANUP_SHA
FINAL_SHA
```

发生：

```text
事实模型问题 -> AC-02/AC-03
规则问题 -> AC-03
裁决问题 -> AC-04
编排问题 -> AC-05
LLM/ACL 边界 -> AC-06
默认配置 -> AC-07
cleanup 误删 -> revert AC-08
```

---

# 14. 阶段执行顺序

必须：

```text
【执行前置：密钥出库 + 工作区固化，见第 0.2 节】
  ↓
AC-00
  ↓
AC-01
  ↓
AC-02
  ↓
AC-03
  ↓
【停顿验收 1】
  ↓
AC-04
  ↓
【停顿验收 2】
  ↓
AC-05
  ↓
AC-06
  ↓
AC-07
  ↓
AC-08
  ↓
AC-09
```

---

# 15. 两个强制停顿验收点

## 15.1 AC-03 后

必须确认：

```text
1. Canonical Fact 已唯一。
2. 所有 Provider 都经过 Resolver。
3. RuleEngine 不关心事实来源。
4. 默认正式规则全部有事实来源。
5. OBJECT-001 已明确：
   - 正式映射后启用
   或
   - 无映射则暂时禁用。
```

任一未满足：

```text
不得开始 AC-04。
```

---

## 15.2 AC-04 后

必须确认：

```text
1. Finding 模型稳定。
2. DecisionReducer 是唯一裁决入口。
3. 主原因优先级已用表驱动测试冻结。
4. 多 finding 场景不会丢失 secondary evidence。
```

任一未满足：

```text
不得开始拆 Evaluator。
```

---

# 16. 每阶段 Codex 执行模板

```text
当前只执行 AC-XX，不开始下一阶段。

【执行前】
1. git status。
2. 记录 HEAD。
3. 运行本阶段基线测试。
4. 列出允许修改文件。
5. 列出禁止修改文件。

【模拟输入】
逐个列出本阶段 Case ID。
给出：
- request
- network mock
- acl mock
- llm mock
- settings override

【限制规则】
逐条确认本阶段不允许改变的行为。

【修改】
只做 AC-XX 范围内修改。

【定向验证】
对每一个 Case 输出：
- HTTP status
- decision
- reason_type
- reason_code
- matched_rules
- network status
- ACL status
- dependency call count

不得只报告“passed”。

【完整验证】
pytest
ruff

【差异审查】
git diff
确认无跨阶段修改。

【交付】
修改文件
行为变化
保持不变
模拟测试结果矩阵
完整测试结果
风险
未处理问题
commit SHA
rollback SHA

创建本地 commit。
不 push。
```

---

# 17. 每阶段交付结果模板

```markdown
# AC-XX 执行结果

## 1. 修改范围
- ...

## 2. 模拟输入
### CASE-X
Request:
...

Network mock:
...

ACL mock:
...

LLM mock:
...

## 3. 阶段限制规则验证
- [x] 未修改 ...
- [x] 未新增 ...
- [x] 未改变 ...

## 4. 输出验证

| Case | HTTP | Decision | Reason Code | Rules | Net | ACL | Calls | PASS |
|---|---:|---|---|---|---|---|---|---|
| ... | ... | ... | ... | ... | ... | ... | ... | YES |

## 5. 精确断言
- ...

## 6. 定向测试
- ...

## 7. 完整测试
- pytest:
- ruff:

## 8. 修改文件
- ...

## 9. 风险
- ...

## 10. 未处理
- ...

## 11. Commit
SHA:
Message:

## 12. 回退点
Previous stable SHA:
```

---

# 18. 本轮不处理

继续明确排除：

```text
新业务规则
NAT
新路由分析
真实策略下发
LLM 生成正式规则
request findings 升正式裁决
ACL candidate 升权威事实
数据库重构
审计存储架构更换
前端/UI
生产安全加固
```

发现这些需求：

```text
记录 backlog，不插入架构收口阶段。
```

---

# 19. 重新确认后的结论

V2 计划的验收单位不再是：

```text
“完成一个代码重构阶段”
```

而是：

```text
输入
  ↓
明确限制条件
  ↓
执行阶段代码
  ↓
产生可观察输出
  ↓
精确断言输出
  ↓
完整回归
  ↓
形成独立回退点
```

也就是说，每个 AC 阶段都必须回答五个问题：

```text
1. 我拿什么输入测试？
2. 本阶段明确不允许改变什么？
3. 正确输出应该是什么？
4. 用什么断言证明它真的正确？
5. 出现回归时精确退回哪里？
```

只有这五项全部明确并通过，阶段才允许提交。

本轮最终核心原则保持不变：

```text
先统一事实
再统一规则
再统一裁决
再拆编排
最后删除 legacy
```

但测试纪律升级为：

> **每一次架构修改都必须通过“模拟真实输入 → 限制规则 → 精确输出 → 断言验证 → 回退点”的闭环证明，而不能仅依赖单元测试数量或全量 pytest 为绿色。**
