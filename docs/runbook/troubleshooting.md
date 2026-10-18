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
| `TOKEN_SCOPE_DENIED` | 403 | 令牌无该能力权限 | 扩大作用域后重试（**不泄漏**能力是否存在，所以文案统一） | `call_logs(status=failed)` |

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
| `DB_BUSY` | 503 | 写等待超时（写序列化器 5s） | 降低并发；查是否有长事务/大导出；持续发生要按 §15.4 归档 `call_logs` |
| `MAINTENANCE` | 503 | 维护模式（备份/恢复中） | 等维护结束；`restore.py` 完成自检后会删除 `.restore-in-progress` 标记 |

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
| `scripts/seed_demo.py` 拒绝执行 | `SKILLROUTER_ENV=production` | — | 这是设计行为；演示数据只能进 development/staging |
| 凭证「检测」一直失败 | 上游地址/网络/密钥错 | 看 `credential_verifications.detail_redacted` | 修凭证或 `base_url`；SSRF 拒绝需调整 `SKILLROUTER_SSRF_DENY`（谨慎） |
| 抓取资料报 SSRF/allowlist | `SKILLROUTER_FETCH_ALLOWLIST` 为空或命中私网段 | 看审计与执行器详情 | 加白名单；不要整体删掉 `SSRF_DENY` |
| Windows 上 `python` 输出中文乱码 | cmd 代码页不是 UTF-8 | — | `chcp 65001`，或用 Windows Terminal |

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
