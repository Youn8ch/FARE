# FARE 架构收口基线快照（AC-00）

> 基线提交：`PRE_CONVERGENCE_SHA = 2505b7952cd4b86f26d7d87f28b3f89fe69ecb8e`
> 记录日期：2026-08-30
> 表征套件：`tests/test_architecture_baseline.py`（17 例）+ 既有
> `tests/cases/evaluations/*.v2.json`（offline 链路表征）与
> `tests/test_network_plan_evaluation.py`（resolver 链路表征）。

## 1. 两条链路

| 链路 | network_plan.mode | 事实来源 | 表征位置 |
| --- | --- | --- | --- |
| resolver 链 | `mock`（multi_region.v1.json） | NetworkPlanResolver → NetworkPlanFact | test_architecture_baseline.py |
| offline 链 | `offline_catalog`（conftest 默认） | policies/network_catalog.yaml（CatalogSegment） | tests/cases/evaluations/core.v2.json |

conftest `settings` fixture 未传 `network_plan_client_mode`，落 dataclass 默认
`offline_catalog`；未传 `acl_decision_mode`，落 dataclass 默认 `required`。
（AC-07 将统一为显式声明。）

## 2. resolver 链基线矩阵（acl_decision_mode=advisory，deterministic_pending_mode=analyze）

| Case | HTTP | decision | reason_type | reason_code | matched_rules | net status | ACL status | 调用次数 |
|---|---:|---|---|---|---|---|---|---|
| CASE-01 单 IP | 200 | 合规 | - | - | [] | complete/complete | verified | network=2, ACL=1 |
| CASE-02 404 | 200 | 待定 | fact_incomplete | NETWORK_PLAN_NOT_FOUND | [] | not_found（源） | skipped | network=2, ACL=0 |
| CASE-03 查询上限 | 422 | - | - | NETWORK_PLAN_QUERY_LIMIT_EXCEEDED（actual=2, limit=1） | - | - | - | network=0, ACL=0，重复提交仍 422 |
| CASE-04 item 上限 | 422 | - | - | EVALUATION_ITEM_LIMIT_EXCEEDED（actual=4, limit=3） | - | - | - | ACL=0，重复提交仍 422 |
| CASE-05 tcp/23 | 200 | 待定 | policy_violation | PORT-001 | [PORT-001] | complete/complete | verified | ACL=1（analyze 模式） |
| CASE-06 any | 200 | 待定 | policy_violation | LEAST-ANY-001 | [LEAST-ANY-001] | not_applicable（源） | verified | network=1（any 不查询）, ACL=1 |
| CASE-07 tcp/1-101 | 200 | 待定 | policy_violation | PORT-001 | [PORT-001, LEAST-PORT-001] | complete/complete | verified | network=2 |
| CASE-08 advisory ACL 依赖失败 | 200 | 合规 | - | - | [] | complete/complete | unverified | ACL http 模式失败 |
| CASE-08 required ACL 依赖失败 | 200 | 待定 | dependency_failure | ACL_DEPENDENCY_FAILURE | [] | complete/complete | unverified | ACL http 模式失败 |
| CASE-09 ACL 明确无路径 | 200 | 待定 | acl_no_path | ACL-PATH-001 | [ACL-PATH-001] | complete/complete | review_required | ACL=1 |
| CASE-10 semantic 权威冲突 | 200 | 待定 | fact_conflict | SEMANTIC_FACT_CONFLICT | [] | complete | verified | semantic=1 |
| CASE-11 policy_gap（现状） | 200 | 待定 | risk_uncertain | SEMANTIC_POLICY_GAP | [] | complete | verified | semantic=1 |

CASE-07 备注：计划预期"matched_rules 包含 LEAST-PORT-001"成立；但 tcp/1-101
同时覆盖端口 23，规则顺序 PORT-001 在前，故主原因码为 PORT-001（实际行为，
AC-09 矩阵以本表为准）。

CASE-11 备注：现状 `temporary_permanent_conflict: review_required` 会执行
合规→待定（SEMANTIC_POLICY_GAP）。计划目标态（candidate 不降级）属业务行为
变更，须业务方书面确认后方可实施（AC-06）。

## 3. offline 链基线（core.v2.json 摘要）

