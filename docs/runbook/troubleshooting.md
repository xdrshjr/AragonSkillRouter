# 故障排查（runbook）

> 依据：spec 附录 A（错误码表）+ §7.14（可观测性与错误约定）+ §14.3（运维手册要点）。
> 原则：**前端只根据 `code` 分支**，所以本表按错误码组织；每条都给「立刻做什么」和「去哪看证据」。
> 报障请提供：`code`、`request_id`、时间（UTC）、操作路径、以及下面 §1 的诊断输出。

## 0. 先跑三条命令

```bash
# 1) 平台活着吗（含 DB 与迁移版本）
curl -sS http://127.0.0.1:8000/healthz

# 2) 对外接口活着吗（含引擎状态）
curl -sS https://router.example.com/api/v1/health

# 3) 容器状态（healthy 才是真的起来）
docker compose -f deploy/docker-compose.yml ps
```

| 现象 | 判断 | 下一步 |
|---|---|---|
| `/healthz` 不通 | api 没起或在重启 | `docker compose logs --tail=200 api`，按 §2 处理 |
| `/healthz` 通、`/api/v1/health` 502 | 反代/网络问题 | 查 `proxy` 与 `frontend` 网络、`nginx -t` |
| 都通但页面接口 401/403 | 会话或改密状态 | 见 §3 |
| `engine.status != available` | 网关/引擎问题 | 见 §8（能力调用不受影响，Agent 面板受影响） |

## 1. 诊断命令速查

```bash
# 服务日志（JSON 行，含 request_id / code / latency_ms）
docker compose -f deploy/docker-compose.yml logs --tail=200 api
docker compose -f deploy/docker-compose.yml logs --tail=200 worker gateway
# 应用日志（RotatingFileHandler，10MB × 5）
docker compose -f deploy/docker-compose.yml exec api tail -n 200 /data/logs/skillrouter.run.log

# 用 request_id 串起来（日志 + 调用记录）
grep -h "<request_id>" /data/logs/*.log
```

没有 `sqlite3` CLI 也能查库（镜像里本来就没有，这是设计如此）：

```bash
docker compose -f deploy/docker-compose.yml exec api python -c "
import sqlite3,json
c=sqlite3.connect('/data/skillrouter.db'); c.row_factory=sqlite3.Row
for r in c.execute('select request_id,status,error_code,latency_ms,created_at from call_logs order by created_at desc limit 5'):
    print(dict(r))
"
```

常用表：`call_logs`（调用记录）、`audit_logs`（管理操作）、`tasks`/`task_queue`（任务与租约）、
`system_state`（`admin_initialized`/`schema_version`/`engine_pin`）、`login_attempts`、
`rate_counters`/`quota_counters`、`capability_test_runs`、`engine_upgrade_runs`。

## 2. 认证与初始化

| 错误码 | HTTP | 含义 | 立刻做什么 | 证据 |
|---|---|---|---|---|
| `NOT_INITIALIZED` | 409 | 平台尚未完成初始化（除 `/healthz`、`/api/admin/auth/bootstrap`、`/api/admin/auth/login` 外一律拦截） | 服务器上跑 `python scripts/bootstrap.py`；**没有**匿名初始化接口，别找 | `system_state.admin_initialized` |
| `AUTH_INVALID_CREDENTIALS` | 401 | 用户名或密码错误 | 注意大小写与前后空格；确认没在「必须改密」状态用旧密码 | `audit_logs`（`admin.*`） |
| `LOGIN_THROTTLED` | 429 | 登录失败次数超限 | 等 `retry_after` 秒；需要立刻解锁就清理 `login_attempts` 对应行（谨慎） | 响应体 `retry_after`，`login_attempts` |
| `PASSWORD_CHANGE_REQUIRED` | 403 | 仍用默认密码 | 打开改密页完成改密（≥12 位、字母+数字、非默认值） | `admin_users.must_change_password` |
| `SESSION_EXPIRED` | 401 | 会话过期或被吊销（改密会吊销其它会话） | 重新登录；前端应跳登录并说明原因 | `admin_sessions.revoked_at/expires_at` |
| `CSRF_INVALID` | 403 | 写请求缺少或不匹配 `X-CSRF-Token` | 刷新页面重新拿 `sr_csrf`；确认反代没有丢 header | 请求头、`admin_sessions.csrf_hash` |
| `TOKEN_REVOKED` | 401 | 平台令牌已撤销 | 管理端重新签发；通知 Agent 侧更新凭据 | `platform_tokens.revoked_at` |
| `TOKEN_INVALID` | 401（查询串通道为 400） | 令牌缺失、拼错，**或令牌被放进了查询串**（见下） | 只发 `Authorization: Bearer <token>`；不要拼 `token` 查询参数 | 响应 `hint`、`call_logs`/`audit_logs` |
| `TOKEN_SCOPE_DENIED` | 403 | 令牌无该能力权限 | 扩大作用域后重试（**不泄漏**能力是否存在，所以文案统一） | `call_logs(status=failed)` |

