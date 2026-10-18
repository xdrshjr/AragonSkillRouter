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

---

## 迭代 2 偏差与已关闭缺陷

> 本节只记录**做了什么、偏离了什么、还剩什么**。状态口径以
> [`../../runbook/verification-checklist.md`](../../runbook/verification-checklist.md) 为唯一权威；
> 具体命令与数字见 [`../../runbook/verification-report.md`](../../runbook/verification-report.md) 的「迭代 2」章节。
> `已关闭` = 有代码 + 有断言或可复现命令；`部分关闭` = 按最小实现收口，取舍写在「偏差点」。

### 1. P0 缺陷（F-01…F-12）

| 编号 | 关闭情况 | 落点（代码 / 断言） | 偏差点 |
|---|---|---|---|
| F-01 | 已关闭 | `apps/web/lib/types.ts` 对齐后端 `{status, used_route}`；`packages/contracts` 导出类型；`cd apps/web && npx tsc --noEmit` | 无（类型即契约） |
| F-02 | 已关闭 | `services/capabilities.py` 产 `last_test_ok/last_test_at/success_rate_24h`；聚合键改为 `call_logs.status='success'`（`LOGGED_STATUS`）；`test_api_contract.py` | 前端新增「上次测试」列（R2-15 指出原描述里根本没这列，补列后症状才可见） |
| F-03 | 已关闭 | `services/credentials.py` 投影白名单 + `meta_sealed`（AAD `credential_meta`）+ `meta_redacted_keys`；`scripts/migrate_seal_meta.py`；`test_credentials_projection.py` / `test_crypto_credentials.py` | 敏感键**不进** `meta`（不是折叠成 `{redacted:true}`，R2-29）；存量库要跑 `migrate_seal_meta.py --apply` |
| F-04 | 已关闭 | `deps.require_token` 只认 Bearer；`logging.redact` 增查询串脱敏；`SKILLROUTER_ALLOW_QUERY_TOKEN` 默认 0 逃生阀；`test_token_transport.py` | 逃生阀**保留**，移除判据写进 `troubleshooting.md` §2.1 与 `deploy/.env.example`（R2-35） |
| F-05 | 已关闭 | `record_cost` 覆盖 sync/stream/async 三条路径；预算检查覆盖 token/agent/capability/provider/global 多层；成本只落 `quota_counters.cost_usd`；`test_budget_accounting.py` | **行为变更需告知**：计费口径扩到流式/异步后，小预算令牌第一次可能 `402 BUDGET_EXCEEDED`；设置页与 `examples.md` 已写明（§A.8 R2） |
| F-06 | 已关闭 | `executors/errors.py` 对 `transport_error="timeout"` 不再 `retryable`；超时进 `status_unknown` + `needs_reconcile`；`test_execution_sync_async_stream.py` | 读超时改为「对账优先」，不自动重投（有重复计费风险） |
| F-07 | 已关闭 | `tasks.cancel` 判定顺序改「终态 → 有 `upstream_task_id` → remote/local_only」；提交前取消 `pending_submit` + HTTP **202**；`ALLOWED["queued"]` 增 `canceling`；`test_task_cancel_race.py` | 无 |
| F-08 | 已关闭 | `worker/leases.py` 的 `release/extend` 校验 owner；续租失败取消 handler；`test_lease_ownership.py` | 无 |
| F-09 | 已关闭 | `upgrade/verify.py` 重写为 **artifact-first 七项三态**；`scripts/verify_artifact.mjs`；网关 `POST /internal/admin/verify-artifact`；`test_engine_upgrade.py` | 检查项从「与 86 个导出对比」改为冻结符号集（R2-34：写死数字必然漂移） |
| F-10 | 已关闭 | 网关 `config.ts` 读 `AGENT_ENGINE_RUNTIME_DIR/current.json` 覆盖 pin 并回显 `pin_source`；`engine-host.ts` 按 `AGENT_ENGINE_HOME_ROOT/<version>` 解析；`settings.agent_engine_home_root`；`test/pointer.test.ts` | 指针文件是唯一实现（不用 symlink/junction）；`SKILLROUTER_ENGINE_HOME_ROOT` 与 `AGENT_ENGINE_HOME_ROOT` **必须同值** |
| F-11 | 已关闭 | `services/bootstrap.py::migrate_or_adopt()` 单轨入口（空库 upgrade / 存量库指纹 stamp / 已接入 upgrade）；`scripts/adopt_alembic.py`（指纹不一致退出 3）；`0003_iteration2` 条件加列；`test_migrations_roundtrip.py` | entrypoint 仍**只**调 `bootstrap.py`，不直接调 alembic；指纹不一致时**拒绝启动**（`MIGRATION_NOT_ADOPTED`，不强行 stamp） |
| F-12 | 已关闭 | `idempotency_keys.state(in_flight\|done)` 先占位；命中 in-flight → `409 IDEMPOTENCY_IN_PROGRESS`；`expires_at < now` 视为可重新占位；`test_execution_sync_async_stream.py` | 占位走独立短事务（R2-22），不塞进调用方事务 |

