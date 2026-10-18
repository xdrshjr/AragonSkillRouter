## 实施过程发现的方案缺陷（Issues Found During Implementation）

> 本节由实现节点追加（§18 评审结论之后）。只记录**实现时发现的、正文存在偏差或缺失**的项；
> 已按「不静默偏离」原则在代码中采用修正后的做法，并在此登记原因与验证方式。

### I1（P1，已修正）`request.url_template` 的占位符语法未定义

- **问题**：§7.6 示例同时出现两种写法 —— `url_template: "{base_url}/v1/chat/completions"`（无 `$`）与
  `body_template: {"model": "${model}"}`（有 `$`），正文从未说明求值器必须同时支持两者。按 v2 字面实现会出现
  「URL 里 `{base_url}` 未被替换 → 不是合法 http(s) → 保存后测试必然 400」的死路。
- **修正**：`executors/mapping.py::fill_template` 同时支持 `{key}` 与 `${key}`；整串为单个占位符时保留原生类型
  （数字/对象不被转成字符串），并保留 `base_url` / `model` / `model_hint` 的隐含别名。
- **验证**：`tests/test_execution_sync_async_stream.py`、`scripts/e2e_live.py`（真实 echo 上游）。

### I2（P1，已修正）`modes` 含 `async` 的校验被写成「每条 route 都要支持」

- **问题**：§7.6 校验规则 7 的原文是「**至少一条 enabled route** 同时提供 `async.submit.task_id_path`
  与 `async.query.url_template`」，但按「逐 route 报 error」实现会使得「主 route 支持异步 + 备用 route 只做同步」
  这一完全合法的配置无法发布（多 route 是 v2 的核心能力）。
- **修正**：逐 route 缺异步配置降级为 `warn`（该 route 在 `mode=async` 时被路由器过滤）；
  能力级检查保留为 `error`：`modes` 含 `async` 但**没有任何** enabled route 支持 → `ASYNC_QUERY_MISSING`。
- **验证**：`tests/test_capabilities_validation.py::test_modes_async_requires_query_config`、
  `scripts/e2e_live.py`（2 条 route、仅主 route 支持异步时发布成功）。

### I3（P0，已修正）SSRF 防护被错误地施加到「管理员配置的上游地址」

- **问题**：§11.1 把「API 测试 URL」也列入 SSRF 防护范围，若照字面对 `credentials_verify` 复用
  `fetch_guard.validate_url()`，则 `base_url = http://127.0.0.1:11434`（本机 Ollama）或内网网关**永远无法验证**，
  与需求「支持任意 OpenAI 兼容端点」直接冲突；同时让「凭证有效性检测」变成不可用功能。
- **修正**：新增 `security/netpolicy.py::validate_admin_target()` —— 对**管理员显式配置**的地址只拒绝非
  http(s) 协议与云元数据地址（`169.254.169.254` 等），**允许私网**；而抓取外部网页、导入 SKILL 等
  「不可信来源」继续走 `fetch_guard.validate_url()` 的完整校验（DNS 解析后逐 IP 拒绝私网、逐跳重定向校验）。
  两类目标在代码上是两条路径，不是同一个开关。
- **验证**：`tests/test_ssrf_guard.py`（外部抓取仍拒绝私网/元数据）、
  `tests/test_credentials_verify.py`（本机上游可探测）、`scripts/e2e_live.py`。

### I4（P1，已修正）部署环境的系统代理会劫持「本机/内网上游」调用

- **问题**：正文未提及出站代理。实测环境（Windows）注册表存在系统代理 `127.0.0.1:7897`，
  httpx 默认 `trust_env=True` 会把 `http://127.0.0.1:<port>` 的上游请求也发给代理，表现为**上游 502**、
  测试面板显示「上游不可用」，与真实原因（本地代理）完全不符，排查成本极高。