### 2.1 `TOKEN_INVALID`：令牌只认请求头（§A.3.3 / F-04）

平台令牌**只在 `Authorization: Bearer <token>` 里认**。把令牌放进查询串的旧做法会
落进反代日志、浏览器历史、`Referer`，所以本轮默认关闭（`SKILLROUTER_ALLOW_QUERY_TOKEN` 默认 `0`）：

```bash
# 错：命中默认关闭的查询串通道 → 400 TOKEN_INVALID，并在 hint 里要求改用头
curl -sS "$SR_BASE/api/v1/whoami?token=srk_k1_xxx"

# 对：
curl -sS "$SR_BASE/api/v1/whoami" -H "Authorization: Bearer $SR_TOKEN"
```

**逃生阀**（仅迁移期）：`SKILLROUTER_ALLOW_QUERY_TOKEN=1` 可恢复旧通道，但每用一次都会记一条
`query_token_used` 告警日志。移除判据（三条同时满足就把它删掉，不要长期留 1）：

1. `docs/api/examples.md` 与 `docs/skills/connect.md` 里查询串令牌的示例为 **0**（CI 的 `scripts` 作业有脚本断言）；
2. 所有外部 Agent 的连接包都已升级到当前 `invoke.mjs`（只发头）；
3. 访问日志连续 30 天没有命中。

## 3. 权限与配额

| 错误码 | HTTP | 含义 | 立刻做什么 |
|---|---|---|---|
| `QUOTA_EXCEEDED` | 429 | 平台配额用尽（令牌/能力/提供商/全局某一层） | 看响应 `details` 里的层与窗口；调整配额或等窗口重置 |
| `BUDGET_EXCEEDED` | 402 | 月预算用尽 | 设置页调整 `monthly_budget_usd`；响应里会给出「已用百分比」 |
| `PROVIDER_NOT_AVAILABLE` | 409 | `options.preferred_provider` 不可用或无可用 route | 用响应里的可用 provider 列表重试，或检查该 provider 的凭证状态 |
| `CREDENTIAL_IN_USE` | 409 | 凭证仍被能力 route 引用 | 先看 `/credentials/{id}/impact`，切换或停用后删除 |
| `PROVIDER_IN_USE` | 409 | 提供商仍有关联凭证/能力 | 看影响面；确需删除用 `?force=true` 软删（保留历史可追溯） |
| `CAPABILITY_IN_USE` | 409 | 能力被 SKILL/令牌/在途任务引用 | 用「停用」而不是删除；看 `/capabilities/{id}/impact` |
| `NOT_FOUND` | 404 | 对象不存在**或**无权查看（同一文案，防探测） | 核对 id；令牌作用域不足时会得到同样的 404 |

配额排查要点：检查顺序是 **令牌 → 能力 → 提供商 → 全局**，任一层超限即拒绝并说明是哪一层；
60s 窗口计数在 `rate_counters`，日/月与费用在 `quota_counters`。

## 4. 能力定义与调用契约

