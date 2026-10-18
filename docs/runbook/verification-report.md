# 实施与验证报告（Verification Report）

> 本文件记录**本轮实现节点实际执行过的命令与结果**，是 `runbook/verification-checklist.md`
> 中「本轮证据」的出处。约定不变：**没有证据的一律不标「已验证」**。

| 项 | 值 |
|---|---|
| 记录时间 | 2026-10-18（第 1 轮迭代，实现节点） |
| 环境 | Windows 10.0.26200；Python 3.11.7；Node v22.18.0；npm 10.9.3；SQLite（本地文件） |
| 规格版本 | `docs/plans/skill-router/spec.md` v2.0（含 §「实施过程发现的方案缺陷」11 项） |
| 上游引擎 | `@aragon-agent/core` 0.2.34（精确 pin），适配层版本 `1` |
| 结论 | 核心闭环**真实跑通**（本地真实 HTTP 上游 + 真实 uvicorn + 真实 CLI 安装/调用）；真实第三方凭证与真实 Agent 推理仍为**待验证** |

---

## 1. 自动化测试

| 套件 | 命令 | 结果 |
|---|---|---|
| 后端全量 | `cd apps/api && python -m pytest tests/ -q` | **197 项全部通过**（含 2 项 `slow` 性能用例；复审前为 190，见 §7.1） |
| 后端分项 | 见 `apps/api/tests/` 18 个文件 | auth 11 / crypto 8 / credentials_verify 9 / capabilities_validation 20 / execution 14 / routing 9 / tasks_recovery 9 / skills 17 / platform_tokens 13 / mcp 10 / agent_adapter 11 / engine_upgrade 20 / audit 11 / ssrf 10 / contract 8 / internal_tools 12 / admin_files 3 / perf 2 |
| Agent 网关 | `cd agents/aragon-gateway && npx tsc --noEmit && npx vitest run` | 类型检查通过；**42 项测试通过**（conformance / tools / policy） |
| npm CLI | `cd packages/skillrouter-cli && npx tsc -p . --noEmit && npx vitest run` | 类型检查通过；**37 项测试通过** |
| 前端 | `cd apps/web && npx tsc --noEmit && npx next lint && npm run build` | 类型检查 0 错误；lint 0 警告；构建成功（18 静态 + 3 动态路由） |
| 迁移 | `python -m alembic -c migrations/alembic.ini upgrade head` | 成功；**49 表 / 54 索引 / 2 触发器**；`PRAGMA user_version=1`；`alembic_version=0002_engine_upgrade` |

### 1.1 与需求的对应关系（关键断言）

| 验收点 | 用例 | 结果 |
|---|---|---|
| 默认密码首次登录强制改密；未改密时业务接口 `403 PASSWORD_CHANGE_REQUIRED` | `test_auth_flow.py` | 通过 |
| 重启/升级不复活默认密码 | `test_auth_flow.py` + `scripts/e2e_live.py` | 通过 |
| 凭证信封加密、响应只出掩码、无明文泄漏 | `test_crypto_credentials.py` | 通过 |
| 凭证探测（成功 / 401 / 429 / 超时）落库 | `test_credentials_verify.py` | 通过 |
| 能力定义 12 条校验规则（含多 route / modes / file_io / examples） | `test_capabilities_validation.py` | 通过 |
| 同步 / 流式 / 异步三模式；**异步提交不触达上游且 ≤1s** | `test_execution_sync_async_stream.py` | 通过 |
| 幂等键命中原 `request_id`；**跨令牌互不干扰（C5）** | `test_execution_sync_async_stream.py`、`test_platform_tokens.py` | 通过 |
| 多 route 故障切换 + route 级决策记录 | `test_routing.py` | 通过 |
| 租约过期回收、重启恢复、取消语义（`remote` / `local_only`） | `test_tasks_recovery.py` | 通过 |
| SKILL 导入校验、路径穿越拒绝、导出包无密钥、**启停独立于 status（R17）** | `test_skills_import_export.py` | 通过 |
| MCP `initialize` / `tools/list` / `tools/call` 与 HTTP 结果一致 | `test_mcp.py` | 通过 |
| 引擎检测 / 验证 / 切换 / 回滚；**升级期间调用 100% 成功（C6）** | `test_engine_upgrade.py` | 通过 |
| 审计脱敏；错误码与 `errors.REGISTRY` 完全一致（C8） | `test_audit_redaction.py` | 通过 |
| `/api/v1` 全端点与 OpenAPI 一致 | `test_api_contract.py` + `docs/api/openapi.json` | 通过 |
| Agent 工具链（提草案 → 校验 → 真实测试） | `test_internal_tools.py` | 通过 |

