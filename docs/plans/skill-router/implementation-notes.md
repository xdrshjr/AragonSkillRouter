# 实现记录：spec → 代码 的对照与偏差说明

> 读者：复审人。
> 用途：**一眼看出哪些是真做了、哪些是半成品、哪些没做**，以及实现与 spec 的偏差为什么合理。
> 状态口径与 [`../../runbook/verification-checklist.md`](../../runbook/verification-checklist.md) 一致：
> 已实现 / 部分实现 / 未实现 / 待验证。

## 0. 本文的快照

本文按**交付时仓库快照**编写。快照依据：

```bash
git ls-files apps/api apps/web agents packages scripts deploy docs          # 目录级
ls apps/api/skillrouter_api/*/ ; ls apps/api/skillrouter_api/*.py           # 模块级
```

交付快照时 `apps/api/skillrouter_api/` 已有：`settings.py`、`errors.py`、`logging.py`、
`crypto/{masterkey,secretbox,keyring}.py`、`db/{base,engine,session,writer}.py`、
`models/{auth,providers,capabilities,skills,execution,access,agent,engine,system}.py`、
`security/{passwords,sessions,csrf,ratelimit,tokens,fetch_guard,instruction_scanner}.py`。
**尚无**：`main.py`、`api/`（admin/v1 路由）、`services/`、`executors/`、`router/`、`agent/`、
`worker/`、`upgrade/`、`migrations/`、`tests/`、`requirements*.txt`。
→ 所以下表里这些位置一律写「未实现/进行中」，不写「已实现」。

复审时若与上句不符，**以仓库实际内容为准**，请顺手更新本文对应行（别只改结论）。

## 1. 逐节映射

