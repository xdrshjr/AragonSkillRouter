# 迭代 2 审计证据附录（iteration-2 audit）

> 读者：本轮实现节点（Subtask #2）与复审节点（Subtask #1 / #3）。
> 用途：spec §A「迭代 2 增量契约」里每条缺陷的**证据出处**。spec 只写结论与修法，本文写「怎么发现的、证据在哪一行」。
> 口径：**代码是唯一事实来源**。凡是只有子代理报告、主代理未逐行复核的条目，一律标 `[待复核]`，
> 实现前必须先在代码里确认；确认不成立就**不要改**，并在实现记录里写明「报告不成立及理由」。

## 0. 方法与范围

第一轮迭代交付了完整实现（337 个文件、1 个提交 `1b5e39d`），并且 197 项后端测试、58 项端到端、9 项前端联通、
12 项网关联通、42 项网关测试、37 项 CLI 测试全绿。**但这些绿灯覆盖的是「设计里写到的路径」，
不是「需求里承诺的行为」。** 迭代 2 的审计因此换了一个视角：

| 维度 | 问题 | 检查方式 |
|---|---|---|
| 契约一致性 | 前端类型/字段名与后端真实载荷是否一致？ | 逐个 `fetch` 调用点 ↔ 对应 handler 返回体 |
| 语义正确性 | 「测试通过」「已发布」「可调用」是否由真实证据驱动？ | 状态字段的生产者与消费者成对阅读 |
| 资金与数据安全 | 预算、幂等、租约、取消、凭证投影是否真的闭环？ | 调用链上的每个写点 + 并发点逐个核对 |
| 可验证性 | 声称的验证流程是否真的会失败？ | 读闸门实现，看「失败路径」是否存在 |
| 可运行性 | 文档里的命令、CI 的作业在真实环境能否跑通？ | 读 Dockerfile/entrypoint/workflow/脚本 |

审计分四个域并行进行（web / api / exec / delivery），每个域产出「已覆盖 + 缺口表（file:line + 修法）」。
主代理对全部 P0 条目在源码中**逐条复核**，复核结果在 §2 标注。

## 1. 结论摘要

| 级别 | 数量 | 含义 |
|---|---|---|
| P0 | 12 | 核心语义错误或安全/资金风险：不修则「平台看起来能用，实际给错结论或重复花钱」 |
| P1 | 14 | 契约漂移、可验证性缺失、文档/CI 与实现不一致：不修则验收无法核对 |
| P2 | 6 | 登记项，本轮不做 |

最重要的三条（因为它们直接摧毁产品承诺）：

1. **F-01**：真实测试成功后前端仍显示「测试未通过」，发布按钮永久禁用 → 「验证能力 → 发布 SKILL」这条主闭环在 UI 上是断的。
2. **F-05**：预算只有同步 token 路径记账，流式与异步完全不记账 → 「费用与资源控制」形同虚设。
3. **F-09**：引擎升级的「验证」闸门在容器里必然 skipped，而 skipped 不计 failed → 升级「永远绿」，
   这与需求「验证失败时继续使用当前版本」直接冲突。

## 2. P0 缺陷（逐条证据）

### F-01 能力测试结果契约错位 `[已复核]`

- 后端返回体（`apps/api/skillrouter_api/services/capabilities.py:807-821`）：
  `{test_run_id, status, http_status, latency_ms, response_preview, output, error, used_route:{route_id,provider_id,credential_id}, dropped}`
- 前端类型（`apps/web/lib/types.ts:302-317`）声明：
  `{test_run_id, ok, status, http_status, latency_ms, response_preview, error, route:{id,provider_id,provider_name,credential_id}, at}`
