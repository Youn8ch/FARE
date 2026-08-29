# FARE 测试指南

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q
.\.venv\Scripts\python.exe -m ruff check . --no-cache
```

默认套件禁止非 loopback 网络连接；`testpaths = ["tests"]` 不收集真实模型评测
目录（evals）。基线矩阵与双轨收口记录见 `docs/architecture-baseline.md`。

## 分层

| 层 | 位置 | 说明 |
| --- | --- | --- |
| 表征基线 | `tests/test_architecture_baseline.py` | CASE-01~11 精确断言（含依赖调用次数）、幂等、CLI 批量结论分布基线 |
| 数据驱动用例 | `tests/cases/evaluations/*.v2.json` + `test_evaluation_cases.py` | core 14 例 + llm_pipeline / acl_candidates / request_findings / explanation |
| 规范化事实 | `tests/test_canonical_fact.py` | canonical 无推断守卫、CASE-01/02/12 |
| Provider 统一 | `tests/test_provider_unification.py` | mock/offline 等价事实逐字段一致、双链路 limit、三 mode resolver |
| 规则可达性 | `tests/test_rule_reachability.py` + `test_rule_limits.py` + `test_policy_fixture.py` | 每条默认规则一正一反；OBJECT-001 fixture 包 |
| 裁决模型 | `tests/test_decision_reducer.py` | F-01~F-05 表驱动、优先级/稳定性/matched_rules |
| 编排 | `tests/test_evaluator_orchestration.py` | 八阶段顺序与调用计数、失败路径提前终止 |
| 门控 | `tests/test_acl_llm_gating.py` | ACL/LLM 职责边界（ACL-01/02、LLM-01/02/03） |
| 默认一致性 | `tests/test_default_consistency.py` | 四入口默认值一致 |
| 面积关系矩阵 | `tests/test_network_requirement_area_relations.py` | 30 个版本化 batch、159 条需求、261 items |

## Markers

```powershell
.\.venv\Scripts\python.exe -m pytest -m llm_guard -q        # 输出守卫单测
.\.venv\Scripts\python.exe -m pytest -m llm_pipeline -q     # 离线 LLM 编排与回退
.\.venv\Scripts\python.exe -m pytest -m llm_http -q         # HTTP 边界合约
.\.venv\Scripts\python.exe -m pytest -m network_plan_contract -q  # 真实网段规划合约（默认跳过）
```

## 真实模型评测（显式门禁）

`evals/llm/` 只提交合成 provisional 数据。真实评测需显式指定目录、
`RUN_REAL_LLM_EVAL=1`、业务 owner 批准的 gold manifest 和安全环境变量，
任一门禁缺失即在建连前 skip（见 `evals/llm/README.md`）。

## 规则可达性约定

每条默认启用规则必须有至少一个正例和一个 near-miss（端到端或规则级）。
OBJECT-001 已在默认包禁用，正/反例由
`tests/fixtures/policies/network_plan_object_relation/` 承接。
