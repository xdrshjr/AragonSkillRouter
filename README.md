# Skill Router

单管理员的 **AI 能力中枢**：把上游 AI 服务的 API 凭证集中管理，把每个接口描述成带输入输出
Schema、认证引用、调用模式与错误映射的**能力（capability）**，用内置 Agent 完成接口理解与真实联调，
再把验证通过的能力与组合成的 **SKILL** 版本化发布出去 —— 外部 Agent 只要装一个连接包、拿一个平台
令牌，就能按授权范围**动态发现并调用**能力，而永远接触不到上游真实密钥。

> 设计文档（实现契约）：[`docs/plans/skill-router/spec.md`](docs/plans/skill-router/spec.md)（v2.0）
> 实现与 spec 的逐节对照：[`docs/plans/skill-router/implementation-notes.md`](docs/plans/skill-router/implementation-notes.md)
> **状态与验收的唯一权威**：[`docs/runbook/verification-checklist.md`](docs/runbook/verification-checklist.md)
> **本轮实测结果（命令与输出）**：[`docs/runbook/verification-report.md`](docs/runbook/verification-report.md)

---

## 1. 它解决什么问题

| 现状痛点 | Skill Router 的做法 |
|---|---|
| 密钥散落在 `.env`、CI 变量、每个人本地 | 凭证信封加密（AES-GCM，主密钥在库外），前端只见掩码，日志/导出包/连接包零明文 |
| 「这个 SDK 能不能用这个模型」「这张图该走哪家」没有统一答案 | 标准能力（对外契约）+ 多条 `route`（各自绑定提供商凭证、映射、限制），按优先级/健康/配额路由与受限故障切换 |
| 接入一个 API 全靠手工，过程不可复现 | 内置 Agent 的七步接入流程（解析 → 草案 → 校验 → 测试 → 样例 → 检查 → 发布），每步落库、可续跑、发布必须有真实测试证据 |
| 每个 Agent 各自手写 HTTP、各自存密钥 | 连接包 + `invoke.mjs` 助手：Agent 只需一条稳定命令；令牌不出现在 argv 里 |
| 出问题查不到原因 | 调用记录（路由决策、重试链、耗时分解）、审计（凭证/发布/撤销/升级）、`X-Request-Id` 贯穿 |

## 2. 核心能力

| 域 | 能力 | 文档 |
|---|---|---|
| 资产 | 提供商 / 凭证（掩码、影响面、有效性检测、替换与轮换）/ 能力（标准层 + `routes[]`） | [docs/api/examples.md](docs/api/examples.md) |
| 执行 | 同步、异步（队列 + 租约 + 恢复）、流式（SSE）、文件输入输出、错误映射、超时重试 | [docs/api/examples.md](docs/api/examples.md) |
| 路由 | 兼容性组、优先级、健康检查、受限故障切换、决策记录 | spec §7.7 |
| Agent | Node 边车托管 AragonAgent 引擎、12 个 `sr_*` 平台工具、事件流、停止报告、引擎热切换 | [docs/runbook/engine-upgrade.md](docs/runbook/engine-upgrade.md) |
| 分发 | SKILL 版本/启停/导入/导出、npm CLI、`invoke.mjs`、MCP（Streamable HTTP） | [docs/skills/connect.md](docs/skills/connect.md) |
| 对外接口 | `/api/v1`：health / whoami / capabilities / invoke / tasks / files / skills / mcp / callbacks | [docs/api/examples.md](docs/api/examples.md) |
| 运维 | 纯标准库备份恢复（指纹 + schema 校验）、性能冒烟、主密钥轮换、升级/回滚 runbook | [docs/runbook/backup-restore.md](docs/runbook/backup-restore.md) |
| 部署 | Docker Compose（api/worker/gateway/web/proxy，健康检查 + 依赖顺序）、nginx（TLS1.2+/HSTS/SSE 不缓冲） | [deploy/](deploy/) |

## 3. 五分钟本地启动（Windows cmd.exe）

前置：Python 3.11、Node ≥ 22（CLI 助手 `invoke.mjs` 只需 Node ≥ 18）。

```bat
:: 0) 依赖（NODE_ENV=production 时必须显式 --include=dev，否则 devDependencies 会被静默跳过）
cd apps\api
pip install -r requirements-dev.txt
cd ..\..\agents\aragon-gateway
npm install --no-audit --no-fund --include=dev
cd ..\..\apps\web
npm install --no-audit --no-fund --include=dev
cd ..\..\packages\skillrouter-cli
npm install --no-audit --no-fund --include=dev
cd ..\..
```

```bat
:: 1) 配置：复制模板并按注释生成密钥（模板里没有任何真实密钥）
copy deploy\.env.example .env
python -c "import secrets;print(secrets.token_urlsafe(32))"
::   把输出填进 .env 的 SKILLROUTER_AGENT_GATEWAY_SECRET
::   本地开发把 SKILLROUTER_ENV 改成 development
```