- **修正**：`security/netpolicy.py::client_kwargs()` 在 loopback / 私网 / link-local / 单标签主机名
  目标上强制 `trust_env=False`（等价 NO_PROXY），外部目标仍尊重环境代理（企业网络需要）；
  `fetch_guard` 的外部抓取一律 `trust_env=False`，避免代理绕过 SSRF 校验。
- **验证**：`scripts/e2e_live.py` 的 58 项真实联调（本机 echo 上游，真实 HTTP）。

### I5（P1，已修正）Alembic 迁移在 SQLite 上会静默回滚

- **问题**：§7.2 要求「`alembic upgrade head`，`PRAGMA user_version` 与 `alembic_version` 双向校验，
  不一致即启动失败」，但正文未给出实现约束。在 `env.py` 里用
  `connection.exec_driver_sql("PRAGMA foreign_keys=ON")` 之后，SQLAlchemy 2.0 会隐式开启事务，
  导致 alembic 自身的 `alembic_version` 写入与迁移内的种子 DML **全部回滚**（实测：
  建表成功（DDL 非事务）但 `alembic_version` 为空、`user_version=0`，`alembic stamp head` 亦无效）。
- **修正**：PRAGMA 改为通过 Engine `connect` 事件设置（与 `db/engine.py` 一致），不在连接上手写 PRAGMA；
  `0001_init` 只做 schema + 触发器 + 种子 + `user_version`。`services/bootstrap.py::assert_migrated()`
  同时校验 `PRAGMA user_version` 与 `alembic_version == head`。
- **验证**：`apps/api/migrations/*` 实际执行 `alembic upgrade head` + `db/migrations/0001_init.sql` 快照导出
  （49 表 / 54 索引 / 2 触发器 / `user_version=1` / `alembic_version=0002_engine_upgrade`）。

### I6（P2，已修正）`SKILLROUTER_DB_PATH` 的相对路径基准未定义

- **问题**：§7.1 默认 `SKILLROUTER_DATA_DIR=var` 且 `SKILLROUTER_DB_PATH=var/skillrouter.db`。
  若把 `db_path` 解析为「相对 data_dir」，默认组合会得到 `var/var/skillrouter.db`（§7.1 的字面配置即失效）。
- **修正**：相对路径统一按**当前工作目录**解析（`Settings.db_file`），默认组合落在 `./var/skillrouter.db`；
  绝对路径原样使用。同时 `db_path`/`data_dir` 等字符串字段做 `strip()`（Windows `set VAR=value &&` 会带尾随空格）。
- **验证**：`scripts/bootstrap.py`、`scripts/e2e_live.py`（自定义数据目录的隔离实例）。

### I7（P2，已修正）登录限流窗口的维度

- **问题**：§7.3 写「固定窗口限流（5 次/5 分钟/IP+用户名）」，未说明是「IP 与用户名各自计数」还是
  「组合计数」。按「各自计数」实现时，攻击者用一个 IP 试错会把该 IP 上所有管理操作锁死，
  也让测试/多实例场景互相污染（实测：一个用例的失败会锁掉后续用例的登录）。
- **修正**：固定窗口按 **(username, ip) 组合**统计（与需求「IP+用户名」的表述一致），
  超限返回 `429 LOGIN_THROTTLED` + `details.retry_after`。
- **验证**：`tests/test_auth_flow.py::test_login_throttled_after_failures`。

### I8（P1，已补充）§9.5 未定义网关 `sr_*` 工具的**实现在哪一侧**

- **问题**：§12.3 把 12 个 `sr_*` 工具定义在网关（Node）侧，§9.5 只列了 `/internal/capabilities`、
  `/internal/credentials/materialize`、`/internal/audit`，没有定义工具**业务逻辑**的落点。
  若工具逻辑写进 Node，就会复制一份「能力校验/发布/测试」的业务规则，形成正文 D2 明确禁止的长期分叉。
