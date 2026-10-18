# 初始化与首次部署（runbook）

> 适用：全新机器从零到「能登录、能改密、能接入第一个能力」。
> 依据：spec §4.4(a) 初始化时序、§7.1 配置与启动、§14.2/§14.3 部署与运维、§15.5 DoD。
> 相关：[backup-restore.md](backup-restore.md) · [engine-upgrade.md](engine-upgrade.md) ·
> [troubleshooting.md](troubleshooting.md) · [verification-checklist.md](verification-checklist.md)

## 0. 部署前检查清单

| 项 | 要求 | 说明 |
|---|---|---|
| 机器 | 4 vCPU / 8 GB / SSD（§15.4 性能基线条件） | 低于此规格请重跑性能采样并记录 |
| 系统 | Linux + Docker Compose v2；Windows 用 Docker Desktop 或直接四进程本地跑 | 备份/恢复脚本跨平台 |
| 域名与证书 | 已解析到本机，证书放 `deploy/certs/fullchain.pem` + `privkey.pem` | `nginx.conf` 固定读这两个路径 |
| 出站 | 能访问上游 API 与模型提供商；`SKILLROUTER_EGRESS_POLICY=deny` 时关闭版本检测出站 | §12.4 |
| 备份目标 | 异地存储（对象存储/另一台机器）与保留策略 | 每日备份、保留 14 份（§14.3） |
| 时钟 | NTP 同步 | 会话过期、幂等、签名 URL 都依赖时间 |

## 0. 容器内跑一次性命令：只能用这两种写法（§A.3.12 / F-22）

api 镜像的 `ENTRYPOINT` 是 `/app/deploy/entrypoint.api.sh`，**第一个参数就是角色名**。
所以 `docker compose run --rm api python …` 会把 `python` 当角色名，直接
`[FATAL] UNKNOWN_ROLE 用法：entrypoint.api.sh api|worker|run`。正确写法（二选一，等价）：

```bash
# 写法 A（最短）：绕过入口脚本，直接跑 python。注意 cwd 是镜像的 WORKDIR=/app
docker compose -f deploy/docker-compose.yml --env-file deploy/.env run --rm \
  --entrypoint python api scripts/bootstrap.py

# 写法 B（推荐，保留入口脚本的目录/环境处理）：显式给 run 角色，剩余参数原样 exec
docker compose -f deploy/docker-compose.yml --env-file deploy/.env run --rm \
  --entrypoint /app/deploy/entrypoint.api.sh api run python scripts/bootstrap.py

# 写法 C（等价于 B）：用环境变量指定角色；此时**所有位置参数都是命令**，不会被当角色名
docker compose -f deploy/docker-compose.yml --env-file deploy/.env run --rm \
  -e SKILLROUTER_ROLE=run --entrypoint /app/deploy/entrypoint.api.sh api \
  python scripts/bootstrap.py
```

角色解析优先级（`deploy/entrypoint.api.sh` 顶部，实现细节，排查时会用到）：

| 输入 | 解析结果 | 位置参数怎么算 |
|---|---|---|
| `SKILLROUTER_ROLE=<角色>` 已设置 | **环境变量优先** | 全部都是命令/参数（不 shift） |
| 只有首个位置参数 | `$1` 即角色 | 其余是命令/参数（shift 掉角色） |
| 两者都没有 | 默认 `api` | — |

`run` 角色**不做**启动前备份、**不跑**迁移，只 `exec` 后面的命令（避免「查一次库顺手又迁移一遍」）。
看日志/查库用 `exec`（在**已在运行**的容器里执行，不经过 ENTRYPOINT，写法不变）：

```bash
docker compose -f deploy/docker-compose.yml exec api python -c "print('ok')"
```

## 0.1 `python scripts/bootstrap.py` 是初始化单轨入口

「迁移 + 种子 + 首次管理员初始化 + 清理过期会话 + ensure_dirs + 启动摘要」全部由它一个入口完成
（`services/bootstrap.py`）。**不要**再手工 `alembic upgrade head` 拼流程，也不要写只跑一半的脚本：