### 2. P1 缺陷（F-13…F-26）

| 编号 | 关闭情况 | 落点 | 偏差点 |
|---|---|---|---|
| F-13 | 已关闭 | `services/settings_runtime.py`（DB 覆盖 + 60s TTL）；`execution` / `retention` / 抓取守卫改读运行时值；`test_settings_runtime.py` | 生效窗口 60s（UI 提示「worker 60s 内生效」），不是即时 |
| F-14 | 已关闭 | 新增 `POST /api/admin/settings/backup/now`（同步返回产物名 + sha256）；旧 `GET` 删除（GET 回 405）；`test_api_contract.py` / `test_auth_flow.py` | 排队接口 `POST /settings/backup`（202）**保留**，两条路并存：排队的给日常、`now` 的给运维 |
| F-15 | 已关闭 | `api/v1/files.py::download` 改成签名优先、令牌次之；上传响应与 `output_files[]` 增 `signed_url`/`expires_at`；`test_admin_files.py::test_single_signature_algorithm` | 无效/过期签名从 `404` 改 **401**（hint 区分原因），会改变既有客户端的错误分支 |
| F-16 | 已关闭 | 后端 `calls` 接受 `date_from/date_to` 并兼容 `from/to`；前端 `FILTER_KEYS` 改新名 | 旧别名保留一个版本（记 `X-Deprecated-Param`） |
| F-17 | 已关闭 | 两端统一 `sort`（白名单 `created_at/updated_at/status`，不支持的列不假排序） | 前端对后端不支持的列**去掉 `sortable`**：宁可不可排序，也不假排序 |
| F-18 | 已关闭 | `GET /api/admin/tasks` 增 `q/status/capability_key/page/page_size/sort`；前端换 `DataTable` + `useTableState` | `q` 在 `request_id/task_id/capability_key/upstream_task_id` 上做**内存子串**匹配（列表上限 2000 行） |
| F-19 | 已关闭 | 新增 `apps/web/app/(app)/credentials/page.tsx`；`AppShell` 加**顶层**「凭证」项（R2-16：导航是扁平列表） | 只加一项，不做导航分组重构 |
| F-20 | 已关闭 | `ci.yml` 的 `e2e`：`SR_BASE_URL` + `E2E_BASE_URL` 双写、先 `npm run build` 再 `npm run start`、`if: vars.E2E_ENABLED == '1'`；新增反向作业 `e2e-skipped` 打 `::warning::e2e skipped ≠ verified；P10 仍待验证` | 开关从 `workflow_dispatch` 输入改为**仓库变量**（`run_e2e` 保留但标废弃）；跳过告警用独立作业实现（`if` 为假时原作业整块跳过、连一行日志都不留） |
| F-21 | 已关闭 | `ci.yml` 新增 `migrations` 作业（§A.3.12 骨架：`working-directory: apps/api`、env 三件、`pip install -r requirements-dev.txt`、`upgrade → downgrade base → upgrade → upgrade`、快照 pytest 子集）；`scripts` 作业增 YAML 自检 + `bash -n` + `run)` 断言 | 断言从「字符串存在」加强为「`run)` 必须排在 `api)` 之前」 |
| F-22 | 已关闭 | `deploy/entrypoint.api.sh` 新增 `run` 角色（`SKILLROUTER_ROLE=run` 或首参 `run`）→ `exec "$@"`，**先判 `run` 再判 `api\|worker`**；用法文案改 `api\|worker\|run`；`init.md`/`backup-restore.md`/`troubleshooting.md` 全改 `--entrypoint python` 或 `run` 角色 | `run` 角色**不**做备份/迁移（只透传命令），避免「查一次库顺手迁移一遍」 |
| F-23 | 已关闭 | `upgrade/switch.py` 切换前 `backup_job.quick_backup(label="pre-upgrade")`；失败 → `stage="backup"`、`backup_file_id=null`、**不装 home、不写指针**；成功回填 `engine_upgrade_runs.backup_file_id` | 备份**只含 DB + manifest**（不是旧文档说的「DB + aragon-home + 网关 lock」）；要连引擎目录留档得手工 `backup.py --include …`。`engine-upgrade.md` 已按实现改写 |
| F-24 | 已关闭 | 新增 `upgrade/compat.py`（按版本比较 `aragon-home/sessions` 的 `schemaVersion`）；`needs_migration`/`migration_hint` 真实产出 | 结构变化 → `blocked` + `needs_migration`（阻断自动部署），不算 `failed` |
| F-25 | 已关闭 | `services/maintenance.py::is_maintenance()`（标记存在 → 写方法与 invoke `503 MAINTENANCE`；mtime > 24h 记 `stale_maintenance_marker` 但**仍**返回 True）；`/healthz.maintenance`；`test_maintenance_mode.py` | 陈旧标记不自动清除（宁可 503 也不让半恢复的库被写），需人工删 |
| F-26 | **部分关闭（最小实现收口）** | `engine_upgrade_runs.verify_status(passed\|blocked\|failed)` + `blocked_reasons`；`result` 与 `verify_status` 语义分离；`engine_candidates` 删 `skipped`；`POST /api/admin/engine/upgrade` 对 `blocked` 要求 `acknowledge_blocked=true`；引擎页三态徽标 + 阻断原因 + 二次确认 | **取舍**：以「状态语义 + UI 文案」最小实现收口，不新增 `candidates.state` 迁移，也不做候选生命周期完整状态机；`acknowledge_blocked` 在 API 层强校验，前端二次确认是同一请求的 UI 呈现 |

