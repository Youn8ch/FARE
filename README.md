# FARE

FARE（Firewall Access Request Evaluator）是一个 FastAPI 同步预审服务。它将防火墙访问申请拆分为独立组合，使用版本化 YAML 网络目录与规则包作确定性评估，并把 ACL 路径/拟配置分析与业务结论严格分开。业务结论只会是 `合规` 或 `待定`；服务不会创建、修改或下发防火墙策略。

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
| `ACL_CLIENT_MODE` | `mock` | `mock` 或 `http` |
| `ACL_MOCK_FILE` | 空 | 可选 Mock 响应文件，格式见 `examples/acl_mock.json` |
| `ACL_API_URL` | 空 | 真实 ACL API 地址；当前 HTTP Adapter 在合约落定前故障闭合 |
| `ACL_TIMEOUT_SECONDS` | `10` | ACL 超时预算 |
| `LLM_CLIENT_MODE` | `mock` | `mock`（离线受控流水线）或 `http`（内网生产模型） |
| `LLM_BASE_URL` | 空 | OpenAI 兼容地址，需包含 `/v1` |
| `LLM_MODEL` | 空 | 内网模型名 |
| `LLM_API_KEY` | 空 | 可选密钥，不写入日志 |
| `LLM_MOCK_FILE` | 空 | 可选版本化 Mock 响应 Fixture |
| `LLM_SEMANTIC_TIMEOUT_SECONDS` | `10` | 全申请批量语义分析超时 |
| `LLM_EXPLANATION_TIMEOUT_SECONDS` | `6` | 结论解释超时 |
| `LLM_MAX_CORRECTION_RETRIES` | `1` | 严格 JSON 纠错次数（仅允许 0 或 1） |
| `MAX_CONCURRENT_EVALUATIONS` | `4` | 进程内评估并发上限 |

每次评估都会批量执行语义分析。`mock` 模式用于外网离线开发，仍会经过声明 Schema、证据定位、规则编号和子项覆盖守卫；`http` 模式用于目标内网生产。必要语义分析超时、非法 JSON、虚构规则或无证据风险均故障闭合为 `dependency_failure` 待定，解释阶段失败只回退规则模板，不改变结论。

离线 Mock 未配置 Fixture 时，会为所有组合生成最小、可定位证据的候选声明。需要验证矛盾、规则缺口或解释回退时，可参考 `examples/llm_mock.json` 提供按 `request_id` 固定的版本化响应；Mock 只验证管道契约，不代表模型质量验收。

## 规则与 ACL 边界

规则包由 `manifest.yaml`、`network_catalog.yaml` 和 `compliance_rules.yaml` 构成，三者版本必须一致。默认发布包不包含尚未获批的区域访问拒绝规则；区域规则能力只在独立测试夹具中验证。仓库其余规则仍是可运行示例，上线前必须替换为经过审批的网络目录、阈值和规则，并保留审批信息。

当前没有真实 ACL API 契约。按照方案要求，`HttpAclClient` 不猜测请求字段，也不会向配置地址发送请求；启用 `http` 模式会为业务项返回 `ACL_DEPENDENCY_FAILURE` 待定。拿到真实 OpenAPI/样例及“明确无路径”的机器表达后，应实现字段映射和合约测试，再启用该模式。

## 幂等与审计

规范化输入相同的 `request_id` 重放会返回首次完成的响应和原 `audit_id`；处理中重放返回 `409 evaluation_in_progress`，相同 ID 配不同输入返回 `409 idempotency_conflict`。完成结果在 HTTP 200 前同步写入按日滚动的 JSON Lines 文件，服务重启时会从保留期内日志重建幂等索引。

## 测试

```bash
pytest
ruff check .
```

真实 ACL 合约、生产规则审批、鉴权与组织级日志脱敏策略仍属于上线前外部依赖，详见原方案文档。

本仓库只包含独立 FARE 服务。定时项目中的 `fare_evaluations` 表、原子任务领取、租约恢复、有限并发和重试逻辑需在现有定时项目仓库中按《FARE-定时任务集成方案》落地；FARE 本身不会连接业务工单数据库或生成技术失败对应的业务结论。
# FARE
