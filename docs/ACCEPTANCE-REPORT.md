# FARE 架构收口最终验收报告（AC-09）

> 执行日期：2026-08-30
> 执行依据：`docs/history/FARE-架构收口修复计划-V2.md`（V2.1 校准版）
> 验收命令：`pytest -p no:cacheprovider -q`、`ruff check . --no-cache`、
> 各 marker 分组（llm_guard / llm_pipeline / llm_http）

## 1. 提交链（回退点）

| 阶段 | SHA | 说明 |
|---|---|---|
| 执行前置 | `2505b79` | PRE_CONVERGENCE_SHA：密钥出库 + 工作区固化 |
| AC-00 | `ce94f9f` | 行为基线锁定（characterization + CLI 批量基线） |
| AC-01 | `0702ffa` | CanonicalNetworkFact / CanonicalAddressSegment |
| AC-02 | `2c9bf99` | 统一 Provider 主链路（query limit 收口） |
| AC-03 | `6d68f3f` | RuleEngine 收口 + OBJECT-001 禁用 + 可达性矩阵（AC03_STABLE_SHA） |
| AC-04 | `5a744cd` | Finding + DecisionReducer 唯一裁决入口（AC04_DECISION_SHA） |
| AC-05 | `11a4300` | Evaluator 八阶段编排化 |
| AC-06 | `4a58932` | ACL/LLM 门控显式固定 |
| AC-07 | `bb0ad02` | 四入口默认值统一（AC07_PIPELINE_SHA） |
| AC-08 | `3b4127e` | Legacy 删除与文档收口（AC08_CLEANUP_SHA） |
| AC-09 | 见 `git log` | FINAL_SHA：最终验收与本报告 |

## 2. 最终场景矩阵（实际输出值）

| Case | HTTP | Decision | Primary Reason | Rules | Net Status | ACL Status | Calls | Result |
|---|---:|---|---|---|---|---|---|---|
| CASE-01 单 IP | 200 | 合规 | - | [] | complete/complete | verified | network=2, ACL=1 | PASS |
| CASE-02 规划 404 | 200 | 待定 | NETWORK_PLAN_NOT_FOUND (fact_incomplete) | [] | not_found(源) | skipped | network=2, ACL=0 | PASS |
| CASE-03 查询上限 | 422 | - | NETWORK_PLAN_QUERY_LIMIT_EXCEEDED (actual=2, limit=1) | - | - | - | 全部依赖=0，重复提交仍 422 | PASS |
| CASE-04 item 上限 | 422 | - | EVALUATION_ITEM_LIMIT_EXCEEDED (actual=4, limit=3) | - | - | - | ACL=0, LLM=0 | PASS |
| CASE-05 Telnet | 200 | 待定 | PORT-001 (policy_violation) | [PORT-001] | complete | verified | ACL=1 | PASS |
| CASE-06 any | 200 | 待定 | LEAST-ANY-001 (policy_violation) | [LEAST-ANY-001] | not_applicable(源) | verified | network=1（any 不查询）, ACL=1(analyze)/0(skip) | PASS |
| CASE-07 tcp/1-101 | 200 | 待定 | PORT-001（tcp/23 同段命中；matched 含 LEAST-PORT-001） | [PORT-001, LEAST-PORT-001] | complete | verified | network=2 | PASS |
| CASE-08-A advisory 失败 | 200 | 合规 | - | [] | complete | unverified | ACL http 故障闭合 | PASS |
| CASE-08-B required 失败 | 200 | 待定 | ACL_DEPENDENCY_FAILURE (dependency_failure) | [] | complete | unverified | 同上 | PASS |
| CASE-09 明确无路径 | 200 | 待定 | ACL-PATH-001 (acl_no_path) | [ACL-PATH-001] | complete | review_required | ACL=1 | PASS |
| CASE-10 权威冲突 | 200 | 待定 | SEMANTIC_FACT_CONFLICT (fact_conflict) | [] | complete | verified | semantic=1 | PASS |
| CASE-11 policy_gap | 200 | 待定 | SEMANTIC_POLICY_GAP (risk_uncertain) | [] | complete | verified | semantic=1 | PASS（现状冻结，见 §4） |
| CASE-12 跨区域 /23 | 200 | 待定 | NETWORK_PLAN_NOT_FOUND（仅 404 子段） | [] | 16.201.2.0/24=complete；16.201.3.0/24=not_found | verified / skipped | 拆分不扩大至 /22 | PASS |

补充场景（全部由既有命名测试覆盖并通过）：