### 3. 设计级缺陷（R2-01…R2-10）

| 编号 | 关闭情况 | 落点 | 偏差点 / 取舍 |
|---|---|---|---|
| R2-01 | 已关闭 | `deploy/Dockerfile.gateway` 增 `git`（保留 `node:22-slim` 与 digest 占位注释）；`upgrade/toolchain.py` 走网关内部端点；`SKILLROUTER_ENGINE_TOOLCHAIN=gateway\|local`；`test_upgrade_toolchain.py` | `local` **仅** `SKILLROUTER_ENV=development` 生效，生产填 `local` 被忽略并记 `toolchain_local_ignored`；api/worker 镜像**不加** node/git（保持最小攻击面） |
| R2-02 | 已关闭 | `0003_iteration2` 每列条件加（`PRAGMA table_info` 探测），`downgrade` 与 `upgrade` 对称；CI `migrations` 跑 `downgrade base → upgrade head → upgrade head` | `downgrade` 在 `meta_sealed` 非空时**中止**（`[ABORT]`），必须先 `migrate_seal_meta.py --unseal --out <file>`；见 `backup-restore.md` §5.1 |
| R2-03 | 已关闭 | `execution.LOGGED_STATUS` 统一 `success/failed/status_unknown`；成功率按该值聚合 | 无 |
| R2-04 | 已关闭 | `test_runs[].ok` 前端判定改布尔（`=== true`），不再与整数 `1` 比较 | 无 |
| R2-05 | 已关闭 | `ALLOWED["queued"]` 增 `canceling`（提交前取消是唯一合法路径） | 与 §10.2 不变式 4 的冲突以 §A.3.6 表为准（§A.10 已同步） |
| R2-06 | 已关闭 | 成本只落 `quota_counters.cost_usd`；`rate_counters` 只记 `calls/concurrent`；`usage_records.source ∈ {provider, estimated, unknown, probe}` | 文档同步：`examples.md` §3 明确 `limits`（配置）vs `usage`（实际用量） |
| R2-07 | 已关闭 | 计费按 `DEFAULT_PRICES` 兜底（不假设「有价目才计费」）；设置页与文档写明口径 | **行为变更需告知**：存量小预算令牌可能第一次就 `402` |
| R2-08 | 已关闭 | 复用既有 `services/files.py` 的 `SIGN_TTL_S=600 / _sign / signed_url / verify_signature`，不另造算法；`test_admin_files.py::test_single_signature_algorithm` | 上传响应与 `output_files[]` 复用同一函数、同一 TTL 常量 |
| R2-09 | 已关闭 | 网关新增 `AGENT_ENGINE_RUNTIME_DIR`（compose 与 `.env.example` 三侧同值）；`/healthz` 增 `pin_source`/`resolved_path` | 变量从「两侧都不存在」变成「三侧显式同值」，`.env.example` 逐条写了「同值」的坑 |
| R2-10 | 已关闭 | 网关实现 `POST /internal/admin/build`、`POST /internal/admin/verify-artifact`（`X-Internal-Token` + `/data` 前缀校验）；`/internal/admin/reload` 增字段；`types.ts` 共享类型 | 文档原先写的 `/internal/engine/*` 是错的，已统一为 `/internal/admin/*` |