| spec 章节 | 应有实现 | 现状 | 证据 / 缺口 |
|---|---|---|---|
| §4.2 进程与端口 | web:3000 / api:8000 / worker:无 / gateway:8500 / proxy:443 | 已实现（部署侧） | `deploy/docker-compose.yml`；worker 以无端口 + DB 心跳探针实现 |
| §4.3 信任域 | api/worker 独占主密钥；网关只拿短时凭证 | 部分 | `crypto/` 与 `security/` 已就位；`/internal/credentials/materialize` 由后端补齐 |
| §4.4(a) 初始化与闸门 | `bootstrap.py` 顺序 + `409 NOT_INITIALIZED` / `403 PASSWORD_CHANGE_REQUIRED` | 未实现（后端） | 部署侧已按此写 `entrypoint.api.sh`；`docs/runbook/init.md` §4 给复现步骤 |
| §7.1 配置 | `settings.py` 全变量 + 缺失即 `[FATAL]` | 已实现 | `settings.py` 与 spec §7.1 表逐项对齐；`deploy/.env.example` 由它生成 |
| §7.2 数据访问 | engine PRAGMA / writer 单写者 / Alembic 单 head | 部分 | `db/{engine,writer,base,session}.py` 已有；`migrations/` 未建 |
| §7.3 安全层 | password/sessions/csrf/ratelimit/tokens/gates | 部分 | 前五者已有代码；`gates.py` 未建（闸门顺序依赖它） |
| §7.4 加密与主密钥 + 备份 | AES-GCM secretbox / keyring 双读 / **纯 Python 备份恢复** | 部分 | `crypto/*` 已实现；**备份恢复已实现并可运行**（本文 §2） |
| §7.5 提供商与凭证 | providers/credentials + 注入 + 影响面 | 部分 | `models/providers.py` 有；`services/*`、`executors/auth.py` 未建 |
| §7.6 能力（标准层 + routes[]） | 定义、校验、`param_map`、`response_map`、modes/file_io | 部分 | `models/capabilities.py` 有；`services/capabilities.py`、`executors/*` 未建 |
| §7.7 路由 | 兼容性 / 优先级 / 健康 / 故障切换 / 决策 | 未实现 | `router/` 未建 |
| §7.8 异步队列 | 租约 / 心跳 / 恢复 / 取消 | 未实现 | `worker/` 未建 |
| §7.9 SKILL 与导出契约 | `allowed-tools: [bash]` + `invoke.mjs` + 启停独立 | 已实现（导出/安装侧） | `packages/skillrouter-cli`（owner 报告：37 条 vitest 绿）；平台侧 `services/skills.py` 未建 |
| §7.10 调用记录/用量/配额/审计 | call_logs / usage / rate_counters / audit | 部分 | `models/execution.py`、`models/access.py` 有；服务层未建 |
| §7.11 文件与产物 | 内容寻址 + 签名 URL + 保留 | 未实现 | `services/files.py` 未建 |
| §7.12 Agent 层 | Python adapter + Node 网关注解/工具/事件 | 已实现（网关侧） | `agents/aragon-gateway`（owner 报告：typecheck + 42 条 vitest、host/inline 双模启动） |
| §7.12-6 engine-host 热切换 | 子进程 + reload 四步 | 已实现（网关侧） | 见 `agents/aragon-gateway/README.md`「Engine modes and hot reload」 |
| §7.13 / §12.4 升级流水线 | detect/build/verify/switch/rollback | 未实现（平台侧） | 网关侧 reload 已有；`apps/api/upgrade/` 待建 |
| §8.2 后端文件清单 | ~140 个文件 | 部分 | 见 §0 快照；缺 `main.py`、`api/`、`services/`、`executors/`、`worker/`、`upgrade/`、`tests/` |
| §8.3 网关 | `agents/aragon-gateway` | 已实现 | owner 交付 |
| §8.4 前端 | `apps/web` | 已实现（构建层面） | owner 报告：21 路由，`next build` 通过；浏览器 e2e 未跑 |
| §8.5 npm 包 | `packages/skillrouter-cli` | 已实现 | `npm pack` 产物路径可用；真实 Agent 端到端待验证 |
| §9.1–§9.2.1 接口面 | 管理端 + `/api/v1` | 未实现（除 health 契约） | 路由文件未建；**示例文档已写**（`docs/api/examples.md`），实现时按它对齐 |
| §9.4 SSE 事件 | 13 类 Agent 事件 + task/call 事件 | 部分 | 网关侧已产出 12 类（`todo.updated` 上游无对应事件，README 明说不伪造）；API 侧 broker 未建 |
| §9.6 概览形状 | 六项指标 + attention | 未实现 | 形状见 spec；前端页面骨架已有 |
| §9.7 MCP 适配 | `initialize/tools.list/tools.call` + 工具命名 | 未实现 | 附录 C 保持「待验证」 |
| §9.8 CLI | install/verify/list/update/uninstall/doctor | 已实现（packaging 侧） | `packages/skillrouter-cli` + `docs/skills/connect.md` |
| §10 数据模型 | 全表 + 不变式 + 迁移 | 部分 | `models/*` 覆盖主要域；迁移与触发器未建 |
| §11 安全 | 五检查点 + 外部内容规则 | 部分 | `security/fetch_guard.py`、`instruction_scanner.py` 已有 |
| §12.1–12.3 上游接入 | pin 0.2.34 + 12 个 `sr_*` 工具 | 已实现（网关侧） | `agents/aragon-gateway/README.md` |
| §13 前端 | 页面清单 + 浮窗 + 令牌 | 已实现（构建层面） | owner 交付 |
| §14 部署 | compose/Dockerfile/nginx/runbook | **已实现**（本文 §2） | 见 `deploy/`、`docs/runbook/*` |
| §15 测试与验收 | 分层测试 + 验收矩阵 | 部分 | 网关/CLI 有单测；API 测试未建；验收清单已建（全部「待验证」） |
| §17.2 交付物 | 9 项 | 部分 | ①④⑤⑥⑧⑨ 已交付/部分；②③⑦ 见各包 owner |
| 附录 A 错误码 | 全量落地 `errors.py` | 部分 | `errors.py` 已存在（内容以文件为准），`docs/runbook/troubleshooting.md` 已按码表组织 |
| 附录 C 兼容矩阵 | 实测后填 | **全部保持「待验证」** | `docs/skills/connect.md` §5 与 checklist §E 同步 |

## 2. 本次（ops/docs）交付物与它们的验证方式