- 差异：`ok` 与 `route` 后端从不返回；`used_route`/`output` 前端未声明。
- 后果：`CapabilityTestResult.ok === undefined` → 所有以 `result.ok` 判定「测试通过」的位置恒为 false
  （`apps/web/app/(app)/capabilities/[id]/page.tsx:288-298,405`、`components/capabilities/TestPanel.tsx:116-118,219-223`），
  而发布闸门要求「有 24h 内成功的测试证据」→ 用户永久看不到「发布」可用。

### F-02 能力列表的「成功率 / 上次测试」没有数据源 `[待复核]`

- 前端：`apps/web/app/(app)/capabilities/page.tsx:110-122`、`capabilities/[id]/page.tsx:123,405`、`lib/types.ts:282-284`
  读 `success_rate_24h` / `last_test_ok` / `last_test_at`。
- 后端：`services/capabilities.py::_brief/list_capabilities`（约 `:507-624`）未产出这三个字段。
- 后果：两列恒为「—」，用户无法判断「这个能力上次测试成功了吗」。

### F-03 凭证投影：字段缺失 + `meta` 明文外泄 `[已复核]`

- `services/credentials.py:30-47` 的 `_brief()` 返回 `"secret": {"masked": row.secret_mask}` 与 `"meta": row.meta or {}`。
  - 缺 `secret_mask` / `secret_fingerprint`（列在 `models/providers.py:35-36` 是存在的），
    前端 `providers/[id]/credentials/[cid]/page.tsx:198-200,250` 读不到 → 掩码空白、指纹 `—`。
  - `meta` 原样透出。`api/v1/callbacks.py:92-99` 把**共享密钥存在 `credentials.meta["webhook_secret"]`**
    （回调验签用的 HMAC 密钥），因此 `GET /api/admin/credentials/{id}`、`GET /api/admin/providers/{id}`、
    `/impact` 都可能把明文密钥送到浏览器与审计日志。
- 对照：`services/providers.py:108` 在提供商详情里用的就是 `secret_mask`，说明两个投影原本应该一致。

### F-04 平台令牌可经查询串传入 `[已复核]`

- `deps.py:75-83`：`raw = header[7:].strip() ... ; if not raw: raw = request.query_params.get("token")`。
- 风险面：查询串会进代理 access log、反代错误页、Referer、浏览器历史；
  `logging.py:20-28` 的脱敏规则不匹配 `token=`。
- 需求原文要求「令牌只在创建时完整展示」「平台令牌与上游凭证分开管理」，查询串通道与之相悖。

### F-05 预算不生效 `[已复核]`

- 全仓 `record_cost` 只有一个调用点：`services/execution.py:405`（同步路径，且只在 token 作用域）。
- `check_budget` 也只有一个调用点：`services/execution.py:135`（`scope_type="token"`）。
- 流式路径（`services/execution.py` 内 `stream`，约 `:596-603`）只 `usage.record`，不 `record_cost`；
  异步路径在提交时就 `release_concurrency`（约 `:454-457`），worker 侧 `worker/handlers/capability_invoke.py:123-129`
  写死 `unit="images", total=1` 且不记账。
- 后果：`rate_counters.cost_usd` 不增长 → 月预算的 `BUDGET_EXCEEDED` 永不触发。需求 §「费用与资源控制」要求
  「按提供商、能力和接入 Agent 设置速率、并发和预算」，当前只有 limiter 的一半在用。

### F-06 读超时后自动重投非幂等 POST `[待复核]`

- 报告位置：`executors/http_executor.py:174-192`（`if attempts >= max_attempts or not last_error["retryable"]`）
  与 `:280`（退避后重试）；`executors/errors.py::map_error(transport_error="timeout")` 返回 `retryable=True`。
- 需求原文：「发生网络超时且无法判断上游是否已受理时，优先查询任务并标记待确认，不能盲目重新提交或切换提供商」。
- 实现现状：`status_unknown` 只在**重试耗尽后**才出现，重试本身已经把可能已受理的请求重发过一次。

### F-07 取消 × 提交竞态 `[已复核]`