## 2. 端到端真实联调（`scripts/e2e_live.py`）

真实进程、真实 HTTP、真实 CLI，**上游用本机 echo 服务**（不 mock HTTP 层）：

```
python scripts/e2e_live.py
→ 端到端结果：58/58 项通过
```

覆盖并实际跑通的链路：

1. `bootstrap.py` 初始化 → `uvicorn` 真实进程 → `/healthz`（含 DB 与 `schema_version`）。
2. 默认 `admin/adminjr` 登录 → 未改密被闸门拦截 → 改密 → 新会话。
3. 创建提供商（`base_url` 指向本机 echo）→ 创建凭证 → **真实请求 echo `/models` 完成凭证探测**。
4. 创建能力（**2 条 route**：主 route 同步+异步，备用 route 仅同步）→ 结构校验 0 error。
5. 无测试证据发布被拒（`PUBLISH_EVIDENCE_MISSING`）→ 真实调用测试成功 → 发布 `1.0.0`。
6. 创建外部 Agent + 平台令牌（响应仅此一次返回完整令牌 + `install_command`）。
7. `/api/v1` 同步调用成功（输出 `echo:外部调用`，用量取自上游响应）。
8. `/api/v1` 异步提交 `202 + task_id`（上游零请求）→ worker 领取 → 轮询上游 → 成功并回填 `upstream_task_id`。
9. 调用记录可查（含 `route_decision.chosen_route_id = rt_primary`）、审计覆盖凭证/发布/令牌且无明文密钥。
10. MCP `initialize` → `tools/list`（`capability__text_generate_echo`）→ `tools/call` 结果与 HTTP 一致。
11. SKILL 创建 → 发布 → `node <cli> install --token-stdin` 写入 `SKILL.md`/`invoke.mjs`/`skillrouter.config.json`/`README.md`
    → `node ./invoke.mjs capabilities.list` 动态发现 → `node ./invoke.mjs invoke ...` **真实调用并拿到结果**。
12. 备份产物生成（纯 Python `sqlite3.backup` + `zipfile`，带 sha256）。
13. 主密钥轮换 → 轮换后凭证仍可解密并真实调用成功。

## 3. 前端联通性（`scripts/check_web.py`）

真实 `next start` + 真实 FastAPI：

```
python scripts/check_web.py
→ 前端联通性：9/9 项通过
```

覆盖：`/api/health`、登录页与强制改密页渲染、Next `/api` 反代到 FastAPI（bootstrap 契约字段）、
`/api/v1/health` 经反代可用、`X-Request-Id` 与 `X-Frame-Options` 透传、未登录访问管理接口经反代返回 401。

## 3.5 性能基线（本机实测，非目标硬件）

`cd apps/api && python -m pytest tests/ -m slow -q --capture=no`（数据集为本地测试库，请求数少）：

| 指标 | 目标（§15.4） | 本机实测 | 结论 |
|---|---|---|---|
| `/api/admin/overview` P95 | ≤ 500 ms | **25.0 ms**（p50 9.2 ms） | 通过 |
| `/api/admin/providers` P95 | ≤ 500 ms | **12.5 ms**（p50 7.7 ms） | 通过 |
| `/api/admin/capabilities` P95 | ≤ 500 ms | **8.1 ms**（p50 6.4 ms） | 通过 |
| 异步任务提交（平台侧）P95 | ≤ 1 s | **54.5 ms**（p50 26.8 ms） | 通过 |

> 该结果只说明「实现路径没有把同步/异步做成队列阻塞」，**不替代** §15.4 在
> 4 vCPU / 8 GB / 50 万 `call_logs` 条件下的生产基线采样（仍为待验证，见 §6）。

## 3.6 AragonAgent 网关联通性（`scripts/check_gateway.py`）