- **修正**：网关只做「JSON Schema 参数校验 + 超时 + 审计」；工具的**唯一实现**在 Python
  `api/internal.py::POST /internal/tools/{tool_name}`（复用 `services/*`，与人工操作同一条代码路径）。
  未注册的工具名一律 404 并提示白名单（保证「上游新增工具默认不注册」）。
- **验证**：`tests/test_internal_tools.py`（含 Agent 提草案 → 结构校验 → 真实测试的端到端用例）、
  `agents/aragon-gateway/src/platform-client.ts`（转发契约）。

### I9（P2，记录在案）浮窗 Agent 的 SSE 断线与「turn 收尾」仍需真实 Agent 运行时验证

- **现状**：`agent_turn` handler 已把消息投递给网关，网关事件经
  `worker/handlers/agent_turn.py::ingest_stream()` 回灌 `agent_events` 并广播到
  `/api/admin/agent/sessions/{id}/stream`。但本机没有可用模型凭证，**真实推理链路仍为「待验证」**
  （已在 `docs/runbook/verification-checklist.md` 登记，未标为已验证）。
- **影响**：不影响已发布普通能力（§7.12：它们不经过网关，e2e 已验证升级/停机期间照常调用）。

### I10（P2，记录在案）`output_schema.additionalProperties=false` 对生成类能力偏严

- **问题**：§7.6 规则 4 要求 `input_schema` 与 `output_schema` 的 `additionalProperties` 都必须为 `false`。
  对输入是正确约束（「不支持的参数必须显式报错」），但对**上游返回**的输出 Schema 过严：
  图像/视频类供应商常返回未声明的元数据字段。
- **处置**：实现按正文执行（两者都必须是 `false`），但把「输出可能包含未声明字段」写进
  `docs/api/examples.md` 与能力编辑器的提示文案，建议用 `output_files` / 显式字段承接。
  若后续要放宽，只需改 `capabilities.validate_definition()` 的一处判定并更新本文。

### I11（P2，记录在案）`sse_events` 与 `agent_events` 的主键策略

- **问题**：§10.1 要求 `sse_events` 只承载非 Agent 通道（P04），但其 `id` 为自增 INTEGER，
  跨进程（api + worker 同时写）取「MAX(id)+1」有竞争。
- **处置**：`sse/broker.py` 优先用 `MAX(id)+1` 分配并在 `db_write()` 写锁内落库，失败时回退为进程内自增；
  事件表只用于 24h 回放，重复 id 不会造成业务错误（最坏丢失一次回放，前端可整段刷新）。
  真正需要严格顺序的 Agent 通道走 `agent_events`（`(turn_id, seq)` 唯一索引，由单一写入者分配）。

### I12（P1，已修正）共享密钥的空白字符与「本机代理」会让 Agent 接入直接失败

- **问题**：§7.1 只写了「`SKILLROUTER_AGENT_GATEWAY_SECRET`（32 字节随机）」，未涉及两件实测会踩到的事：
  1. 部署脚本里 `set VAR=value && next` 会把**尾随空格**带进变量，空格进入 `X-Internal-Token` 头后
     httpx 直接抛 `LocalProtocolError: Illegal header value`，而平台侧把它包装成「网关不可达」，
     提示完全指向错误方向（实测复现：`check_gateway.py` 首跑失败）。
  2. 与 I4 同源：适配层访问 `127.0.0.1:8500` 时同样会被系统代理接管。
- **修正**：`Settings` 对所有字符串字段做 `strip()`（`field_validator("*", mode="before")`）；
  `agent/client.py` 使用 `client_kwargs()`（本地目标 `trust_env=False`）并单独捕获
  `httpx.LocalProtocolError` / `InvalidURL`，错误提示明确写「检查两侧共享密钥是否完全一致（不含空格/换行）」。
- **验证**：`scripts/check_gateway.py` 12/12（真实网关进程 + 真实 SDK 0.2.34）。

---

_本节以上 12 项均已按「修正后的做法」落地，其中 I1/I3/I4/I5/I8/I12 属会直接导致功能不可用的缺陷。_
