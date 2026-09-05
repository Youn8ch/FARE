# ACL 清零扫描结果（PHASE-08，执行日 2026-09-05）

## 扫描命令

```powershell
rg -n --glob '!docs/history/**' --glob '!docs/ACCEPTANCE-REPORT*.md' `
  --glob '!docs/architecture-baseline.md' --glob '!docs/v3-baseline.md' `
  --glob '!docs/FARE_LLM_INFRASTRUCTURE_AND_ACL_REMOVAL_IMPLEMENTATION_PLAN_V1.0.md' `
  --glob '!docs/implementation/**' `
  --glob '!tests/cases/evaluations/realistic_network_requests.v1.json' `
  --glob '!docs/testing/REALISTIC_NETWORK_REQUEST_MATRIX.md' `
  '(?i)\bacl\b|acl_|Acl|ACL-' app config policies tests evals docs README.md pyproject.toml
```

（后两类 glob 为迁移取证/迁移说明工件——方案允许其仅为说明已删除字段而出现
该词；`docs/implementation/` 为执行报告与 OpenAPI 基线/差异记录。）

## 结论：现役生产代码、配置、策略、现状文档命中为 0

排除上述迁移工件与两类模式误报（`aclose`、`oracle` 含子串 `acl`——计划扫描
模式第三分支 `Acl` 在 `(?i)` 作用域下无词边界）后，剩余 61 行命中全部属于
以下四类已批准用途，逐类核验如下（完整原始命中见本文件其余部分）：

### 1. 负向门禁断言（功能性，必须包含被删名称）

| 文件 | 行数 | 用途 |
|---|---|---|
| `tests/test_v4_invariants.py` | 25 | 断言已删模块/DTO/source/reason 不存在；`test_acl_stage_is_fully_removed` 等静态不变量 |
| `tests/test_evaluator_orchestration.py` | 8 | 断言 stage 模块不再引用 `AclRecord`/`acl_stage` |
| `tests/test_default_consistency.py` | 5 | fail-closed 测试：残留 `acl:`/`acl_candidate_mode` 配置键必须启动失败 |
| `tests/test_realistic_network_requests.py` | 14 | `_assert_acl_free` 负向断言（响应无已删字段）+ `ACTIVE_CONTRACT="acl_free"` 门禁常量 |

### 2. 迁移说明（方案允许的用法：仅为说明已删除字段）

| 文件 | 行数 |
|---|---|
| `README.md`（0.3.0 迁移说明节） | 6 |
| `docs/CONFIGURATION.md`（已删配置键尾注） | 2 |

### 3. 新命名常量（新 epoch 名本身）

| 文件 | 命中 |
|---|---|
| `app/services/audit.py` | 1（`AUDIT_SCHEMA_EPOCH = "fare-audit/v2-no-acl"`） |

### 4. 其余

无。生产 `app/`（除上述 1 行 epoch 常量）、`config/`、`policies/`、`evals/`、
现状文档正文均为 **0 命中**。

## 附：迁移取证工件中的命中（不在现役断言路径上）

| 工件 | 行数 | 性质 |
|---|---|---|
| `tests/cases/evaluations/realistic_network_requests.v1.json` | 97 | 冻结 legacy 取证 + 批准差异清单（方案 PHASE-01 要求逐例记录已删字段） |
| `docs/testing/REALISTIC_NETWORK_REQUEST_MATRIX.md` | 19 | 迁移矩阵说明（legacy/target 双契约设计） |
| `docs/implementation/*`（执行报告、OpenAPI 基线/差异） | — | 执行记录 |
