# 桌面端（Electron）运维手册

> 适用对象：拿到 `SkillRouter-Setup-0.1.0-win-x64.exe` 的单机用户与排错的人。
> 设计契约：[`../plans/desktop-electron/spec.md`](../plans/desktop-electron/spec.md)（v2.0）。
> 构建与出包：[`apps/desktop/README.md`](../../apps/desktop/README.md)。

桌面端**不是**生产部署方案（R5）：它按单管理员、单机、回环监听设计，生产请用
[`deploy/docker-compose.yml`](../../deploy/docker-compose.yml)。

---

## 1. 首次使用

1. 双击 `SkillRouter-Setup-0.1.0-win-x64.exe`（未签名 → SmartScreen 提示「更多信息 → 仍要运行」）。
2. 选择安装目录（默认 `%LOCALAPPDATA%\Programs\SkillRouter`）→ 下一步到底。
3. 双击桌面「Skill Router」。**冷启上限 120 秒、热启上限 45 秒**出现登录页。
4. 用 `admin` / `adminjr` 登录 → 立刻被要求改密（≥12 字符、含字母+数字、不等于默认值）。
5. 默认密码只在 `admin_initialized != '1'` 时创建**一次**；重启或升级不会复活它。

## 2. 目录布局

| 路径 | 内容 | 能不能删 |
|---|---|---|
| `%LOCALAPPDATA%\Programs\SkillRouter\` | 安装目录（`SkillRouter.exe` + `resources\{python,web,gateway}`） | 卸载会整体删除；**手改无效**，升级会整体替换 |
| `%APPDATA%\SkillRouter\desktop.json` | 桌面端配置（端口、`gatewaySecret`、`adminInitialized`） | 删了会按默认值重建（`apiPort` 变回构建期值 18100） |
| `%APPDATA%\SkillRouter\data\skillrouter.db` | 全部业务数据（SQLite WAL） | **不要手删**，先备份 |
| `%APPDATA%\SkillRouter\data\credentials-master.key` | 主密钥 | **丢失 = 凭证全部报废** |
| `%APPDATA%\SkillRouter\data\file-signing.key` | 文件签名密钥 | 同上 |
| `%APPDATA%\SkillRouter\data\desktop-first-run.json` | 首次运行摘要（只含指纹，不含密钥） | 可删 |
| `%APPDATA%\SkillRouter\data\agent-policy.json` | 网关策略（12 个 `sr_*` 工具白名单） | 每次 bootstrap 重写 |
| `%APPDATA%\SkillRouter\data\{files,logs,backup,aragon-home,engine-runtime,engine-builds,agent-sessions}` | 运行时目录 | 按需 |
| `%APPDATA%\SkillRouter\logs\desktop\{main,api,worker,gateway,web}.log` | 五路日志（10 MB × 5 轮转） | 可删（正在写的除外） |
| `%APPDATA%\SkillRouter\logs\desktop\smoke-report.json` | 冒烟报告 | 可删 |

> `resources\` 是只读的（安装目录在 `Program Files` 下不可写）。所有可写状态只在 `%APPDATA%\SkillRouter`。

## 3. 备份与恢复

数据目录里的库与开发 / 容器库**同构**（`PRAGMA user_version=2`，alembic head `0003_iteration2`），
因此可以直接用仓库里的脚本：

```bat
:: 备份（先关应用，避免 WAL 未收敛）
python scripts\backup.py --data-dir "%APPDATA%\SkillRouter\data" --out "%APPDATA%\SkillRouter\data\backup"

:: 恢复
python scripts\restore.py --file "%APPDATA%\SkillRouter\data\backup\skillrouter-YYYYmmdd-HHMMSS.zip" ^
  --data-dir "%APPDATA%\SkillRouter\data"
