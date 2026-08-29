# V4 基线冻结记录（`a70ea45`）

> 记录时间：2026-08-30　分支：`codex/fare-main-chain-v4`（自 `a70ea45` 创建）
> 性质：V4-P0 阶段产物。在主链二次收口动工前，把当前可观察行为完整冻结。
> 表征测试：`tests/test_v4_characterization.py`；历史表征：`tests/test_architecture_baseline.py`（AC-00）。

## 1. 环境与基线状态

| 项目 | 值 |
|---|---|
| 基线提交 | `a70ea45`（与 `origin/main` 一致） |
| Python | 3.13.x |
| pytest | 515 passed（`pytest -p no:cacheprovider -q`，约 20s） |
| ruff | 全绿（`ruff check . --no-cache`） |
| 默认策略 | `policies/`：manifest 2026.08.0；激活规则 PORT-001、LEAST-ANY-001、LEAST-CIDR-001、LEAST-PORT-001、LEAST-COMBINATION-001、ACL-PATH-001（object_type 规则已注释禁用） |

## 2. 当前真实执行链（阶段名与顺序）

`Evaluator.evaluate()` 的 `_stage()` 观察顺序（conftest dev 默认 `analyze` 模式）：

```text
plan → network → acl → rules → semantic → reduce → explain → assemble
```

注意：**"acl" 在 "rules" 之前**——ACL gating 为判断 `deterministic_pending_mode=skip`
提前执行了 `PolicyBundle.match()`（`evaluator.py:518`），Rules 阶段再次执行同一匹配
（`evaluator.py:570`）。reduce 与 explain 之间内联了 ACL candidate shadow 与
request findings shadow（无独立 stage 名，metrics 记在 `model_raw["stages"]`）。

`model_raw["stages"]` 键顺序（shadow off）：`semantic → explanation`；
（shadow 全开）：`semantic → acl_candidates → request_findings → explanation`。

## 3. 调用计数基线（每 item，1 个普通 item 请求）

| 调用 | 次数 | 位置 |
|---|---|---|
| `PolicyBundle.match()`（非网络阻断 item） | **2** | ACL gating 1 + Rules 阶段 1 |
| `PolicyBundle.match()`（网络阻断 item） | **1** | 仅 Rules 阶段（ACL 跳过） |
| `DecisionReducer.reduce()`（每 item） | **2** | `_build_deterministic_stage:574` + `_reduce_items:911` |
| ACL client（analyze 模式、无规则命中） | 1 | |
| ACL client（skip 模式、规则命中） | 0 | |
| ACL client（网络阻断） | 0 | |
| semantic / explanation（成功时各） | 1 | 批量调用 |

## 4. 冻结的隐性输出（后续阶段不得静默改变）

1. **`llm_added_pending_count`**（`model_raw["metrics"]`）：= max(0, 最终待定数 − 确定性阶段待定数)。
   语义冲突降级场景 = 1；全合规场景 = 0。V4-P2 改由 reducer 快照推导，值必须逐场景一致。
2. **`exceptions` 列表顺序**：ACL 依赖 → LLM semantic → LLM ACL candidates → LLM request
   findings → LLM explanation（按阶段执行顺序追加）。
3. **offline_catalog 字段伪装**（`network_plan_client.py:324-327`）：目录 zone 写入
   `area`/`areaId`/`regionName`，environment 写入 `platformName`。观察出口：
   `network_analysis.source_regions[].area_id == region_name == platform_name == 目录 zone`、
   `platform_name == 目录 environment`（PROD-APP/PROD-DB 均为 `production`）。
   offline 下 `usage_code` 恒为 None（不伪装 object_type）。
4. **offline 目录多命中**：provider 返回 code 409 → `NETWORK_PLAN_INVALID_RESPONSE`
   （fact status `invalid_response`）；`_legacy_entry` 对多命中静默返回 None（无冲突码）。
5. **ACL-PATH-001 仅在恰为 primary 时进入 `matched_rules`**（`evaluator.py:738-747`）；
   作为 finding 恒由 explicit_no_path 事实产生（source="acl"）。
6. **audit `network_plan_raw`**：`{query_subnet, http_status, validation[, body|raw_text]}`
   （transport 错误时 `{query_subnet, transport_error, validation}`）。
7. **`decision_trace` schema**：`deterministic_decision / semantic_effect(五值 Literal) /
   semantic_finding_ids / final_decision / final_reason_code`，不得增删字段。

## 5. P0 冻结场景（模拟输入 → 精确输出）

| 场景 | 限制 | 冻结输出 |
|---|---|---|
| P0-C01 普通 HTTPS | tcp/443，facts complete，ACL verified，semantic empty | item=合规，request=合规，ACL=1，semantic=1，reduce=2 |
| P0-C02 规划不存在 | source `NETWORK_PLAN_NOT_FOUND` | item=待定，reason=NETWORK_PLAN_NOT_FOUND，ACL=0，reduce=2 |
| P0-C03 Telnet+skip | tcp/23，`deterministic_pending_mode=skip` | item=待定，reason=PORT-001，ACL=0，match=2 |
| P0-C04 ACL 无路径 | 网络完整、无规则命中、explicit_no_path | item=待定，reason=ACL-PATH-001，matched_rules=[ACL-PATH-001]，verification=review_required |
| P0-C05 语义冲突 | 网络完整、无规则命中、ACL verified、verified fact conflict | deterministic=合规，final=待定，reason=SEMANTIC_FACT_CONFLICT，effect=downgraded，llm_added_pending=1 |

## 6. 已批准的行为变更预告（V4 执行中将发生，非漂移）

| 阶段 | 变更 | 依据 |
|---|---|---|
| V4-P1 | 阶段顺序变为 `plan/network/rules/acl/semantic/reduce/post_decision/assemble`；`PolicyBundle.match` 每 item 恰好 1 次 | V4 方案 §8（不变量 4.2） |
| V4-P2 | `DecisionReducer.reduce()` 每 item 恰好 1 次（`tests/test_evaluator_orchestration.py` 的 2× 断言与注释同步改写）；`llm_added_pending_count` 改由 reducer 快照推导 | V4 方案 §9；**D1：有意推翻 AC-05 的"两次 reduce"决策**——单次 reduce 在一次调用内同时产出 deterministic 快照与 final（信息不丢失），消除双裁决入口；AC-05 当时的两阶段实现是达成 trace 的手段而非目的 |
| V4-P3 | Resolver 删除 `legacy_entry`/`offline_catalog`/`_legacy_entry`；校验迁入 provider 适配层；伪装字段与离线多命中行为**不变** | V4 方案 §10（冻结清单） |
| V4-P4 | item 输出新增 `decision_findings`（additive） | V4 方案 §11（D3） |
| V4-P5 | Evaluator 拆分（P5a 纯搬移 / P5b stages 编排），零行为变化 | V4 方案 §12（D5） |
| V4-P6 | post_decision 成具名 stage，顺序保持现状（acl_candidates → request_findings → explanation），串行 | V4 方案 §13（D4） |
| V4-P7 | request 聚合组件化，规则不变 | V4 方案 §14 |

以上之外的一切输出漂移均为回归，必须修复。
