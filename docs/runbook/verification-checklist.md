# 验收清单与待验证登记（runbook）

> 本文是**验收状态的唯一权威**。spec §15.2/§15.3/§15.4/§15.5 的每一项都在这里给出
> 复现步骤、最小凭证需求、证据要求与**当前状态**。
>
> 规则（不可协商）：
> 1. 只有登记了**证据**（命令、输出片段、`request_id`、时间、环境）的项才能写「已验证」；
> 2. 缺凭证、缺环境、没演练的项一律写「**待验证**」，并写清阻塞原因与复现步骤；
> 3. **禁止**用 MOCK 数据、假上游结果、或 `scripts/seed_demo.py` 的演示数据冒充验收
>    （§15.2 第 7 项、§15.5）；
> 4. 状态只能由本次记录里的人改，不能靠「看起来能跑」推断。

## 0. 环境记录

| 项 | 值 |
|---|---|
| 代码版本 / commit | 待填 |
| 部署方式 | docker compose / 本地四进程 |
| 机器规格（§15.4 要求 4 vCPU / 8 GB / SSD） | 待填 |
| Python / Node / SQLite 版本 | 待填 |
| 主密钥来源 | env / `credentials.master.key` |
| 引擎 pin（`system_state.engine_pin`） | 待填 |
| 迁移版本（`alembic_version` / `schema_version`） | 待填 |
| 外部 Agent 环境（AragonAgent / Claude Code 版本、OS） | 待填 |
| 执行人 / 日期（UTC） | 待填 |

## 1. 最小凭证清单

真实联调至少要准备下面这些。**没有的项保持「待验证」，不要用 mock 顶替**。

| 编号 | 能力类型 | 需要什么 | 用在哪 | 状态 |
|---|---|---|---|---|
| C1 | 文本（同步 + 流式） | 1 个 Anthropic 或 OpenAI 兼容端点的 API Key | A1 | 待准备 |
| C2 | 语音（TTS **或** ASR） | OpenAI `audio/speech` 或 `audio/transcriptions` 的 Key | A2 | 待准备 |
| C3 | 异步生成（图像或视频） | 1 家异步任务型厂商（如 Kling / 图像异步）的 Key + 提交/查询接口文档 | A3 | 待准备 |
| C4 | 跨提供商 | 在 C1–C3 中覆盖 ≥2 个提供商、≥2 种 `kind` | A4 | 待准备 |
| C5 | 外部 Agent 环境 | 本机 Node ≥18 + AragonAgent CLI（目标 0.6.22）或 Claude Code | A5 | 待准备 |
| C6 | MCP 客户端 | 任意可实现 `initialize/tools.list/tools.call` 的 MCP 客户端 | A6 | 待准备 |
| C7 | 平台 | 已发布能力 + 已签发令牌 `srk_...` + 管理员会话 | 全部 | 待准备 |

费用提示：A2/A3 的真实调用可能产生费用；请用最小参数（1 句话、1 张图、最短时长），
并在记录里写明花费。

## A. §15.2 必须完成的真实联调

### A1 文本能力：同步 + 流式各一次

| 项 | 内容 |
|---|---|
| 目标 | 至少 1 家（Anthropic 或 OpenAI 兼容）真实 `chat`，同步与流式各 1 次，记录真实响应、用量、耗时 |
| 前置 | C1 凭证；一个 `modes: ["sync","stream"]` 的已发布能力 |
| 复现 | ① 管理端「测试」跑一次同步；② `curl` 按 [../api/examples.md](../api/examples.md) §6.1 跑同步；③ 同 §6.3 跑流式（`curl -N`）；④ 打开 `/calls` 看两条记录 |
| 证据 | 两次的 `request_id`、`usage`（tokens 与来源 `provider`）、`timings.upstream_ms`、流式的首字节时间、`/calls` 详情截图 |
| 状态 | **待验证**（阻塞：C1 凭证） |

### A2 语音能力：产出真实音频文件

| 项 | 内容 |
|---|---|
| 目标 | 至少 1 家 TTS 或 ASR，产出真实音频文件，UI 可预览/下载 |
| 前置 | C2 凭证；能力声明 `file_io.output: ["audio/mpeg"]` 或输入 |
| 复现 | ① 管理端测试；② 从响应 `output_files[0].file_id` 用 `GET /api/v1/files/{id}` 下载；③ 用 `<audio>` 预览（签名 URL）；④ ASR 场景用 `POST /api/v1/files` 上传音频后以 `{"$file":...}` 调用 |
| 证据 | `file_id`、MIME、字节数、可播放/可下载证明、`request_id` |
| 状态 | **待验证**（阻塞：C2 凭证） |

