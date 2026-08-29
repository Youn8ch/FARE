# FARE 裁决模型（Decision Model）

业务结论只有两种：`合规` 或 `待定`。服务不创建、修改或下发防火墙策略。

## Finding

各阶段（network / rule / ACL / semantic）只产出 `Finding`
（`app/services/decision_reducer.py`），不再直接决定结论：

```python
Finding(code, source, reason_type, priority, detail, affects_decision)
```

- `source`：`network | rule | acl | semantic`；
- `priority`：`network(0) < rule(10) < catalog(20) < acl(30) < semantic(40)`；
- `affects_decision=False` 的信息性 finding 只记录，不参与裁决。

## DecisionReducer（唯一裁决入口）

`DecisionReducer.reduce(findings, matched_rules=...)` 返回 `Decision`：

```python
Decision(decision, primary_finding, reason_type, reason_code, findings, matched_rules)
```

规则：

1. 主 finding = 影响裁决的 findings 中优先级最高者；同优先级按插入顺序（稳定）。
2. secondary finding 永不覆盖更高优先级的 primary；
3. `matched_rules` 原样保留（含事实驱动的 ACL-PATH-001）；
4. 无影响裁决的 finding → `合规`；否则 `待定`，reason = primary。

### 确定性优先级（表驱动冻结，见 `tests/test_decision_reducer.py` F-01~F-05）

| 顺序 | 来源 | 示例 reason_code | reason_type |
|---|---|---|---|
| 1 | network 规划事实错误 | NETWORK_PLAN_NOT_FOUND / *_MISMATCH / DEPENDENCY_FAILURE / IPV6_UNSUPPORTED | fact_incomplete / fact_conflict / dependency_failure |
| 2 | 正式规则 | PORT-001、LEAST-*（按规则包顺序取首个） | policy_violation 等 |
| 3 | 目录事实错误（兼容链路） | ADDRESS_ANY 等 | fact_incomplete / fact_conflict |
| 4 | ACL 事实 | ACL-PATH-001 / ACL_FACT_AMBIGUOUS / ACL_PORT_MISMATCH / ACL_FIREWALL_UNRESOLVED / ACL_DEPENDENCY_FAILURE | acl_no_path / fact_conflict / fact_incomplete / dependency_failure |
| 5 | 语义（已验证且 review_required） | SEMANTIC_FACT_CONFLICT / SEMANTIC_POLICY_GAP / LLM_SEMANTIC_ANALYSIS_FAILURE | fact_conflict / risk_uncertain / dependency_failure |

## 语义影响边界

- 语义分析**永远不能**把 `待定` 提升为 `合规`；
- 已验证的权威事实冲突（claim conflict / 矛盾）按 `evaluation.semantic_effects`
  的 `fact_conflict` 策略影响结论（默认 `review_required` → 降级待定）；
- `policy_gap` 目标态为仅候选/信息（不独立降级），该行为变更已冻结，
  须业务方书面确认后实施（见 `evaluation.semantic_effects` 与 CASE-11）；
- 语义失败（超时/非法 JSON/虚构规则/无证据风险）故障闭合为
  `LLM_SEMANTIC_ANALYSIS_FAILURE` 待定，仅对确定性合规项生效；
- 虚构规则编号使整个语义批次被拒绝，不会进入 `matched_rules`；
- `decision_trace` 记录 `deterministic_decision → semantic_effect → final_decision`；
- 解释阶段只写 `llm_explanation` / `llm_recommendation`，失败仅模板回退。

## ACL 调用门控（已测试固定）

| 场景 | ACL 调用 |
|---|---|
| 网段规划事实错误/未规划 | 0（skipped） |
| 确定性待定 + `deterministic_pending_mode=skip`（生产默认） | 0 |
| 确定性待定 + `analyze` | 1（候选观测） |
| 正常项 | 1 |

ACL 结果只能维持或加深待定（无路径/歧义/端口不一致/未确认防火墙），
不能把待定提升为合规。