| 错误码 | HTTP | 含义 | 立刻做什么 |
|---|---|---|---|
| `VALIDATION_FAILED` | 400 | 通用字段校验失败 | 按 `details.fields` 逐项修 |
| `INPUT_SCHEMA_INVALID` | 400 | 输入不符合能力的 `input_schema` | 按 `details.fields` 修；多余字段放 `options.extensions`（`additionalProperties: false` 时） |
| `UNSUPPORTED_PARAM` | 400 | 未声明的参数，或 MIME 不在 `file_io.input` 声明里 | 删掉该字段或改用 `{"$file":...}`；`details.field` 指向具体字段 |
| `UNSUPPORTED_MODE` | 400 | 请求模式不在能力 `modes` 里 | 看 `details.supported`；流式能力才会支持 `stream` |
| `PARAM_MAP_UNRESOLVED` | 400 | route 的 `param_map` 引用了未声明的来源 | 修 `param_map` 的 `input.*` / `route.*` 引用 |
| `IDEMPOTENCY_KEY_REUSED` | 409 | 同一幂等键配了不同请求体 | 换新的 `Idempotency-Key`（幂等作用域是 `token:<id>`/`admin:<id>`，跨令牌互不干扰） |
| `IDEMPOTENCY_IN_PROGRESS` | 409 | 同一幂等键的请求**还在处理中**（`idempotency_keys.state=in_flight`），本轮新增（§A.5.1） | **不要换键**：用同一个 `Idempotency-Key` 退避重试（1s→2s→4s，上限 30s），或先查 `GET /api/v1/tasks/{id}` / `call_logs` 看首次请求的结果。换键会产生第二次上游调用（重复扣费） |
| `INVALID_STATE_TRANSITION` | 409 | 状态机非法迁移（任务/流程/版本） | 刷新拿最新状态再操作；不要并发点两次 |

## 5. 上游与任务

| 错误码 | HTTP | 含义 | 立刻做什么 |
|---|---|---|---|
| `UPSTREAM_AUTH` | 502 | 上游拒绝鉴权 | 到凭证页「检测」；必要时 `POST /credentials/{id}/replace`（就地换密文，不动引用） |
| `UPSTREAM_RATE_LIMIT` | 429 | 上游限流 | 降低该提供商并发/提高优先级，或用另一条 route；平台按 `errors[].backoff_ms` 重试至 `max_attempts` |
| `UPSTREAM_QUOTA` | 402 | 上游余额/配额不足 | 到提供商控制台充值；同时考虑切到备用 provider |
| `UPSTREAM_TIMEOUT` | 504 | 上游超时 | 先看任务状态，**不要重复提交**（非幂等请求会进 `status_unknown`） |
| `UPSTREAM_UNAVAILABLE` | 503 | 上游不可用 | 查上游状态页；临时切 provider |
| `TASK_STATUS_UNKNOWN` | 409 | 无法判定上游是否受理 | `POST /api/admin/tasks/{id}/reconcile` 对账后再决定，禁止直接重投 |
| `CANCEL_UNSUPPORTED` | 409 | 上游不支持取消 | UI 明示「仅停止本地等待，远端可能仍在跑」；任务标 `canceled_local` |
| `LEASE_LOST` | 409 | 任务队列租约丢失（心跳超时后被别的 worker 领走），本轮新增（§A.3.6） | **无需人工干预**：任务会被重新领取执行；`retryable=true`。若同一任务反复出现，查 worker 是否被 OOM/重启（`docker compose ps` 的 restarts、`worker_heartbeats.last_seen_at` 断档） |
| `DB_BUSY` | 503 | 写等待超时（写序列化器 5s） | 降低并发；查是否有长事务/大导出；持续发生要按 §15.4 归档 `call_logs` |
| `MAINTENANCE` | 503 | 维护模式：`<data>/.restore-in-progress` 存在（恢复备份中） | 等维护结束；读接口照常、写请求与 invoke 被暂停。`restore.py` 完成自检后会自动删除标记；标记存活超过 24h 视为**陈旧**，日志会打 `stale_maintenance_marker`，此时按 §5.1 手工确认后删除 |
| `MIGRATION_NOT_ADOPTED` | —（**不是 HTTP 错误码**） | 存量库的 alembic 版本与指纹不匹配：启动期常量，只出现在日志与退出码里（非 0 退出） | 见 §5.2：跑 `python scripts/adopt_alembic.py`（比对指纹后 stamp）再启动 |