### A3 异步生成：提交 → 轮询 → 成功/失败

| 项 | 内容 |
|---|---|
| 目标 | 至少 1 家异步任务型（视频或图像），覆盖提交、轮询、成功与失败各一次；`upstream_task_id` 必须落库 |
| 前置 | C3 凭证；能力含 `async.submit/query/poll` 配置 |
| 复现 | ① `POST .../invoke` + `options.mode=async` → 202 + `task_id`；② 轮询 `GET /api/v1/tasks/{id}` 到终态；③ 库内确认 `tasks.upstream_task_id` 非空；④ 用一个明显非法的输入触发一次失败，确认 `failure_class` 与错误映射 |
| 证据 | `task_id`、`upstream_task_id`、轮询次数与间隔、终态耗时、失败用例的错误码 |
| 状态 | **待验证**（阻塞：C3 凭证） |

### A4 跨提供商：同能力 2 条 route + 一次真实故障切换

| 项 | 内容 |
|---|---|
| 目标 | 覆盖 ≥2 提供商、≥2 种 `kind`；同一能力配 2 条 route 时能看到路由决策与**一次真实故障切换** |
| 前置 | C1 + 另一家同 `compatibility_group` 的凭证；两条 route（`priority` 不同） |
| 复现 | ① 两条 route 都 `enabled`，主 route 优先级更高；② 正常调用一次，`/calls` 详情里应显示选中的 route 与候选；③ 让主 route 失败（把主 route 的凭证停用，或制造 429）→ 再调用一次 → 应切到备 route；④ `/calls` 详情确认 `route_decisions.dropped` 记录了排除原因 |
| 证据 | 两次调用的 `request_id`、`route_decision`（候选/最终选择/理由）、切换前后的 provider |
| 状态 | **待验证**（阻塞：需要第二个可用提供商凭证） |
| 备注 | 「已产生首个 delta 后不切换」是设计约束，流式场景不要求切换成功 |

### A5 外部 Agent 全闭环（必须用文档里的命令）

| 项 | 内容 |
|---|---|
| 目标 | 干净目录安装 → `verify` 三项 ✓ → `node ./invoke.mjs capabilities.list` 发现 → `invoke` 真实调用 → `/calls` 有记录 |
| 前置 | C5 + C7（已发布 SKILL、令牌、`install_command`） |
| 复现 | 在一个**空目录**里执行：<br>`npx @skillrouter/cli install --base-url https://<host> --token-stdin`（或 `npx ./skillrouter-cli-0.1.0.tgz install ...`）<br>`skillrouter verify`<br>`skillrouter verify --probe-capability <key>`<br>`node ./invoke.mjs capabilities.list`<br>`node ./invoke.mjs invoke --capability <key> --input '{"prompt":"..."}'`<br>然后到 `/calls` 核对记录 |
| 证据 | 目录树（`SKILL.md` / `invoke.mjs` / `skillrouter.config.json` / `README.md` 四个文件都在）、`SKILL.md` 的 `allowed-tools` 只含 `bash`、`verify` 的逐项 ✓ 输出、调用返回的 `request_id`、`/calls` 记录 |
| 状态 | **待验证**（阻塞：C5 真实 Agent 环境） |
| 反例 | 用 `curl` 手工调通**不算**通过本项（§15.2 明确要求用文档里的命令） |

### A6 MCP 通道：initialize → tools/list → tools/call

| 项 | 内容 |
|---|---|
| 目标 | 用真实 MCP 客户端完成一次调用，结果与 HTTP 一致（`structuredContent` 相同），工具名为 `capability__<key 归档化>` |
| 前置 | C6；`POST /api/v1/mcp` 可访问 |
| 复现 | ① `initialize`（`protocolVersion` ∈ `2025-06-18`/`2025-03-26`）并保存 `Mcp-Session-Id`；② `notifications/initialized`；③ `tools/list` 记录工具名集合；④ `tools/call` 调 `capability__<key>`；⑤ 用 HTTP 调同一能力，比较 `structuredContent` |
| 证据 | 会话 id、工具名清单、两次结果对比（逐字段一致）、`request_id` |
| 状态 | **待验证**（阻塞：C6 客户端；附录 C 保持「待验证」） |
| 备注 | CLI 的 `--transport mcp` 当前**直接拒绝**（未实测不生成），这是有意为之，不要为了「跑通」而改 |