```bat
:: 2) 初始化（创建 DB + 管理员 + 目录，执行迁移）
python scripts\bootstrap.py
```

```bat
:: 3) 四个进程（四个终端；端口 8000 / 8500 / 3000 + worker 无端口）
cd apps\api
python -m uvicorn skillrouter_api.main:app --reload --port 8000

cd apps\api
python -m skillrouter_api.worker

cd agents\aragon-gateway
set AGENT_GATEWAY_SHARED_SECRET=<与 .env 里同一个值，≥32 字符>
npm run dev

cd apps\web
npm run dev
```

```bat
:: 4) 首次登录（默认 admin / adminjr，登录后强制改密）
start http://localhost:3000/login
```

POSIX 等价命令见 spec §14.1；用 GNU Make 或 PowerShell 时可直接 `make help` / `.\make.ps1 help`
（目标：`install / dev / test / lint / typecheck / e2e / up / down / backup / restore / seed / perf`）。

想用**一条命令**起上面四个进程（并合并成一路彩色日志）：

```bat
powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1
```

`start.ps1` 依次做：检查/安装依赖 → 首次生成 `.env`（随机网关密钥）→ 用**绝对路径**固定
`SKILLROUTER_DATA_DIR` / `SKILLROUTER_DB_PATH` → 跑 `scripts/bootstrap.py` → 后台起
gateway/api/worker/web 并逐个探测就绪 → 打开 `/login`。Ctrl+C 停全部；
`-Action status | stop | restart | logs | check` 管理生命周期，
`-Components api,web` 只起前后端，`-ApiPort/-GatewayPort/-WebPort/-Force/-NoBootstrap/-NoFollow`
按需覆盖，日志落在 `var/logs/dev/<组件>.out.log`。

容器部署：

```bat
copy deploy\.env.example deploy\.env
::   填入主密钥、网关密钥、对外域名
docker compose -f deploy\docker-compose.yml up -d
```

> 生产前请把 `deploy/docker-compose.yml` 与三个 Dockerfile 里的基础镜像**固定到 digest**
> （模板中标注为 `<fill-me>` 的位置），见文件头说明与 §14.2。

## 4. 目录地图

```
apps/api/                    FastAPI 服务（api + worker 共用一个镜像）
  skillrouter_api/           settings / errors / logging / crypto / db / models / security / …
  migrations/                Alembic（alembic.ini + versions）
  tests/                     pytest：单元 / 契约 / 集成
apps/web/                    Next.js 15 管理控制台（standalone 输出，/api 反代到 FastAPI）
agents/aragon-gateway/       Node 22 边车：AragonAgent 引擎宿主 + sr_* 工具 + 事件流
packages/skillrouter-cli/    @skillrouter/cli（npm 安装器，产出 SKILL.md + invoke.mjs + 配置）
packages/contracts/          契约类型与生成物（CI 校验无 diff）
db/                          可审阅 SQL 快照（0001_init.sql）
deploy/                      docker-compose.yml / 三个 Dockerfile / nginx.conf / .env.example / entrypoint
scripts/                     bootstrap.py（后端 owner）· backup.py · restore.py · perf_smoke.py · seed_demo.py · rotate_master_key.py
docs/                        文档索引见 docs/README.md（deploy / api / skills / runbook / plans）
.github/workflows/ci.yml     lint+typecheck / pytest / vitest / 契约 diff / compose 校验 / 可选 e2e
```

运行时数据全部落在 `SKILLROUTER_DATA_DIR`（本地默认 `var/`，容器内 `/data`）：
`skillrouter.db`、`files/`、`logs/`、`backup/`、`engine-builds/`、`aragon-home/`、`engine-runtime/`。
该目录已被 `.gitignore` 忽略，**也不应进入镜像**。

## 5. 状态矩阵

> 规则：**没有证据就不许写「已验证」**。下表「证据」列写明证据类型；
> 所有真实联调、性能、升级演练的当前状态以
> [`docs/runbook/verification-checklist.md`](docs/runbook/verification-checklist.md) 为准。
> 本表随代码推进可能过时 —— 出现冲突时以 checklist 记录的证据为准。