任务排查：

```bash
# 队列租约是否卡住（leased 但 lease_expires_at 已过期 → 等 worker 重启复位，或手动复位）
docker compose -f deploy/docker-compose.yml exec api python -c "
import sqlite3;c=sqlite3.connect('/data/skillrouter.db')
print(c.execute(\"select status,count(*) from task_queue group by status\").fetchall())
print(c.execute(\"select id,status,attempts,failure_class from tasks where status not in ('succeeded','canceled') order by created_at desc limit 10\").fetchall())
"
# worker 心跳（容器 healthcheck 也是看它）
docker compose -f deploy/docker-compose.yml exec api python -c "
import sqlite3;c=sqlite3.connect('/data/skillrouter.db')
print(c.execute('select last_seen_at from worker_heartbeats order by last_seen_at desc limit 3').fetchall())"
```

### 5.1 维护模式（`MAINTENANCE` / `503`）

判据只有一个：`<SKILLROUTER_DATA_DIR>/.restore-in-progress` 存在（`scripts/restore.py` 创建与删除）。
不是「服务没起来」，而是「库正在恢复、写进去会被污染」。

```bash
# 现在是不是维护中（true 就别再怪客户端）
curl -sS http://127.0.0.1:8000/healthz
# 标记文件的年龄（秒）
docker compose -f deploy/docker-compose.yml --env-file deploy/.env run --rm \
  --entrypoint /app/deploy/entrypoint.api.sh api run \
  python -c "import os,time;p='/data/.restore-in-progress';print(p, time.time()-os.stat(p).st_mtime)"
```

- 恢复脚本异常退出、标记残留超过 **24h** → 日志出现 `stale_maintenance_marker`，此时平台**仍然**
  按维护处理（宁可 503，也不让半恢复的库被写）。确认恢复确实已失败/放弃后由人工删除：
  `docker compose ... run --rm --entrypoint /app/deploy/entrypoint.api.sh api run rm -f /data/.restore-in-progress`，
  然后 `curl /healthz` 看到 `maintenance:false` 再放流量。
- 容器内一次性命令必须走 `--entrypoint python` 或 `run` 角色：`docker compose run --rm api python …`
  会把 `python` 当角色名 → `[FATAL] UNKNOWN_ROLE`（见 [init.md](init.md) §0）。
  本机已实测三条路径（`run` 位置参数 / `SKILLROUTER_ROLE=run` / 错误角色），命令与输出见 §F1 的 R2-6。

### 5.2 `MIGRATION_NOT_ADOPTED`（启动期，不进 HTTP 错误码表）

存量库有业务表但没有 `alembic_version`（F-11 之前用 `create_all` 建的库），
`bootstrap.py` 会先算**业务表指纹**再决定 stamp：

| 情况 | 行为 | 你要做什么 |
|---|---|---|
| 指纹一致 | `stamp 0002_engine_upgrade` → `upgrade head`，启动继续 | 什么都不用做（日志 `mode=adopted_then_upgrade`） |
| 指纹不一致 | 打 `[FATAL] MIGRATION_NOT_ADOPTED …` 并以非 0 退出，**不写任何版本号** | 先 `--dry-run` 看差异，人工确认后再决定 |

```bash
# 只诊断，不写库（对当前库跑，容器内一次性命令用 --entrypoint python）
docker compose -f deploy/docker-compose.yml --env-file deploy/.env run --rm \
  --entrypoint python api scripts/adopt_alembic.py --db /data/skillrouter.db --dry-run
# 退出码：0 = 一致/dry-run 通过；3 = 指纹不一致（拒绝 stamp）；2 = 用法/环境错误
```