### A7 缺凭证的项：登记而不是绕过

任何因缺第三方凭证无法执行的项，都必须在本文件 §F「待验证登记表」里登记，并写清复现步骤。
**禁止**把 mock/假上游结果写进本文件的证据列。

## B. §15.3 异常与升级验收矩阵

| 场景 | 期望行为 | 对应测试 | 状态 |
|---|---|---|---|
| 无效凭证 | 测试返回 `UPSTREAM_AUTH` + 明确 hint；能力不可发布；概览出现待办 | `test_credentials_verify.py`、`test_capabilities_validation.py` | 待验证 |
| 权限不足（令牌作用域外） | `403 TOKEN_SCOPE_DENIED`，记 `call_logs(status=failed)`，不泄漏能力是否存在 | `test_platform_tokens.py` | 待验证 |
| 令牌撤销 | 撤销后下一次调用 `401 TOKEN_REVOKED`，**即时生效**（无缓存窗口） | `test_platform_tokens.py` | 待验证 |
| 重复提交（同幂等键同体） | 返回同一 `request_id`，上游只被调用 1 次 | `test_execution_sync_async_stream.py` | 待验证 |
| 幂等键跨令牌隔离 | 令牌 A 的幂等键被 B 使用**不得**命中 A 的请求 | `test_platform_tokens.py` | 待验证 |
| 异步提交不触达上游 | 假上游零请求前提下 202 + `task_id`，耗时 ≤1s | `test_execution_sync_async_stream.py` | 待验证 |
| 多 route 故障切换 | 主 route 429 且未产生输出 → 切备 route，`route_decisions.dropped` 记原因；已出首个 delta 后不切 | `test_routing.py` | 待验证 |
| SKILL 停用即时生效 | `POST /skills/{id}/disable` 后 `/api/v1/skills` 立即不返回，执行计划 `409 SKILL_DISABLED`，版本行不变 | `test_skills_import_export.py` | 待验证 |
| 上游超时 | `UPSTREAM_TIMEOUT`；非幂等请求进 `status_unknown` 而非直接重投 | 同上 | 待验证 |
| 上游限流 | 按 `errors[]` 映射 `UPSTREAM_RATE_LIMIT`，backoff 重试至 `max_attempts` | 同上 | 待验证 |
| 任务取消 | 上游支持 → `canceled`；不支持 → `409 CANCEL_UNSUPPORTED` + 「仅停止本地等待」 | `test_tasks_recovery.py` | 待验证 |
| 服务重启 | worker 重启后过期租约被复位并重领；`running` 任务按 `resume_policy=check` 先查再定 | `test_tasks_recovery.py` | 待验证 |
| 备份恢复 | 恢复后计数一致、能力可调用、凭证仍可解密 | `scripts/restore.py` 自检 + 手工抽查 | **部分**：脚本在 Windows + Python 3.11 上用合成库跑通（备份→删数据→恢复→行数自检通过；主密钥指纹不匹配时按预期拒绝，退出码 3）；**生产数据演练待验证** |
| 引擎升级失败 | 自动回滚到 previous，`engine_upgrade_runs.result=failed`，概览高优待办 | `test_engine_upgrade.py` | 待验证 |
| 升级期间调用 | 已发布且非 `requires_agent_reasoning` 的能力成功率 100% | `test_engine_upgrade.py::test_calls_during_upgrade` | 待验证 |
| 会话过期 | 管理接口 `401 SESSION_EXPIRED`，前端跳登录并说明 | `test_auth_flow.py` | 待验证 |
| 网络中断（前端） | SSE 自动重连 + `Last-Event-ID` 补事件，不重复执行，页面显示「连接已恢复」 | `e2e/agent-panel.spec.ts` | 待验证 |

跑法（§15.1 分层）：

```bash
# 单元 + 契约 + 集成（DB 用临时文件，覆盖 WAL/锁）
cd apps/api && python -m pytest -q
# 网关契约/升级回归（导出清单、事件、工具、切换、回滚）
cd agents/aragon-gateway && npx vitest run
# CLI 单测
cd packages/skillrouter-cli && npx vitest run
# e2e（可选，需先起服务）
cd apps/web && npx playwright test
```

