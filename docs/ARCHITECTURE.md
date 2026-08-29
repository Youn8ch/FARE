# FARE 架构总览

> 架构收口（V2.1 计划）完成后的主链路。历史方案文档见 `docs/history/`。

## 主链路

```text
EvaluationRequest
    │
    ▼
plan        原始组合数 item 上限守卫
    │
    ▼
network     NetworkFactProvider（mock / http / offline_catalog 兼容 Provider）
    │         → NetworkPlanResolver（唯一；查询粒度 /24，query limit 全模式生效）
    ▼
CanonicalNetworkFact / CanonicalAddressSegment   ← 规范化层（app/services/canonical.py）
    │
    ▼
acl         AclEnricher：每个可评估组合至多 1 次 ACL 调用（网络事实失败/确定性待定可跳过）
    │
    ▼
rules       RuleEngine（只读 canonical 字段）→ rule Findings
    │
    ▼
semantic    SemanticReviewer（批量 1 次）→ 语义 findings（冲突/缺口/失败）
    │
    ▼
reduce      DecisionReducer（唯一裁决入口，见 docs/DECISION_MODEL.md）
    │
    ▼
explain     Explanation（语义成功后 1 批次；失败仅模板回退）
    │
    ▼
assemble    ResponseAssembler（EvaluationResponse）
    │
    ▼
AuditStore（SQLite 权威幂等 + JSONL 归档）
```

三条入口（HTTP API、`python -m app requirements` CLI 批量、evals 脚本）共享同一
`Runtime.evaluate_request`，行为同源。

## 事实模型

- **唯一事实词汇**：`CanonicalNetworkFact` / `CanonicalAddressSegment`（`app/services/canonical.py`）。
  只做规范化：字段逐字拷贝，不做业务推断；`usage_code` 不映射 `object_type`，
  `platform_name` 不映射 `environment`，不从 `description` 推断任何字段。
  `object_type` / `environment` 仅允许显式正式来源（offline 目录条目经
  `legacy_entry` 携带）。
- **Provider 唯一主路径**：三种 `network_plan.mode` 都构造 `NetworkPlanResolver`：
  - `mock`：版本化 fixture；
  - `http`：真实 API（合约确认前不发出请求）；
  - `offline_catalog`：显式兼容 Provider（`OfflineCatalogNetworkPlanClient`），
    不绕过 query limit。
- **规则事实来源**：RuleEngine 只读 canonical 字段（`zone` / `environment` /
  `object_type` / `labels` / `primary_fact`），不访问 `.entry`，不 import
  `NetworkCatalog`。OBJECT-001 在默认规则包中暂时禁用（无正式映射），能力由
  fixture 规则包保留。

## 已删除的 legacy

- legacy split 主路径（`split_request`）及 `resolver=None` 兼容分支；
- `LlmClient.review` 死代码与 `LlmReview*` 模型；
- `ResolvedAddressSegment` 上的 `network/original/matches/entry` 兼容属性；
- offline Provider 将 `entry.object_type` 冒充 `usageCode` 的 shim。

## 代码地图

| 模块 | 职责 |
| --- | --- |
| `app/config.py` | 单 YAML 严格配置 → `Settings` |
| `app/main.py` | `build_runtime` / `create_app` / Runtime 编排与幂等前置 |
| `app/requirement_runner.py` | CLI 批量入口（与 HTTP 共享 Runtime） |
| `app/services/canonical.py` | 规范化事实模型 |
| `app/services/network_plan_client.py` | Provider 边界（mock/http/offline 兼容） |
| `app/services/network_plan_resolver.py` | 唯一 Resolver：/24 查询、校验、分段聚合 |
| `app/services/splitter.py` | 组合拆分（canonical 段） |
| `app/services/rule_loader.py` | 规则包加载与匹配（canonical 输入） |
| `app/services/evaluator.py` | 八阶段编排（plan→…→assemble） |
| `app/services/decision_reducer.py` | 唯一裁决入口 |
| `app/services/llm_client.py` | 语义/解释/影子阶段模型边界 |
| `app/services/output_guard.py` | 语义与申请级发现输出守卫 |
| `app/services/audit.py` | SQLite 幂等 + JSONL 审计与脱敏 |