真实 `node agents/aragon-gateway/dist/src/main.js`（引擎 `@aragon-agent/core` 0.2.34，`engine-host` 子进程模式）
+ 真实 FastAPI：

```
python scripts/check_gateway.py
→ 网关联通性：12/12 项通过
```

| 检查 | 结果 |
|---|---|
| Python → 网关 `/healthz` | 通过 |
| 引擎版本 == `SKILLROUTER_ENGINE_PIN`（0.2.34） | 通过 |
| 引擎状态 `available` | 通过 |
| 注册工具数 == 12（`sr_*` 白名单） | 通过 |
| 关键工具在位（`sr_capabilities_list` / `sr_request_approval`） | 通过 |
| **未注册任何上游内置工具**（`bash`/`write_file`/`read_file`/`web`/`skill_install`） | 通过（D4 权限边界） |
| 能力状态表可读、三态齐备 | 通过 |
| 适配层 `connectivity()` | 通过 |
| 上游 `schemaVersion == 1` | 通过 |
| 网关热切换 `reload`（切到同一版本） | 通过 |
| reload 后版本未漂移 | 通过 |

> 真实**模型推理**（需要第三方模型凭证）仍为待验证，见 §6。

## 4. OpenAPI 构建产物

`python scripts/export_openapi.py` → `docs/api/openapi.json`（**116 paths**：`/api/v1` 14、`/api/admin` 94、`/internal` 5），
由 `test_api_contract.py` 断言清单中的每个端点都存在。

## 5. 本轮修复的真实缺陷（实现期）

| 编号 | 现象 | 处置 | 证据 |
|---|---|---|---|
| I1 | `url_template` 的 `{base_url}` 未被替换 → 400 | 占位符同时支持 `{x}` 与 `${x}` | e2e / execution 测试 |
| I2 | 「主 route 支持异步 + 备 route 只做同步」无法发布 | 逐 route 降为 warn，能力级保留 error | capabilities 测试 + e2e |
| I3 | 本机/内网上游（Ollama 等）被 SSRF 策略误伤，凭证永不通过 | 管理员配置地址只禁非 http(s) 与云元数据 | ssrf / verify 测试 + e2e |
| I4 | 系统代理把 `127.0.0.1` 上游转发出去 → 上游 502 | 本地目标强制 `trust_env=False` | e2e（真实 HTTP） |
| I5 | `alembic upgrade head` 静默回滚 `alembic_version` 与种子 | PRAGMA 改走 Engine 事件；启动时双向校验 | 迁移实测 + 快照导出 |
| I6 | 默认 `data_dir` + `db_path` 组合解析成 `var/var/...` | 相对路径按 CWD 解析并 strip | bootstrap / e2e |
| I7 | 登录限流把整个 IP 锁死 | 窗口按 (username, ip) 组合 | auth 测试 |
| I8 | `sr_*` 工具实现落点缺失（易形成业务分叉） | 唯一实现在 Python `/internal/tools/{name}` | internal_tools 测试 |
| I9 | 共享密钥尾随空格 → `Illegal header value`，错误被笼统报成「网关不可达」 | 所有字符串设置统一 `strip()`；适配层区分「请求头/地址非法」与「不可达」并给出密钥一致性提示 | check_gateway / settings 测试 |
| I10 | Python→网关的本机调用被系统代理带走 | 适配层同样走 `client_kwargs()`（本地目标 `trust_env=False`） | check_gateway（真实进程） |

## 6. 仍为「待验证」的项（不得视为已完成）

| 项 | 原因 | 复现步骤 |
|---|---|---|
| §15.2-1/2/3 真实第三方能力（文本/语音/异步生成） | 本机无真实第三方凭证 | 见 `verification-checklist.md` §15.2，填入真实 Key 后按脚本跑 |
| §15.2-4 跨提供商真实故障切换 | 需要两家真实提供商的 key | 同上（配置两条 route，主 route 用限流/无效 key 触发） |
| 内置 Agent 真实推理（浮窗对话、工具流、停止报告） | 无可用模型凭证；网关未接真实 provider | 在 `/agent` 配置模型与凭证后重跑 `test_agent_adapter.py` 的集成段 |
| `@aragon-agent/cli` 一致性冒烟（`aragon info --json` 等） | 未安装该 CLI（`SKILLROUTER_EGRESS_POLICY=deny`） | `npm i -D @aragon-agent/cli@0.6.22` 后运行 `upgrade/verify.py` 的 CLI 契约项 |
| Docker Compose / nginx 实际启动 | 本机无 Docker | `docker compose -f deploy/docker-compose.yml up -d`（需先填镜像 digest） |
| §15.4 生产性能基线（4 vCPU/8 GB/50 万 call_logs） | 未在目标硬件上采样 | `python scripts/perf_smoke.py --base-url ...` |
| MCP 真实客户端通路 | 只做了契约测试（`test_mcp.py`） | 用任一 MCP 客户端按 `docs/skills/connect.md` 连接 |