## C. §15.4 性能基线

条件：4 vCPU / 8 GB / SSD，10 个并发客户端（+1 个 SSE 长连），200 能力 / 20 提供商 / 50 万 `call_logs`。

```bash
python scripts/perf_smoke.py \
  --base-url http://127.0.0.1:8000 \
  --cookie "sr_session=<...>" --csrf "<X-CSRF-Token>" \
  --token "$SR_TOKEN" \
  --invoke-capability <已发布的 async 能力> \
  --invoke-input '{"prompt":"ping"}' \
  --duration 30 --concurrency 10 \
  --label "4c8g-2026-10" --json var/logs/perf-smoke.json
```

| 指标 | 目标 | 测量方式 | 结果 | 状态 |
|---|---|---|---|---|
| 管理接口 P95 | ≤ 500 ms | `perf_smoke.py` 稳态 5 分钟 | 待测 | 待验证 |
| 异步任务提交（平台侧） | ≤ 1 s | 同上，排除上游耗时 | 待测 | 待验证 |
| 同步调用平台开销 | ≤ 150 ms | **不在 perf_smoke 范围**：需 §15.1 假上游 10ms 集成测试 | 待测 | 待验证 |
| SSE 首字节 | ≤ 300 ms | `curl -N` 或 `perf_smoke.py --sse-url` | 待测 | 待验证 |
| 任务领取延迟 | ≤ 500 ms | 队列时间戳差（`tasks.created_at` → worker 执行） | 待测 | 待验证 |
| 数据库写等待 | `DB_BUSY` 率 < 0.1% | 日志统计（`skillrouter.run` 里 code=DB_BUSY 的比例） | 待测 | 待验证 |

不达标时的既定处置（照做，不要临场发明）：`call_logs` 归档（>90 天转 `call_logs_archive`）、
列表查询强制索引、概览聚合改物化快照表（每 30s 刷新）。

把 `--json` 报告归档到 `var/logs/`，并把路径写进本表。

## D. §15.5 端到端验收清单（DoD）

全部**未勾选**。勾选前请在右列写入证据位置。

- [ ] `docker compose up -d` 后 5 分钟内可完成首次登录与改密 —— 证据：
- [ ] 重启服务后默认密码不复活、`admin_initialized` 保持 1 —— 证据：
- [ ] 提供商 → 凭证（掩码）→ 能力（结构化）→ 测试（真实响应）→ 发布，全链路可点通 —— 证据：
- [ ] 同一能力 2 条 route，能选主力并在主 route 限流时完成一次真实故障切换，`/calls` 能看到决策与排除原因 —— 证据：
- [ ] 概览六项指标均可跳转 —— 证据：
- [ ] 浮窗 Agent：Enter 发送 / Shift+Enter 换行 / 排队 / 流式 / 工具进度 / 停止报告 / 重试 / 切页面保留 / 拖拽上传预览 —— 证据：
- [ ] `/agent` 接入流程：七步状态可见、证据可展开、人为失败后能从该步继续并最终发布 —— 证据：
- [ ] SKILL 导入（Git、URL、文件各一次）+ 校验报告 + 发布 + 导出 zip（`secret_scan` 通过） —— 证据：
- [ ] SKILL「已发布」与「已启用」独立可操作（停用外部不可见、启用恢复、不产生新版本） —— 证据：
- [ ] 外部 Agent 安装脚本可用（tarball 路径真实存在），`verify` 逐项 ✓，安装目录确有四个产物 —— 证据：
- [ ] `/api/v1` 六接口真实可用，OpenAPI 与实现一致（契约测试）；上传后能用 `{"$file":...}` 发起带文件调用 —— 证据：
- [ ] MCP `initialize/tools/list/tools/call` 与 HTTP 结果一致 —— 证据：
- [ ] 调用记录可按能力/状态/时间筛选，详情含路由决策与耗时分解，正文默认脱敏 —— 证据：
- [ ] 审计覆盖凭证、发布、导入、撤销、路由、升级、SKILL 停用、审批决策，且不含明文密钥 —— 证据：
- [ ] 引擎升级：检测 → 验证报告 → 切换成功或自动回滚；**切换不重启容器，回滚在网关进程内完成** —— 证据：
- [ ] 性能基线达标并归档报告 —— 证据：
- [ ] 备份/恢复演练各一次，`verify_restore()` 通过；备份脚本在 `python:3.11-slim` 内可执行（无 `sqlite3`/`tar` 依赖） —— 证据：
- [ ] 幂等键跨令牌互不干扰 —— 证据：
- [ ] 文档齐全（`docs/README.md`、接口示例、SKILL 接入、升级与回滚 runbook、本清单） —— 证据：