| 交付物 | 验证方式 | 结果 |
|---|---|---|
| `scripts/backup.py` | Windows + Python 3.11 真实执行：合成库 → full 备份 | 通过（产物 zip + manifest + sha256 三件套） |
| `scripts/restore.py` | 同上：删数据 → 恢复 → 行数自检；再测指纹不匹配 | 通过（自检全绿；指纹不匹配按预期退出 3） |
| `scripts/perf_smoke.py` | `--help` 语法与参数检查；httpx 依赖装在 API venv 内，未对真实部署采样 | 脚本可用；**基线数据待验证** |
| `scripts/seed_demo.py` | `--dry-run` 在无 `providers` 表的库上运行 | 按预期报「schema 未就绪」并退出 2；生产环境拒绝逻辑为纯代码分支 |
| `scripts/rotate_master_key.py` | `--offline` 打印步骤并按预期退出 2；HTTP 路径需真实会话 | 未对真实平台调用 |
| `deploy/docker-compose.yml` | YAML 解析 + 五个 healthcheck 命令语法校验（python `compile` / `node --check`） | 通过；**未 `docker compose up`（本机无 Docker）** |
| `deploy/nginx.conf` | 人工核对 §14.2 清单（TLS1.2+/HSTS/`proxy_buffering off`/50m/安全头/路由） | 静态核对通过；**未 `nginx -t`** |
| `deploy/Dockerfile.{api,web,gateway}` | 人工核对多阶段、uid 1000、ENV、HEALTHCHECK | **未构建（本机无 Docker）** |
| `deploy/entrypoint.api.sh` | `bash -n` 语法检查 | 通过；未在容器内运行 |
| `Makefile` / `make.ps1` | `make.ps1 help` 实际运行 | 通过（其它目标未逐个执行） |
| `.github/workflows/ci.yml` | 人工核对 job/step；本机无法跑 Actions | 未在 CI 上执行过 |
| 文档 6 篇 | 人工交叉核对 spec 章节号、错误码、命令 | 通过（引用的命令与各包 README 一致） |

## 3. 与 spec 的偏差（每条都说明为什么）

