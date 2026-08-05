# FARE 网段规划 API 改造测试方案

## 1. 文档信息

| 项目 | 内容 |
| --- | --- |
| 文档状态 | 待评审 |
| 被测范围 | `/24` 查询规划、网段规划 Client、Resolver、规则、LLM、ACL 辅助和端到端评估 |
| 配套计划 | `网段规划API改造计划书.md` |
| 测试框架 | pytest、FastAPI TestClient、httpx MockTransport |
| 编制日期 | 2026-08-05 |

## 2. 测试目标

验证改造后系统满足以下核心不变量：

1. 网段规划 API 只接收 IPv4 `/24`。
2. 查询范围与实际访问范围严格分离。
3. 相同 `/24` 去重查询，但不同原始地址不被错误合并。
4. 成功、404、依赖失败、非法响应和事实冲突被准确区分。
5. 多 `/24` 地址能够按区域聚合或拆分。
6. LLM 深度参与语义分析，但不能覆盖权威网络事实或最终裁决。
7. ACL advisory/required 模式行为清晰、稳定。
8. 评估结果可审计、可重放且测试期间不访问真实外部服务。
9. 超大 CIDR 和访问组合在列表物化及依赖调用前被确定性拒绝。
10. 同一 `/24` 跨源、目的角色仍只形成一次 lookup、一次调用和一份权威事实。

## 3. 测试范围

### 3.1 包含范围

- IPv4 地址和 CIDR 到 `/24` 查询计划的转换。
- 多地址、多 `/24` 去重。
- Network Plan Mock/HTTP Client。
- 响应 Schema 和业务一致性校验。
- 区域聚合键生成。
- 地址范围按区域和失败边界拆分。
- 访问组合生成。
- 网段规划字段规则匹配。
- LLM 输入、输出和守卫。
- ACL 辅助/强制模式。
- API 响应、幂等、审计、脱敏和回归。
- `/24` 数量与访问 item 数量的两级资源安全预检。
- 生产 HTTP 模式与静态网络目录解耦。

### 3.2 不包含范围

- 真实网络路由、NAT 和连通性。
- 真实防火墙策略下发。
- 未取得正式契约前的真实网段规划环境压力测试。
- 未审批区域访问制度的业务正确性背书。

## 4. 测试分层

| 层级 | 目标 | 是否访问真实网络 |
| --- | --- | --- |
| 单元测试 | 地址归一化、响应校验、聚合、拆分、规则和守卫 | 否 |
| Client 合约测试 | HTTP 方法、参数、状态、超时和响应解析 | 否，使用 MockTransport |
| 组件测试 | Resolver 与 Mock Client 协作 | 否 |
| 集成测试 | Evaluator、规则、LLM Mock、ACL Mock | 否 |
| API 端到端测试 | `/v1/evaluations` 响应、幂等和审计 | 否 |
| 真实合约测试 | 经批准的真实 API 样例或隔离测试环境 | 默认关闭 |

所有表格中的“拒绝”必须落到明确的 HTTP 状态、原因码、item 状态或守卫状态，禁止保留“接受或拒绝”“拒绝或 candidate”一类不可自动断言的预期。

默认测试套件必须继续阻断非回环网络连接。

## 5. 测试文件规划

建议新增：

```text
tests/fixtures/network_plan/multi_region.v1.json
tests/fixtures/network_plan/failures.v1.json
tests/fixtures/network_plan/contract_samples.v1.json
tests/cases/evaluations/network_planning.v2.json
tests/test_network_plan_normalization.py
tests/test_network_plan_client.py
tests/test_network_plan_http_contract.py
tests/test_network_plan_resolver.py
tests/test_network_plan_rules.py
tests/test_network_plan_llm_guard.py
tests/test_network_plan_evaluation.py
tests/test_network_plan_resource_limits.py
```

现有数据驱动用例 Schema 增加：

```python
class Dependencies(StrictModel):
    network_plan_fixture: str | None = None
    acl_fixture: str | None = None
    policy_dir: str | None = None
```

同时修改：

```text
tests/case_schema.py
tests/helpers/case_loader.py
tests/conftest.py
tests/test_evaluation_cases.py
pyproject.toml
```

`case_schema.py` 需增加 `network_planning` suite、网段规划期望事实、逐 item ACL 状态和 HTTP 422 错误期望；`case_loader.py` 在新模式下不得再调用静态 `NetworkCatalog` 预计算组合，须安全加载 Network Plan Fixture 并在 Resolver 后生成符号；`pyproject.toml` 注册 `network_plan_contract` marker。

建议至少扩展以下期望结构：

```python
class ExpectedItem(StrictModel):
    source_network_fact_ids: list[str]
    destination_network_fact_ids: list[str]
    source_network_fact_status: str
    destination_network_fact_status: str
    acl_verification_status: str


class ExpectedError(StrictModel):
    http_status: Literal[422]
    code: Literal[
        "NETWORK_PLAN_QUERY_LIMIT_EXCEEDED",
        "EVALUATION_ITEM_LIMIT_EXCEEDED",
    ]
    dependency_calls: dict[str, int]
```