退出码 `3` 意味着**当前模型的表结构与这个库对不上**（例如中间版本改过列）。这时不要强行 stamp：
要么先用备份恢复到一致版本，要么按差异手工补 SQL，再重试。

## 6. SKILL 与分发

| 错误码 | HTTP | 含义 | 立刻做什么 |
|---|---|---|---|
| `SKILL_DISABLED` | 409 | SKILL 被停用（`enabled=0`） | 管理员在 SKILL 页启用；停用不产生新版本、不改历史 |
| `SKILL_DEPENDENCY_MISSING` | 409 | 依赖的能力版本缺失或已废弃 | 补依赖或发新版本 SKILL；看 `/skills/{id}/dependencies` |

外部 Agent 侧问题（安装/验证/调用失败、退出码、目录探测）见
[`../skills/connect.md`](../skills/connect.md) §7。

## 7. 引擎与升级

| 错误码 | HTTP | 含义 | 立刻做什么 |
|---|---|---|---|
| `ENGINE_UNAVAILABLE` | 503 | 网关不可用或引擎不可用 | 查 `GET /healthz` 的 `engine.status/detail`；`docker compose logs gateway`；**已发布能力不受影响** |
| `UPGRADE_IN_PROGRESS` | 409 | 有升级正在执行 | 等当前升级结束；崩溃残留需人工把 `engine_upgrade_runs.result` 置 `failed`（不会自动重投） |

详见 [engine-upgrade.md](engine-upgrade.md) §4。

## 8. 现象级排查（没有错误码的那种）

