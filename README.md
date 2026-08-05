# FARE

FARE（Firewall Access Request Evaluator）是一个 FastAPI 同步预审服务。它先按 IPv4 `/24` 查询网段规划 API 获取权威区域事实，再按事实边界拆分实际申请范围并执行确定性规则；查询粒度不会扩大单 IP、`/25` 等真实访问范围。ACL 路径/拟配置分析仍与业务结论严格分开。业务结论只会是 `合规` 或 `待定`；服务不会创建、修改或下发防火墙策略。

## 本地运行

要求 Python 3.11 以上：

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/uvicorn app.main:app --reload
```

PowerShell 可直接测试：

```powershell
$body = @{
  request_id = "fare-eval-demo-001"
  sources = @(@{address = "16.1.30.10"; description = "生产应用"})
  destinations = @(@{address = "16.1.30.20"; description = "生产应用"})
  protocol = "tcp"
  ports = @(@{start = 443; end = 443})
  request_description = "生产应用内部 HTTPS 访问"
} | ConvertTo-Json -Depth 5
Invoke-RestMethod http://localhost:8000/v1/evaluations -Method Post -ContentType application/json -Body $body
```

也可以使用 `docker compose up --build`。OpenAPI 位于 `/docs`，存活和就绪探针分别为 `/healthz`、`/readyz`。

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `POLICY_DIR` | `policies` | 只读规则包目录 |
| `AUDIT_LOG_DIR` | `audit_logs` | JSON Lines 审计目录 |
| `AUDIT_LOG_RETENTION_DAYS` | `30` | 审计与幂等索引保留天数 |
| `NETWORK_PLAN_CLIENT_MODE` | `mock` | `mock`、`http` 或显式兼容模式 `offline_catalog` |
| `NETWORK_PLAN_MOCK_FILE` | 空 | 版本化 Mock Fixture，格式见 `tests/fixtures/network_plan/` |
| `NETWORK_PLAN_API_URL` | 空 | 真实 API 地址；`http` 模式必填 |
| `NETWORK_PLAN_HTTP_QUERY_PARAMETER` | 空 | 合约确认后的查询参数名；未配置时 HTTP Adapter 故障闭合且不发请求 |
| `NETWORK_PLAN_TIMEOUT_SECONDS` | `5` | 单个 `/24` 查询超时 |
| `NETWORK_PLAN_BATCH_TIMEOUT_SECONDS` | `15` | 单申请规划查询总超时 |
| `NETWORK_PLAN_MAX_CONCURRENCY` | `8` | 单申请网段查询并发上限 |
| `NETWORK_PLAN_MAX_SUBNETS_PER_REQUEST` | `64` | 单申请唯一 `/24` 硬上限 |
| `NETWORK_PLAN_CACHE_TTL_SECONDS` | `0` | `0` 仅请求级去重；正数启用有界成功响应缓存 |
| `NETWORK_PLAN_CACHE_MAX_ENTRIES` | `1024` | 跨请求缓存容量；404、失败和非法响应不缓存 |
| `MAX_EVALUATION_ITEMS` | `256` | 构造源×目的×端口组合前的硬上限 |
| `ACL_CLIENT_MODE` | `mock` | `mock` 或 `http` |
| `ACL_MOCK_FILE` | 空 | 可选 Mock 响应文件，格式见 `examples/acl_mock.json` |
| `ACL_API_URL` | 空 | 真实 ACL API 地址；当前 HTTP Adapter 在合约落定前故障闭合 |
| `ACL_TIMEOUT_SECONDS` | `10` | ACL 超时预算 |
| `ACL_MAX_CONCURRENCY` | `8` | 单申请 ACL 辅助调用并发上限 |
| `ACL_DECISION_MODE` | `advisory` | `advisory` 仅辅助；`required` 下未核验会转待定 |
| `LLM_CLIENT_MODE` | `mock` | `mock`（离线受控流水线）或 `http`（内网生产模型） |
| `LLM_BASE_URL` | 空 | OpenAI 兼容地址，需包含 `/v1` |
| `LLM_MODEL` | 空 | 内网模型名 |
| `LLM_API_KEY` | 空 | 可选密钥，不写入日志 |
| `LLM_MOCK_FILE` | 空 | 可选版本化 Mock 响应 Fixture |
| `LLM_ACL_CANDIDATE_MODE` | `off` | `off` 或 `shadow`；影子抽取不参与业务裁决 |
| `LLM_REQUEST_FINDINGS_MODE` | `off` | `off` 或 `shadow`；`guarded` 尚未获批并会拒绝启动 |
| `LLM_SEMANTIC_TIMEOUT_SECONDS` | `10` | 全申请批量语义分析超时 |
| `LLM_EXPLANATION_TIMEOUT_SECONDS` | `6` | 结论解释超时 |
| `LLM_MAX_CORRECTION_RETRIES` | `1` | 严格 JSON 纠错次数（仅允许 0 或 1） |
| `MAX_CONCURRENT_EVALUATIONS` | `4` | 进程内评估并发上限 |

每次评估都会批量执行语义分析。`mock` 模式用于外网离线开发，仍会经过声明 Schema、证据定位、规则编号和子项覆盖守卫；`http` 模式用于目标内网生产。必要语义分析超时、非法 JSON、虚构规则或无证据风险均故障闭合为 `dependency_failure` 待定，解释阶段失败只回退规则模板，不改变结论。

离线 Mock 未配置 Fixture 时，会为所有组合生成最小、可定位证据的候选声明。需要验证矛盾、规则缺口或解释回退时，可参考 `examples/llm_mock.json` 提供按 `request_id` 固定的版本化响应；Mock 只验证管道契约，不代表模型质量验收。

`LLM_ACL_CANDIDATE_MODE=shadow` 会对同一申请的全部组合执行一次批量 LLM 候选事实抽取，无 ACL 原文的组合以空字符串参与完整覆盖。候选事实必须逐项绑定 `item_id`、`source` 和可逐字定位且包含事实值的 `evidence`。响应中的 `acl_candidate_analysis` 始终分别保留 `deterministic` 与 `llm_candidate` 两层及 `agree|llm_only|deterministic_only|conflict|empty|rejected` 合并状态；该字段只用于影子观测和审计，不会写回正式 `acl_analysis.extracted_facts`，也不会改变 `decision`、`reason_code` 或 `matched_rules`。默认 `off` 时不调用该阶段，响应也不包含该字段。Mock Fixture 可在 `semantic`、`explanation` 之外增加 `acl_candidates` stage；缺省时生成覆盖全部输入 item 的空候选。

`LLM_REQUEST_FINDINGS_MODE=shadow` 会对同一申请的全部组合执行一次申请级批量分析，仅观察跨组合的业务上下文混用、目的不一致、缺少依据、临时范围不匹配和审批上下文缺失。每条 finding 都必须完整绑定受影响 item 及其 `source/quote` 证据，并为请求人生成补充问题。结果只写入独立 `request_findings` 响应字段、审计和计数，不会改变正式 `decision`、`reason_code`、`matched_rules`、`reason` 或 `recommendation`。默认 `off` 时不调用且省略字段；`guarded` 尚无获批阈值，配置后会明确拒绝启动。Mock Fixture stage 名为 `request_findings`，缺省时生成完整 item 覆盖且 findings 为空的结果。

解释阶段只可写入独立的 `llm_explanation` 与 `llm_recommendation`；服务端规范 `reason`、`recommendation`、`decision`、`reason_code` 和 `matched_rules` 始终锁定。解释必须完整覆盖 item、非空且不超过 4000 字符，只能引用该 item 实际命中的正式规则，并会拒绝已审批、现网已放通、路由/NAT 已确认或与结论相反等已知越权表述。守卫采用有限词表和结构校验，不是对任意自然语言的完备证明；失败时只清空可选模型文案并保留规范模板。

## 规则与 ACL 边界

生产 `mock/http` 模式只加载 `manifest.yaml` 和 `compliance_rules.yaml`，不要求 `network_catalog.yaml`；规则版本与网段事实独立审计。只有显式 `offline_catalog` 兼容模式加载静态目录并校验目录与规则版本。规则加载器会拒绝未知、拼写错误或不生效的 `when` 条件。默认发布包不包含尚未获批的区域访问拒绝规则。

同一申请的源、目的地址先统一规划并去重，每个唯一 `/24` 最多查询一次。成功、404、依赖失败和非法响应分别保留；相邻且区域聚合键一致的 `/24` 可在同一原始地址内重新合并，不同区域和失败状态形成拆分边界。`any` 不查询，IPv6 当前以 `NETWORK_PLAN_IPV6_UNSUPPORTED` 待定。网段规划失败不会静默回退静态目录，并会跳过对应 item 的 ACL 调用。

网段 API 的请求方法、参数名和鉴权仍待正式合约确认。`HttpNetworkPlanClient` 在未配置已确认的查询参数前不会猜测或发出请求；拿到正式 OpenAPI/脱敏样例后，应补齐带 `network_plan_contract` 标记且默认跳过的真实合约测试，再启用生产 HTTP 流量。

当前没有真实 ACL API 契约。按照方案要求，`HttpAclClient` 不猜测请求字段，也不会向配置地址发送请求；启用 `http` 模式会为业务项返回 `ACL_DEPENDENCY_FAILURE` 待定。拿到真实 OpenAPI/样例及“明确无路径”的机器表达后，应实现字段映射和合约测试，再启用该模式。

## 幂等与审计

规范化输入相同的 `request_id` 重放会返回首次完成的响应和原 `audit_id`；处理中重放返回 `409 evaluation_in_progress`，相同 ID 配不同输入返回 `409 idempotency_conflict`。完成结果在 HTTP 200 前同步写入按日滚动的 JSON Lines 文件，服务重启时会从保留期内日志重建幂等索引。

审计为网段规划原始响应与校验结果、semantic、ACL candidate、request findings 和 explanation 分别记录状态、耗时、失败类型与纠错次数，并携带 model、Prompt、policy 和 Fixture 版本。最终 item 可追溯到原始地址、查询 `/24`、稳定事实 ID、逐 item ACL 状态和规则。输入、依赖/模型原文、异常与最终响应使用同一脱敏规则；首次响应、内存重放和重启重放均返回同一份已脱敏结果。

## 测试

```bash
pytest
ruff check .
```

默认测试禁止非 loopback 网络连接，并且 `testpaths = ["tests"]` 不会收集真实模型评测目录。可按职责选择离线测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -m llm_guard -q
.\.venv\Scripts\python.exe -m pytest -m llm_pipeline -q
.\.venv\Scripts\python.exe -m pytest -m llm_http -q
.\.venv\Scripts\python.exe -m pytest evals/llm/test_contract_dataset.py -q
```

`evals/llm/` 内提交的是至少 20 条完全合成的 provisional 合同数据，只验证 Schema、runner、安全不变量和机器指标格式，不代表业务 gold 或模型质量。真实模型评测还必须显式指定该目录、设置 `RUN_REAL_LLM_EVAL=1`、使用业务 owner 批准的 gold manifest，并从安全环境提供 endpoint/model；任一门禁缺失都会在建连前 skip。详细门禁见 `evals/llm/README.md`。

真实 ACL 合约、生产规则审批、鉴权与组织级日志脱敏策略仍属于上线前外部依赖，详见原方案文档。

本仓库只包含独立 FARE 服务。定时项目中的 `fare_evaluations` 表、原子任务领取、租约恢复、有限并发和重试逻辑需在现有定时项目仓库中按《FARE-定时任务集成方案》落地；FARE 本身不会连接业务工单数据库或生成技术失败对应的业务结论。