> 上述「待验证」项在 `docs/runbook/verification-checklist.md` 中同样保持「待验证」状态；
> 本文件不修改该清单的状态列，只提供本轮证据。


---

## 7. 复审轮（review node）补充：修掉的缺陷与新的实测数字

复审对 251 个设计文件、`/api/admin` + `/api/v1` + 网关 + CLI + 前端做了逐项核对，
**每个问题都在源码里直接修掉并补了回归断言**（没有留 TODO）。下表是「现象 → 根因 → 修法」。

| # | 现象（用户可见） | 根因 | 修法 / 回归 |
|---|---|---|---|
| R1 | 未初始化时访问业务接口 → 500 | `errors.REGISTRY[NOT_INITIALIZED]` 是 3 元组，`ApiError` 取 `entry[3]` 抛 `IndexError` | 补成 4 元组 + 元组解包兜底；`test_audit_redaction.py::test_every_registered_code_can_be_raised` |
| R2 | SKILL 详情页整页白屏 | 页面读 `skill.plan_steps`，后端从不返回该字段 | 改读 `definition.execution_plan.steps`；`SkillDetail` 类型对齐真实载荷（`skill_md`/`versions`/`missing_dependencies`） |
| R3 | 打开能力详情再保存 → 真实 schema/示例被覆盖成 `{prompt}` | 页面读 `capability.draft_definition`，后端详情返回 `definition` | `type Capability.definition` + `toDraft()`/保存基线都改读 `definition` |
| R4 | 发布按钮永远灰、依赖永远「未发布」 | 前端用不存在的 `dep.published` / `dep.compatible` | 改用后端算好的 `missing_dependencies[]`（含 `reason`） |
| R5 | 点「预览 SKILL.md」必 405 | 前端 `POST /skills/{id}/preview`，后端是 GET | 改 `apiGet` |
| R6 | 导出声称「已开始下载」但没有下载 | 后端只返回 `file_id/download_url`，前端找 `skill_md/url` | 按 `download_url` 打开并显示真实文件名 |
| R7 | 设置页「保存设置」提示成功但预算没生效 | 前端发 `budget:{...}`，后端 DTO 无此字段（被 pydantic 丢弃 → 200） | 改发 `default_budget_usd`；引擎策略 `pinned`→`notify`（后端只接受 `auto/notify/manual`）、载荷策略 `hash_only`→`none` |
| R8 | 设置页环境徽标空白、预算永远 0 | 读 `environment` / `budget.monthly_usd`，后端给 `env` / `default_budget_usd` | 类型与读取全部对齐 |
| R9 | 「立即备份」提示 `file_id undefined`，且轮换护栏误判为「已有备份」 | `POST /settings/backup` 只入队（202），前端当成同步结果 | 改走 `GET /settings/backup/now`（真实产物 + sha256） |
| R10 | 主密钥轮换提示「任务 undefined」 | 后端返回 `rotation_id/status/rows_*`，前端读 `task_id` | 类型与文案对齐；`rotate_master_key` 改为按主键 keyset 分批 + 收尾自检残留行 |
| R11 | 引擎页永远「—/未知版本」，能力状态表永远空 | `/engine/status` 是嵌套 `engine.*`，`/engine/capabilities` 是 `{groups}`，`/engine/upgrade` 返回 `{run_id}` | 页面按真实结构读取；`/engine/status` 增加 `engine_policy` 与 `last_checked_at` |
| R12 | 浮窗 Agent 拖拽附件静默失败 | 控制台上传打 `/api/v1/files`（要平台 Bearer，控制台只有会话 Cookie） | 新增 `POST /api/admin/files`（会话 + CSRF），`uploadFile` 指向它并把 `AbortSignal` 接上（取消按钮真的会取消） |
| R13 | 任意令牌可读/取消别人的异步任务 | `_owned()` 读 `task["token_id"]`，而 `_detail()` 从不返回该字段（守卫是死代码） | 新增 `tasks.get_owned()`，`/api/v1/tasks/*` 与 MCP `task_status`/`task_cancel` 都走它 |
| R14 | 反复被限额拒绝后令牌并发额度永久耗尽 | `_quota_gate` 逐层计数，后续层抛错时已计入的名额不释放 | 计数放进 `try`，异常时 `_release()` 已获取的作用域 |
| R15 | 异步任务续跑/取消打到另一家提供商，或按新版本定义提交 | worker 用 `lookup(key)` + 重新选路，忽略任务冻结的 `capability_version_id` / `route_id` | 新增 `tasks.runtime_context()` + `routing.resolve_pinned()`：按冻结版本与 route 执行，找不到就明确报错而不是重新选路 |
| R16 | 重启可能把同一异步任务提交两次（重复计费） | `recover_on_startup` 无条件补队列条目；handler 无提交认领 | 恢复只补「队列里没有在途条目」的任务；提交前 `tasks.claim_submission()` compare-and-set；`test_tasks_recovery.py` 新增两条断言 |
| R17 | api 与 worker 两个进程可能领到同一条队列项 | `leases.claim` 是 SELECT-then-mutate（`db_write()` 的锁只在进程内） | 改为单条 `UPDATE ... WHERE id=(SELECT ...) RETURNING id` 条件更新 |
| R18 | 自定义认证头/`extra_headers` 的密钥原文进了 `call_logs.request_preview` | `redact_headers` 只认固定头名 | 按「本次注入的头名 + 本次密钥原文」脱敏（含 `extra_headers`） |
| R19 | 抓取超大响应可把进程内存吃光 | `fetch_text` 先 `resp.content` 全量再截断 | 改 `client.stream()` 边读边截断（`max_bytes`） |
| R20 | 迁移头校验静默失效（`MIGRATION_OUT_OF_DATE` 永不触发） | `bootstrap.assert_migrated` 用了未导入的 `Path`，`NameError` 被 `except Exception` 吞掉 | 补导入 + 抽出 `_alembic_head()`，降级时 `log.warning` |
| R21 | 切换失败后 `current.json` 仍指向未验证版本（网关重启就按它启动） | `write_pointer()` 在 reload 确认之前执行且失败不回退 | reload 失败时把指针写回上一版本，并在返回体里给 `pointer_restored` |
| R22 | 空 `scopes` 的令牌等于全权限 | `row.scopes or ["*"]` 把 `[]` 变成全量 | 校验时空作用域 fail-closed；创建时拒绝空 `scopes` |

