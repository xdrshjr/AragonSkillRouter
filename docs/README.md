# Skill Router 文档索引

> 单一入口。找「怎么用」看 deploy/api/skills；找「出问题怎么办」看 runbook；
> 找「为什么这么设计」看 plans。
> **验收状态以 [`runbook/verification-checklist.md`](runbook/verification-checklist.md) 为唯一权威。**

## 按目的查阅

| 我想…… | 看这里 | 说明 |
|---|---|---|
| 5 分钟把项目跑起来 | [../README.md](../README.md) §3 | 依赖、`.env`、`bootstrap.py`、四进程、首次登录 |
| 部署到服务器 | [../deploy/](../deploy/) + [runbook/init.md](runbook/init.md) | `docker-compose.yml`、三个 Dockerfile、`nginx.conf`、`.env.example`、入口脚本 |
| 调用平台接口 | [api/examples.md](api/examples.md) | `/api/v1` 全部端点的 curl / TypeScript / Python 示例（含 SSE 与 MCP） |
| 把能力接进外部 Agent | [skills/connect.md](skills/connect.md) | 安装 / 验证 / 更新 / 卸载的**确切命令**、已验证 Agent 矩阵、排错 |
| 备份、恢复、演练 | [runbook/backup-restore.md](runbook/backup-restore.md) | 纯标准库备份、指纹与 schema 校验、`verify_restore()` 自检 |
| 升级 AragonAgent 引擎 | [runbook/engine-upgrade.md](runbook/engine-upgrade.md) | 检测 → 隔离构建 → 验证 → 热切换 → 回滚（不重启容器） |
| 排查报错 | [runbook/troubleshooting.md](runbook/troubleshooting.md) | 附录 A 每个错误码 → 原因 / 处置 / 定位命令 |
| 核对验收 | [runbook/verification-checklist.md](runbook/verification-checklist.md) | §15.2/§15.3/§15.4/§15.5 的复现步骤、最小凭证、待验证登记 |
| 了解实现与 spec 的差异 | [plans/skill-router/implementation-notes.md](plans/skill-router/implementation-notes.md) | 逐节映射 + 已实现/部分/未实现/待验证 + 偏差解释 |
| 装桌面端（Electron，免 Python / 免 Node） | [../apps/desktop/README.md](../apps/desktop/README.md) · [plans/desktop-electron/spec.md](plans/desktop-electron/spec.md) · [runbook/desktop-app.md](runbook/desktop-app.md) | 出包（`npm run dist:win`）、产物路径、双击验收、数据目录与备份、桌面端错误码（`E-D01`~`E-D07` / `W-D01` / `W-D06`） |
| 读设计契约 | [plans/skill-router/spec.md](plans/skill-router/spec.md) | v2.0，3300+ 行，实现以它为准 |
| 查上游 AragonAgent 事实 | [plans/skill-router/aragonagent-upstream.md](plans/skill-router/aragonagent-upstream.md) | 实测记录与能力边界 |

## 文档清单

### 部署（`deploy/`）

| 文件 | 内容 |
|---|---|
| [`docker-compose.yml`](../deploy/docker-compose.yml) | 五个服务（api/worker/gateway/web/proxy）、卷、健康检查、`depends_on: service_healthy`、网络划分、镜像 digest 占位说明 |
| [`.env.example`](../deploy/.env.example) | 全部 `SKILLROUTER_*` 变量 + `[compose]`/`[gateway]` 分类，含密钥**生成命令**，无任何真实值 |
| [`nginx.conf`](../deploy/nginx.conf) | TLS1.2+、HSTS、安全响应头、`proxy_buffering off`（SSE）、`client_max_body_size 50m`、`/api` → `api:8000`、`/` → `web:3000` |
| [`Dockerfile.api`](../deploy/Dockerfile.api) | api/worker 共用镜像，非 root（uid 1000），无 sqlite3 CLI 依赖 |
| [`Dockerfile.web`](../deploy/Dockerfile.web) | Next.js standalone 多阶段构建 |
| [`Dockerfile.gateway`](../deploy/Dockerfile.gateway) | Node 22 多阶段（构建含 devDeps，运行 omit=dev） |
| [`entrypoint.api.sh`](../deploy/entrypoint.api.sh) | 启动前 quick 备份 → 迁移/初始化 → uvicorn；worker 分支等 DB 就绪 |