```

最少要备份两个文件：`data\skillrouter.db`（连同 `-wal` / `-shm`）与 `data\credentials-master.key`。
**没有主密钥的库是废库**（凭证全部解不开）。

恢复后重启应用：每次启动都会跑幂等 `bootstrap`（`upgrade` 模式），schema 会自动回到 head。

## 4. 端口与冲突

| 端口 | 谁在用 | 冲突时 |
|---|---|---|
| 18100 | api（**固定**，构建期烧进 web 的 `/api` rewrite） | `E-D01`（外部进程）或 `W-D06`（另一个 Skill Router） |
| 18101 起 | web（Next standalone，动态） | 自动顺延，最多试 10 个 |
| 18102 起 | 网关（Node sidecar，动态） | 自动顺延，最多试 10 个 |

定位占用者：

```bat
netstat -ano | findstr :18100
tasklist /FI "PID eq <上面的 PID>"
```

桌面端用 181xx 段，与开发模式的 8000 / 3000 / 8500 **刻意分开**：两套可以同时运行，数据目录也不同。

## 5. 错误码与处置

| 码 | 触发 | 处置 |
|---|---|---|
| `E-D01 DESKTOP_PORT_IN_USE` | 18100 被非 SkillRouter 进程占用 | 按上面两条命令定位并关闭占用程序；或重启机器后重试 |
| `E-D02 DESKTOP_RESOURCE_MISSING` | `resources\` 缺件（安装包被截断 / 杀软删除） | 重新安装；若杀软报毒，把 `resources\python\skillrouter-server.exe` 加入排除项（onedir 已降低误杀率，R2） |
| `E-D03 DESKTOP_BOOTSTRAP_FAILED` | ① `bootstrap()` 抛异常；② **launcher 的迁移硬校验失败（退出码 21）**：`user_version` / `alembic head` / `alembic_version` 任一不符 | 看 `logs\desktop\api.log` 与 `main.log` 里的 `[FATAL] E-D03` 行。**先备份**，再按 §3 恢复；仍失败则用仓库的 `python scripts\bootstrap.py` 手工排错。注意：这条校验与 `SKILLROUTER_ENV` 无关，**永远阻断**（P0-3） |
| `E-D04 DESKTOP_API_TIMEOUT` | 90 s 内 `/healthz` 未就绪 | 常见：数据目录不可写、库被其它进程占用、杀软拦截 exe。看 `api.log` 的第一条 Traceback |
| `E-D05 DESKTOP_WEB_TIMEOUT` | 120 s 内 `/login` 未就绪 | 多为 `resources\web\.next\static` 缺失（安装包损坏）→ 重装；看 `web.log` |
| `E-D07 DESKTOP_PORT_CONTRACT_MISMATCH` | `desktop.json.apiPort` 与 `routes-manifest.json` 烧死的端口对不上，且无法自动校正 | 删除 `%APPDATA%\SkillRouter\desktop.json` 让其按构建期值重建；仍不符则重装（安装包与 web 产物不同源） |
| `W-D01 DESKTOP_GATEWAY_DOWN` | 网关起不来 | 控制台其余页面可用，仅「内置 Agent」不可用；看 `gateway.log`（常见：`agent-policy.json` 缺失 → bootstrap 未成功） |
| `W-D02 DESKTOP_STRAY_ENV_FILE` | `<data>\.env` 存在（`Settings` 会按 cwd 读它） | 只警告不删除。若不是你建的，改名备份后用菜单「打开数据目录」核对 |
| `W-D06 DESKTOP_INSTANCE_RUNNING` | 本实例拿到单实例锁，但 18100 已被**另一个安装/会话**的 Skill Router 占用 | 提示「检测到另一个 Skill Router 正在使用 18100」，只提供「退出 / 打开日志」。同安装同用户的第二次双击由单实例锁直接处理（退出码 0、聚焦已有窗口），**不产生任何错误码** |

> `E-D06` **不是**错误码（v1.0 的错误，已在 spec §2.3.2 更正为 `W-D06` 警告级）。

## 6. 日志

行格式：

```text
[2026-10-08T10:00:12.345Z] [api] uvicorn running on http://127.0.0.1:18100 (Press CTRL+C to quit)
[2026-10-08T10:00:12.401Z] [main] state=starting_api probe=/healthz attempt=3 status=200
[2026-10-08T10:00:41.900Z] [main] state=ready ports={"api":18100,"gateway":18102,"web":18101} degraded=false
```

`main.log` 的五个键固定，可直接 grep：`state=` / `error=` / `pid=` / `exit=` / `restart=`。
另有几个关键事件行：

| 行 | 含义 |
|---|---|
| `event=second_instance` | 第二次双击（单实例锁生效） |
| `event=port_contract_ok` / `event=port_contract_repair` | 端口契约三方比对结果 / 自动校正 |
| `event=first_run admin_created=true` | 首次初始化建了管理员 |
| `event=ready_open … gateway.internal_url=http://127.0.0.1:18100` | 网关回调目标（**必须是 18100，不是 8000**） |
| `event=child_crash` / `restart=` | 崩溃与退避重启（1 s / 3 s / 9 s） |
| `event=shutdown_begin` / `event=shutdown_done` | 关停编排 |

磁盘预算：6 路 × 10 MB × 5 轮转 ≈ 上限 300 MB。

## 7. 升级与卸载

- **升级**：直接跑新安装包覆盖安装。数据目录保留；每次启动都会跑幂等 `bootstrap`，schema 自动迁移到新 head。
  升级前建议先备份（§3）。
- **卸载**：设置 → 应用 → Skill Router → 卸载。`deleteAppDataOnUninstall: false`，
  因此 `%APPDATA%\SkillRouter`（数据 + 主密钥 + 日志）**会保留**；要彻底清除需手工删除该目录。
- **忘记管理员密码**：`python scripts\bootstrap.py reset-admin-password --user admin`
  （把 `SKILLROUTER_DATA_DIR` 指向 `%APPDATA%\SkillRouter\data`）。

## 8. 关停与「第二次打不开」

关闭窗口会触发逆序关停（web → gateway → worker → api）：先 `kill()`，等 8 秒 `exit`，
仍存活则 `taskkill /PID <pid> /T /F`。**必须 `/T`**：网关可能再拉起 engine 孙进程，
不带 `/T` 会留下孤儿占着 `agent-sessions`，下次启动就 `DB_BUSY`。

如果任务管理器里仍有残留：

```bat
taskkill /IM skillrouter-server.exe /T /F
taskkill /IM SkillRouter.exe /T /F
```

## 9. 安全边界（如实说明）

- 三个服务只监听 `127.0.0.1`，局域网不可达。
- 运行时 `SKILLROUTER_ENV=development`：**Cookie 不加 Secure**（桌面端是明文回环 http，
  加了 Secure 会导致登录后不收 Cookie、控制台永远 401）。补偿控制：仅回环监听、
  `SKILLROUTER_PAYLOAD_POLICY=redacted_preview`、`SKILLROUTER_EGRESS_POLICY=deny`、
  api 与网关之间用 48 字符随机 `X-Internal-Token`。
- 主密钥与文件签名密钥在用户目录（Windows ACL 继承），日志与首次运行摘要**只含指纹**。
- 未做代码签名：SmartScreen 会提示。**不声称已签名**。