- `services/tasks.py:278-294`：`cancel()` 在 `cancel_capability != "remote"` 时**直接** `transition(row,"canceled")`，
  不区分「上游任务是否存在」。
- `worker/handlers/capability_invoke.py:32-55`：提交前 `claim_submission`，`await async_poll.submit(...)` 之后
  才 `tasks.mark_running(...)`。若这期间用户取消：`mark_running` 抛 `INVALID_STATE_TRANSITION`。
- `worker/runner.py:132-134`：捕获到 `INVALID_STATE_TRANSITION` 时 `leases.release(row.id, ok=True)` —— **当作成功**。
- 后果：上游任务真实创建并计费，平台侧却已标 `canceled` 且无人轮询 → 既拿不到结果，也不知道要付钱。

### F-08 租约丢失后仍可写 / 续租失败不取消 handler `[已复核]`

- `worker/leases.py:75-94`：`release()` 不校验 `lease_owner`；`renew()`（`:66-72`）校验了。
- `worker/runner.py:148-153`：`_renew_loop` 续租失败只 `log.error("lease_lost")` 然后 `return`，
  handler 仍在跑（`asyncio.wait_for` 未取消），随后 `release(ok=True)` 会把**别人的新租约**覆写成 done。
- 需求原文：「后台任务……支持任务领取、执行租约、并发限制、超时和重启恢复」；双执行同时意味着上游重复下单。

### F-09 引擎升级验证「假绿」 `[已复核]`

- `upgrade/verify.py:41-78`：用相对路径 `Path("agents/aragon-gateway")` 判断目录存在；
  api 镜像里没有该目录（`deploy/Dockerfile.api:38-45` 只拷 `apps/api`）→ 3 项检查全部 `skipped`。
- `:127-134`：`failed = [c for c in checks if c["status"] == "failed"]`；**skipped 不计 failed** → `status="passed"`。
- 更根本：它编译/测试的是**镜像里既有的网关源码**，不是候选版本的构建产物；`:106-126` 的「工具清单」「数据兼容」
  是硬编码 `passed` 文案。
- 需求原文：「新版本经过隔离构建、兼容性检查和回归验证后……验证失败时继续使用当前版本」。当前实现永远验证通过。

### F-10 升级产物不进运行解析路径 `[待复核]`

- 报告位置：`upgrade/build.py:78-99,142-143` 产出 `engine-builds/<v>-<prov>/runtime`；
  网关子进程只拿到 `ARAGON_HOME`（`agents/aragon-gateway/src/engine-host.ts:516-535`），
  引擎包仍按镜像内 `node_modules/@aragon-agent/core` 解析（`src/config.ts:226-229`）；
  `engine-runtime/current.json` 由 `upgrade/switch.py:23-36` 写入，但网关侧无读取点（`grep current.json` 0 命中）。
- 后果：换任何非 0.2.34 版本 → `wait_ready` 必然超时；网关重启后指针丢失，回退到环境变量 pin。

### F-11 schema 双轨：迁移从不执行 `[已复核]`

- `services/bootstrap.py:24-28`：`Base.metadata.create_all(engine)` + `PRAGMA user_version=SCHEMA_VERSION`。
- `services/bootstrap.py:166-168`：`alembic_rev = _alembic_revision()`；**为 None 时直接 `return`**（静默放过）。
- 因此 `apps/api/migrations/versions/0001_init.py`、`0002_engine_upgrade.py` 在本机 bootstrap 生成的库里从不执行，
  `alembic_version` 为空；而 `scripts/restore.py:278-303` 恢复时又真的跑 alembic → 两套 schema 来源。
- 需求原文：「提供数据库迁移、备份和恢复机制」；现状是「有迁移文件，但没有迁移机制」。

### F-12 幂等无 in-flight 预留 `[待复核]`

- 报告位置：`services/execution.py:58-119`（`_idempotency_lookup` 在调用上游前查、`_idempotency_store` 在返回后插）、
  `:410`；唯一索引 `uq_idem_scope_key`（`models/execution.py:115-118`）。