### 7.1 本轮实测（复审后重跑）

| 套件 | 命令 | 结果 |
|---|---|---|
| 后端全量 | `cd apps/api && python -m pytest tests/ -q` | **197 项通过**（复审前 190；+7 条新增/改写的回归断言，含 2 项 `slow`） |
| 前端类型 | `cd apps/web && npx tsc --noEmit` | 0 错误 |
| 前端 lint | `cd apps/web && npx next lint` | 0 warning / 0 error |
| 前端构建 | `cd apps/web && npm run build` | 成功（静态 + 动态路由全部生成） |
| 网关类型 | `cd agents/aragon-gateway && npx tsc --noEmit` | 通过 |
| CLI 类型 | `cd packages/skillrouter-cli && npx tsc -p . --noEmit` | 通过 |
| OpenAPI 构建产物 | 由 `create_app().openapi()` 重新导出 | `docs/api/openapi.json` **117 paths**（新增 `/api/admin/files`） |
| 设计文件清单 | `python scripts/check_file_plan.py` | 251/251 存在，缺失 0 |

> 说明：`docs/api/openapi.json` 此次由应用对象直接导出（`scripts/export_openapi.py` 需要 8000 端口上的运行实例），
> 与脚本产物同格式（`ensure_ascii=False, indent=2`）。
