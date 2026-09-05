# FARE 统一 YAML 配置使用说明

> 本文档描述 YAML schema 的完整字段与模式切换方法。仓库内跟踪的 `config/*.yaml`
> 一律不携带真实 `llm.http.api_key`；真实密钥放在未入库的 `config/fare.local.yaml`。
> 开发/测试默认（`config/fare.yaml` 与测试 conftest）为全 mock：
> `network_plan.mode=mock`（绑定版本化 fixture）、
> `llm.mode=mock`、shadow 特性 `off`；`offline_catalog` 与 `required` 必须显式指定。

## 1. 配置文件和原则

FARE 只读取一个配置文件：

```text
config/fare.yaml
```

程序不读取环境变量。规则包始终从 `policy.directory` 指定的本地目录加载；
`requirement_source.mode` 只决定网络开通需求来自本地 JSON 还是需求 API。

所有相对路径以 `fare.yaml` 所在目录为基准。例如 `../policies` 对应仓库根目录下的
`policies/`，不会随启动命令的当前目录变化。

仓库提供 `fare.external-test.yaml`、`fare.intranet-uat.yaml` 和 `fare.production.yaml`
三个完整 Profile。应用不会在运行时热切换配置；应通过 `--config` 选择目标 Profile 并重启。

启动前可以校验配置并获取稳定的配置指纹：

```powershell
.\.venv\Scripts\python.exe -m app validate-config --config config/fare.intranet-uat.yaml
```

响应和审计记录包含 `config_id`、`environment` 和 `config_fingerprint`。幂等记录按配置
指纹隔离，同一 `request_id` 在不同 Profile 下会分别执行。

## 2. 启动 API 服务

```powershell
.\.venv\Scripts\python.exe -m app serve --config config/fare.yaml
```

服务地址由以下字段控制：

```yaml
server:
  host: 0.0.0.0
  port: 8000
```

## 3. 执行网络需求输入

```powershell
.\.venv\Scripts\python.exe -m app requirements --config config/fare.yaml
```

该命令读取配置的数据源，把每条需求严格校验成 `EvaluationRequest`，经过与
`POST /v1/evaluations` 相同的完整流程，然后把结果写入：

```yaml
requirement_source:
  output_file: ../run_results/network_requirement_results.json
```

`execution.mode=once` 执行一次后退出；`poll` 按 `poll_interval_seconds` 周期执行。
重复的 `request_id` 仍由现有审计幂等机制处理。

## 4. 本地需求模式

```yaml
requirement_source:
  mode: local
  execution:
    mode: once
    batch_size: 100
    poll_interval_seconds: 30
  local:
    directory: ../inputs/network_requirements
    pattern: "*.json"
    recursive: false
```

一个本地文件是一批网络需求，格式为：

```json
{
  "schema_version": "fare-requirement-batch/v1",
  "requests": [
    {
      "request_id": "example-001",
      "sources": [{"address": "20.1.10.10", "description": "办公终端"}],
      "destinations": [{"address": "16.1.20.20", "description": "生产数据库"}],
      "protocol": "tcp",
      "ports": [{"start": 443, "end": 443}],
      "request_description": "办公终端访问生产数据库"
    }
  ]
}
```

目录中所有文件的 `request_id` 必须唯一，未知字段、非法地址、非法端口和错误版本都会终止
本批执行。仓库示例位于 `inputs/network_requirements/example.json`。

## 5. 网络开通需求 API 模式

只需把模式改成 `api`：

```yaml
requirement_source:
  mode: api
  execution:
    mode: once
    batch_size: 100
    poll_interval_seconds: 30
  api:
    url: http://127.0.0.1:9000/v1/network-requirements
    method: GET
    timeout_seconds: 30
    auth:
      type: bearer
      token: test-requirement-token
```

GET 模式会发送查询参数 `batch_size`；POST 模式会发送：

```json
{"batch_size": 100}
```

API 必须返回与本地文件相同的 `fare-requirement-batch/v1` JSON。返回非 2xx、无法解析的
JSON、未知字段、重复 `request_id` 或超过 `batch_size` 都会使本批失败。

需求 API 只能返回网络需求，不能指定规则包。本地规则包始终由以下配置决定：

```yaml
policy:
  directory: ../policies
```

## 6. 网段规划和大模型

网段规划 API：

```yaml
network_plan:
  mode: http
  http:
    url: http://127.0.0.1:9001/v1/network-plan
    query_parameter: subnet
    auth:
      type: bearer
      token: test-network-plan-token
```

0.3.0 起不再存在任何候选路径/拟配置分析配置（原 `acl:` 节与
`llm.features.acl_candidate_mode` 已删除）；配置 schema 保持 `extra='forbid'`，
残留键会在启动时明确报错。

大模型 API：

```yaml
llm:
  mode: http
  http:
    base_url: https://open.bigmodel.cn/api/paas/v4
    model: glm-4.5-air
    api_key: test-bigmodel-api-key
    temperature: 1
    max_tokens: 4096
    top_p: 1
    stream: false
    stop: null
    thinking: disabled
```

大模型接口采用 OpenAI 兼容的 `/chat/completions` 契约。
FARE 需要一次性接收并严格校验完整 JSON，因此 `stream` 固定为 `false`；配置为 `true`
会在启动时被拒绝。

## 7. 配置校验

配置加载器采用严格 Schema：

- `schema_version` 必须是 `fare-config/v1`。
- 未知或拼写错误的字段会阻止启动。
- 当前模式需要的目录、URL 和参数必须存在。
- 数量、并发、超时和端口必须在合法范围内。
- `local/api` 只切换网络需求来源，不影响本地规则包。
- `mock/http/offline_catalog` 只控制对应依赖，不改变需求来源。

## 8. Python 3.13.7 运行环境

项目统一使用 Python 3.13.7。首次安装时创建独立虚拟环境：

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

`python --version` 必须输出 `Python 3.13.7`。启动时通过 `--config` 显式选择完整
Profile，不使用环境变量覆盖 YAML：

```powershell
.\.venv\Scripts\python.exe -m app serve --config config/fare.intranet-uat.yaml
```

切换 Profile 后必须重启 Python 进程。外网测试、内网 UAT 和生产 Profile 应分别使用
不同的 `audit.directory` 与 `requirement_source.output_file`。生产运行应交由操作系统服务
管理器托管，并为配置和规则目录设置只读权限，为审计和结果目录设置受控写权限。