| 你想做 | 用什么 | 不要用什么 |
|---|---|---|
| 首次初始化 / 每次启动的迁移 | `python scripts/bootstrap.py` | 手写 `alembic upgrade head`（会漏种子与 `schema_version`） |
| 存量库（无 `alembic_version`）接入版本管理 | `bootstrap.py` 内部会先算指纹再 `stamp`；诊断用 `python scripts/adopt_alembic.py --dry-run` | 直接 `alembic stamp head`（会把历史迁移跳过） |
| 重置管理员密码 | `python scripts/bootstrap.py reset-admin-password --user admin` | 直接改 `admin_users` 表 |

之所以「单轨」：迁移在入口脚本里只允许调 `bootstrap.py`（`do_migrate()` 的第一分支），
避免「A 脚本跑了迁移、B 脚本没跑种子」导致的状态漂移。`entrypoint.api.sh` 里退回
`alembic upgrade head` 的分支只在 `scripts/bootstrap.py` **整个缺失**时才走，并会打 `[WARN]`。

## 1. 生成三个密钥（一次，妥善保存）

```bash
# 1) 主密钥：唯一能解密凭证的东西，丢失 = 所有凭证报废
python -c "import base64,secrets;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"

# 2) 文件签名密钥：给 /api/v1/files 的预览链接签名，与主密钥分离（轮换互不牵连）
python -c "import base64,secrets;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"

# 3) 网关共享密钥：api/worker 与 Node 网关之间的 X-Internal-Token，≥32 字符
python -c "import secrets;print(secrets.token_urlsafe(32))"
```

存哪里：

- 生产：密钥管理服务或部署机的 `deploy/.env`（`chmod 600`，不进 git、不进镜像、不进工单）；
- 至少留一份**离线副本**（主密钥丢失后，备份包也解不出凭证 —— `scripts/restore.py`
  会用指纹拒绝恢复并提示原因）。

## 2. 准备 `deploy/.env`

```bash
cp deploy/.env.example deploy/.env
# 必改项：
#   SKILLROUTER_ENV=production
#   SKILLROUTER_PUBLIC_BASE_URL=https://router.example.com
#   SKILLROUTER_MASTER_KEY / SKILLROUTER_FILE_SIGNING_SECRET / SKILLROUTER_AGENT_GATEWAY_SECRET
#   SKILLROUTER_DATA_DIR=/data      SKILLROUTER_DB_PATH=/data/skillrouter.db
#   SKILLROUTER_ENGINE_POLICY=notify（先别 auto，等升级演练通过再放开）
#   SKILLROUTER_EGRESS_POLICY=allow（只有在需要自动检测引擎新版本时）
```

模板里**没有任何真实密钥**，所有 `<generate-me…>` 都要替换；`SKILLROUTER_ADMIN_PASSWORD`
只在首次初始化时生效。

## 3. 启动

```bash
# 生产前把基础镜像固定到 digest（见 deploy/docker-compose.yml 文件头）
docker compose -f deploy/docker-compose.yml up -d
docker compose -f deploy/docker-compose.yml ps           # api/worker/gateway/web/proxy 全部 healthy
```

启动顺序由 `depends_on: condition: service_healthy` 保证：

```
api（迁移 → 首次管理员初始化 → 启动前 quick 备份）
  → gateway / worker
    → web
      → proxy
```

`entrypoint.api.sh api` 依次做：

1. 建目录（`/data`：`logs/ files/ backup/ engine-builds/ aragon-home/ engine-runtime/ downloads/`）；
2. DB 已存在则跑 `python scripts/backup.py --quick`（失败即中止；临时跳过用
   `SKILLROUTER_SKIP_STARTUP_BACKUP=1`）；
3. `python scripts/bootstrap.py`（迁移 → 种子 → **首次**管理员初始化 → 清理过期会话 → 目录检查 → 打印摘要）；
4. `uvicorn`（无 `--reload`）。

验证：