## 6. Mock 响应设计

### 6.1 Fixture 顶层结构

为了同时模拟 HTTP 状态、超时、非 JSON 和业务响应，建议使用以下 Fixture 格式：

```json
{
  "fixture_version": "2026.08.0",
  "purpose": "fixture purpose",
  "responses": {
    "16.201.1.0/24": {
      "http_status": 200,
      "delay_ms": 0,
      "body": {}
    }
  }
}
```

单个响应支持四种模拟方式：

```json
{
  "http_status": 200,
  "delay_ms": 0,
  "body": {}
}
```

```json
{
  "transport_error": "timeout"
}
```

```json
{
  "transport_error": "connection_error"
}
```

```json
{
  "http_status": 200,
  "raw_body": "not-json"
}
```

如果查询键不存在，Mock Client 必须返回明确的 Fixture 缺失错误，不能自动构造成功响应。

### 6.2 多区域主 Fixture

建议保存为 `tests/fixtures/network_plan/multi_region.v1.json`：

```json
{
  "fixture_version": "2026.08.0",
  "purpose": "多个 /24 的同区域聚合、多区域拆分及部分 404 测试",
  "responses": {
    "16.201.0.0/24": {
      "http_status": 200,
      "delay_ms": 0,
      "body": {
        "code": 200,
        "msg": "success",
        "data": {
          "area": "鳌峰科创生产区",
          "areaId": "鳌峰科创生产区",
          "regionName": "鳌峰生产区（科创鲲鹏应用）",
          "platformName": "鳌峰生产区（科创鲲鹏应用）",
          "network": "16.201.0.0/22",
          "gateway": "16.201.0.1",
          "subnet": "16.201.0.0/24",
          "vlanId": "299",
          "usageCode": null,
          "description": "鲲鹏虚拟机（云管分配）"
        },
        "success": true
      }
    },
    "16.201.1.0/24": {
      "http_status": 200,
      "delay_ms": 0,
      "body": {
        "code": 200,
        "msg": "success",
        "data": {
          "area": "鳌峰科创生产区",
          "areaId": "鳌峰科创生产区",
          "regionName": "鳌峰生产区（科创鲲鹏应用）",
          "platformName": "鳌峰生产区（科创鲲鹏应用）",
          "network": "16.201.0.0/22",
          "gateway": "16.201.0.1",
          "subnet": "16.201.1.0/24",
          "vlanId": "300",
          "usageCode": null,
          "description": "鲲鹏虚拟机（云管分配）"
        },
        "success": true
      }
    },
    "16.201.2.0/24": {
      "http_status": 200,
      "delay_ms": 0,
      "body": {
        "code": 200,
        "msg": "success",
        "data": {
          "area": "鳌峰科创生产区",
          "areaId": "鳌峰科创生产区",
          "regionName": "鳌峰生产区（通用计算平台）",
          "platformName": "鳌峰通用计算平台",
          "network": "16.201.0.0/22",
          "gateway": "16.201.2.1",
          "subnet": "16.201.2.0/24",
          "vlanId": "301",
          "usageCode": "GENERAL_VM",
          "description": "通用虚拟机"
        },
        "success": true
      }
    },
    "16.201.3.0/24": {
      "http_status": 200,
      "delay_ms": 0,
      "body": {
        "code": 404,
        "msg": "网段规划不存在",
        "data": null,
        "success": false
      }
    },
    "16.210.8.0/24": {
      "http_status": 200,
      "delay_ms": 0,
      "body": {
        "code": 200,
        "msg": "success",
        "data": {
          "area": "本部办公区",
          "areaId": "本部办公区",
          "regionName": "本部办公终端区",
          "platformName": "办公网络",
          "network": "16.210.8.0/22",
          "gateway": "16.210.8.1",
          "subnet": "16.210.8.0/24",
          "vlanId": "810",
          "usageCode": "OFFICE_CLIENT",
          "description": "办公终端"
        },
        "success": true
      }
    },
    "16.220.16.0/24": {
      "http_status": 200,
      "delay_ms": 0,
      "body": {
        "code": 200,
        "msg": "success",
        "data": {
          "area": "核心生产区",
          "areaId": "核心生产区",
          "regionName": "核心生产数据库区",
          "platformName": "核心数据库平台",
          "network": "16.220.16.0/22",
          "gateway": "16.220.16.1",
          "subnet": "16.220.16.0/24",
          "vlanId": "916",
          "usageCode": "PROD_DATABASE",
          "description": "生产数据库服务器"
        },
        "success": true
      }
    }
  }
}
```

### 6.3 异常 Fixture

建议保存为 `tests/fixtures/network_plan/failures.v1.json`：

