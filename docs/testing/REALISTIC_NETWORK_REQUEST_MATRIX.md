# Realistic Network Request Matrix（模拟真实网络需求回归矩阵）

> 版本：realistic.v1（`tests/cases/evaluations/realistic_network_requests.v1.json`）
> 冻结时间：PHASE-01（ACL 删除与 LLM runtime 替换之前）
> 用途：旧实现行为取证（`legacy_expected`）+ ACL-free 目标门禁（`target_expected`）。
> `legacy_expected` 仅用于迁移取证；ACL 删除完成后不再作为任何断言依据。

## 1. 数据安全规则（已由测试强制）

- 所有测试地址仅使用 RFC 5737 文档保留段：`192.0.2.0/24`、`198.51.100.0/24`、`203.0.113.0/24`
  （`tests/test_realistic_network_requests.py::test_realistic_addresses_are_documentation_reserved`）。
- 域名/工单/系统名全部虚构（`CHG-EXAMPLE-001`、`PARTNER-A`、`MOCK-FW-01` 等）。
- token/password 一律使用 `TEST-PLACEHOLDER-NOT-REAL` / `FAKE-EXAMPLE-VALUE` 等明显占位符。
- 默认测试仅使用 mock 网段规划 fixture 与 fixture/passthrough LLM；无任何真实内网或真实 LLM 访问。
  conftest 的 `block_real_network` 自动拦截非回环 socket。

## 2. 模拟拓扑与 fixture 粒度说明（与实施计划的差异）

计划的模拟拓扑表将 7 个区域分配为 3 个 /24 内的 /26 子段。FARE 权威网段规划的
传输查询粒度冻结为 /24（`_materialize_query_plan`，V4 基线行为），且 mock fixture
按 /24 查询键返回唯一应答体；因此 /26 粒度的同 /24 双区域无法在不改变权威规划
API 契约的前提下表达（该契约变更不在本任务授权范围内）。

适配方式：保持 7 个模拟区域与全部文档保留地址不变，将区域→/24 的绑定下沉到
各 dependency profile（fixture 文件）。任一 case 内的每个 /24 只承载一个区域。

| 区域 | 模拟用途 | usage code | 承载 fixture（`tests/fixtures/network_plan/realistic/`） |
|---|---|---|---|
| OFFICE（192.0.2.0/24） | 员工终端 | ENDPOINT | main / notfound / failure408 / failure401 / failureinvalid / conflict |
| OPS（192.0.2.0/24） | 运维堡垒机 | ADMIN | ops |
| PROD-DB（198.51.100.0/24） | 生产数据库 | PROD_DATABASE | main / ops / notfound / failure408 / failure401 / failureinvalid / conflict |
| PROD-APP（198.51.100.0/24） | 生产应用 | APPLICATION | apptier_shared |
| DMZ（203.0.113.0/24） | 对外 API 网关 | GATEWAY | main / ops / partner_dmz |
| SHARED（203.0.113.0/24） | DNS、NTP、监控 | INFRA | apptier_shared |
| PARTNER（198.51.100.0/24） | 合作方 | EXTERNAL | partner_dmz |

失败画像：`notfound`（404→NETWORK_PLAN_NOT_FOUND）、`failure408`（408→
NETWORK_PLAN_DEPENDENCY_FAILURE）、`failure401`（401→NETWORK_PLAN_AUTH_FAILURE）、
`failureinvalid`（坏 data→NETWORK_PLAN_INVALID_RESPONSE）、`conflict`
（subnet 与查询不一致→NETWORK_PLAN_SUBNET_MISMATCH）。

## 3. 回归策略包

`tests/fixtures/policies/realistic_network/`：manifest（2026.09.0）+ 规则 +
目录。规则序（=同优先级 finding 插入序）：`RN-TELNET-001`（tcp/23）、
`RN-OFFICE-PROD-001`（office→production）、`RN-OPS-PROD-001`（ops→production）、
`RN-ANY-001`（any）、`RN-PORT-SPAN-001`（>32 端口）、`RN-COMBO-001`（>64 组合）、
`ACL-PATH-001`（现 loader 强制要求的占位规则，PHASE-03 随特例一并删除）。

## 4. 场景矩阵（35 例）

### 4.1 核心真实感场景（RN-001~RN-024，另含扩展失败/上限场景 RN-025~RN-028）