| 场景 | 覆盖位置 |
|---|---|
| IPv6 unsupported | `ipv6_prefix_too_broad_v2`（NETWORK_PLAN_IPV6_UNSUPPORTED） |
| provider invalid response | `tests/test_network_plan_client.py` / resolver 校验 |
| provider dependency failure | `tests/fixtures/network_plan/failures.v1.json` + resolver 测试 |
| ACL ambiguous / port mismatch / missing firewall | `acl_ambiguous_v2` / `acl_port_mismatch_v2` / `acl_firewall_unresolved_v2` |
| semantic invalid schema | `tests/test_llm_business_effects.py` + `test_semantic_guard.py` |
| semantic fabricated rule | `llm_fabricated_rule_is_rejected_v2` + `test_acl_llm_gating.py` |
| explanation failure | `llm_explanation_falls_back_to_template_v2` |
| idempotency replay / conflict | `test_architecture_baseline.py`（200 同 audit_id / 409 idempotency_conflict） |
| CLI 全量回归 | `test_final_acceptance.py`：30 batch / 159 需求 / 261 items 全部与版本化期望一致 |

## 3. 最终架构断言（13.6）

- [x] 所有 network mode 使用同一个 Resolver（`test_all_provider_modes_build_a_runtime_resolver`）
- [x] 所有 segment 使用 CanonicalAddressSegment（`test_case01_combinations_hold_canonical_segments`）
- [x] RuleEngine 不直接读取 NetworkCatalog（`test_rule_engine_has_no_catalog_or_entry_dependency`）
- [x] RuleEngine 不使用 `.entry`（同上）
- [x] 默认正式规则全部可达（`test_every_default_active_rule_has_positive_and_near_miss`；OBJECT-001 已禁用并由 fixture 包承接）
- [x] `decision/reason_code` 只有 DecisionReducer 产生（Evaluator 各阶段仅产出 Finding；`test_decision_reducer.py`）
- [x] Network/Rule/ACL/Semantic 先生成 Finding（同上）
- [ ] **policy_gap 默认不独立改变 decision —— 冻结项**：目标态须业务方书面确认（计划 C8）；现状按 `evaluation.semantic_effects` 默认 `review_required` 降级，行为已由 `test_case11_policy_gap_current_behavior_downgrades` 与 `test_llm02_policy_gap_target_state_is_frozen` 固定并有显式变更指引
- [x] LLM 不能执行 `待定 -> 合规`（`test_llm_never_upgrades_pending_to_compliant` + 单向矩阵）
- [x] ACL 调用门控有单独测试（`test_acl_llm_gating.py`：ACL-01/02）
- [x] Evaluator 仅负责编排（八阶段 + stage_observer 验证）
- [x] Settings dataclass / YAML schema / conftest / README 默认一致（`test_default_consistency.py`，11 例）
- [x] offline_catalog 只是 Provider compatibility mode（三 mode 共享 Resolver，等价事实逐字段一致）
- [x] legacy runtime branch 已删除（`test_legacy_cleanup.py`：STATIC-01/02/03、review 死代码、usageCode shim、旧属性）
- [x] HTTP API / CLI 批量 / evals 三入口行为同源（共享 `Runtime.evaluate_request`；CLI 全量回归对照版本化期望通过）
- [x] config/*.yaml 无明文密钥（`test_tracked_config_profiles_do_not_embed_llm_api_keys`；真实密钥仅在未跟踪 `config/fare.local.yaml`）
- [x] pytest 全绿：484 + AC-09 新增 31 例（分 marker：llm_guard 120 / llm_pipeline 94 / llm_http 21）
- [x] Ruff 全绿
- [x] CASE-01~CASE-12 全部有精确输出断言（§2 矩阵，全部为实际运行值）

## 4. 冻结与未处理事项（backlog，不阻塞收口）

1. **CASE-11 / LLM-02（policy_gap 目标态）**：等待业务方书面确认。确认后需同步修改
   `evaluation.semantic_effects` 默认值、`test_case11_policy_gap_current_behavior_downgrades`
   与 `test_llm02_policy_gap_target_state_is_frozen`（测试内已写明变更指引）。
2. CASE-07 主原因码为 PORT-001（tcp/1-101 同时覆盖端口 23，规则顺序在前）；
   计划只要求 matched_rules 包含 LEAST-PORT-001，已满足并固定。
3. OBJECT-001 恢复启用前提：正式 usageCode→object_type 数据字典落地后，
   以显式 mapping 提供主路径事实来源（fixture 规则包保留能力）。
4. 真实 ACL 合约、生产规则审批、鉴权与组织级日志脱敏策略（上线前外部依赖）。
5. 本轮明确排除项（NAT、路由分析、策略下发、数据库重构等）未引入，记录于计划第 18 章。
