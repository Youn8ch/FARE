# FARE 集成与部署

## 启动

```powershell
.\.venv\Scripts\python.exe -m app serve --config config/fare.yaml         # API 服务
.\.venv\Scripts\python.exe -m app requirements --config config/fare.yaml  # CLI 批量
.\.venv\Scripts\python.exe -m app validate-config --config config/fare.yaml
```

- 程序只读取一个 YAML（默认 `config/fare.yaml`），不读取环境变量；
- 相对路径相对配置文件所在目录解析；
- HTTP API、CLI 批量、evals 三入口共享同一 Runtime，行为同源；
- 每个响应与审计记录携带 `config_id` / `environment` / `config_fingerprint`，
  幂等缓存按指纹隔离。

## 配置 Profile

| 配置 | 用途 |
| --- | --- |
| `config/fare.yaml` | 开发/测试默认：全 mock（网段规划绑定版本化 fixture），LLM mock |
| `config/fare.local.yaml` | 未入库本地副本：真实 `llm.http.api_key` 只放这里 |
| `config/fare.external-test.yaml` | 外网模型验证（offline 目录 + 真实模型） |
| `config/fare.intranet-uat.yaml` | 内网需求、网段规划与模型联调 |
| `config/fare.production.yaml` | 生产目标（真实 ACL Adapter 完成前不得验收） |

密钥管理：仓库内 `config/*.yaml` 一律不含真实 `llm.http.api_key`；
部署时复制对应 profile 到 `config/fare.local.yaml`（已 gitignore）后填写，
并以 `--config config/fare.local.yaml` 启动。

## 默认值一致性

四个默认入口（Settings dataclass 默认、YAML schema 默认、conftest fixture、
dev profile）保持一致：`network_plan.mode=mock`、`acl.mode=mock`、
`acl.decision_mode=advisory`、`llm.mode=mock`、shadow 特性 `off`。
`offline_catalog` 与 `required` 永远显式声明，禁止作为隐式默认。
一致性由 `tests/test_default_consistency.py` 验证。

## 依赖边界

- 网段规划 API：`GET {url}?{query_parameter}={/24}`，返回
  `{code, success, data:{areaId, regionName, platformName, network, subnet, usageCode, ...}}`；
  合约变更需先补 `network_plan_contract` 标记合约测试；
- ACL Adapter：真实契约未定，`http` 模式故障闭合为 `ACL_DEPENDENCY_FAILURE`；
- LLM：OpenAI 兼容 `/chat/completions`；mock 模式离线开发，输出一律过守卫。

## 幂等与审计

- 相同 `request_id` + 相同规范化输入 → 返回首次响应（同 `audit_id`）；
- 处理中重放 → `409 evaluation_in_progress`；不同输入 → `409 idempotency_conflict`；
- SQLite（`fare-audit.sqlite3`）维护权威幂等状态，JSONL 按日滚动归档；
- 审计记录网段规划原文/校验、semantic、ACL candidate、request findings、
  explanation 各阶段状态、耗时、失败类型与纠错次数，输入与原文统一脱敏。

## 上线前外部依赖（未处理，不属于本轮架构收口）

真实 ACL 合约、生产规则审批、鉴权与组织级日志脱敏策略。