## E. 外部 Agent 兼容矩阵（附录 C 同步）

| Agent | 已验证版本 | 接入方式 | 目录 | 状态 |
|---|---|---|---|---|
| AragonAgent CLI | 待实测（目标 `@aragon-agent/cli` 0.6.22） | 写 `SKILL.md` 到 `.aragon/skills/` 或 `ARAGON_SKILLS_PATH` | `./.aragon/skills/skill-router/` | **待验证** |
| Claude Code | 待实测 | 写 `SKILL.md` 到 `.claude/skills/` | `./.claude/skills/skill-router/` | **待验证** |
| 任意 MCP 客户端 | MCP `2025-06-18` | `POST /api/v1/mcp` + Bearer 令牌 | — | **待验证**（仅契约测试覆盖） |
| 通用 HTTP 客户端 | `curl` / 任意语言 | `/api/v1` OpenAPI | — | 已设计，**待实测** |

每一项真实跑通后：把版本号、命令输出片段、`request_id` 写进
[`../skills/connect.md`](../skills/connect.md) §5，并同步改这里的表格。

## F. 待验证登记表

| 编号 | 项目 | 阻塞原因 | 复现步骤 | 负责人 | 日期 |
|---|---|---|---|---|---|
| P1 | A1 文本同步/流式真实调用 | 缺 C1 凭证 | §A1 | 待指派 | — |
| P2 | A2 语音能力真实产出 | 缺 C2 凭证 | §A2 | 待指派 | — |
| P3 | A3 异步生成（提交/轮询/失败） | 缺 C3 凭证 | §A3 | 待指派 | — |
| P4 | A4 同能力双 route 故障切换 | 缺第二家可用提供商凭证 | §A4 | 待指派 | — |
| P5 | A5 外部 Agent 全闭环 | 缺 C5 真实 Agent 环境 | §A5 | 待指派 | — |
| P6 | A6 MCP 真实客户端 | 缺 C6 客户端 | §A6 | 待指派 | — |
| P7 | B 表全部 pytest/vitest 用例 | 后端测试套件未跑绿 | `cd apps/api && python -m pytest -q` | 后端 | — |
| P8 | C 性能基线 | 需 4C8G + 50 万 `call_logs` 数据集 | §C | 待指派 | — |
| P9 | D 全部 DoD 勾选 | 依赖 P1–P8 | — | 待指派 | — |
| P10 | 部署产物真实起栈 | 本机无 Docker（仅做了 YAML 解析 + 5 个 healthcheck 命令的语法校验） | `docker compose -f deploy/docker-compose.yml up -d` | 待指派 | — |
| P11 | 引擎升级真实演练（部署 + 回滚） | 未做真实候选版本升级 | [engine-upgrade.md](engine-upgrade.md) §2 | 待指派 | — |
| P12 | 生产数据备份/恢复演练 | 只在合成库上验证过脚本 | [backup-restore.md](backup-restore.md) §7 | 待指派 | — |
| P13 | 凭证解密抽查（恢复后） | 需要后端 AAD 约定才能自动核对 | 恢复后人工抽查 1 个凭证 + 1 个能力 | 待指派 | — |

## G. 证据归档规范

- 每条证据至少包含：**命令原文**、**关键输出**（含 `request_id`）、**UTC 时间**、**环境标识**。
- 截图放 `docs/runbook/evidence/<日期>/`（若仓库不便存图，则存到团队文档库并在本文件留链接）。
- 性能报告：`scripts/perf_smoke.py --json <path>` 的产物，路径写进 §C 表格。
- 调用证据优先用 `request_id`：它同时出现在响应体、响应头、日志、`call_logs`。
- 不要把真实密钥、完整令牌、客户数据放进证据；脱敏后保留前缀（如 `srk_k1_x…`）。