- 并发同键两个请求：都查不到 → 都调上游（双计费）→ 后写者撞唯一索引 → 未捕获 `IntegrityError` → 500。

## 3. P1 缺陷

| ID | 现象 | 证据 | 复核 |
|---|---|---|---|
| F-13 | 设置项写库不生效：`payload_policy`/`retention_days`/`fetch_allowlist`/`ssrf_deny`/`engine_policy` 运行时仍读环境变量 | 写 `api/admin/settings.py:24-31`、`services/settings.py:57-83`；读 `services/execution.py:179`、`worker/handlers/retention.py:17`、`security/fetch_guard.py:41,53`、`api/admin/engine.py:44,84` | [待复核] |
| F-14 | `GET /settings/backup/now` 有副作用，`require_csrf` 只校验写方法 → 跨站 `<img>` 可触发全量备份 | `api/admin/settings.py:81-97`、`security/csrf.py:8,11-15` | [待复核] |
| F-15 | 签名 URL 契约漂移：文档称「10 分钟有效、无需令牌」，实现强制 `require_token`，`save_bytes` 也不返回签名 URL | `api/v1/files.py:42-56`、`docs/api/examples.md:436`、`services/files.py:155-162` | [待复核] |
| F-16 | 调用记录时间筛选失效：前端发 `from`/`to`，后端只认 `date_from`/`date_to` | `apps/web/app/(app)/calls/page.tsx:18,135-154` ↔ `api/admin/calls.py:23-24` | [待复核] |
| F-17 | 列表排序是假的：表头标 sortable，后端固定 `created_at desc` 且不接受 `sort` | `calls/page.tsx:59,72,109`、`tasks/page.tsx:46-49` ↔ `api/admin/calls.py:17-40`、`services/calls.py:51`、`api/admin/tasks.py:14-27` | [待复核] |
| F-18 | 任务页无搜索/筛选/分页，固定 `page_size=100` 全量拉取 | `apps/web/app/(app)/tasks/page.tsx:45-49` | [待复核] |
| F-19 | 凭证没有独立列表页（搜索/筛选/排序/分页缺失），只能在提供商详情里看到 | `apps/web/app/(app)` 下无 `credentials/` 目录（已复核目录清单） | [已复核] |
| F-20 | CI 的 e2e 作业按现状必挂：注入 `E2E_BASE_URL` 而 playwright 读 `SR_BASE_URL`；且未 `npm run build` 就 `next start` | `.github/workflows/ci.yml:268-293`、`apps/web/playwright.config.ts:15,21-28` | [待复核] |
| F-21 | CI 无迁移往返作业（`alembic upgrade head` / `downgrade base`），也没有容器 entrypoint 冒烟 | `.github/workflows/ci.yml`（8 个作业，无 alembic 相关 step） | [已复核] |
| F-22 | runbook 两条命令在容器内必然失败：`docker compose run --rm api python …` 撞 `ENTRYPOINT`，`python` 被当 ROLE → `[FATAL] UNKNOWN_ROLE` | `deploy/Dockerfile.api:65`、`deploy/entrypoint.api.sh:20-21,112-114`、`docs/runbook/backup-restore.md:24-25`、`docs/runbook/init.md:129-130` | [待复核] |
| F-23 | 「切换前自动备份」只存在于文档：`switch()` 不调备份，`engine_upgrade_runs.backup_file_id` 永不写入 | `upgrade/switch.py:66-118`、`models/engine.py:55`、`docs/runbook/engine-upgrade.md:16,83` | [待复核] |
| F-24 | 无状态兼容检查与数据迁移方案：`needs_migration` 硬编码 `False`，无 sessions 结构断言，无旧版本回收 | `upgrade/verify.py:106-126,132` | [已复核] |
| F-25 | 维护模式未闭环（v2 遗留 D7/U1）：`var/.restore-in-progress` 未映射为 `503 MAINTENANCE`，`Code.MAINTENANCE` 无消费点 | `scripts/restore.py`（标记）、`errors.py:40`（定义）、全仓无读取点 | [待复核] |
| F-26 | 升级结果状态语义不足：UI 只区分 passed/failed，无法表达「blocked/待人工验证」，与「已可用/待适配/受权限限制」三态要求不齐 | `api/admin/engine.py`、`apps/web/app/(app)/settings/engine/page.tsx:181-421` | [待复核] |