- CASE-01 等价：`compliant_https_v2` → 16.1.30.10/32→16.1.30.20/32 合规。
- CASE-02 等价：`unknown_catalog_address_v2` → 待定 ZONE_UNRESOLVED。
- CASE-05：`tcp_23_is_rejected_v2` → PORT-001；near-miss `udp_23_...` 合规。
- CASE-06：`any_address_v2` → LEAST-ANY-001。
- CASE-07 near-miss：`port_span_100_is_allowed_v2` 合规 / `port_span_101...` LEAST-PORT-001。
- CASE-09：`acl_explicit_no_path_v2` → ACL-PATH-001。
- OBJECT-001：`object_office_to_production_database_v2` 仅 offline_catalog 模式可达
  （catalog 的 object_type 经 offline client 冒充 usageCode 回流，AC-03 收口）。

幂等基线（offline 链）：
- 相同 request_id + 相同 payload → 200，同 audit_id（缓存重放）。
- 相同 request_id + 不同 payload → 409 idempotency_conflict。

## 4. CLI 批量链路基线（requirement_runner，config/fare.test-area-relations.yaml）

| batch | requests | 合规 | 待定 |
|---|---:|---:|---:|
| area_relation_success.batch.json | 30 | 14 | 16 |
| area_relation_not_found.batch.json | 1 | 0 | 1（NETWORK_PLAN_NOT_FOUND，目的端 not_found，ACL skipped） |
| area_relation_mixed_01.batch.json | 8 | 2 | 6 |

该分布由 `test_cli_batch_conclusion_distribution_baseline` 冻结，作为
AC-05 / AC-06 的对照基线；调整必须伴随显式评审。

## 5. 已知双轨现状（供后续阶段收口）

1. offline_catalog 走 `split_request` + CatalogSegment；mock/http 走
   `NetworkPlanResolver` + ResolvedAddressSegment —— 两套事实模型并存（AC-01/AC-02）。
2. query limit 仅在 `resolver is not None` 时执行（Runtime.evaluate_request
   前置）；offline 链无 query limit（AC-02 收口）。
3. `Settings.acl_decision_mode` dataclass 默认 `required` vs YAML schema 默认
   `advisory`（AC-07 收口）。
4. offline client 将 `entry.object_type` 冒充 `usageCode`（AC-03/AC-08 清理）。
5. RuleEngine 通过 `.entry` / `primary_fact` 双路径读取事实（AC-03 收口）。
6. `LlmClient.review` 为死代码（AC-08 清理）。

## 6. AC-02 收口记录（2026-08-30）

offline_catalog 改为显式兼容 Provider，三种 mode 共享同一
`provider -> resolver -> canonical segment` 主链路。随统一发生的、计划批准的
行为变更（offline 链，AC-00 表征值 -> 新值）：

| 场景 | AC-00 基线 | AC-02 统一后 |
|---|---|---|
| 目录未规划地址 | 待定 ZONE_UNRESOLVED（status complete） | 待定 NETWORK_PLAN_NOT_FOUND（status not_found） |
| IPv4 过大前缀（/15，>64 个 /24） | 待定 LEAST-CIDR-001 | 422 NETWORK_PLAN_QUERY_LIMIT_EXCEEDED（query limit 不再被 offline 绕过） |
| any 地址 | status complete（兜底） | status not_applicable（与 resolver 链一致） |
| 目录多义命中 | 待定 ZONE_CONFLICT | 待定 NETWORK_PLAN_INVALID_RESPONSE（409 冲突体） |
| offline 响应 | network_analysis=None、无 network_plan_raw | 携带 network_analysis.lookups 与 provider raw 记录 |

LEAST-CIDR-001 的 IPv4 正例端到端不可达（任何 prefixlen<16 的 /24 数都超过
query limit），规则级正/反例由 `tests/test_rule_limits.py` 覆盖；v2 套件的
`ipv4_prefix_too_broad_v2` 改为 `ipv6_prefix_too_broad_v2`（IPv6 不产生 /24
查询，可端到端表达前缀检查）。OBJECT-001 经 offline 目录显式字段
（legacy_entry -> canonical zone/environment/object_type）继续命中，行为不变。