```json
{
  "fixture_version": "2026.08.0",
  "purpose": "网段规划依赖失败和非法响应测试",
  "responses": {
    "192.0.2.0/24": {
      "transport_error": "timeout"
    },
    "192.0.3.0/24": {
      "transport_error": "connection_error"
    },
    "192.0.4.0/24": {
      "http_status": 500,
      "body": {
        "code": 500,
        "msg": "internal error",
        "data": null,
        "success": false
      }
    },
    "192.0.5.0/24": {
      "http_status": 200,
      "raw_body": "not-json"
    },
    "192.0.6.0/24": {
      "http_status": 200,
      "body": {
        "code": 200,
        "msg": "success",
        "data": null,
        "success": true
      }
    },
    "192.0.7.0/24": {
      "http_status": 200,
      "body": {
        "code": 200,
        "msg": "success",
        "data": {
          "area": "测试区",
          "areaId": "测试区",
          "regionName": "测试区 A",
          "platformName": "测试平台",
          "network": "192.0.4.0/22",
          "gateway": "192.0.7.1",
          "subnet": "192.0.8.0/24",
          "vlanId": "700",
          "usageCode": "TEST",
          "description": "返回了错误 subnet"
        },
        "success": true
      }
    },
    "192.0.8.0/24": {
      "http_status": 200,
      "body": {
        "code": 200,
        "msg": "success",
        "data": {
          "area": "测试区",
          "areaId": "测试区",
          "regionName": "测试区 B",
          "platformName": "测试平台",
          "network": "192.0.4.0/24",
          "gateway": "192.0.8.1",
          "subnet": "192.0.8.0/24",
          "vlanId": "701",
          "usageCode": "TEST",
          "description": "subnet 不属于 network"
        },
        "success": true
      }
    },
    "192.0.9.0/24": {
      "http_status": 200,
      "body": {
        "code": 200,
        "msg": "success",
        "data": {
          "area": "测试区",
          "areaId": "测试区",
          "regionName": "测试区 C",
          "platformName": "测试平台",
          "network": "192.0.8.0/22",
          "gateway": "invalid-gateway",
          "subnet": "192.0.9.0/24",
          "vlanId": "702",
          "usageCode": "TEST",
          "description": "非法网关"
        },
        "success": true
      }
    }
  }
}
```

## 7. 地址归一化测试用例

| 编号 | 输入 | 预期查询 | 预期实际范围 |
| --- | --- | --- | --- |
| NP-NORM-001 | `16.201.1.10` | `16.201.1.0/24` | `16.201.1.10/32` |
| NP-NORM-002 | `16.201.1.10/32` | `16.201.1.0/24` | `16.201.1.10/32` |
| NP-NORM-003 | `16.201.1.128/25` | `16.201.1.0/24` | `16.201.1.128/25` |
| NP-NORM-004 | `16.201.1.0/24` | `16.201.1.0/24` | `16.201.1.0/24` |
| NP-NORM-005 | `16.201.0.0/23` | `.0/24`、`.1/24` | `16.201.0.0/23` 或按区域拆分 |
| NP-NORM-006 | `16.201.0.0/22` | `.0/24` 至 `.3/24` | `/22` 内按区域拆分 |
| NP-NORM-007 | 同一 `/24` 的两个 IP | 只查询一次 | 保留两个 `/32` |
| NP-NORM-008 | `any` | 不查询 | 保留 `any` 并命中最小开放规则 |
| NP-NORM-009 | IPv6 `/128` | 不查询 | `NETWORK_PLAN_IPV6_UNSUPPORTED` |
| NP-NORM-010 | 超过 `/24` 数量上限的 CIDR | 不枚举、不发请求 | HTTP 422 `NETWORK_PLAN_QUERY_LIMIT_EXCEEDED` |
| NP-NORM-011 | `16.201.1.130/25` | `16.201.1.0/24` | 规范化实际范围 `16.201.1.128/25` |
| NP-NORM-012 | `0.0.0.0/0` | 区间计数，不枚举覆盖子网 | HTTP 422 `NETWORK_PLAN_QUERY_LIMIT_EXCEEDED` |
| NP-NORM-013 | `255.255.255.255/32` | `255.255.255.0/24` | `255.255.255.255/32` |
| NP-NORM-014 | 多个完全或部分重叠的大 CIDR | 合并 `/24` 索引区间后准确计数 | 不因简单求和误报超限，不枚举覆盖子网 |

关键断言：任何测试中都不得用查询 `/24` 替换单 IP、`/25` 等实际访问范围。

## 8. 响应校验测试用例