```bash
curl -sS http://127.0.0.1:8000/healthz          # 含 DB 与迁移版本
curl -sS https://router.example.com/api/v1/health
docker compose -f deploy/docker-compose.yml logs --tail=50 api gateway
```

## 4. 首次登录与强制改密（§4.4(a)）

1. 打开 `https://router.example.com/login`；
2. 用 `SKILLROUTER_ADMIN_USER` / `SKILLROUTER_ADMIN_PASSWORD`（默认 `admin` / `adminjr`）登录；
3. 系统提示必须改密：≥12 字符、含字母+数字、不等于默认值；
4. 改密成功后其它会话被吊销，签发正常会话，写审计 `admin.password_changed`。

**必须验证的硬约束**（§15.5 第 2 项）：

```bash
docker compose -f deploy/docker-compose.yml restart api
# 重启后用新密码能登录、默认密码不能登录、system_state.admin_initialized 仍为 '1'
```

用 SQL 复核（不需要 sqlite3 CLI）：

```bash
docker compose -f deploy/docker-compose.yml exec api python -c \
"import sqlite3;c=sqlite3.connect('/data/skillrouter.db');print(c.execute(\"select value from system_state where key='admin_initialized'\").fetchone())"
```

## 5. 接入第一个能力（闭环）

| 步 | 操作 | 检查点 |
|---|---|---|
| 1 | 提供商（`/providers`）→ 新建：填 `base_url` | 保存后列表可见 |
| 2 | 凭证（`/providers/{id}/credentials`）→ 新建：粘贴密钥 | 界面只显示掩码；「检测」后 `last_verify_result` 有结果 |
| 3 | 能力（`/capabilities/new`）或 Agent 浮窗生成草案 | `input_schema` / `output_schema` / `modes` / `examples` 齐全 |
| 4 | 「校验」→「测试」：**真实调用一次** | 测试记录 `ok=1`，响应预览已脱敏 |
| 5 | 「发布」：填版本与变更说明 | 产生不可变 `capability_versions` 行 + 发布证据 |
| 6 | 外部 Agent：`/access` 建 Agent + 令牌 → 复制 `install_command` | 见 [../skills/connect.md](../skills/connect.md) |
| 7 | 在干净目录跑 `install` → `verify` → `node ./invoke.mjs invoke ...` | 拿到结果且 `/calls` 能看到记录 |

每一步的证据（真实响应、用量、`request_id`）都要留在
[verification-checklist.md](verification-checklist.md) 里，未跑通的保持「待验证」。

## 6. 忘记管理员密码

```bash
# 1) 停掉 api（避免并发写）
docker compose -f deploy/docker-compose.yml stop api worker
# 2) 交互式重置（脚本属后端交付物：scripts/bootstrap.py）
#    注意：必须走 --entrypoint python（或 `run` 角色），否则会被当成角色名而 UNKNOWN_ROLE
docker compose -f deploy/docker-compose.yml --env-file deploy/.env run --rm --no-deps \
  --entrypoint python api scripts/bootstrap.py reset-admin-password --user admin
# 3) 起服务 → 用新密码登录 → 立刻改密
docker compose -f deploy/docker-compose.yml start api worker
```

- 重置会写审计；**不会**复活默认密码，也**不会**改动 `admin_initialized`。
- 如果重置后仍无法登录：查 `login_attempts` 是否被限流（`429 LOGIN_THROTTLED`，等待
  `retry_after` 秒；限流参数见 `SKILLROUTER_LOGIN_*`）。

## 7. 主密钥轮换（不停服）

```bash
# 1) 生成新密钥并配上「旧密钥」与新 key_id，然后重启 api/worker
#    SKILLROUTER_MASTER_KEY=<新>
#    SKILLROUTER_MASTER_KEY_PREVIOUS=<旧>
#    SKILLROUTER_MASTER_KEY_ID=mk2

# 2) 触发全表重加密（后台任务：分批 50 行 + master_key_rotations 记录）
python scripts/rotate_master_key.py --base-url https://router.example.com --wait
#    Cookie/CSRF 走 --cookie-stdin / --csrf 或 $SKILLROUTER_SESSION_COOKIE / $SKILLROUTER_CSRF_TOKEN

# 3) 抽查：1 个凭证能解密、1 个能力能调用、概览无异常
# 4) 移除 SKILLROUTER_MASTER_KEY_PREVIOUS 并再次重启（关闭双读窗口）
```