| # | 偏差 | 原因 | 影响 |
|---|---|---|---|
| D1 | compose 里网关 `AGENT_GATEWAY_BIND=0.0.0.0` 且 `AGENT_GATEWAY_ALLOW_PUBLIC_BIND=1`，而 §4.2 写 `127.0.0.1` | 容器之间无法共用 loopback | 用「不发布端口 + 只在 `backend` 内部网络 + `X-Internal-Token`」等价实现；`frontend`（proxy/web）不可达网关。已写进 compose 文件头第 4 条 |
| D2 | 网关额外加入 `egress` 网络 | 引擎要调用模型提供商（§4.3 允许持有模型 Key 瞬时值），必须出网 | 与「仅回环」的取舍：可达性靠网络隔离保证，不靠 loopback |
| D3 | worker 健康检查读 `worker_heartbeats.last_seen_at`（90s 阈值） | §14.2 只规定「用心跳判定」，没给阈值 | 崩溃/卡死后 90s 内变 unhealthy；表缺失时探针失败（api 已建表才会启动 worker，顺序由 `depends_on` 保证） |
| D4 | 三个镜像统一 uid/gid `1000:1000` | 共享 `/data` 卷必须写权限一致 | 改用户需同步改卷权限，已在 compose 与 Dockerfile 注释说明 |
| D5 | `apps/web` 的容器 Dockerfile 有两份（`apps/web/Dockerfile` 与 `deploy/Dockerfile.web`） | 分工：包内一份给包 owner 独立构建；`deploy/` 一份是 §17.2 要求的交付物 | 两份内容等价（standalone 多阶段）；漂移风险由 CI 的 web 构建作业暴露 |
| D6 | 备份指纹定义为 `sha256(master_key 字符串)[:16]`（对**原始字符串字节**取哈希，含 trim） | §7.4 只写 `sha256(master_key)[:16]`，未说对编码后的 32 字节还是 base64 字符串 | 一致性由同一脚本族（backup/restore）保证；换实现必须保持同一约定 |
| D7 | `restore.py` 用 `<data-dir>/.restore-in-progress` 标记维护态，并**只在校验通过后**删除 | spec 说「进入维护模式」但没规定机制（`MAINTENANCE` 错误码由谁读） | **契约缺口**：API 侧需要把该标记（或环境变量）映射为 `503 MAINTENANCE`；当前 runbook 的正式做法是「先停 api/worker」。见 §5 未决项 |
| D8 | `restore.py` 的「凭证可解密」抽查未内置 | 解密 AAD = `f"{record_type}:{record_id}:{key_id}"`（§7.4）中的 `record_type` 取值属后端实现细节 | 改为人工抽查并在 checklist P13 登记，避免脚本依赖未定死的约定 |
| D9 | `seed_demo.py` 直接写 SQLite 且只写 `draft`/`disabled` 行 | 没有可用的 ORM/服务层时，走 HTTP 需要全部端点就绪 | 所有行带 `[MOCK]` 前缀 + id 含 `mock`；`--clean` 只删自己的行；生产环境默认拒绝 |
| D10 | `rotate_master_key.py` 只做 HTTP 包装，不本地重加密（`--offline` 直接退出 2） | 本地重加密会绕过 `master_key_rotations`、分批事务与 AAD 重算，产出「打不开的密文」 | 明确不提供该路径 |
| D11 | `perf_smoke.py` 不测「同步调用平台开销 ≤150ms」 | 该指标只能在「假上游 10ms」下测（§15.1 集成测试职责），对真实部署采样毫无意义 | 报告里显式写明不适用范围 |
| D12 | `nginx.conf` 的 CSP 与 `apps/web/next.config.mjs` 保持一致（都含 `'unsafe-inline'`） | 两处都发 CSP 时浏览器取交集；不一致会导致「本地正常、经 proxy 样式丢失」 | §14.2 只要求「允许 self + data: 图片」，此处更宽松一点但可用；收紧需两边同时改 |
| D13 | `deploy/Dockerfile.api` 依赖 `apps/api/requirements.txt`；仓库根的 `db/migrations/0001_init.sql` **不进镜像** | 前者属后端 owner（§8.2）；后者是「与 Alembic 对齐的审阅快照」（§10.3），不是运行期依赖，且当前尚未提交 | 后端文件就位前 `docker build` 会失败——这是**预期**，不是遗漏；迁移只读 `apps/api/migrations/alembic.ini` |
| D14 | 备份恢复脚本放在仓库根 `scripts/`，镜像内复制到 `/app/scripts/` | §7.4 指定 `scripts/backup.py`；entrypoint 以 `/app/scripts/backup.py` 调用 | 路径两处都支持（脚本按 cwd 解析，`--data-dir/--db` 可显式指定） |

## 4. 明确未做（本轮不做，不是遗漏）

- 多租户 / RBAC、计费、分布式队列（spec §2.2 N1–N3）。
- 上游 OAuth / 签名类认证（N4）：只留 `auth_adapter` 扩展位。
- 移动端专门布局（N5）。
- Agent 文件系统沙箱（N6）：平台的对策是**不给**文件/网络工具，不是假装隔离。
- MCP 协议通知 `notifications/tools/list_changed`（P05 保留项）：本阶段用 `tools/list` 的 ETag 语义。
- `docker compose up` 的真实起栈验证（本机无 Docker）——已登记 checklist P10。

## 5. 未决项 / 需要后端确认的契约

| # | 未决项 | 影响 | 建议 |
|---|---|---|---|
| U1 | 维护模式如何被 API 感知（D7） | 备份/恢复期间的写入风险 | 约定：`var/.restore-in-progress` 存在 → 管理写接口 `503 MAINTENANCE` |
| U2 | `scripts/bootstrap.py` 的 `reset-admin-password` 子命令签名 | runbook `init.md` §6 的命令 | 后端按 runbook 实现（`--user` + 交互输入） |
| U3 | 内部工具转发路径（`POST /internal/tools/{tool_name}`） | 网关已按此实现（其 README「Assumptions」） | API 侧按此路径落地，或双方同时改 |
| U4 | `credential` 解密的 AAD `record_type` 取值（D8） | 恢复后自动抽查凭证 | 后端在 `crypto/secretbox.py` 附近注释里写明 |
| U5 | `worker_heartbeats` 的写入间隔 | worker 健康阈值 90s 的合理性 | 建议 ≤30s 一次心跳 |
| U6 | `SKILLROUTER_AGENT_MODEL_PROVIDER` / `SKILLROUTER_AGENT_MODEL_BASE_URL` 只被 compose 使用 | 网关变量命名空间是 `AGENT_*` | 若后端要读它们，需在 `settings.py` 增加字段（当前不存在，故 `.env.example` 标注为 `[compose]`） |