| 组件 | 状态 | 证据 / 说明 |
|---|---|---|
| `apps/api` 配置层（`settings.py`） | 已实现 | 与 spec §7.1 逐项对齐（含 `SKILLROUTER_EGRESS_POLICY` 别名）；`deploy/.env.example` 按它生成 |
| `apps/api` 其余（main/路由/worker/executors/upgrade） | 待验证 | 交付时仍按 §8.2 清单推进；`pytest` 未见绿之前不标已实现 |
| `apps/web` 控制台 | 已实现（构建层面） | owner 报告：`tsc` strict + eslint + `next build` 通过，21 条路由；第 3 轮补了**有头 Chrome 浏览器巡检**：13 条路由零控制台错误 + 11 条 e2e（9 通过 / 2 条需真实模型引擎，跳过），证据见 [runbook/verification-report.md](docs/runbook/verification-report.md) §10 |
| `agents/aragon-gateway` | 已实现（自身测试层面） | owner 报告：typecheck 通过、42 条 vitest 绿、inline/host 两种模式可启动并报告引擎 0.2.34 / 12 个 `sr_*` 工具 |
| `packages/skillrouter-cli` | 已实现（自身测试层面） | owner 报告：`tsc` 通过、37 条 vitest 绿、`install → verify → list → update → uninstall` 在临时目录跑通（假服务端） |
| 外部 Agent 端到端（AragonAgent / Claude Code） | **待验证** | 需要真实 Agent 环境跑「安装 → 发现 → 调用」，见 checklist §A5 |
| 真实上游联调（文本/语音/异步/跨提供商） | **待验证** | 需要真实第三方凭证；缺凭证的场景按规矩保持待验证 |
| MCP 通道（initialize/tools.list/tools.call） | **待验证** | 只有契约测试覆盖；附录 C 全部保持「待验证」 |
| 部署产物（compose / Dockerfile / nginx） | **待验证（本机无 Docker）** | YAML 可解析、五个 healthcheck 命令已做语法校验（python `compile` / `node --check`）；未真正 `docker compose up` |
| 备份/恢复脚本 | 已在 Windows + Python 3.11 实测 | 全量备份 → 删除数据 → 恢复 → 行数自检通过；主密钥指纹不匹配时按预期拒绝（退出码 3） |
| 性能基线（§15.4） | **待验证** | `scripts/perf_smoke.py` 可运行，但基线需在 4C8G + 真实数据集上采样归档 |

## 6. 安全须知

- **默认凭据**：`admin / adminjr` 只在 `system_state.admin_initialized != '1'` 时创建一次，登录后必须改密
  （≥12 字符、含字母+数字、不等于默认值）。重启或升级**不会**复活默认密码（§4.4(a)）。
- **主密钥**：`SKILLROUTER_MASTER_KEY`（或 `var/credentials-master.key`，0600）是唯一能解密凭证的东西。
  丢失 = 凭证全部报废；`scripts/backup.py` 只导出指纹 `sha256(key)[:16]`，**从不导出密钥本体**。
- **不要把 `.env`、`credentials-master.key`、`var/` 提交进仓库**（`.gitignore` 已覆盖）。
- **网关密钥** `SKILLROUTER_AGENT_GATEWAY_SECRET` ≥32 字符，网关用 `X-Internal-Token` 校验；
  容器部署时网关只在内部网络可达、不发布端口（见 `deploy/docker-compose.yml` 文件头）。
- **不建议**在 SKILL 里声明未实测的工具名：上游规则是「无法解析的工具名会让整份声明失效」，
  因此导出的 `SKILL.md` 只声明 `allowed-tools: [bash]`，平台调用一律经 `invoke.mjs`。
- 平台侧的真正边界是**令牌作用域 + 配额 + 预算**，不是 SKILL 里的声明文案。

## 7. 文档

- 文档索引：[`docs/README.md`](docs/README.md)
- 接口示例（curl / TypeScript / Python）：[`docs/api/examples.md`](docs/api/examples.md)
- 外部 Agent 接入：[`docs/skills/connect.md`](docs/skills/connect.md)
- 运维手册：初始化 [init](docs/runbook/init.md) · 备份恢复 [backup-restore](docs/runbook/backup-restore.md) ·
  引擎升级 [engine-upgrade](docs/runbook/engine-upgrade.md) · 故障排查 [troubleshooting](docs/runbook/troubleshooting.md) ·
  验收清单 [verification-checklist](docs/runbook/verification-checklist.md)
- 设计与实现记录：[spec](docs/plans/skill-router/spec.md) · [implementation-notes](docs/plans/skill-router/implementation-notes.md) ·
  [aragonagent-upstream](docs/plans/skill-router/aragonagent-upstream.md)

## 8. 上游与许可

- 引擎：[AragonAgent](https://github.com/xdrshjr/AragonAgent)（MIT），锁定 `@aragon-agent/core@0.2.34`
  （commit `0a119a64890717b0e3017edbaaf00944babf19a8`，tag `core-v0.2.34`）。
- 网关依赖的 7 个上游符号与其边界记录在 [`agents/aragon-gateway/README.md`](agents/aragon-gateway/README.md)。
- 本仓库其余代码许可见 `packages/skillrouter-cli/package.json`（`proprietary`）。