### 接口（`docs/api/`）

| 文件 | 内容 |
|---|---|
| [`examples.md`](api/examples.md) | health、whoami、capabilities（list/get）、invoke（sync/async/stream）、task status/cancel、file upload、skills（list/read/bundle）、MCP（initialize/tools.list/tools.call） |

`docs/api/openapi.json` 由 CI 的 `api` 作业从 FastAPI 应用导出并做漂移检查（见
`.github/workflows/ci.yml`），**不手写**；实现变更后重新生成并与实现一起提交。

### SKILL 接入（`docs/skills/`）

| 文件 | 内容 |
|---|---|
| [`connect.md`](skills/connect.md) | `npx @skillrouter/cli install/verify/update/uninstall/doctor` 的确切命令、退出码、`invoke.mjs` 子命令、目录探测顺序、故障排查、已验证 Agent 矩阵（未跑通一律「待验证」） |

### 运维手册（`docs/runbook/`）

| 文件 | 场景 |
|---|---|
| [`init.md`](runbook/init.md) | 首次部署、密钥生成、首次登录改密、忘记密码、主密钥轮换、令牌管理 |
| [`backup-restore.md`](runbook/backup-restore.md) | 每日备份、保留策略、恢复流程、指纹/schema 拒绝逻辑、演练记录模板 |
| [`engine-upgrade.md`](runbook/engine-upgrade.md) | 引擎升级流水线与回滚（`engine-host` 热切换，无容器重启、无 Docker socket） |
| [`troubleshooting.md`](runbook/troubleshooting.md) | 错误码 → 处置；SSE 卡顿、401/403 循环、`NOT_INITIALIZED`、`DB_BUSY` 等常见现象 |
| [`verification-checklist.md`](runbook/verification-checklist.md) | §15.2 真实联调逐项复现步骤、§15.3 异常矩阵、§15.4 性能记录、§15.5 DoD、最小凭证清单、待验证登记表 |

### 设计与记录（`docs/plans/skill-router/`）

| 文件 | 说明 |
|---|---|
| [`spec.md`](plans/skill-router/spec.md) | v2.0 实现契约（表名、字段、路由、事件名、环境变量以它为准） |
| [`aragonagent-upstream.md`](plans/skill-router/aragonagent-upstream.md) | 上游实测事实与能力边界 |
| [`implementation-notes.md`](plans/skill-router/implementation-notes.md) | 实现映射、偏差解释、已实现/部分/未实现/待验证清单 |

### 桌面端（`docs/plans/desktop-electron/`）

| 文件 | 说明 |
|---|---|
| [`spec.md`](plans/desktop-electron/spec.md) | v2.0 桌面端（Electron）实现契约：端口契约、sidecar 双检、状态机、错误码、验收清单 |
| [`../apps/desktop/README.md`](../apps/desktop/README.md) | 构建 / 出包 / 双击验收 / 排错的唯一入口（命令 + 期望输出） |
| [`runbook/desktop-app.md`](runbook/desktop-app.md) | 数据目录、备份恢复、端口冲突、日志、升级卸载、错误码处置表 |

## 阅读约定

- 本文档集的「状态」只有四种：**已实现**（有代码）/ **部分实现**（点名缺什么）/
  **未实现**（本轮不做）/ **待验证**（代码在，但缺真实环境证据）。
- 任何标「待验证」的条目**不得**因为「看起来能跑」而改成已验证；只有
  [`verification-checklist.md`](runbook/verification-checklist.md) 里登记了证据（命令、输出、`request_id`、
  时间、环境）才能改状态。
- 代码标识符、环境变量、错误码保持英文原文；说明用中文。

## 本轮实测

| 本轮实测结果 | [runbook/verification-report.md](runbook/verification-report.md) | 197 项后端测试、58 项端到端联调、9 项前端联通性的实际命令与结果（含复审轮修掉的 22 个缺陷与仍待验证项） |