## 6. 待验证清单（不在本文重复，指过去）

见 [`../../runbook/verification-checklist.md`](../../runbook/verification-checklist.md)：

- §A1–§A7 真实联调（文本/语音/异步/跨提供商/外部 Agent/MCP/缺凭证登记）
- §B 异常与升级矩阵
- §C 性能基线（§15.4）
- §D DoD 逐项
- §E 附录 C 兼容矩阵
- §F 待验证登记表（P1–P13）

## 7. 复审建议动作（10 分钟）

```bash
# 1) 骨架是否齐全
ls apps/api/skillrouter_api; ls deploy scripts docs/runbook

# 2) 脚本能否运行（不碰生产数据）
python scripts/backup.py --help && python scripts/restore.py --help
python scripts/perf_smoke.py --help && python scripts/seed_demo.py --help
python scripts/rotate_master_key.py --offline

# 3) compose 语法（有 Docker 的机器）
mkdir -p deploy/certs && cp deploy/.env.example deploy/.env
docker compose -f deploy/docker-compose.yml config -q

# 4) 文档与实现是否对得上（抽查 3 条命令 + 3 个错误码）
grep -n "allowed-tools" docs/skills/connect.md
grep -n "UPSTREAM_AUTH\|TOKEN_SCOPE_DENIED\|DB_BUSY" docs/runbook/troubleshooting.md
```

结论：**不要**相信本文的状态表能替代实测。本文的价值是「把不确定的地方点名」，
而不是「证明它们都对」。

## 实施完成后的状态更新（实现节点追加）

> 本文件写作时后端尚未落地（见 §0 的「文档写作时刻快照」）。实现节点完成后的事实如下，
> 详细命令与结果见 [`../../runbook/verification-report.md`](../../runbook/verification-report.md)。

| 范围 | 写作时状态 | 当前状态 |
|---|---|---|
| `apps/api/skillrouter_api/` 全量模块 | 仅有 settings/errors 等骨架 | **已实现**：db(5) + models(9) + schemas(8) + security(8) + crypto(3) + services(17) + executors(7) + router(3) + agent(5) + sse(3) + api/admin(11) + api/v1(8) + api/internal(1) + worker(10) + upgrade(7) |
| 数据库表 | 计划 40+ | **49 张表**（含 `capability_routes` / `rate_counters` / `agent_flows` / `agent_flow_steps` / `agent_approvals` / `engine_*`），Alembic `0001_init` + `0002_engine_upgrade` 已实测执行，附 `db/migrations/0001_init.sql` 快照 |
| 测试 | 计划 20 个文件 | **17 个测试文件 / 190 项通过**（另有 2 项 slow 性能用例） |
| 端到端闭环 | 计划 | **已真实跑通**（`scripts/e2e_live.py` 58/58：真实 uvicorn + 真实 HTTP 上游 + 真实 CLI 安装/发现/调用 + MCP + 备份 + 主密钥轮换） |
| 前端 `apps/web` | 计划 | **已实现**：21 个路由段 + ui/common/capabilities/agent 组件 + `lib/*`；`tsc --noEmit`、`next lint`、`next build` 全绿；与真实后端联通 9/9 |
| 网关 `agents/aragon-gateway` | 计划 | **已实现**：12 个 `sr_*` 工具、13 类事件映射、`engine-host` 子进程热切换；42 项测试通过；工具业务逻辑统一在 Python `/internal/tools/{name}`（见 spec I8） |
| npm CLI `packages/skillrouter-cli` | 计划 | **已实现**：install/verify/list/update/uninstall/doctor + `invoke.mjs` 助手；37 项测试通过；真实安装与真实调用已验证 |
| 真实第三方凭证 / 真实 Agent 推理 / Docker / 生产性能 | 待验证 | **仍待验证**（原因与复现步骤见 `verification-report.md` §6） |