离线模式（`--offline`）只打印步骤，**不会**在本地重加密：那样会写出一份「看起来加密了
但打不开」的数据。

## 8. 令牌与作用域

| 动作 | 位置 | 注意 |
|---|---|---|
| 新建外部 Agent | `/access` | 名称用于日志与调用记录归因 |
| 签发令牌 | `POST /api/admin/access/agents/{id}/tokens` | **只返回一次**完整令牌；库里只有 `sha256` 与前缀 |
| 撤销令牌 | `DELETE /api/admin/access/tokens/{id}` | **即时生效**，下一次调用 `401 TOKEN_REVOKED`（无缓存窗口） |
| 收紧作用域 | 令牌编辑 | 只影响后续调用；历史记录不变 |
| 限流/预算 | 配额与预算页 | 层次：令牌 → 能力 → 提供商 → 全局；超限返回 `429`/`402` 并指明是哪一层 |

令牌泄露处置：立即撤销 → 查 `call_logs` 中该令牌的调用 → 视情况轮换对应上游凭证
（`POST /credentials/{id}/rotate` 原子切换引用，不影响 SKILL）。

## 9. 停服务顺序与维护模式

```bash
# 正常停（保留数据卷）
docker compose -f deploy/docker-compose.yml stop proxy web worker gateway api
# 恢复顺序相反（api 先起，等 healthy）

# 维护模式（备份/恢复前）：管理写接口返回 503 MAINTENANCE
#   restore.py 会在 <data-dir>/.restore-in-progress 放标记，成功自检后自动删除
```

升级镜像：

```bash
docker compose -f deploy/docker-compose.yml pull
docker compose -f deploy/docker-compose.yml up -d     # 启动前自动 quick 备份
# 回滚 = 用上一个镜像 tag 再 up -d（数据在命名卷里，不随镜像变化）
```

## 10. 首次部署常见失败

| 现象 | 原因 | 处置 |
|---|---|---|
| api 容器反复重启，日志 `[FATAL] STARTUP_BACKUP_FAILED` | 磁盘满 / 卷不可写 | 查 `df -h`、卷权限（uid 1000）；临时 `SKILLROUTER_SKIP_STARTUP_BACKUP=1` |
| `[FATAL] BOOTSTRAP_MISSING` | `scripts/bootstrap.py` 不在镜像里 | 确认 `deploy/Dockerfile.api` 的 `COPY scripts/` 与代码版本一致 |
| `[FATAL] DATA_DIR_NOT_WRITABLE` | 卷属主与容器 uid（1000）不一致 | `chown -R 1000:1000` 卷目录 |
| `[FATAL] UNKNOWN_ROLE 用法：entrypoint.api.sh api\|worker\|run` | 用了 `docker compose run --rm api python …`，`python` 被当成角色名 | 改 `--entrypoint python` 或 `--entrypoint /app/deploy/entrypoint.api.sh … run python …`（见 §0） |
| `502` 且 api healthy | nginx 与 api 网络不一致 | 两服务都需在 `frontend` 网络（见 compose） |
| `409 NOT_INITIALIZED` | 初始化闸门生效但未初始化 | 跑 `bootstrap.py`；不要期待匿名初始化接口（不提供） |
| 页面能打开但接口全 403 | 还在「必须改密」状态 | 先完成改密 |
| 浮窗 Agent 报 `ENGINE_UNAVAILABLE` | 网关或引擎未就绪 | `curl http://127.0.0.1:<gw-port>/healthz`；查 `engine.detail` |
| `SKILLROUTER_ENV=production` 下 Cookie 不生效 | 用 http 访问导致 `Secure` Cookie 被丢弃 | 必须走 https（`nginx` 443），或临时把环境改成 development |

更多错误码对照：[troubleshooting.md](troubleshooting.md)。