| 编号 | 场景 | 预期状态/原因码 |
| --- | --- | --- |
| NP-RESP-001 | 标准成功响应 | `resolved` |
| NP-RESP-002 | HTTP 200 + 业务 404 | `not_found / NETWORK_PLAN_NOT_FOUND` |
| NP-RESP-003 | HTTP 404 + 合法业务 404 | `not_found / NETWORK_PLAN_NOT_FOUND` |
| NP-RESP-004 | 超时 | `dependency_failure / NETWORK_PLAN_DEPENDENCY_FAILURE` |
| NP-RESP-005 | 连接失败 | `dependency_failure / NETWORK_PLAN_DEPENDENCY_FAILURE` |
| NP-RESP-006 | HTTP 500 | `dependency_failure / NETWORK_PLAN_DEPENDENCY_FAILURE` |
| NP-RESP-007 | 非 JSON | `invalid_response / NETWORK_PLAN_INVALID_RESPONSE` |
| NP-RESP-008 | `code=200` 但 `data=null` | `invalid_response` |
| NP-RESP-009 | `code=200` 但 `success=false` | `invalid_response` |
| NP-RESP-010 | 查询和返回 `subnet` 不同 | `NETWORK_PLAN_SUBNET_MISMATCH` |
| NP-RESP-011 | `subnet` 不属于 `network` | `NETWORK_PLAN_NETWORK_MISMATCH` |
| NP-RESP-012 | 非法 gateway | `NETWORK_PLAN_INVALID_RESPONSE` |
| NP-RESP-013 | 缺少区域必要字段 | `NETWORK_PLAN_INVALID_RESPONSE` |
| NP-RESP-014 | 返回 `subnet` 不是 `/24` | `NETWORK_PLAN_INVALID_RESPONSE` |
| NP-RESP-015 | 未知附加字段 | 接受并忽略，已知字段仍严格校验 |
| NP-RESP-016 | `code="200"` 或布尔值代替整数 | `NETWORK_PLAN_INVALID_RESPONSE`，禁止隐式转换 |
| NP-RESP-017 | HTTP 401/403 | `NETWORK_PLAN_AUTH_FAILURE`，内部告警信息不进入公开响应 |
| NP-RESP-018 | HTTP 408/429 | `NETWORK_PLAN_DEPENDENCY_FAILURE` |
| NP-RESP-019 | HTTP 204、3xx、其他未约定 4xx | `NETWORK_PLAN_INVALID_RESPONSE` |
| NP-RESP-020 | HTTP 200 但业务 `code=500` | `NETWORK_PLAN_INVALID_RESPONSE` |
| NP-RESP-021 | HTTP 500 但业务体伪装成功 | `NETWORK_PLAN_DEPENDENCY_FAILURE`，HTTP 状态优先 |
| NP-RESP-022 | camelCase `areaId/regionName/platformName/vlanId/usageCode` | Provider DTO 正确解析为内部 snake_case 事实 |

`gateway` 是否必须位于 `subnet`、`vlanId` 格式及 Unicode/空白规范化在正式契约确认前标记为 pending contract tests，不得写成猜测性生产断言。

## 9. 去重、并发和缓存测试用例

| 编号 | 场景 | 预期结果 |
| --- | --- | --- |
| NP-CACHE-001 | 源有两个 `.1/24` 内 IP，目的一条 `.8/24` IP | 只调用 2 个唯一 `/24` |
| NP-CACHE-002 | 源和目的均位于同一个 `/24` | 全申请只调用一次 |
| NP-CACHE-003 | 10 个唯一 `/24` | 调用 10 次，无重复 |
| NP-CACHE-004 | 响应完成顺序随机 | 最终事实和 item 顺序稳定 |
| NP-CACHE-005 | 其中一个 `/24` 超时 | 其他 `/24` 正常完成 |
| NP-CACHE-006 | 配置并发上限为 2 | 活跃查询数从不超过 2 |
| NP-CACHE-007 | 相同 `request_id` 成功重放 | 返回原评估结果，不重新调用依赖 |
| NP-CACHE-008 | Fixture 没有查询键 | 明确 Fixture 错误，不生成默认成功响应 |
| NP-CACHE-009 | 同一 `/24` 同时作为源和目的 | 只调用一次、一个 lookup、一个 fact，分别绑定两个角色 |
| NP-CACHE-010 | 评估任务被取消 | 不再创建新查询任务，已创建任务正确取消或收敛 |
| NP-CACHE-011 | 单次超时均未耗尽但批次总超时达到 | 批次结束，未完成 lookup 归一为依赖失败 |

Mock Client 应提供调用记录和最大并发计数，供测试精确断言。

Mock Client 的调用记录以规范化 `/24` 为键，不带 source/destination 角色。Fixture 命中后先构造 Provider DTO，再由 Resolver 转换为内部事实，确保 camelCase alias、严格类型和未知字段策略都经过真实边界。

## 10. 区域聚合与地址拆分测试用例

### NP-SEG-001：单 IP 不扩大

输入：

```text
16.201.1.10
```

预期：

- 查询 `16.201.1.0/24`。
- 访问范围为 `16.201.1.10/32`。
- 绑定一个事实 ID。

### NP-SEG-002：`/23` 同区域聚合

输入：

```text
16.201.0.0/23
```

