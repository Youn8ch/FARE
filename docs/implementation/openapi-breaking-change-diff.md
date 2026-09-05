# OpenAPI Breaking-Change Diff（0.2.0 → 0.3.0）

> 生成方式：`app.openapi()` 路径级 diff + 基线提交（45805c6）`app/schemas.py`
> 与当前 `app/schemas.py` 的 `model_json_schema()` 字段级对比。
> （说明：本环境 fastapi 0.141 对带 wrap serializer 的模型在 OpenAPI 合成中
> 生成退化的空 properties——该怪癖在 0.2.0 基线即存在，与本次变更无关；
> 因此字段级 diff 以 pydantic 模型 schema 为准。）

## 1. 版本

- `info.version`: **0.2.0 → 0.3.0**

## 2. 路径

| 变更 | 内容 |
|---|---|
| 新增 | `POST /v2/evaluations`（唯一正式评估入口；200/409/422） |
| 语义变更 | `POST /v1/evaluations` 收敛为兼容窗口存根，仅返回 **410 + `API_VERSION_RETIRED`**（响应 schema 不再包含 200 EvaluationResponse）；路由本身留待后续协调发布删除 |
| 不变 | `/healthz`、`/readyz` |

## 3. 响应 schema 字段移除（breaking）

| 模型 | 移除字段 |
|---|---|
| `EvaluationItem` | `acl_verification_status` |
| `EvaluationResponse` | `acl_analysis`、`acl_candidate_analysis` |
| `DecisionFinding.source` | Literal 收敛：移除 `'acl'`（现 `network \| rule \| semantic`） |
| `ReasonType` | 移除 `'acl_no_path'` |

## 4. 删除的 schema 组件（11 个）

`AclAnalysis`、`AclCandidateAnalysis`、`AclCandidateComparison`、
`AclCandidateEvidenceSource`、`AclCandidateFactType`、`AclCandidateMergeStatus`、
`AclRawResponse`、`ExtractedFacts`、`LlmAclCandidateFact`、
`LlmAclExtractionItem`、`LlmAclExtractionResponse`

## 5. 调用方迁移

1. `POST /v1/evaluations` → `POST /v2/evaluations`（请求体契约不变；响应不再含
   已删字段）。
2. 解析 `acl_analysis` / `acl_candidate_analysis` / `acl_verification_status`
   的代码需移除；决策语义变化见批准差异（RN-030~034 由待定变合规的情形）。
3. 配置中的 `acl:` 节与 `llm.features.acl_candidate_mode` 删除后重启。
4. 审计侧：改读 `fare-audit-v2.sqlite3`（epoch `fare-audit/v2-no-acl`）；
   0.3.0 前归档只读保留。
