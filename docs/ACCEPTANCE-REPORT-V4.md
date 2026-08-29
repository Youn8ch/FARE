# FARE 主链二次收口验收报告（V4）

> 基线：`a70ea45`（AC-09 FINAL_SHA）　分支：`codex/fare-main-chain-v4`
> 计划：`FARE-主链二次收口修复与后续优化计划-V4.md`
> 基线冻结记录：`docs/v3-baseline.md`（含 D1 反转记录与 §6 预注册变更清单）

## 1. 阶段提交记录

| 阶段 | SHA | 内容 |
|---|---|---|
| V4-P0 baseline-freeze | `71bed0a` | 冻结 `a70ea45` 行为基线（`docs/v3-baseline.md` + 表征测试） |
| V4-P1 stage-contracts-and-rule-before-acl | `3e7d904` | 类型契约 + Rules 前置；`PolicyBundle.match` 每 item 恰好一次；ACL gating 消费 `RuleStageResult`；item_id 铸造提前 |
| V4-P2 single-reduce | `0e398ec` | `DecisionReducer.reduce_item` 每 item 恰好一次正式裁决；`llm_added_pending_count` 改由快照推导；删除 `_apply_semantic_failure` 死代码 |
| V4-P3 provider-fact-unification | `f7ff6c2` | 单一 typed `ProviderLookup` 通道；校验迁入 provider 适配层；Resolver 删除 `NetworkCatalog`/`legacy_entry`/`offline_catalog`；伪装与离线多命中行为冻结 |
| V4-P4 findings-output | `b6b7fd6` | `decision_findings` 进入 item 输出与 audit；恢复历史 finding 插入顺序 |
| V4-P5a evaluator-mechanical-extraction | `fe5a71d` | finding 构造/文案映射/item 装配/request 聚合/response 聚合 纯搬移 |
| V4-P5b+P6 stage-decomposition | `a98e02b` | `stages/{rule,acl,semantic,reduce,post_decision}` 具名阶段；Evaluator 缩至 259 行纯编排；post_decision 串行（D4） |
| V4-P7 request-aggregation | `b6424e5` | `RequestDecisionAggregator` 专项验收 |
| V4-P8 acceptance-matrix | `3fd9251` | 主链模拟验收矩阵 V3-01~33 |
| V4-P9 docs-cleanup | （本提交） | 文档收口与最终验收 |

## 2. 最终 PASS 要求核验（计划 §16.3）

| # | 要求 | 结果 |
|---|---|---|
| 1 | baseline 测试全部通过或有已批准行为变更记录 | ✅ 577 passed；变更全部在 `docs/v3-baseline.md` §6 预注册（D1 反转记录在案） |
| 2 | V4 新测试全部通过 | ✅ 表征 9 + 不变量 6 + provider 契约 5 + findings 4 + 聚合 5 + 矩阵 33 |
| 3 | Ruff 全绿 | ✅ `ruff check . --no-cache` |
| 4 | 不要求真实外部 API | ✅ `tests/conftest.py::block_real_network` 保持，全链路模拟 |
| 5 | `DecisionReducer.reduce_item()` = item 数 | ✅ `RecordingDecisionReducer` 断言（矩阵 V3-01 等） |
| 6 | Rule match 计数 = item 数 | ✅ `RecordingPolicyBundle` 断言 |
| 7 | Resolver 无 catalog 第二事实通道 | ✅ 静态检查 `test_resolver_has_no_second_fact_channel` |
| 8 | post-decision 无权修改业务裁决 | ✅ business core 快照断言（表征/矩阵 V3-28） |
| 9 | offline 伪装与 audit raw_records 与 `a70ea45` 一致 | ✅ 表征测试 `test_p0_offline_masquerade_freeze` 等 |

## 3. 不变量达成清单（计划 §4）

- 事实：RuleEngine 只读 Canonical；Resolver 只消费 typed `ProviderLookup`；
  `ProviderNetworkFact = NetworkPlanFact + Optional[ExplicitNetworkClassification]`；
  Canonical 不做隐式推断；zone→area_id 规则回退保留。
- 规则：`PolicyBundle.match` 每 item 恰好一次（RuleStage）；ACL gating 消费
  `RuleStageResult`；RuleStage 不产生 decision；`total_combinations` 进入契约。
- 裁决：每 item 恰好一次 `reduce_item`；deterministic 快照与 final 同调用形成；
  request 聚合独立为 `RequestDecisionAggregator`；语义/LLM 永不 `待定→合规`。
- 输出：primary 唯一并决定 reason；secondary findings 全部可审计
  （`decision_findings` + audit）；`DecisionTrace` schema 不变
  （含 `semantic_finding_ids`）；shadow 失败不改变结论；解释失败仅模板回退；
  `exceptions` 顺序确定；新字段经递归脱敏链路。

## 4. 明确不扩展范围

V4 全程未新增 NAT 分析、路由计算、策略下发、防火墙写入、审批系统、数据库架构、
UI、权限模型或新产品模块（计划 §18）。伪装清理与 post-decision 并行化按 D2/D4
推迟至 O1/O12。

## 5. 遗留与后续（O 项）

- O1 Provider Contract 正式化（TransportAdapter/Validator 分层 + 伪装清理，需行为评审）；
- O2 RuleEngine 索引（当前规模无需）；O3 Finding Registry；O4 Domain/Adapter 分层；
- O5/O6 统一 Stage Metrics / Pipeline Trace（收集器已具雏形 `stage_metrics.py`）；
- O7 Property-Based 测试；O8 Golden Dataset 分层；O12 性能优化（post-decision
  并行化在此实施，需先建立 exceptions 顺序无关断言）。