使用 `.0/24`、`.1/24` 两个成功响应。虽然 `vlanId` 不同，但区域聚合键相同。

预期：

- 查询两个 `/24`。
- 形成一个 `16.201.0.0/23` 区域段。
- 该段绑定两个事实 ID。
- 两条原始响应仍分别保留。

### NP-SEG-003：`/22` 多区域加 404

输入：

```text
16.201.0.0/22
```

预期形成：

| 访问子范围 | 状态 | 区域 |
| --- | --- | --- |
| `16.201.0.0/23` | resolved | 鳌峰生产区（科创鲲鹏应用） |
| `16.201.2.0/24` | resolved | 鳌峰生产区（通用计算平台） |
| `16.201.3.0/24` | not_found | 无 |

未规划 `.3/24` 不能归入 `.2/24` 区域。

### NP-SEG-004：离散 IP 不合并

输入：

```text
16.201.1.10
16.201.1.20
```

预期只查询一次 `.1/24`，但保留两个独立 `/32` 地址和各自描述。

### NP-SEG-005：相同 area、不同 region

`.1/24` 和 `.2/24` 的 `areaId` 相同但 `regionName/platformName` 不同。

预期不得聚合成一个区域段。

### NP-SEG-006：部分依赖失败

一个 CIDR 覆盖三个 `/24`，其中一个成功、一个 404、一个超时。

预期形成三个状态明确的子范围，成功事实不受其他失败污染。

### NP-SEG-007：聚合键边界

分别覆盖以下输入，结果必须与已确认聚合键完全一致：

- `areaId/regionName/platformName` 相同但 `network` 不同。
- 其他字段相同但 `usageCode` 一个为空、一个非空。
- 中文字段前后空白或 Unicode 表示不同。
- 相邻 `/24` 属于不同原始地址输入，禁止跨输入合并。

在接口方确认 `network/usageCode` 的业务语义前，前两类作为契约待确认用例，不得自行修改预期。

### NP-SEG-008：顺序稳定

输入地址顺序与查询 `/24` 排序不同，且 Mock 响应以随机顺序完成。预期 item 顺序仍为“原始源顺序、源内子范围网络序、原始目的顺序、目的内子范围网络序、端口输入顺序”，不能按异步完成顺序或全局 `/24` 排序。

## 11. 访问组合测试用例

| 编号 | 源范围 | 目的范围 | 预期 |
| --- | --- | --- | --- |
| NP-COMB-001 | 1 个解析段 | 1 个解析段 | 1 个组合/端口 |
| NP-COMB-002 | 2 个源区域段 | 1 个目的段 | 2 个组合/端口 |
| NP-COMB-003 | 2 个源区域段 | 2 个目的区域段 | 4 个组合/端口 |
| NP-COMB-004 | 2×2 区域段、2 个端口范围 |  | 8 个组合 |
| NP-COMB-005 | 含一个 404 源段 | 1 个目的段 | 404 组合单独待定 |
| NP-COMB-006 | 多离散源 IP共享一个规划事实 | 1 个目的段 | 保留每个原始源组合 |
| NP-COMB-007 | 唯一 `/24` 未超限但解析段笛卡尔积超限 |  | HTTP 422 `EVALUATION_ITEM_LIMIT_EXCEEDED`，不构造组合列表 |
| NP-COMB-008 | 组合数等于硬上限 |  | 正常构造并继续评估 |
| NP-COMB-009 | 组合数超过业务规则阈值但未超过硬上限 |  | HTTP 200，命中正式组合数规则并待定 |

每个 item 必须能够追溯到原始地址、实际访问子范围和源/目的事实 ID。

## 12. 规则测试用例

| 编号 | 场景 | 预期 |
| --- | --- | --- |
| NP-RULE-001 | 同区域、未命中拒绝规则 | 事实完整时合规 |
| NP-RULE-002 | 办公网到生产区且测试规则明确禁止 | 命中指定规则并待定 |
| NP-RULE-003 | 相同 `areaId`、不同 `regionName` | 可按 region 精确匹配 |
| NP-RULE-004 | 相同显示名称、不同稳定 ID | 按稳定 ID 区分 |
| NP-RULE-005 | `usageCode=null` 且规则要求 usage | 事实不完整，不从 description 推断 |
| NP-RULE-006 | 404 地址 | `NETWORK_PLAN_NOT_FOUND` 优先于区域规则 |
| NP-RULE-007 | Provider 正式契约支持同一查询返回显式重复记录且字段冲突 | `NETWORK_PLAN_FACT_CONFLICT`；若正式契约只返回单对象则删除此首期强制用例 |
| NP-RULE-008 | LLM 召回规则但确定性条件不匹配 | 不得命中正式规则 |
| NP-RULE-009 | `when` 含未知或拼写错误字段 | 规则包启动失败 |
| NP-RULE-010 | `zone_relation` 只有不生效条件 | 规则包启动失败，不得扩大匹配 |
| NP-RULE-011 | 网段 404 且同时命中特殊端口规则 | 主原因是 `NETWORK_PLAN_NOT_FOUND`，端口规则只作为附加命中保留 |
| NP-RULE-012 | HTTP 认证失败且存在区域规则 | 主原因是 `NETWORK_PLAN_AUTH_FAILURE`，不得用规则覆盖依赖状态 |