| 现象 | 最可能的原因 | 验证方式 | 处置 |
|---|---|---|---|
| 浮窗 Agent 一直转圈，最后一次性刷出全部内容 | 反代开了缓冲 | `curl -N` 直连 api 8000 对比经 proxy 的行为 | nginx 必须 `proxy_buffering off` + `X-Accel-Buffering: no`（`deploy/nginx.conf` 已配） |
| SSE 30–60s 后断开 | 反代/网关读超时 | 看 nginx `proxy_read_timeout`、网关 `AGENT_SSE_KEEPALIVE_MS=15000` | 提到 3600s；确认每 15s 有 `: keep-alive` |
| 上传 413 | `client_max_body_size` 太小 | 报错来自 nginx 而非应用 | 调到 50m（与 `SKILLROUTER_MAX_UPLOAD_MB` 对齐） |
| 页面能开、接口 401 循环 | `SKILLROUTER_ENV=production` 下走 http，`Secure` Cookie 被丢弃 | 浏览器 DevTools 看 Set-Cookie | 必须 https；仅本地测试才改 development |
| 概览数字不动 | 概览是聚合快照/缓存 | 刷新页面；看 api 日志有无慢查询 | 按 §15.4 处置：归档 `call_logs`、补索引、物化快照 |
| 备份失败 `[FATAL] 数据库不存在` | 路径解析与 api 不一致 | 确认 `SKILLROUTER_DATA_DIR` / `SKILLROUTER_DB_PATH` 与容器内一致（compose 已固定 `/data`） | 用 `--db` 显式指定 |
| 点开文件/图片链接返回 `401`（以前是 404） | 签名 URL 只 **10 分钟**有效（`exp` 为 Unix 秒，过期或签名不符），或既没签名也没带令牌。本轮把「无效签名」从 404 统一为 401，避免被误读成「文件不存在」 | 响应 `hint`：`链接已过期，请重新获取（签名 URL 有效期 10 分钟）` / `缺少 Authorization 头` | 重新取 `output_files[].signed_url`（任务详情/调用响应里都有），或改用 `Authorization: Bearer` + `files:read` 作用域 |
| `scripts/seed_demo.py` 拒绝执行 | `SKILLROUTER_ENV=production` | — | 这是设计行为；演示数据只能进 development/staging |
| 凭证「检测」一直失败 | 上游地址/网络/密钥错 | 看 `credential_verifications.detail_redacted` | 修凭证或 `base_url`；SSRF 拒绝需调整 `SKILLROUTER_SSRF_DENY`（谨慎） |
| 抓取资料报 SSRF/allowlist | `SKILLROUTER_FETCH_ALLOWLIST` 为空或命中私网段 | 看审计与执行器详情 | 加白名单；不要整体删掉 `SSRF_DENY` |
| Windows 上 `python` 输出中文乱码 | cmd 代码页不是 UTF-8 | — | `chcp 65001`，或用 Windows Terminal |
| **`/login` 卡片区一片空白、一直停在骨架屏**（dev 环境，`next dev`） | `next.config.mjs` 的 CSP `script-src` 缺 `'unsafe-eval'`；`next dev` 的产物用 eval 包模块，被 CSP 拦掉后整页不 hydrate，React Query 不会发 `/admin/auth/bootstrap`，骨架屏永远不消失（SSR 出来的就是骨架） | `curl -s localhost:3000/login \| findstr sr-skeleton` 有输出但页面无表单；浏览器控制台有 `Evaluating a string as JavaScript violates … 'unsafe-eval' is not an allowed source` | `next.config.mjs` 已按 `NODE_ENV` 区分：dev 放 `'unsafe-eval'`，production 保持严格。改完必须**重启 web 进程**（`.\start.ps1 -Action restart`），Next 不会热加载配置 |
| 生产构建的页面一片空白 | 误把 dev 的 CSP 带进生产，或反之漏配 `'unsafe-inline'` | `node --input-type=module -e "console.log((await (await import('./next.config.mjs')).default.headers())[0].headers.find(h=>h.key==='Content-Security-Policy').value)"` 配合 `NODE_ENV=production` 看实际值 | 生产应只含 `script-src 'self' 'unsafe-inline'`；出现 `'unsafe-eval'` 说明 `NODE_ENV` 不是 `production` |
| dev 首次访问某路由控制台报 `Invalid or unexpected token` | 该路由的 chunk 正在被 `next dev` 首次编译，浏览器拿到了半成品 | 再刷新一次即消失 | 等待编译完成再刷新；持续存在才是真 JS 语法错误 |
| 登录后任意页面整页崩成 `Application error: a client-side exception has occurred`，控制台 `Cannot read properties of undefined (reading 'length')`，栈指向 `components/agent/useAgentSession.ts` | 后端契约缺字段：`GET /api/admin/agent/sessions/{id}` 曾经没返回 `tool_calls`（只在 `turn_id` 维度有 `events.tool_calls()`，会话级汇总没人拼），而 `AgentProvider` 挂在 `(app)/layout.tsx` 里包住**所有**页面，读 `detail.tool_calls.length` 直接抛 | `curl -s -b cookies.txt http://127.0.0.1:8000/api/admin/agent/sessions/<id>` 看键里有没有 `tool_calls` | 已修两侧：`agent/sessions.py::get_session` 现在返回 `tool_calls`（字段名对齐前端 `args_summary`）；`useAgentSession.ts` 对 `turns`/`tool_calls`/`todos` 做 `Array.isArray` 兜底，缺字段退化成「没有历史」而不是白屏 |
| 用了 `asChild` 的按钮把整页打崩：`Slot failed to slot onto its children` | `components/ui/button.tsx` 无条件渲染 `{icon}` + `{children}` 两个槽位；`asChild` 下传给 Radix `Slot` 的是数组（缺 icon 时 `undefined` 也被 `React.Children.count` 计入），永远不等于 1 | 控制台搜 `Slot failed to slot onto its children` | 已修：`asChild` 分支用 `<Slottable>{children}</Slottable>` 包住子节点，`Spinner`/`icon` 作为并列兄弟。**不要**用 `<>…</>` 包起来——Fragment 会被 Slot 当作目标并灌 `type`/`disabled`，报 `Invalid prop 'type' supplied to React.Fragment` |

## 9. 报障模板

```
时间（UTC）：
环境：production / staging / 本地
组件：api / worker / gateway / web / proxy
操作路径：
错误 code + request_id：
复现步骤（3 行内）：
已做的诊断（§0/§1 输出关键行）：
影响范围：哪些能力/Agent 受影响；是否影响已发布能力调用
```