### 4. 未做 + 原因

| 项 | 未做的原因 |
|---|---|
| `quota_released` 的独立列 | **幂等标记存在 `tasks.result`（JSON）里**，不新增列：`0003` 已经动了 6 列，再加一列要连带改投影、备份 manifest 兼容与前端类型，收益与风险不成比例；「一次性执行副产物」放 `tasks.result` 语义正确（同 `result.cancel_requeues`） |
| `capability_test_runs` 加 `output`/`dropped` 列（R2-30） | 本轮不扩表：测试历史面板只能看实时响应，刷新后历史详情缺失（**已记录在案**，明确不改） |
| 候选生命周期完整状态机（F-26 收口） | 见 F-26 偏差点：用 `verify_status` + `blocked_reasons` 的**状态语义 + UI 文案**最小实现 |
| 导航分组重构（R2-16） | `AppShell` 是扁平列表，只加顶层「凭证」一项；分组重构属于范围外的 UI 重构 |
| 前端 8 项手测清单（§A.7.3） | 需人工在浏览器逐条点；本机只跑了 `tsc --noEmit` / `next lint` / `next build` / vitest。已登记 `verification-checklist.md` §F1 的 R2-3 |
| CI `migrations` / `e2e` 作业的真实执行 | 本机只做了 YAML 解析与静态断言（作业存在、`run)` 位置）。已登记 §F1 的 R2-1 / R2-2 |
| 真实引擎升级演练（部署 + 回滚各一次） | 无真实候选版本与可用模型；已登记 P11 |
| `docker compose up` 真起栈、网关镜像构建 | 本机无 Docker；已登记 P10 与 §F1 的 R2-5 / R2-6 |
| MCP 真实客户端、外部 Agent 全闭环、性能基线 | 缺客户端 / 环境 / 目标硬件；保持 P5 / P6 / P8 |
| 已知测试隔离问题（非本轮交付面） | **已修复（本轮收尾）**：`test_async_releases_concurrency_once` 原先断言「五层并发绝对归零」，当它排在会留下在途任务的用例之后时会红；改为**增量断言**（提交 +1、终态回基线、重复释放不变），`pytest tests/test_tasks_recovery.py tests/test_budget_accounting.py` 与反向顺序都绿。另修掉 `test_auth_flow.py::test_session_expiry_and_revocation` 的秒级 `created_at` 排序 flaky（加 `rowid DESC` 二级排序） |