默认生产规则包仍不得加入未经批准的示例区域拒绝规则。区域规则行为使用独立测试规则包验证。

## 13. LLM 输入与守卫测试用例

| 编号 | 场景 | 预期 |
| --- | --- | --- |
| NP-LLM-001 | LLM 正确引用源、目的事实 ID | 通过 |
| NP-LLM-002 | 引用不存在的事实 ID | 批次拒绝；原本合规 item 转 `LLM_SEMANTIC_ANALYSIS_FAILURE` |
| NP-LLM-003 | 源 item 引用另一个 item 的事实 | 批次拒绝；原本合规 item 转 LLM 失败待定 |
| NP-LLM-004 | 使用存在的 fact ID 但发明“核心生产区” | 该声明 `rejected`，事实和其他合法声明不变 |
| NP-LLM-005 | 将 404 地址推断为办公区 | 该声明 `rejected`，item 保持 `NETWORK_PLAN_NOT_FOUND` |
| NP-LLM-006 | 使用事实引用修改 API 的 `areaId` | 该声明 `rejected`，权威字段不变 |
| NP-LLM-007 | 根据 description 创造 `usageCode` | 声明 `rejected`；不得成为 verified/candidate 正式事实 |
| NP-LLM-008 | 漏掉多区域中的一个 item | 完整覆盖守卫拒绝 |
| NP-LLM-009 | 申请描述与 API 用途矛盾 | 两侧证据完整时 claim 为 `conflict`，服务端按既定优先级转待定 |
| NP-LLM-010 | 召回不存在的规则编号 | 批次拒绝；原本合规 item 转 `LLM_SEMANTIC_ANALYSIS_FAILURE` |
| NP-LLM-011 | 输出最终 `decision` | extra-forbid Schema 导致批次拒绝；原本合规 item 转 LLM 失败待定 |
| NP-LLM-012 | LLM 超时 | 权威事实和已命中规则不变；原本合规 item 转 `LLM_SEMANTIC_ANALYSIS_FAILURE` |
| NP-LLM-013 | Prompt 注入要求忽略 API | 不执行，事实保持不变 |
| NP-LLM-014 | fact ID 合法但字段名不存在 | 批次拒绝 |
| NP-LLM-015 | 源 item 引用同 item 的目的事实作为源 | 批次拒绝 |
| NP-LLM-016 | 申请说明与 API `description/usageCode` 矛盾且两侧证据完整 | claim 为 `conflict`，服务端按规则转待定 |

需要断言模型看到的是完整批次，而不是只看到首个 `/24` 或首个区域。

批次级拒绝与单声明 `rejected` 必须分别测试，不能使用同一个宽泛的“守卫拒绝”断言。

## 14. ACL 主辅模式测试用例

| 编号 | ACL 场景 | advisory 预期 | required 预期 |
| --- | --- | --- | --- |
| NP-ACL-001 | 正常且一致 | 正常评估 | 正常评估 |
| NP-ACL-002 | 超时 | `unverified`，不单独改变业务结论 | 待定 |
| NP-ACL-003 | 连接失败 | `unverified` | 待定 |
| NP-ACL-004 | 未抽取到防火墙 | `unverified` | 待定 |
| NP-ACL-005 | 明确无路径 | 待定 | 待定 |
| NP-ACL-006 | 端口明确不一致 | 待定 | 待定 |
| NP-ACL-007 | 文本歧义 | 待定 | 待定 |
| NP-ACL-008 | 网段规划已经 404 | ACL skipped | ACL skipped |
| NP-ACL-009 | 同一申请含成功、超时和 404 三个 item | 分别为 `verified/unverified/skipped` | 分别为 `verified/unverified/skipped`，required 下未核验 item 待定 |

还需断言响应不会把候选 ACL 分析描述为现网已放通。每个 item 必须断言独立 `acl_verification_status`；顶层只断言各状态计数，禁止用单个聚合状态覆盖混合结果。

## 15. API 端到端测试用例

### NP-E2E-001：单 IP 成功

- 源：`16.201.1.10`
- 目的：`16.220.16.20`
- 端口：TCP/443
- 预期：查询 `.1/24`、`.16/24`；item 保留两个 `/32`；响应包含源、目的事实 ID。

### NP-E2E-002：重复 `/24` 去重

- 两个源 IP 都在 `16.201.1.0/24`。
- 一个目的 IP 在 `16.220.16.0/24`。
- 预期：规划 API 调用 2 次，访问组合保留两个源 IP。

### NP-E2E-003：同区域 `/23`