| ID | 模拟业务需求 | 关键断言 | legacy 与 target 关系 |
|---|---|---|---|
| RN-001 | 生产应用→生产数据库 PostgreSQL tcp/5432 | 规范化、facts resolved、稳定 item id、合规 | 决策一致；仅 ACL 表面字段差异 |
| RN-002 | 办公终端→生产数据库 | 确定性区域规则待定，LLM 不得覆盖 | 决策一致 |
| RN-003 | DMZ→生产应用 HTTPS | 合法最小端口/方向，合规 | 决策一致 |
| RN-004 | 合作方→DMZ 临时联调 | verified 规则缺口 review_required 降级（SEMANTIC_POLICY_GAP） | 决策一致 |
| RN-005 | 运维堡垒机 SSH 生产库 | 特权访问规则待定 + missing information 仅提问 | 决策一致 |
| RN-006 | 生产应用→共享 DNS udp/53 | 协议端口组合，合规 | 决策一致 |
| RN-007 | 双端口 UDP 查询 DNS | 协议语义不混合（两 item 均 udp） | 决策一致 |
| RN-008 | 监控平台×2 节点×双端口段 | 8 item 笛卡尔积顺序、semantic/explanation 各 1 次调用 | 决策一致 |
| RN-009 | 双应用→单数据库 | item id 稳定、每 item 恰一次 rule/reduce | 决策一致 |
| RN-010 | any→生产数据库 | 规则+目录事实双 finding，primary=规则 | 决策一致 |
| RN-011 | Telnet tcp/23 | 正式端口规则优先 | 决策一致 |
| RN-012 | 未登记网段 | NETWORK_PLAN_NOT_FOUND（fact_incomplete） | 决策一致；legacy ACL skipped |
| RN-013 | 规划依赖 408 | NETWORK_PLAN_DEPENDENCY_FAILURE（dependency_failure） | 决策一致；legacy ACL skipped |
| RN-025 | 规划认证 401 | NETWORK_PLAN_AUTH_FAILURE（dependency_failure） | 决策一致 |
| RN-026 | 规划响应损坏 | NETWORK_PLAN_INVALID_RESPONSE（fact_incomplete） | 决策一致 |
| RN-014 | 规划自相矛盾 | NETWORK_PLAN_SUBNET_MISMATCH（fact_conflict），LLM 不得修复 | 决策一致 |
| RN-015 | 描述 443 vs 结构 8443 | verified 矛盾→review_required 降级（SEMANTIC_FACT_CONFLICT） | 决策一致 |
| RN-016 | 伪造规则 ID 注入 | guard 整体 fail-close，explanation 不调用，规则结论保持 primary | 决策一致 |
| RN-017 | 描述缺少业务目的 | missing information 仅提问（question_only），request findings 影子完成 | 决策一致 |
| RN-018 | 幂等重放（同 id 同输入） | 返回缓存，provider 调用不增加 | 决策一致 |
| RN-019 | 幂等冲突（同 id 异输入） | 409 idempotency_conflict，无额外调用 | 决策一致 |
| RN-020 | 恰好 6 item（max=6） | 上限内成功 | 决策一致 |
| RN-027 | 7 item>max=5 | 422 EVALUATION_ITEM_LIMIT_EXCEEDED，LLM 0 调用 | 一致 |
| RN-028 | 3 个 /24 查询>max=2 | 422 NETWORK_PLAN_QUERY_LIMIT_EXCEEDED，LLM 0 调用 | 一致 |
| RN-021 | 中英混合+占位敏感值 | schema/item id 稳定 | 决策一致 |
| RN-022 | 双规则同时命中 | primary 稳定=规则包序先命中者（RN-TELNET-001） | 决策一致 |
| RN-023 | semantic provider 失败 | 全 item fail-close（LLM_SEMANTIC_ANALYSIS_FAILURE），explanation 不调用 | 决策一致 |
| RN-024 | explanation 越权引用 | explanation guard 拒绝→模板回退，锁定结论不变 | 决策一致 |

### 4.2 ACL 差异专用迁移用例（每例均含显式 approved_differences）

| ID | 迁移类别 | legacy 行为（取证） | target 行为（门禁） | 批准差异 |
|---|---|---|---|---|
| RN-029 | ACL advisory 依赖失败 | 合规；acl_verification_status=unverified | 合规 | 仅审计/验证表面消失，无决策漂移 |
| RN-030 | ACL required 依赖失败 | 待定（ACL_DEPENDENCY_FAILURE + ACL_FIREWALL_UNRESOLVED） | 合规 | **批准漂移：待定→合规** |
| RN-031 | explicit no path | 待定 acl_no_path（ACL-PATH-001 注入 matched_rules） | 合规 | **批准漂移：待定→合规**；禁止 semantic 仿造 |
| RN-032 | 歧义 | 待定 ACL_FACT_AMBIGUOUS | 合规 | **批准漂移：待定→合规** |
| RN-033 | 端口不一致（观察 8080/申请 443） | 待定 ACL_PORT_MISMATCH | 合规 | **批准漂移：待定→合规**；文本矛盾走 RN-015 通道 |
| RN-034 | no firewall（required） | 待定 ACL_FIREWALL_UNRESOLVED | 合规 | **批准漂移：待定→合规** |
| RN-035 | ACL candidate shadow | shadow LLM 调用 1 次、acl_candidate_analysis 存在（agree） | 字段与能力整体消失，调用 0 次 | **批准变更：能力整体删除** |

## 5. 运行方式

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_realistic_network_requests.py -q -p no:cacheprovider
```

runner 的 `ACTIVE_CONTRACT` 常量决定断言契约：PHASE-01/02 为 `legacy`；
PHASE-03 起（ACL 删除 commit）翻转为 `acl_free`，此后 `legacy_expected`
不再被任何断言读取。

## 6. 已知的固化行为差异（PHASE-01 取证新增发现）

- required 模式下 ACL 依赖失败实际产生两个 finding（依赖失败 + 未确认防火墙），
  RN-030 的 legacy 取证按真实行为冻结。
- ACL candidate shadow 的 merge 状态由确定性抽取与 LLM 候选的全量事实对比决定；
  RN-035 fixture 已补全 candidate_acl/address_object 使状态为 agree。