## 4. P2（本轮登记，不实现）

| ID | 项 | 说明 |
|---|---|---|
| F-27 | §15.4 生产性能基线未采样 | `scripts/perf_smoke.py` 已可用；缺 4C8G + 50 万 `call_logs` 数据集的归档报告 |
| F-28 | 附录 C 外部 Agent 兼容矩阵全「待验证」 | 建议新增 `scripts/verify_compat.py` 把矩阵变成可执行脚本（仍需真实 Agent 环境才出证据） |
| F-29 | `docs/api/examples.md` 无漂移检查 | OpenAPI 已有 CI 漂移检查；examples 里的 curl 只能人工核对 |
| F-30 | 日志保留策略只在 compose 里 | `json-file 20m×5` 有了，但 runbook 缺「日志保留/轮转」一节 |
| F-31 | 无障碍无自动检查 | 焦点陷阱/aria 手写到位，缺 `axe`/`pa11y` 之类的自动门禁 |
| F-32 | 恢复后凭证可解密抽查未自动化 | 因为 AAD 的 `record_type` 取值未在代码注释中固定；补注释后可在 `restore.py` 内置抽查 |

## 5. 「已覆盖」清单（不要重复做）

审计同时确认下列能力**已经是真实现**，本轮不要重写：

- 闸门链 `require_initialized → session → CSRF`（`deps.py:61-66`）+ 强制改密覆盖 12 个 admin router。
- AES-256-GCM 信封加密与 AAD 绑定、主密钥轮换（keyset 分批 + 残留自检）。
- 租约领取的原子条件更新（`UPDATE … WHERE id=(SELECT …) RETURNING`）。
- 任务 7 态状态机与非法迁移拦截、`claim_submission` CAS、重启恢复只补在途缺失项。
- 流式「首个 delta 后不切换 route、不重试」。
- 发布必须带 24h 内测试证据（`PUBLISH_EVIDENCE_MISSING`）。
- 令牌只存 `sha256`、撤销即时生效、空 scopes fail-closed、发现接口按 `scope_allows` 过滤。
- 回调 HMAC 验签 + 时间戳窗 + `(provider_slug,event_id)` 去重。
- SKILL 导入路径穿越拒绝、导出包 secret scan、启停独立于 status。
- 前端设计令牌全量映射（tsx 内零硬编码 hex）、`DataTable` 搜索/筛选/排序/分页/窄窗卡片、
  `DangerConfirm` 影响面 + 输入动作词、`EmptyState` 五态、浮窗 Agent 的焦点陷阱/排队/停止对账。

## 6. 未覆盖范围（明确声明）

- 未做**真实第三方凭证**联调、真实模型推理、真实 MCP 客户端、真实 Docker 起栈、目标硬件性能采样
  ——这些是环境阻塞，不是代码缺陷，继续留在 `docs/runbook/verification-checklist.md` §F。
- 未做**压力/并发实测**：F-05/F-07/F-08/F-12 的并发结论来自代码阅读（WAL + deferred BEGIN + 进程内锁的推理），
  spec §A.7 为每条缺陷配了可复现的回归测试，实现时以测试结果为准。
- 未审计 `packages/contracts` 的生成物内容与 `db/migrations/0001_init.sql` 快照的一致性（属 P2，若 CI 的 contracts job 覆盖则不必另做）。