- 源：`16.201.0.0/23`。
- 目的：`16.220.16.20`。
- 预期：源查询 2 次，源区域段保持 `/23`，绑定 2 个事实 ID。

### NP-E2E-004：多区域和部分 404

- 源：`16.201.0.0/22`。
- 目的：`16.220.16.20`。
- 预期：源产生 `/23`、`.2/24`、`.3/24` 三个子范围；`.3/24` 独立待定。

### NP-E2E-005：区域规则命中

- 源：`16.210.8.10`，办公终端区。
- 目的：`16.220.16.20`，生产数据库区。
- 使用显式测试规则包。
- 预期：命中指定区域规则，结论待定。

### NP-E2E-006：网段规划成功、ACL 超时

- advisory：业务规则结论保持，ACL 状态 `unverified`。
- required：总体待定。

### NP-E2E-007：LLM 虚构区域

- LLM Fixture 返回不存在的区域。
- 预期：守卫拒绝，API 权威事实不变；根据既定语义失败策略形成模板结果或待定。

### NP-E2E-008：幂等重放

- 第一次完成后，用相同 `request_id` 和相同输入重放。
- 预期：响应、`audit_id` 和事实 ID 一致；网段规划、ACL、LLM 不重复调用。

### NP-E2E-009：相同 request_id 不同输入

- 预期 HTTP 409 `idempotency_conflict`，不调用依赖。

### NP-E2E-010：查询数量超限

- 输入覆盖超过配置上限的 CIDR。
- 预期通过区间完成计数，耗时不随覆盖 `/24` 数增长；不枚举 `/24`，不创建规划、ACL 或 LLM 调用，HTTP 422 ErrorResponse 为 `NETWORK_PLAN_QUERY_LIMIT_EXCEEDED`，不生成 `EvaluationResponse`。

### NP-E2E-011：访问 item 硬限制超限

- 唯一 `/24` 数量不超限，但解析后的源段 × 目的段 × 端口范围超过 `MAX_EVALUATION_ITEMS`。
- 预期 Resolver 可完成必要查询，但不构造访问组合、不调用 ACL/LLM，HTTP 422 `EVALUATION_ITEM_LIMIT_EXCEEDED`。

### NP-E2E-012：HTTP 模式不依赖静态目录

- 使用 HTTP Network Plan MockTransport 启动，并移除或指向不存在的 `network_catalog.yaml`。
- 预期服务可以就绪并完成评估；只有显式 `offline_catalog` 模式要求静态目录存在及版本匹配。

### NP-E2E-013：资源拒绝释放幂等 claim

- 先触发查询数量或 item 硬限制，再使用相同 `request_id` 重试。
- 预期不会返回 `evaluation_in_progress`；调整输入或提高限制后可以重新评估。查询数量预检不得创建 claim，item 限制触发后必须释放已有 claim。

## 16. 审计和脱敏测试

| 编号 | 场景 | 预期 |
| --- | --- | --- |
| NP-AUDIT-001 | 成功查询 | 审计记录查询 `/24`、事实 ID、响应和 item 绑定 |
| NP-AUDIT-002 | 404/超时 | 记录明确状态和公开原因，不泄露内部异常 |
| NP-AUDIT-003 | API 鉴权配置 | 密钥、Token 不进入日志 |
| NP-AUDIT-004 | LLM 原始输出 | 使用既有脱敏规则 |
| NP-AUDIT-005 | 重启后幂等重放 | 从审计恢复相同结果，不重新调用依赖 |
| NP-AUDIT-006 | Fixture 或内部路径错误 | 对外响应不泄露本地绝对路径 |
| NP-AUDIT-007 | 跨角色共享同一 `/24` | 审计只记录一次 lookup/原始响应，item 分别记录角色绑定 |
| NP-AUDIT-008 | HTTP 422 资源预检拒绝 | 不生成正式评估审计和 `audit_id`；记录不含原始依赖数据的限流指标 |
| NP-AUDIT-009 | Provider 返回新增未知字段 | 未知字段不进入内部规则事实；原始响应按既定脱敏策略记录 |

## 17. 性能与稳定性测试

在隔离环境使用 Mock 延迟测试：

- 1、8、32、64 个唯一 `/24`。
- `/0`、`/1` 等明显超限前缀的不枚举区间计数。
- 并发上限分别为 1、4、8。
- 单个和多个慢响应。
- 混合成功、404 和超时。

验证：

- 并发上限有效。
- 结果排序稳定。
- 单个超时不阻塞已完成结果处理。
- 总耗时符合整体评估超时预算。
- 不因大量 `/24` 产生无界任务、内存或日志增长。
- item 上限判断前不物化笛卡尔积，超限后 ACL/LLM 调用数为 0。
- 批次总超时生效，最坏耗时不等于“批次数 × 单次超时”的无界累加。

真实环境的目标 P95、限流阈值和缓存 TTL 应在接口方提供容量信息后确定。