### 5. 表外支撑文件（§A.4 未逐行列，但属于已完成条目的必要支撑）

复审按表核对 diff 时，下列文件会出现在 diff 里但不是「范围蔓延」：

| 文件 | 为什么必须改（对应条目） |
|---|---|
| `apps/api/skillrouter_api/schemas/engine.py` | `POST /engine/upgrade` 新增 `acknowledge_blocked` 字段（F-26 / §A.5.2） |
| `apps/api/skillrouter_api/services/backup_job.py` | 新增 `quick_backup(label=...)`，切换前自动备份（F-23 / §A.3.7-3） |
| `apps/api/skillrouter_api/agent/client.py` | `reload_engine()` 同时发 `version` 与 `target_version`（兼容尚未升级的网关，R2-10） |
| `apps/api/skillrouter_api/upgrade/{state,jobs,rollback,detect}.py`、`upgrade/__init__.py` | 三态落库（`verify_status`/`blocked_reasons`）、`policy=auto` 只在 `passed` 生效、构建 `blocked` 不产生候选（F-09 / F-26） |
| `apps/api/tests/fixtures/fake_gateway.py` | 假网关补 `build`/`verify-artifact` 两个端点与 `pin_source`（T-41 / T-47 的夹具） |
| `apps/api/migrations/env.py` | 允许调用方覆盖 `sqlalchemy.url`（`alembic.ini` 是占位符），否则迁移单轨跑不起来（F-11，见 spec §D2 I2-5） |
| `scripts/e2e_live.py` | 验收脚本：`schema_version` 1→2、备份改 POST、新增 12 项断言（T-37） |
| `scripts/check_file_plan.py` | 同时校验 §8 与 §A.4 四张表（T-40，310/310） |
| `db/migrations/0001_init.sql`、`docs/api/openapi.json` | 重新导出的构建产物（6 个新列 / 117 paths），不是手改 |


## 最终评审的补修（提交前复核）

复审对照 §A 逐条核对 diff 后，直接修掉 14 类缺陷（清单与证据见
[`docs/runbook/verification-report.md` §9](../../runbook/verification-report.md)），并对其中 3 条收紧了断言/契约：

| 取舍 | 说明 |
|---|---|
| 异步调用记录的终态口径 | `call_logs.status` 只有 `success/failed/status_unknown` 三个合法值；任务 `canceled` 按 `failed` 记并写 `error_code=CANCELED`，理由是「本次调用没有成功」。没有新增 `canceled` 取值，避免全库数据迁移 |
| `npm-only` 构建 | npm 包内没有 `packages/core` 工作区，隔离构建在 tarball 目录里必然失败；按 §A.3.7-1 判 `blocked`（不是候选的错）并指引用 tagged 通道，与 §A.3.7-0「不能验证就说不能验证」一致 |
| `ssrf_deny` vs `ssrf_deny_extra` | 保留 DB 侧的 `ssrf_deny`（已写进 `EDITABLE`、`db/seed.py`、前端），运行时同时接受两个键并减掉部署基线；不重命名既有键，避免存量行失效 |

评审过程中复跑了全部可机验项（pytest 247、alembic 四步、网关 tsc+vitest 48、web tsc/lint/build 22 路由、
e2e 70/70、check_web 9/9、check_gateway 12/12、check_file_plan 310/310），结果见 verification-report §9.2。
`ruff` 的存量告警仍未处理（§8.7 已如实登记），本轮只保证不新增。