## 18. 回归测试

必须继续执行并根据新契约更新以下既有能力：

- 请求 Schema 和地址格式校验。
- `any`、过大 CIDR、过大端口范围和组合数限制。
- 当前业务组合数规则与新增运行安全硬限制的边界差异。
- 特殊端口规则。
- 对象、区域规则测试夹具。
- ACL 明确无路径、歧义和端口冲突。
- LLM Schema、证据、候选规则和 Prompt 注入守卫。
- LLM explanation 模板回退。
- request findings 和 ACL candidate 影子模式。
- 幂等冲突、并发处理中和重启重放。
- 审计落盘、脱敏和保留期。

旧用例中 `ZONE_UNRESOLVED` 应迁移为新的网段规划原因码，或在显式静态目录离线模式中保留原语义。

现有 `tests/helpers/case_loader.py` 使用静态目录计算 item 数和符号。网段规划 suite 必须改为先通过 Resolver/Fixture 得到解析段，再生成 `@item` 和事实符号；旧 suite 可在 `offline_catalog` 兼容模式继续沿用原逻辑。

## 19. 测试数据驱动约束

- Fixture 必须包含 `fixture_version` 和 `purpose`。
- Fixture 路径必须限制在允许的测试目录内，禁止绝对路径和目录穿越。
- 普通端到端用例控制组合数，复杂拆分使用专用组件测试。
- item 引用继续使用稳定符号，例如 `@item:1`。
- 增加网络事实引用符号时，建议使用：

```text
@source-fact:1
@destination-fact:1
```

- 事实符号解析必须绑定到具体 item 和角色，不能仅用全局序号猜测；加载器必须验证事实存在、角色正确、item 可访问、引用完整且不越界。
- 默认测试不得访问真实网段规划、ACL 或 LLM 服务。

## 20. 测试执行建议

按以下顺序执行：

```powershell
pytest tests/test_network_plan_normalization.py -q
pytest tests/test_network_plan_client.py tests/test_network_plan_http_contract.py -q
pytest tests/test_network_plan_resolver.py tests/test_network_plan_rules.py -q
pytest tests/test_network_plan_llm_guard.py -q
pytest tests/test_network_plan_evaluation.py -q
pytest tests/test_network_plan_resource_limits.py -q
pytest tests -q
ruff check .
```

真实合约测试必须使用单独标记并默认跳过，例如：

```text
network_plan_contract
```

只有显式配置批准的测试环境、URL 和凭证时才允许运行。
该 marker 必须注册到 `pyproject.toml`，未注册 marker 警告视为测试配置失败。

## 21. 缺陷判定优先级

以下问题视为阻断上线：

- 查询 `/24` 导致实际访问范围被扩大。
- 404 网段被自动归入其他区域。
- 同一 `/24` 多次调用且造成不一致裁决。
- LLM 能覆盖权威区域或最终结论。
- 非 `/24` 参数能够到达 HTTP Client。
- 多区域地址被错误聚合。
- API 密钥或敏感原文写入未脱敏日志。
- 网段规划失败时静默使用静态目录。
- advisory/required 模式实际行为与配置不一致。
- 超大前缀在检查限制前被完整枚举，或 item 超限前已构造笛卡尔积。
- 同一 `/24` 因源/目的角色不同产生两次调用或两份互相独立的权威事实。
- 规则条件拼写错误被静默忽略并扩大匹配。
- 混合 ACL 状态被顶层聚合成单一状态，无法追溯到 item。
- 生产 HTTP 模式仍因缺少静态网络目录而无法启动。

## 22. 测试完成标准

- 本文列出的单元、组件、合约、集成和端到端用例全部通过。
- 原有完整测试套件通过，已批准的契约变化除外。
- Ruff 检查通过。
- 每个正式启用的错误码至少有一个正向触发用例；契约上不可产生的 `NETWORK_PLAN_FACT_CONFLICT` 不得用伪造双调用制造。
- 所有拒绝行为均有唯一 HTTP 状态、原因码和依赖调用次数断言，不含二选一预期。
- `/24` 查询与访问范围分离有明确防回归测试。
- 多 `/24` 同区域聚合、多区域拆分和部分 404 均有端到端证据。
- LLM 虚构、覆盖、跨 item 引用和漏项均被守卫拒绝。
- ACL advisory/required 两种模式均覆盖成功、失败、无路径和冲突。
- `/0` 等超大前缀和 item 笛卡尔积超限均证明在列表物化及依赖调用前拒绝。
- 同一 `/24` 跨角色共享 lookup/fact、逐 item ACL 状态和规则条件白名单均有防回归测试。
- HTTP 权威模式在无静态目录时可启动，离线目录模式继续验证版本一致性。
- 审计记录可以从 item 追溯到原始地址、查询 `/24`、模拟响应、事实 ID、规则和模型证据。
- 真实 HTTP 契约未确认的部分被明确标记，不以猜测实现进入生产。
