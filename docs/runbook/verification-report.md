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

---

## 8. 迭代 2（第 2 轮迭代：缺陷修复）实测

本轮范围是 `spec.md` 的 §A（v4.0，经 §R2 评审修正）。验收口径按 §A.7.4：**每条 P0 都有一个会红的

回归断言**，不新增任何「跳过即通过」的检查项；无法在本机闭环的（真实第三方凭证、真实 Docker、

目标硬件性能、真实 MCP 客户端）继续留在 `verification-checklist.md` §F。

### 8.1 执行顺序（§A.7.2，逐条真跑）

| 步 | 命令（`apps/api` 或仓库根） | 真实结果 |

|---|---|---|

| 1 | `python -m alembic -c migrations/alembic.ini upgrade head`（空库） | `Running upgrade -> 0001_init` → `0001_init -> 0002_engine_upgrade` → `0002_engine_upgrade -> 0003_iteration2`，exit **0** |

| 2 | `python -m alembic -c migrations/alembic.ini downgrade base` | `0003_iteration2 -> 0002_engine_upgrade` → `0002_engine_upgrade -> 0001_init` → `0001_init -> (base)`，exit **0** |

| 3 | `python -m alembic -c migrations/alembic.ini upgrade head` | 三条 `Running upgrade` 再次出现，exit **0** |

| 4 | `python -m alembic -c migrations/alembic.ini upgrade head`（幂等证据，R2-02） | 只有 `Context impl SQLiteImpl`，**无任何 `Running upgrade`**，exit **0** |

| 5 | `python -m pytest tests -q` | **247 passed in 78.37s**（连续两次重跑同结果：`247 passed in 76.20s`） |

| 6 | `python scripts/e2e_live.py` | **70/70 项通过**（第 1 轮 58 项全部保留，新增 12 项） |

| 7 | `python scripts/check_web.py` | **9/9 项通过** |

| 8 | `python scripts/check_gateway.py` | **12/12 项通过** |

第 4 步是 R2-02 的直接证据：`0001`/`0002` 是 `create_all` 型迁移，模型已含新列，`0003` 必须按

`PRAGMA table_info` 判决后再加列，否则第二次 `upgrade head` 会以 `duplicate column name` 失败。

迁移落库后的列与版本（临时库实测）：

```

alembic_version = 0003_iteration2 (head)

PRAGMA user_version = 2 ; tables = 49

idempotency_keys.state / tasks.cancel_requested_at / credentials.meta_sealed

call_logs.needs_reconcile / engine_upgrade_runs.verify_status / engine_upgrade_runs.blocked_reasons  —— 6 列各 1 个

```

### 8.2 各套件与真实输出

| 套件 | 命令 | 结果 |

|---|---|---|

| 后端全量 | `cd apps/api && python -m pytest tests -q` | **247 passed**（第 1 轮 197 + 本轮新增 50；0 skipped） |

| 端到端（真实进程 + 真实 HTTP + 真实 CLI） | `python scripts/e2e_live.py` | **70/70** |

| 前端联通 | `python scripts/check_web.py` | **9/9** |

| 网关联通（含热切换） | `python scripts/check_gateway.py` | **12/12** |

| 前端类型 | `cd apps/web && npx tsc --noEmit` | 0 错误 |

| 前端 lint | `cd apps/web && npx next lint` | `✔ No ESLint warnings or errors` |

| 前端构建 | `cd apps/web && npm run build` | 成功，**22 条路由**（新增 `/credentials`） |

| 网关类型 | `cd agents/aragon-gateway && npx tsc --noEmit` | 通过 |

| 网关单测 | `cd agents/aragon-gateway && npx vitest run` | **48 passed**（42 + 新增 `pointer.test.ts` 6 条） |

| CLI 单测 | `cd packages/skillrouter-cli && npx vitest run` | **37 passed** |

| OpenAPI | `python scripts/export_openapi.py` | `docs/api/openapi.json` **117 paths**（v1=14 / admin=95 / internal=5），重新导出 |

| schema 快照 | `python scripts/export_schema_snapshot.py` | `tables=49 indexes=56 triggers=2 -> db/migrations/0001_init.sql` |

| 设计文件清单 | `python scripts/check_file_plan.py` | **310/310 存在，缺失 0**（§8 与 §A.4 四张表合并校验） |

| CI 交付面 | 见 §A.7.1 T-35/T-36 | `True True True`；`run)` 分支存在 = `True`；compose YAML 可解析 |

### 8.3 新增的回归断言（对应 §A.7.1 的 T 编号）

| 文件 | 断言 |

|---|---|

| `tests/test_capabilities_validation.py` | T-01（返回体键集合 + `test_runs[].ok` 必须是 JSON boolean）、T-02（3 调 2 成功 → `0.666…`；无调用 → `null`；未测试 → `None`） |

| `tests/test_credentials_projection.py` | T-03（四个投影无明文且 `probe_path` 仍可见）、T-04（`webhook_secret` 密封后仍能验签）、I4（敏感键不进 `meta`） |

| `tests/test_token_transport.py` | T-05（`?token=` 默认 400 + 逃生阀单独一条）、T-06（日志无令牌原值且 `exp` 保留） |

| `tests/test_budget_accounting.py` | T-07（流式/异步都计费，日/月两行递增）、T-08（四级定价阶梯）、T-45（`agent` 作用域 402）、T-46（终态并发归零且重复释放不变负） |

| `tests/test_execution_sync_async_stream.py` | T-09（读超时不重投、`needs_reconcile=1`）、T-10（连接超时可重试）、T-11（同键并发只打一次 + 过期占位可重占） |

| `tests/test_task_cancel_race.py` | T-12（提交中取消到上游）、T-13（`canceling` 不变式 + 两类悬挂行回收）、T-44（`queued→canceling` 合法） |

| `tests/test_lease_ownership.py` | T-14（越权 release 返回 False）、T-15（续租丢失取消 handler） |

| `tests/test_maintenance_mode.py` | T-25（写 503 / 读 200 / `healthz.maintenance`）+ 陈旧标记仍拦截 |

| `tests/test_settings_runtime.py` | T-26（PATCH 后同进程立即生效）、T-27（白名单外 `KeyError`） |

| `tests/test_migrations_roundtrip.py` | T-23（往返）、T-24（指纹不匹配退出 3）、T-42（连续两次 upgrade 幂等） |

| `tests/test_upgrade_toolchain.py` | T-41（网关不可达 → `blocked` 且不产生候选）+ `local` 在 production 被忽略 + 产物路径在 data_dir 下 |

| `tests/test_engine_upgrade.py` | T-16…T-22（含 `blocked` 阻断 auto、备份失败不动指针、`pin_source`、compat 检出结构变化） |

| `tests/test_api_contract.py` | T-28（时间别名 + `X-Deprecated-Param`）、T-29（`sort` 白名单）、T-30（tasks 列表筛选）、T-43（`LOGGED_STATUS` 聚合口径） |

| `tests/test_admin_files.py` | T-31（签名 URL 无令牌下载 + 过期 401）、T-32（GET 405 / 无 CSRF 403 / 有 CSRF 200）、T-48（同一签名算法） |

| `agents/aragon-gateway/test/pointer.test.ts` | T-47（指针覆盖 env、损坏回落、`pin_source` 与实际来源一致） |

### 8.4 端到端新增的 12 项（`scripts/e2e_live.py`）

原有 58 项全部保留，新增：`schema_version 为 2`、`备份产物生成并带 sha256`（改 POST + CSRF）、

测试返回体形状与前端类型一致、测试返回体不含旧的 `ok`/`route`、`test_runs[].ok` 是 JSON boolean、

24h 内存在通过测试记录、上传响应返回签名 URL、上传响应带 `expires_at`、签名 URL 无令牌下载成功、

过期签名返回 401、维护模式三条（`healthz.maintenance` / 写 503 / 读放行）、维护标记移除后恢复

`maintenance=false`。

### 8.5 本轮**没有**新增的证据（保持待验证）

- 真实第三方凭证的真实联调（文本/语音/异步、两个提供商）—— 仍为待验证（P1/P2/P3）。

- 真实外部 Agent（AragonAgent / Claude Code）安装 → 发现 → 调用闭环、真实 MCP 客户端 —— 仍为待验证（P5/P6）。

- 真实 `docker compose up`（本机无 Docker）、目标硬件（4C8G）性能基线 —— 仍为待验证（P8/P9）。

- 真实候选版本升级（需上游新版本 + 真实构建）—— 升级流水线升级为**部分验证**：

| 已能证明的 | 仍不能证明的 |

|---|---|

| 工具链归属确定（构建/验证在网关容器；`local` 仅开发机）；网关不可达 → `blocked` 且不产生候选；verify 七项三态；`blocked` 阻断 auto 切换；备份失败不动指针；`install_home` 后能解析到候选版本且 `pin_source='pointer'`；compat 能检出会话结构变化 | 真实 `@aragon-agent/core` 新版本在隔离构建 → 验证 → 切换全链路实际成功的记录（需要上游发布 + 真实网关容器 + 模型凭证） |

### 8.6 实施期发现并登记的设计缺陷

7 条（`ALL_EVENT_NAMES` 不存在、`tasks.released_at` 不在 6 列清单、`save_bytes` 字段名与

`describe()` 不一致、`lib/hooks/useTableState.ts` 实为新建、`migrations/env.py` 的占位 URL、

`db_path`/`data_dir` 相对路径坑、§A.10 同步项只落地一部分）已逐条登记在

`docs/plans/skill-router/spec.md` 的「实施过程发现的方案缺陷 → D2」，并按登记的处置实现。

> 运维提醒（I2-6）：`SKILLROUTER_DATA_DIR` 与 `SKILLROUTER_DB_PATH` 必须用**绝对路径**，且 api /

> worker / gateway / web 四个进程取同值。用相对路径时「从仓库根 bootstrap、从 `apps/api` 起

> uvicorn」会打开两个不同的库，表现为启动即 500 `no such table: system_state`。

### 8.7 `ruff check .` 的现状（本轮未修，如实记录）

`cd apps/api && python -m ruff check .` 当前报 **105 条**违规，全部属于**本轮之前就存在**的存量问题：

- 类型：`UP017`（提示用 `datetime.UTC` 别名）、`F401`（未使用导入）、少量 `ANN201`/`UP035`；
- 分布：`api/admin/agent_chat.py`（12）、`security/ratelimit.py`（8）以及 `db/base.py`、`router/*`
  等**本轮未改动**的文件；
- 证据：`git show HEAD:apps/api/skillrouter_api/api/admin/agent_chat.py`（本轮零改动的文件）
  单独跑 ruff 仍报 **9 条** F401，说明 `ruff check .` 在本轮之前就是红的。

本轮的处理原则是**只修自己引入的问题、不顺手重构**（§A 的「保持 diff 聚焦」）：本轮新增/修改的
代码没有新增 F401；对比 `git show HEAD:<file>` 后，`api/admin/providers.py`、`api/admin/settings.py`
里报出的未使用导入在 HEAD 版本里就已存在（导入行本轮未改）。

> 结论：CI 的 `api` 作业里 ruff 这一步在存量代码上仍会红。要转绿需要一次独立的「纯格式化/清理」
> 提交（`ruff check --fix` 可自动修 92 条），不在本轮范围内；`verification-checklist.md` §F1 已登记。


## 9. 最终评审（提交前复核，iteration 2）

复核方式：按 `spec.md` §A.2 / §A.3 / §A.4 / §A.7 逐条对照未提交的 diff，对每条可疑点回到源码交叉验证，
**能确认的当场修掉**（不留 TODO 清单），无法在本机闭环的继续按 §A.7.3 的口径留在「待验证」。
本节只记录本轮评审新增的命令与结果，§8 的结论不变。

### 9.1 评审直接修复的缺陷（14 项）

| # | 文件 | 缺陷（用户可见后果） | 依据 |
|---|---|---|---|
| 1 | `services/execution.py` | 异步调用在提交时把 `call_logs.status` 写成 `queued` 且**全仓无人回写终态** → `/calls` 永远显示「排队中」，成功率分母里塞着永不成败的行（不变式 I7 被破）。新增 `finalize_task_call()`，由 worker 终态统一回写（`canceled` 记 `failed` + `error_code=CANCELED`） | §A.3.1-2 / I7 |
| 2 | `services/execution.py` | `_log_call` 仍读 `settings.payload_policy`（env 快照）→ 设置页改「载荷策略」永不生效；改为 `settings_runtime.get` | F-13 / §A.3.9 |
| 3 | `services/execution.py`、`services/capabilities.py`、`worker/handlers/capability_invoke.py` | 定价阶梯第一级（route 显式 `unit_price_usd`）在生产调用点全部未传 → 声明了显式价的提供商被按价目表/默认价计费，概览金额与预算阈值都错 | §A.3.4-3 / T-08 |
| 4 | `worker/handlers/capability_invoke.py` | 4 条退出路径（提交前取消、他人已取消、轮询 `status_unknown`、认领丢失）没有收口 → 五层并发名额永久泄漏、调用记录永远 `queued` | §A.3.4 / T-46 |
| 5 | `worker/runner.py` | 超时 / handler API 异常 / 内部异常只改任务行，不释放并发、不回写调用记录 → 每次超时泄漏 5 层额度；租约丢失与优雅停机则**刻意不**收口（任务会在新 owner 继续执行，此时收口会二次占用额度） | §A.3.4 / §A.3.6-7 |
| 6 | `services/capabilities.py` | 成功率聚合分母含 `queued`（未终态）行 → 异步能力成功率恒偏低；分母改为只统计 `LOGGED_STATUS` 的三种终态 | §A.3.1-2 |
| 7 | `services/settings_runtime.py` | 白名单键是 `ssrf_deny_extra`，而 `EDITABLE`/`db/seed.py` 写的是 `ssrf_deny` → 设置页改 SSRF 黑名单永不生效。两个键都读并减掉部署基线 | F-13 / §A.3.9 |
| 8 | `services/settings.py` | `GET /settings` 原样回 DB 里的逗号字符串，前端类型是 `string[]` → **保存过一次设置后设置页每次打开都 TypeError 白屏**。`fetch_allowlist`/`ssrf_deny` 归一成 `list[str]`；前端同时加兜底 | §A.3.9 / T-33 |
| 9 | `api/admin/providers.py`、`services/credentials.py` | 凭证列表的 `q` 与 `status=expiring` 只在**前 100 条**里本地筛选 → 超过 100 条时「即将过期」会漏、搜索会漏、`total` 也错。改为服务层 JOIN + 条件筛选 | F-19 / §A.3.11 |
| 10 | `api/admin/engine.py` + `tests/test_engine_upgrade.py` | `blocked` 候选缺 `acknowledge_blocked` 返回 400，与 §A.5.2 的 409 不符；测试还用 `in (400, 409)` 兜住（等于放宽断言）。改为 409，断言收紧 | §A.5.2 / T-18 |
| 11 | `deploy/Dockerfile.gateway`、`agents/aragon-gateway/src/main.ts` | 镜像里**没有** `scripts/verify_artifact.mjs` → 「导出清单兼容」「事件映射契约」恒 `blocked`，`policy=auto` 永不生效（F-09 的「假绿」换成「永阻塞」）。补 COPY + `AGENT_VERIFY_ARTIFACT_SCRIPT`，并修正回落路径深度 | §A.3.7-1 / R2-01 |
| 12 | `agents/aragon-gateway/src/main.ts`、`src/artifact.ts` | `/healthz` 的 `pin_source`/`resolved_path` 取自启动时的 config → 热切换之后仍报旧结论（排查入口说谎）。改为请求时重读指针；`npm-only` 构建在 tarball 目录里跑 `npm ci` 必失败，改为 `blocked` + 可执行下一步 | §A.5.3 / T-47 |
| 13 | `upgrade/switch.py`、`upgrade/rollback.py`、`upgrade/verify.py` | 健康门只比版本号（引擎起不来但版本号对得上也算通过）→ 加 `ok is not False`；回滚把 `engine_pin` 写成 `engine_previous_pin`（与指针分叉、留裸 `@`）→ 改写目标版本；冒烟检查的 detail 改成真话（本轮不自动做真实推理） | §A.3.7-3-4 / F-23 |
| 14 | `agent/client.py`、`web/app/(app)/capabilities/[id]/page.tsx`、`web/app/(app)/settings/page.tsx` | `replay_messages` 用了未导入的 `select`（会话历史重建必抛 NameError）；能力「发布」按钮在首次发布后**永久禁用**（改完定义、重测通过也发不出新版本）；设置页 allowlist 兼容字符串 | §A.3.1 / §A.5.2 |

### 9.2 本轮复跑的命令与结果

| 项 | 命令 | 结果 |
|---|---|---|
| 后端测试 | `cd apps/api && python -m pytest tests/ -q` | **247 passed**，0 skipped（新增用例仍为 47 个，见 §8.3） |
| 迁移往返 | `alembic upgrade head → downgrade base → upgrade head → upgrade head`（独立临时库） | 4/4 `exit=0`；6 个新列 + 2 索引到位；`alembic_version=0003_iteration2`；第二次 `upgrade head` 幂等 |
| 网关类型 | `cd agents/aragon-gateway && npx tsc --noEmit` | 0 错误 |
| 网关测试 | `cd agents/aragon-gateway && npx vitest run` | **48 passed**（4 个文件，含 `pointer.test.ts` 6 项） |
| 前端类型 | `cd apps/web && npx tsc --noEmit` | 0 错误 |
| 前端 lint | `cd apps/web && npx next lint` | `✔ No ESLint warnings or errors` |
| 前端构建 | `cd apps/web && npm run build` | 成功，**22 条路由**（19 静态 + 3 动态） |
| 端到端 | `python scripts/e2e_live.py` | **70/70 项通过** |
| 前端联通 | `python scripts/check_web.py`（API:8000 + web:3000 实起） | **9/9 项通过** |
| 网关联通 | `python scripts/check_gateway.py`（网关:8500 inline 实起） | **12/12 项通过**，热切换后版本未漂移 |
| 设计清单 | `python scripts/check_file_plan.py` | **310/310**，缺失 0 |
| 健康探测实测 | `curl /healthz` | `pin_source=env`、`resolved_path=var/aragon-home/engines/0.2.34/node_modules/@aragon-agent/core`、`maintenance=false` |
| 静态检查 | `cd apps/api && python -m ruff check .` | **104 条存量**（比 §8.7 少 1 条：修掉 `agent/client.py` 的 F821），仍属存量问题、不在本轮范围 |

### 9.3 评审发现的测试夹具偶发问题（未改，记录在案）

`tests/conftest.py::make_token` 用 `test-agent-{n}-{os.urandom(2).hex()}` 命名，2 字节随机后缀在同一会话里
存在撞名概率（实测撞到一次 `test-agent-1-f0dc`，`POST /access/agents` 返回 409 使该用例红）。单独复跑与整轮
复跑均通过，属夹具随机性而非本轮改动引入；建议后续把后缀扩到 4 字节。

### 9.4 本轮仍未闭环的（口径不变）

真实第三方凭证联调、真实外部 Agent / MCP 客户端、真实 Docker Compose、目标硬件性能基线、真实候选版本升级
仍为**待验证**（P1/P2/P3/P5/P6/P8/P9/P11）。`blocked` 与 `skipped` 一律不计入 `passed`。

---

## 10. 第 3 轮：有头浏览器自动化巡检（2026-10-18）

本节只登记**本轮实际跑过的命令与输出**；口径不变：没有证据的不写「已验证」。

**环境**：Windows 10.0.26200；Python 3.11.7；Node v22.18.0；SQLite（本地文件）；
浏览器 = 系统 Chrome（`channel=chrome`，有头）；`start.ps1` 起 api/worker/gateway/web 四进程，
上游用本机 echo 服务（`http://127.0.0.1:8791`，`POST /v1/generate`）。

### 10.1 自动化命令与结果

| 套件 | 命令 | 结果 |
|---|---|---|
| 后端全量 | `cd apps/api && python -m pytest tests -q` | **全部通过**（退出码 0） |
| 前端类型 | `cd apps/web && npx tsc --noEmit` | 0 错误 |
| 前端 lint | `cd apps/web && npx next lint` | 0 warning / 0 error |
| 浏览器 e2e（有头） | `cd apps/web && set SR_E2E_CHANNEL=chrome && npx playwright test --headed --workers=1` | **9 passed / 2 skipped**（skip 的两条要 `SR_E2E_AGENT=1` + 真实模型引擎） |

浏览器 e2e 覆盖（`e2e/smoke-headed.spec.ts` 为本轮新增）：

| 用例 | 断言 |
|---|---|
| 全部管理台路由零控制台错误 | 13 条路由逐个访问，控制台 error / pageerror / HTTP≥400 **全部为空** |
| `/healthz` 经反代返回维护标志 | 200 + `maintenance` 为 boolean（维护横幅依赖它） |
| 高对话框完整居中，底部按钮可点 | 「签发令牌」对话框 `y ≥ 0`、`y+height ≤ 视口`、垂直居中误差 ≤8px、主按钮在视口内 |
| 调用详情抽屉渲染耗时分解 | `排队 / 上游 / 总计` 可见，无 pageerror |

### 10.2 本轮通过浏览器复现并修掉的缺陷（12 项）

| # | 现象（用户可见） | 根因 | 修法 |
|---|---|---|---|
| B1 | 首次登录按提示改完密码，又被弹回 `/change-password`，概览永远进不去 | 改密成功后 `(app)` 布局仍读到改密页缓存的 `/me`（`must_change_password: true`，staleTime 60s） | 改密成功后先 `setQueryData` 打补丁再 `invalidateQueries`，然后跳转 |
| B2 | 「签发令牌」等较高的对话框下半截在视口外，主按钮**点不到** | `.sr-enter` 的 `animation ... both` 把 `transform: translateY(0)` 永久留在元素上，覆盖了 Tailwind 的 `-translate-y-1/2` 居中；实测 617px 高的对话框从 y=475 起 | keyframes 改用 `translate` 长属性（与 `transform` 共存），不再覆盖定位 |
| B3 | 调用记录点开详情 → 整块白屏 | 前端读 `detail.data.timings.queue_ms`，后端返回 `latency_breakdown` | 类型与页面同时接受两种字段名，缺失时回落 `null`（不再抛异常） |
| B4 | 用向导建的能力**永远发布不了**：400 `INPUT_SCHEMA_INVALID`（`output_schema.additionalProperties 必须为 false`） | 向导默认 `output_schema` 写成 `additionalProperties: true`；后端 `default_definition()` 写成 `{"type":"object"}`，同样违规 | 两处默认值改为 `additionalProperties: false` |
| B5 | 所有 `Select` 的标签点击无效，读屏/自动化无法按标签定位控件 | `FormField` 只渲染 `label htmlFor={id}`，从不给子控件补 id（`Input` 手写了 id，`Select` 全都漏了） | `FormField` 对单元素子节点兜底补 `id` 与 `aria-describedby` |
| B6 | 本地开发时每个页面都刷一条 404；维护横幅永远不出现 | 维护横幅读 `/healthz`，但 `next dev` 没有把 `/healthz` 反代到 FastAPI（生产靠 nginx 的 `location = /healthz`） | `next.config.mjs` 增加 `/healthz` rewrite，本地与容器行为一致 |
| B7 | 能力状态显示英文 `testing`；状态筛选缺「测试中」 | 后端 `validate()` 通过后把能力推进为 `testing`，`StatusBadge`/筛选没有这一项 | 补 `testing → 测试中` 徽标与筛选项 |
| B8 | 校验结果里的**警告**被显示成「错误」 | 后端 issue 字段叫 `level`，前端读 `severity` | 类型与渲染同时接受 `level`/`severity` |
| B9 | 签发令牌页给出的安装命令把**完整令牌**写进 argv | `access.py` 把真实令牌拼进 `install_command`（CLI 自己会警告 `--token` 会进 shell 历史） | `install_command(token_stdin=True)`，统一 `--token-stdin`；令牌只出现在独立字段里 |
| B10 | 每个页面都在控制台报一条 404 | 没有 favicon，浏览器请求 `/favicon.ico` 落空 | 新增 `app/icon.svg`（Next 自动注入 `<link rel="icon">`） |
| B11 | 按文档写 `.\start.ps1 -Follow:$false` 直接报「找不到参数 Follow」 | 帮助注释写的是 `.PARAMETER Follow`，实际开关是 `-NoFollow` | 帮助注释改为 `.PARAMETER NoFollow` |
| B12 | 既有 e2e 用例本身跑不起来：`56.000061 !== 56`、Alt+J 面板打不开、任务页断言常驻区块 | 亚像素精度断言；按键发生在布局 hydration 之前；断言了一个只在有 `status_unknown` 任务时才渲染的区块 | 改 `toBeCloseTo`、先等热键宿主挂载、改成与数据无关的不变量 |

> B1/B2/B3/B4 属于「按 UI 提示操作也走不通」的阻断级缺陷；B5/B6/B8/B9 会稳定误导使用者，
> 其余为可访问性/准确性/文档问题。全部改动都有对应回归：B1（手动复核 + 改密页）、B2/B3/B6（新增 e2e）、
> B4（向导 → 测试 → 发布实跑通过）、B7/B8/B5（同一轮浏览器巡检 0 控制台错误）。

### 10.3 有头浏览器实测通过的业务闭环

1. 登录 → 强制改密 → 落到概览（不再回环）；
2. `/providers` 新建提供商 → 详情添加 bearer 凭证（掩码 `echo-***9999`）→ `/credentials` 打开详情 → **检测有效性**（真实探测 echo `/models`，200，最近验证=刚刚）；
3. `/capabilities/new` 向导（手工配置）→ 草稿 → `发送测试`（真实打到 echo，`succeeded`/HTTP 200）→ `发布 1.0.0` → 四态徽标 `已保存 / 测试通过 / 已发布 / 可调用`；
4. `/access` 新建 Agent → 签发令牌（对话框完整可见、令牌只显示一次）→ 用令牌 `POST /api/v1/capabilities/text.generate.echo3/invoke` 拿到 `succeeded` → `/whoami`、`/capabilities` 正常；
5. `/calls` 列表出现该次调用（耗时/重试/request_id）→ 点开详情看到路由决策、耗时分解、脱敏请求响应；
6. `/skills` 手工建草稿 → 详情 → 预览 `SKILL.md` → 发布（输入确认词）→ 导出连接包 zip 下载成功；
7. `/settings` 七个 Tab 全部渲染、保存往返成功（`已保存 · 刚刚`）；`/settings/engine` 「立即检测」返回 `最近检测=刚刚`、12 条能力状态表可读；无候选版本时「升级/验证」按钮按设计禁用；
8. 浮窗 Agent：`Alt+J` 开关、跨路由保持、SSE `已连接`；**真实模型推理仍未验证**（本机无模型凭证，消息停在「排队中」）。

### 10.4 本轮仍未闭环（口径不变）

真实第三方凭证联调、真实模型推理、真实外部 Agent / MCP 客户端安装、`docker compose up`、
目标硬件性能基线、真实候选版本升级 —— 仍为**待验证**。

### 10.5 本轮补跑的三个联通性脚本（README 引用的数字出处）

README 的「质量与证据」表引用的三个脚本结果，本轮重新跑了一遍（同一天、同一台机器、
api/worker/gateway/web 四进程由 `start.ps1` 起在 8000/8500/3000）：

| 命令 | 输出 |
|---|---|
| `python scripts/e2e_live.py` | `端到端结果：70/70 项通过`（§2 记录的是 58 项，此后陆续补了 12 项：测试返回体形状、`test_runs[].ok` 类型、24h 证据、签名 URL 上传/下载/过期、维护模式三条、主密钥轮换后仍可调用等） |
| `python scripts/check_web.py` | `前端联通性：9/9 项通过` |
| `python scripts/check_gateway.py` | `网关联通性：12/12 项通过`（含热切换后版本未漂移） |

迁移状态同步复核：

```
cd apps/api && python -m alembic -c migrations/alembic.ini upgrade head
→ 无输出（已在 head）；PRAGMA user_version=2，alembic_version=0003_iteration2，49 表
```

> 这三个脚本用的都是真实进程与真实 HTTP（上游为本机 echo 服务），因此它们证明的是
> **链路可用**，不证明**第三方上游可用** —— 后者仍按 §10.4 保持「待验证」。

---

## 11. 第 4 轮：把 CI 从「0 job 失败」修到全绿，并顺带挖出 2 个真实缺陷

起点：本仓库首次推送到 GitHub 后，CI **每次都 failure 且 0 个 job**。本节记录根因链、
修法、以及过程中被门禁挖出来的真实缺陷。口径不变：只有跑过的命令才写进这里。

### 11.1 0 job 的两个原因（GitHub 一次只报一批）

| # | 原因 | 表现 | 修法 |
|---|---|---|---|
| 1 | `jobs.<id>.env` 里用了 `${{ runner.temp }}`。该位置只允许 `github / needs / strategy / matrix / vars / secrets / inputs` | 整份 workflow 被判 `Invalid workflow file`：**run 显示 failure 但 0 个 job**（不是只坏一个 job） | 数据目录改在第一步用 `$RUNNER_TEMP` 算出来写进 `$GITHUB_ENV` |
| 2 | 修 #1 时把守卫写成内联 python heredoc，脚本里出现字面量表达式定界符；GitHub 会对 `run:` 做表达式替换 | 仍然 0 job，报 `parser did not reach end of input after parsing the expression` | 守卫搬到 `scripts/check_workflow_contexts.py`，workflow 里只留一行调用 |

> 排查方法教训：拆成单 job 能跑、整份不能跑 + 0 job，基本可以判定为「文件被判非法」而不是「job 挂」。
> 靠 actionlint（`actionlint .github/workflows/ci.yml`）本地复现才是最快路径 —— 这次是它一次点出 #2。

### 11.2 变绿过程中修掉的三个门禁问题

| 作业 | 现象 | 根因 | 修法 |
|---|---|---|---|
| `web` | `next build` 只在 Linux 失败：`Error occurred prerendering page "/404"` + `<Html> should not be imported outside of pages/_document` | workflow 级 `NODE_ENV=development`（为了 npm 装 devDependencies）让 `next build` 走 pages 的 **dev 运行时**去预渲染内置 `/404`、`/_error` | 只在 `next build` 这一步覆盖 `NODE_ENV=production`；另补 `app/not-found.tsx` / `app/global-error.tsx`，让构建不必回落到 Pages Router |
| `contracts` | `api.d.ts` 与 `openapi.json` 不一致 | 生成物自 `259d62b` 后未再生成 | `npm run gen` 重新生成（7158 插入 / 428 删除），`gen:check` 通过 |
| `scripts`/`api` | 同一份代码，ruff 有时 0 条、有时 121 条 | CI 用 `pip install ruff`（每次都拉最新）：0.15.20 报 0 条，0.17.1 报 43 条 | 钉死 `ruff==0.15.20`（ci.yml 两个作业 + `requirements-dev.txt`）；再 `--fix` 清 144 处 + 手修 12 处 |

### 11.3 被门禁挖出的 2 个真实缺陷（不是类型标注问题）

| # | 位置 | 缺陷 | 用户可见后果 |
|---|---|---|---|
| 1 | `services/execution.py::finalize_task_call` | 读 `task.error`，而 `Task` 模型没有该列 → `AttributeError` 在 `db_write()` 内抛出 | **整个事务回滚**：`call_logs.status` 永远停在 `queued`，`/calls` 一律显示「排队中」。被 `worker/runner.py` 的 `try/except` 吞成一条 warning，且此前无用例断言终态 |
| 2 | `db/seed.py` + `worker/handlers/retention.py` | 我在 mypy 修复里做变量重命名时，循环体仍用旧变量名 | seed 会把上一个循环遗留的 `str` 写进 Setting；retention 会**删错行**（删上一轮已擦除的 CallLog，而不是过期的 IdempotencyKey）。两处都由 ruff `B007` 当场抓回 |

> 缺陷 1 恰好是 §9.1 第 1 项想修的症状（异步记录永不成败）以另一种形式复发 —— 说明
> 「修一次」不等于「锁死」，本轮补了 4 条回归（`tests/test_task_finalize_call_log.py`）。

### 11.4 本轮新增的门禁与回归

| 类型 | 文件 | 作用 |
|---|---|---|
| CI 守卫 | `scripts/check_workflow_contexts.py` | 按 GitHub 上下文可用性表检查 `env`/`if`/`with`/`run`；错用即 `::error::` 退出 1（防止再次 0 job 失败） |
| CI 守卫 | `.github/workflows/ci.yml` 的 `ci.yml 上下文可用性` 步骤 | 上面那个脚本的调用点 |
| 后端回归 | `apps/api/tests/test_task_finalize_call_log.py` | 4 条：成功→success、失败取 `result.error.code`、取消→failed+CANCELED、未终态不写死 |
| 前端回归 | `apps/web/app/not-found.tsx`、`app/global-error.tsx` | App Router 的 404 / 全局错误页（此前缺失，构建只能回落 Pages Router） |

### 11.5 本轮实测命令与结果

```text
cd apps/api && python -m pytest tests -q            -> 251 passed（247 原有 + 4 新增）
cd apps/api && python -m mypy skillrouter_api       -> Success: no issues found in 136 source files
cd apps/api && python -m ruff check .               -> All checks passed!
<repo root>  && python -m ruff check scripts        -> All checks passed!
cd apps/web  && npx tsc --noEmit                    -> 0 errors
cd apps/web  && npx next lint                       -> No ESLint warnings or errors
cd apps/web  && npm run build                       -> 成功（22 路由）
python scripts/e2e_live.py                          -> 70/70 项通过
python scripts/check_web.py                         -> 9/9 项通过
python scripts/check_gateway.py                     -> 12/12 项通过
packages/contracts: npm run gen:check                -> 与生成结果一致
```

### 11.5.1 OpenAPI 漂移检查的真实原因（补记）

`api` 作业的「OpenAPI 导出并检查漂移」步骤此前从来没跑到过（每次都在 ruff / mypy 先失败），
本轮第一次真正执行就红了。查下来**内容完全一致**，唯一差异是文件末尾换行：

```diff
-}                        <- 仓库里那份：没有结尾换行
\ No newline at end of file
+}                        <- CI 用 print() 导出：必然多一个换行
```

CI 的命令是 `python -c "...print(json.dumps(...))" > docs/api/openapi.json`，
`print()` 一定追加换行；而仓库里那份没有。于是这项检查**永远**认为有漂移。
修法：按 CI 的确切输出重写（UTF-8 + 结尾换行），此后 `git diff` 为空。

> 顺带记一个坑：在 cmd.exe 里用 `>` 重定向接 python 的 stdout，会把中文写成 **GBK**，
> 生成的文件中文全变乱码（本轮踩了一次，diff 里 17 行 description 全花）。
> 正确做法是用 Python 自己写文件：`io.open(p, "w", encoding="utf-8", newline="\n")`。

#### 11.5.2 真正的坑：导出 OpenAPI **必须用 requirements 锁定的 fastapi/pydantic**

补上结尾换行后 CI 仍然红（run #18）。继续查才发现根因更根本：

| 环境 | fastapi | pydantic | 生成结果 |
|---|---|---|---|
| 本机全局 | 0.136.1 | 2.14.0 | 与仓库那份一致（但多了 170 行） |
| `requirements.txt` 锁定 | **0.115.6** | **2.9.2** | CI 实际用来比对的那份 |

两个版本生成的 OpenAPI 判若两份文件，差异**全部**是 schema 表述细节：
`additionalProperties: true` 的有无、`contentMediaType` vs `format: binary`、
`HTTPValidationError` 是否带 `input`/`ctx`。

已核对：**路由与操作数完全一致**（117 paths / 139 operations，`missing` 与 `added` 两个集合都为空），
所以这纯粹是依赖版本问题，不是接口变了。

处置：
1. 建一个按 `requirements-dev.txt` 装的 venv，用**它**导出 `docs/api/openapi.json`；
2. 因为 `packages/contracts/src/api.d.ts` 是从该 JSON 生成的，同步 `npm run gen` 重新生成
   （650 行变动，`gen:check` 与 `tsc -p tsconfig.json --noEmit` 均通过）；
3. 在 CI 的报错分支里加了 `::notice::`，直接告诉下一个人「用锁定依赖导出」的确切命令，
   并把 diff 前 60 行打出来（此前只打 `--stat`，看不出差异在哪）。

> 结论：**OpenAPI / 契约这两个生成物只能在锁定依赖的环境里生成**，
> 换一个 pydantic 版本重新导出会把这两个文件一起弄脏。

### 11.6 CI 转绿（run #19，commit `d2c329d`）

```text
9 passed / 0 failed / 1 skipped（e2e (optional) 按设计跳过）
conclusion: success
https://github.com/xdrshjr/AragonSkillRouter/actions/runs/39190593842
```

| 作业 | 结果 | 说明 |
|---|---|---|
| `scripts (py3.11)` | ✅ | 含新增的「ci.yml 上下文可用性」守卫 |
| `api (py3.11)` | ✅ | ruff + mypy + pytest + OpenAPI 漂移，四项全过 |
| `migrations (py3.11)` | ✅ | upgrade → downgrade → upgrade → 再 upgrade（幂等） |
| `gateway (node22)` | ✅ | tsc + vitest |
| `cli (node22)` | ✅ | tsc + vitest |
| `web (node22)` | ✅ | eslint + tsc + next build |
| `contracts diff` | ✅ | 生成物无 diff |
| `deploy config` | ✅ | compose / nginx / entrypoint 校验 |
| `e2e skipped (not verified)` | ✅ | 显式告警「跳过 ≠ 通过」 |
| `e2e (optional)` | ⏭ | 需要仓库变量 `E2E_ENABLED=1` 与已起好的 api |

> 口径不变：`e2e` 那一条是**按设计跳过**，不计入 passed。真实模型推理、真实第三方凭证、
> 真实外部 Agent / MCP 客户端、`docker compose up`、目标硬件性能基线仍为**待验证**。

### 11.7 仍然待验证的（口径不变）

真实第三方凭证、真实模型推理、真实外部 Agent / MCP 客户端、`docker compose up`、
目标硬件性能基线、真实候选版本升级 —— 与 §10.4 相同，本轮没有新增证据。

---

## 12. 桌面端（Electron）实测

> 机器：Windows 10.0.26200（`process.platform==='win32'`）· Node 22.18.0 · npm 10.9.3 ·
> 构建解释器：uv 管理的 standalone **CPython 3.11.16**（非 conda）·
> Electron 33.4.11 · electron-builder 26.15.3 · PyInstaller 6.12.0 · Next 15.5.7。
> 口径不变：**没有命令与输出就不算验证**。本节每条都带可复现命令与真实输出。

### 12.1 单测（`apps/desktop`）

```bat
cd apps\desktop
npx vitest run
npx tsc --noEmit
```

```text
 ✓ tests/env.test.ts      (13 tests)
 ✓ tests/startup.test.ts  (11 tests)
 ✓ tests/config.test.ts    (5 tests)
 ✓ tests/ports.test.ts    (13 tests)
 Test Files  4 passed (4)
      Tests  42 passed (42)
```

`npx tsc --noEmit` → 退出码 0（0 错误）。

覆盖的关键断言：`SKILLROUTER_INTERNAL_URL` 在 **api 与 gateway 两份 env** 中都存在且都指向 18100（P0-1）；
`SKILLROUTER_MASTER_KEY` 不存在；继承的 `SKILLROUTER_*`/`AGENT_*`/`ENGINE_*`/`PYTHON*` 被剔除（P1-12）；
`SKILLROUTER_ENGINE_TOOLCHAIN==='gateway'`；`E-D01` 与 `W-D06` 的分支；`checkPortContract()` 的
`none` / `repair` / `E-D07` 三种结果；`routes-manifest.json` 的**数组与 Next 15 对象两种形态**（I-4 回归守卫）；
状态机九条转移 + `E-D03/E-D04/E-D05` + 退避 1/3/9 s。

### 12.2 出包（`npm run dist:win`）

```bat
cd apps\desktop
npm run dist:win
```

```text
[build:all] ▶ gateway
[build:all] ✔ gateway（39.6 s）
[build:all] ✔ web（55.6 s）
[build:gateway] 组装完成：...\build\gateway
[build:web] 端口契约断言通过：/api/:path* → http://127.0.0.1:18100/api/:path*
[build:sidecar] OK  --version → skillrouter-server 0.1.0 (skillrouter_api 0.1.0, py3.11)
[build:sidecar] OK  bootstrap 自检通过（migration=empty_db_upgrade, admin.created=true）
[build:sidecar] OK  api 自检通过（/healthz ok=true initialized=true schema=2）
[build:all] ✔ sidecar（63.9 s）
[build:all] ✔ main（0.2 s）
[build:all] 资源汇总完成
• building        target=nsis file=release\SkillRouter-Setup-0.1.0-win-x64.exe
```

产物：

| 产物 | 大小 |
|---|---|
| `apps\desktop\release\SkillRouter-Setup-0.1.0-win-x64.exe` | **122.8 MB** |
| `apps\desktop\release\win-unpacked\SkillRouter.exe` | 180.0 MB |
| `apps\desktop\release\win-unpacked\resources\{python,web,gateway}` | 176 / 2096 / 2427 files |

### 12.3 `npm run verify`

```text
1) 产物与资源完整性
  ok   NSIS 安装器存在 — 122.8 MB
  ok   win-unpacked/SkillRouter.exe 存在 — 180.0 MB
  ok   resources/python 存在 — 176 files
  ok   resources/web 存在 — 2096 files
  ok   resources/gateway 存在 — 2427 files
  ok   resources/app.asar 存在 — 0.1 MB
  ok   resources/web/server.js 存在 — 7 KB
  ok   PyInstaller 双布局迁移目录存在 — 2 布局

2) 两个 exe 的 --version（P1-5）
  ok   skillrouter-server.exe --version — skillrouter-server 0.1.0 (skillrouter_api 0.1.0, py3.11)
  ok   SkillRouter.exe --version — Skill Router 0.1.0 (electron 33.4.11)

3) 端口契约（构建期烧死的 /api 目标）
  ok   routes-manifest.json /api/:path* → 127.0.0.1:18100 — http://127.0.0.1:18100/api/:path*

4) 哈希清单
  ok   写出 SKILLROUTER-SHA256SUMS.txt
[verify] 全部通过
```

`apps/desktop/release/SKILLROUTER-SHA256SUMS.txt`（**最后一轮出包之后**的值；本轮链是
`npm run dist:win` → 修完审阅问题 → `npm run build:main` + `electron-builder` 重新打包，
下面这组哈希对应「本次冒烟/交互实测所用的那份产物」）：

```text
f324d78d55e81329d05b494edf95336807f84ada363b3e7cb27fe56be738d9df  SkillRouter-Setup-0.1.0-win-x64.exe
bb2a5f435e0a7f2c6f32df88571d1ee2313dc51ef07c85843c0fb32935285a28  win-unpacked/SkillRouter.exe
51a9ee61dcfea0be4b68f0d3dd63c7d38f94c2fc7a512b88db671c2e063e2991  resources/python/ (176 files)
418a6555c68c533f8e0bb51125ef7e6364333a26bfb76c1fffdbb90ba01f2633  resources/web/ (2096 files)
69d9127ca88247d585c7c966d9cce596e057bd6573269adfc52337923c474526  resources/gateway/ (2427 files)
a68cc52d279478113aeca58ebe87881cbf32d6945242a36e24e41f23a7991aaf  resources/python/skillrouter-server.exe
```

> 安装器哈希在两次出包之间会变（NSIS 自身的时间戳/压缩非确定性），`SkillRouter.exe` 与
> `resources/gateway/` 稳定；`resources/{python,web}` 的目录哈希会随 PyInstaller / Next 产物微调而变。
> 清单的用途是「这个安装包和这次冒烟是同一份」，不是跨次复现的固定值。

### 12.4 `npm run smoke`（净化 PATH）

```bat
cd apps\desktop
npm run smoke
```

```text
[smoke] 纯净前探测：pythonOnPath=true nodeOnPath=true
[smoke] 被测进程 PATH=C:\Windows\system32;C:\Windows
[smoke] 启动 ...\release\win-unpacked\SkillRouter.exe --smoke-test --data-dir "C:\...\Temp\Skill Router Data 1792472395824"
[smoke] 被测进程退出码 0（7.3 s）
[smoke] checks：resources_present=ok port_contract=ok sidecar_bootstrap=ok api_healthz=ok
        gateway_healthz=ok gateway_platform_reach=ok web_login_page=ok web_api_proxy=ok login=ok single_instance=ok
[smoke] shellChecks：clean_shutdown=ok
[smoke] 冒烟通过
```

报告要点（`apps/desktop/release/smoke-report.json`，`ok: true`，`durationMs: 6969`——**从进程启动算到报告落盘**，
含自举与四次 spawn，不再是「只统计检查耗时」）：

- `externalRuntime.pathUsed = "C:\Windows\system32;C:\Windows"` —— **目标机不需要 Python / Node**；
- `port_contract = { ok: true, apiPort: 18100, manifestPort: 18100 }` —— 三方比对里可判定的两方真实相等
  （第三方式 `--port` 由状态机在同一值上注入，见 `main.ts` 的 `enforcePortContract()`）；
- `sidecar_bootstrap = { ok: true, code: 0, migrationMode: "empty_db_upgrade", adminCreated: true }`
  —— 退出码来自本次 bootstrap 的真实退出码，不是「字符串长度 > 0」；
- `api_healthz.schemaVersion = 2`、`initialized = true`；
- `gateway_platform_reach.internalUrl = "http://127.0.0.1:18100"`（P0-1 静态项必判；`dynamic: "skipped"`，
  理由 `engine_not_configured`，**不冒充已验证**）；
- `login.mustChangePassword = true`（经 Next 反代 → api，`sr_session` + `sr_csrf` 双 Cookie）；
- `single_instance.detail = "second launch exited with code 0 and spawned 0 services"`；
- `shellChecks.clean_shutdown = { ok: true, detail: "tasklist: skillrouter-server.exe=0 SkillRouter.exe=0" }`，
  `artifact_hash.exe = bb2a5f43…`（与 `SKILLROUTER-SHA256SUMS.txt` 中 `win-unpacked/SkillRouter.exe` 一致）。

> 反面证据（保留）：I-5 修复**之前**同一套冒烟的输出是
> `gateway_healthz=FAIL / web_login_page=FAIL / web_api_proxy=FAIL / login=FAIL`，而
> `sidecar_bootstrap`、`api_healthz`、`port_contract`、`clean_shutdown` 全绿。
> 这正是 §4.3 警告的「绿灯但完全不可用」——只探 `/healthz` 抓不到，反代 + 登录闭环抓得到。

### 12.5 交互式启动（等价「双击」，非 `--smoke-test`）

用干净环境启动 `win-unpacked\SkillRouter.exe`（本机 shell 带 `ELECTRON_RUN_AS_NODE=1`，必须剔除——
见 spec「实施过程发现的方案缺陷」I-8），随后从外部探测：

```text
GET  http://127.0.0.1:18101/login          → 200，10746 字节，含登录表单
GET  http://127.0.0.1:18101/api/health     → 200 {"status":"ok"}      ← Next rewrite → api，反代链路通
POST http://127.0.0.1:18101/api/admin/auth/login {"username":"admin","password":"adminjr"}
                                           → 200 {"admin":{...,"must_change_password":true},"csrf_token":"..."}
                                             Set-Cookie: sr_session=…; sr_csrf=…
```

`main.log` 的启动时间线（**冷启**：全新数据目录，要跑迁移 + 建管理员 + 首次加载运行时）：

```text
[05:00:34.961Z] [main] state=idle
[05:00:34.989Z] [main] state=bootstrapping
[05:00:37.098Z] [main] event=first_run admin_created=true
[05:00:37.100Z] [main] state=starting_api
[05:00:40.926Z] [main] state=ready ports={"api":18100,"gateway":18102,"web":18101}
[05:00:41.163Z] [main] event=api_healthz service=skillrouter-api matches=true
```

**`state=idle` → `state=ready` = 5.965 s**（INV-3 冷启上限 120 s / 热启上限 45 s，均满足；
口径与测量点按 spec §0 的 `main.log` 两行时间差）。
`state=ready` 现在**只出现一行**（同时带 ports）——审阅轮发现它此前被 `move('ready')` 与
`hooks.log(...)` 各写一遍，grep 计数会翻倍。

关停（`taskkill /PID <主进程>` 不带 `/F`，等价「点关闭窗口」：WM_CLOSE → `window-all-closed` → `before-quit`）：

```text
[05:00:41.752Z] event=shutdown_begin
[05:00:41.810Z] event=stopped name=web     pid=12944 graceful=true
[05:00:41.859Z] event=stopped name=gateway pid=19116 graceful=true
[05:00:41.914Z] event=stopped name=worker  pid=37196 graceful=true
[05:00:41.958Z] event=stopped name=api     pid=520   graceful=true
[05:00:41.959Z] event=shutdown_done
```

关停后 `tasklist /FI "IMAGENAME eq skillrouter-server.exe"` 与 `…eq SkillRouter.exe` 均返回
「没有运行的任务匹配指定标准」——**零孤儿进程**（逆序 web → gateway → worker → api，全部 graceful，
没有退化到 `taskkill /T /F`）；主进程退出码 0。

### 12.6 手工「双击可用」清单（§6.3）的登记状态

| # | 项 | 状态 | 证据 / 说明 |
|---|---|---|---|
| M1 | 无 Python / 无 Node 的干净机 | **部分** | 本次在同一台机器上用**净化 PATH**（`C:\Windows\system32;C:\Windows`）证明三个运行时都来自安装包；**没有**在真正没装过 Python/Node 的虚拟机里跑过 → 该项保持**待验证** |
| M2 | 双击安装器 → 安装 → 桌面快捷方式 | **待验证** | 安装器已产出（122.8 MB，NSIS，`oneClick: false`、`perMachine: false`、创建桌面/开始菜单快捷方式）；无人值守环境下不执行交互式安装（会弹 UAC/向导） |
| M3 | 双击启动 → 登录页，冷启 ≤120 s / 热启 ≤45 s | **已实现 + 本次实测** | 见 §12.5：冷启 **5.965 s**；`/login` 200 且有登录表单 |
| M4 | `admin`/`adminjr` 登录 → 强制改密 | **部分** | 登录 API 已实测 `must_change_password: true`（后端既有流程）；改密后的 UI 跳转未在真机上点过 |
| M5 | 控制台四态徽标 / `/providers` 新建 | **待验证** | 需要人点；后端与页面在既有 e2e（9 项）里覆盖 |
| M6 | 关窗口 → 任务管理器无残留 | **已实现 + 本次实测** | 见 §12.5 关停日志 + tasklist 空结果 |
| M7 | 再次双击 → 直接进登录页（不重新初始化） | **部分** | 第二实例已被证明「以 0 退出、0 spawn」（`single_instance`）；「数据仍在」由数据目录持久化保证，未人工点 |
| M8 | 运行中再次双击 → 聚焦已有窗口 | **已实现** | `app.requestSingleInstanceLock()` + `second-instance` 聚焦；冒烟里以退出码 0 证明锁生效（聚焦动作本身要人看） |
| M9 | 卸载保留应用数据 | **已实现** | `nsis.deleteAppDataOnUninstall: false`；未执行卸载 |
| M10 | 未签名 → SmartScreen 提示 | **已知差距** | 未做代码签名（R1）；安装器未签名，`signtool` 那几行是 electron-builder 的默认占位动作，**不代表已签名** |

> 因此本轮的诚实结论：**打包成功、双击可用性在「程序行为」层面已实测（启动/登录/反代/关停/单实例）**；
> 「在真正没装 Python/Node 的干净机上人工双击安装」这几条按仓库口径继续标**待验证**，不写成已验证。

### 12.7 回归（现有证据未退化）

```bat
cd apps\api && python -m pytest tests -q
cd agents\aragon-gateway && npx vitest run
cd packages\skillrouter-cli && npx vitest run
cd apps\web && npx tsc --noEmit
```

桌面端是纯增量（INV-1）：**只新增 `apps/desktop/**` 与三份文档**，对既有源码零修改，
因此上面四套不需要重跑即可判定不受影响；`.gitignore` / `README.md` / `docs/README.md` 仅追加。
本轮实际重跑的是桌面端自身的 42 项单测 + `tsc --noEmit` + `dist:win`（含审阅修复后重新 `electron-builder` 打包）
+ `verify` + `smoke` + 交互式启动/关停实测。

### 12.8 复审轮（code review）发现并已修复的问题

> 口径：下面是**复审节点逐文件对照 spec 后发现的问题**，全部已在源码里改掉（没有留 TODO），
> 改完重跑了 §12.1 / §12.3 / §12.4 / §12.5 的四套命令，上面贴的就是**修复后**的输出。
> 判定依据：`splash.html` 的 CSP 与脚本、`main.ts` 的关注点、`supervisor.ts` 的事件处理、
> `smoke.ts` 的报告字段、`verify-artifact.mjs` 的路径来源。

| # | 位置 | 问题（为什么算缺陷） | 修复 |
|---|---|---|---|
| R-1 | `splash.html` | CSP 是 `default-src 'none'; style-src 'unsafe-inline'`，**没有 `script-src`** → 进度/提示脚本被整体拦截：splash 永远停在「正在准备…」，`adminInitialized === false` 时的「首次登录 admin / adminjr」提示（spec §2.11 第 4 条的唯一落点）**永不出现** | 显式加 `script-src 'unsafe-inline'`，其余指令不动（splash 无外部资源） |
| R-2 | `main.ts` 错误窗口回调 | 「打开日志」执行的是 `void app.whenReady()`（一个已 resolve 的 Promise，什么也不做）——按钮点了没反应 | 抽出 `windows.ts: openLogsDir()`，错误窗口 / `W-D06` 对话框共用 |
| R-3 | `main.ts` 两处「重启服务」 | ① 两处重复实现（IPC 与菜单）行为不一致（菜单那处还不重置状态）；② 二者都只复位本模块的 `shuttingDown`，**没有复位 supervisor 的关停标志** → 重启后子进程退出回调被永久拦掉，崩溃监控静默失效 | 合并为唯一的 `restartServices()`（带重入保护），并新增 `Supervisor.resetShuttingDown()` |
| R-4 | `supervisor.ts` | `spawn()` 没有接 `error` 事件：exe 被杀软删掉/路径错时，Electron 会抛未捕获异常（**主进程直接闪退**），且 `exited` 永不 resolve → 关停白等 8 s | 在 `exited` 的同一 Promise 里接 `error`：记 `error=spawn_failed`、摘掉 handle、resolve 收尾 |
| R-5 | `main.ts` `runBootstrap()` | bootstrap 子进程不在 supervisor 管里，也没有引用 → 用户在「初始化数据库」那 120 s 内关窗口时它会变成孤儿继续写库（下次启动 `DB_BUSY`，R9） | 模块级 `bootstrapChild` 引用，`shutdown()` 里一并收尾 |
| R-6 | `services.ts` / `paths.ts` | web 的 `cwd` 用的是 `resources` 根（spec §2.8 明确 `cwd: resourcesWeb`），与「Next standalone 以自身目录为基准」不一致 | `DesktopPaths` 增加 `webRoot` / `gatewayRoot`，web 与网关的 `cwd` 钉到各自根；读 manifest 也统一走 `paths.webRoot` |
| R-7 | `smoke.ts` | 两项检查是**假检查**：`port_contract` 只判 `apiPort !== null`（恒真），`sidecar_bootstrap` 只判字符串长度>0（恒真）；`startedAt`/`durationMs` 从「检查开始」计时，与 spec §4.4 的语义（本次运行）不符；`bootstrapSummary` 字段传的是状态机的 `detail` 且从未被使用 | 传入 manifest 端口与 bootstrap 真实退出码，改为真实比对（`manifestPort === apiPort`、`code === 0 && mode !== 'unknown'`）；`startedAt` 取进程启动时刻；删掉死字段 |
| R-8 | `ipc.ts` / `preload.ts` | 通道名在两侧各写一份字面量（改一处忘另一处 → invoke 永远 pending）；且 `ipcMain.handle` 对同一通道重复注册会抛错（`activate` 重建窗口时可能踩到） | 通道名收敛到 `shared/constants.ts: IPC_CHANNELS`；`registerIpc()` 先 `unregisterIpc()` 保证幂等 |
| R-9 | `verify-artifact.mjs` | 安装器文件名把版本硬编码成 `0.1.0`，升版后「产物明明在，verify 说缺」 | 从 `package.json` 读 `version` |
| R-10 | `startup.ts` | `state=ready` 被写两遍（`move()` 一次 + 额外 `hooks.log()` 一次），grep 计数翻倍；另有 `assertNever` + `void assertNever`、`main.ts` 的 `void unregisterIpc` / `void bootstrapOutput` / 死函数 `announceState` / `smoke.ts` 的 `export { API_SERVICE_TAG }` 等死代码 | `move('ready', ' ports=…')` 写一行；死代码全部删除（无「留着的 TODO」） |
| R-11 | `main.ts` | `ServiceStatus` 在 `failed` 状态下把四个服务都报成 `starting`（与实际相反）；`import … from './../shared/constants'` 与仓库其余文件的 `'../shared/constants'` 风格不一致 | 失败态映射为 `down`；import 路径统一为 `'../shared/…'` |
| R-12 | `ports.test.ts` | `readManifestApiPort()` 只测了数组形态；I-4 记录的「Next 15 对象形态」没有回归守卫 | 新增 1 例对象形态用例（总数 41 → **42**） |
| R-13 | `package.json` | `scripts/make-icon.mjs`（spec §3#33）没有任何入口，等于存在但不可达 | 增加 `make:icon` 脚本 |
| R-14 | `python/requirements-build.txt` | 注释引用的实施记录编号写错（I-2 → 实际是 I-1） | 更正为 I-1 |

**按 spec 保留、故意不改的两项**（写在这里以免下一轮当成漏修）：

1. `taskkill /T /F` 只在「优雅 exit 超时」后触发 —— spec §2.10 的原文顺序就是如此；
   `clean_shutdown`（§12.4）在真实关停下为绿，本轮不擅自改变关停语义。
2. `E-D06` 仍不作为错误码存在（spec §2.3.2 已改为 `W-D06` 警告级），runbook 里那行是「它不是错误码」的说明。

---

## 13. 第 5 轮：真实模型凭证把「内置 Agent 真能用」跑通（2026-10-20）

**这一轮的目标不是加功能，而是把 README 里挂着的
「内置 Agent 真实模型推理 = 待验证」变成有证据的已实现。**
凭证是真实的第三方模型（智谱 GLM，Anthropic 兼容协议），命令与输出都在下面。

### 13.1 复现方式（有头浏览器，可肉眼跟随）

```cmd
cd apps\web
set SR_E2E_ADMIN_USER=admin
set SR_E2E_ADMIN_PASSWORD=<已改密的密码>
set SR_LIVE_AGENT=1
set SR_E2E_CREDENTIAL_MATCH=GLM
set SR_BASE_URL=http://127.0.0.1:3000
set SR_E2E_CHANNEL=msedge          :: 本机无 playwright 自带 chromium 时的兜底
set SR_E2E_SLOWMO=350              :: 慢动作，便于肉眼跟每一步
set SR_E2E_HOLD_MS=30000           :: 结束后窗口保留 30s
npx playwright test e2e/agent-live.spec.ts --headed --project=chromium --reporter=list --workers=1
```

输出（真实）：

```text
  ok 1 [chromium] › e2e\agent-live.spec.ts:63:7 › Agent 真实闭环（有头） › 配置模型 → 测试连通性 → Agent 调用平台工具 (1.1m)
  1 passed (1.2m)
```

截图逐张落在 `var/dev/shots/`：`02-overview` → `03-agent-model-page` → `04-agent-model-filled`
→ `05-agent-model-saved` → `06-connectivity-result` → `07-agent-workbench` → `08-agent-prompt-typed`
→ `09-agent-tool-called` → `10-agent-turn-finished`。

### 13.2 真实证据（不是「看起来能跑」）

| 环节 | 证据 |
|---|---|
| 连通性测试 | 页面 `连通性测试` 区域：**通过** · `模型正常回话` · HTTP **200** · 耗时 **5616 ms** · `anthropic / glm-5.3-flash` |
| 探活接口 | `POST /api/admin/agent/config/test` → `{"ok":true,"checked":true,"status":200,"transport":"proxy","text_chars":2,...}` |
| 一轮对话 | `agent_turns.status = succeeded`，`turn.finished` 事件带 `usage {inputTokens:10364, outputTokens:342}`、`duration_ms 15449`、`iterations 2` |
| 工具调用 | `tool.started` / `tool.finished` 各一条：`sr_capabilities_list`，`ok:true`，`duration_ms:59`，结果 `{"ok":true,"capabilities":[{"key":"text.generate.echo3",...}],"matched":1,"total":1,"truncated":false}` |
| 界面呈现 | 对话区出现 Agent 回复：「工具可用：`sr_capabilities_list` 调用成功（ok=true），limit=5 下实际返回 **1 条**能力——`text.generate.echo3`…」；状态徽标回到「空闲」 |
| 事件全链路 | 该轮共 **163** 条事件落库（`agent_events`），`rowid` 单调递增 |

### 13.3 这一轮挖出并修掉的 4 个真实缺陷

四个都是「代码在、看起来对、但这条链路其实不通」。

| # | 位置 | 缺陷 | 修复 |
|---|---|---|---|
| A-1 | `apps/api/skillrouter_api/worker/handlers/agent_turn.py` | 把 `agent_model.model`（**字符串**）塞进轮次请求的 `model` 字段，且**从不发** `credential_refs` → 网关每次都回 `TURN_CONFLICT: no model configured for this turn` | 新增 `services/agent_model.py` 做唯一翻译层：`resolve()` 产出 `model{providerId,modelId,baseUrl}` + `credential_refs{providerId:credential_id}`；配置不全时**当场**把轮次置 `failed` 并给出可操作提示，不再排进队列干等 |
| A-2 | 同上 + `agents/aragon-gateway/src/engine-host.ts` | `ingest_stream()` **没有任何调用方**：网关的事件从来不落库；且引擎子进程拒绝轮次时只写日志就 `finishTurn`，平台侧轮次**永久 running、0 事件** | worker 接单后 `await ingest_stream(...)` 直到终态（含超时/断流兜底，全部写成 `failed`，绝不留永久 running）；子进程拒绝时补发 `turn.failed`（`rejectedTurnPayload()`） |
| A-3 | `apps/api/skillrouter_api/api/admin/agent_chat.py` | SSE 从**进程内 broker** 取事件，而轮次在 **worker 进程**执行 —— 内存队列跨不过进程边界，浏览器一条都收不到 | 事件源改为权威表 `agent_events`（P04），新增 `agent/live.py`：`rowid` 作游标、`latest_cursor/session_cursor/events_after/tail`，并支持 `last_event_id` 断线续传 |
| A-4 | `agent/live.py::frame()` + `apps/web/lib/sse.ts` | 帧里写了 `event: <type>` → 浏览器 EventSource **不会**派发到 `onmessage`（命名事件只触发同名监听器），163 条事件全被丢弃 | 服务端只发 `id:` + `data:`；前端同时注册 13 个平台事件名做双保险（两种格式都能工作，协议细节不该由「哪一端先改」决定可用性） |

顺带修掉的两个「配置能存进去、但永远是错的」：

- `PATCH /agent/config` 过去接受任意 dict（`provider=zhipu`、写错的 `credential_id` 都能落库），
  现在走 `agent_model.validate_patch()`：白名单键 + 协议族枚举 + 凭证存在性 + 数值范围。
- `update_agent_config()` 过去用 `if v is not None` 过滤，导致 `base_url` / `credential_id`
  **想清空却清不掉**；现在允许显式 `null`。

以及网关侧两个「永远返回空」的读取缺陷：

- `normalizeCatalog()` 只读 `{capabilities}`，而平台返回 `{items:[...]}` → `sr_capabilities_list`
  恒为空。现在兼容 `items`/`data`，并把 `modes[]` 投影成 `mode`（否则按 mode 过滤会全丢）。
- `POST /internal/turns` 的入参校验补上 `model` / `credential_refs` 形状，`resolveModel()`
  兼容「裸字符串当 modelId」（旧平台客户端）。

### 13.4 新增的回归（都对着真实缺陷写，不是凑数）

| 套件 | 文件 | 覆盖 |
|---|---|---|
| pytest | `apps/api/tests/test_agent_model.py`（17 例） | 翻译层形状、自动选凭证、配置校验（未知 provider / 未知键 / 未知凭证）、未配置时拒绝发送、worker 发 `model` 对象 + drain、断流置 failed、探活 200/非 200/只有 thinking |
| pytest | `apps/api/tests/test_agent_stream.py`（6 例） | 跨进程投递、`Last-Event-ID` 续传、keep-alive、**帧里不得出现 `event:` 行** |
| vitest | `agents/aragon-gateway/test/regressions.test.ts`（5 例） | `items` 信封、`modes` 投影、mode 过滤、`turn.failed` 的 payload |
| playwright | `apps/web/e2e/agent-live.spec.ts`（1 例，需真实凭证） | 配置 → 连通性 → 一轮对话 → 工具调用 → 回到空闲（**1 passed**） |
| playwright | `apps/web/e2e/agent-stop.spec.ts`（1 例，需真实凭证） | 长任务中途停止 → StopReport + 对账入口 + 不再是「执行中」（**1 passed**） |

### 13.5 本轮实测命令与结果

```text
# 后端
cd apps/api && python -m pytest tests -q                         → 全部通过
cd apps/api && python -m ruff check tests skillrouter_api        → All checks passed!
cd apps/api && python -m mypy skillrouter_api                    → Success: no issues found in 138 source files

# 网关
cd agents/aragon-gateway && npx tsc --noEmit                     → 0 error
cd agents/aragon-gateway && npx vitest run                       → 53 passed（5 文件，原 48 + 5）

# 前端
cd apps/web && npx tsc --noEmit                                  → 0 error
cd apps/web && npx next lint                                     → No ESLint warnings or errors

# 真实闭环（有头）
cd apps/web && npx playwright test e2e/agent-live.spec.ts --headed  → 1 passed (1.2m)
```

OpenAPI 已随实现重新导出（新增 `POST /api/admin/agent/config/test`，`/agent/config` 响应多了
`effective`/`providers`，共 **118 paths**：v1=14 / admin=96 / internal=5）：

```text
# 注意：**不要**用 `> docs/api/openapi.json` 这种 shell 重定向。
# cmd.exe 会按控制台代码页（GBK）落盘，把整份构建产物写成非 UTF-8，所有消费方都会踩坑
# （本轮就发生过：文件被写成 GBK + CRLF，git 里 232 行全变）。正确做法是让 Python 自己写：
cd apps/api && python -c "import json, skillrouter_api.main as m; open('../../docs/api/openapi.json','w',encoding='utf-8',newline='\n').write(json.dumps(m.create_app().openapi(), ensure_ascii=False, indent=2) + '\n')"
```

> 另一条纪律：本地 fastapi/pydantic 比 `requirements.txt` 钉住的版本新（0.136/2.14 vs 0.115.6/2.9.2），
> 直接导出会多出 `additionalProperties: true`（130 处）、`contentMediaType` 取代 `format: binary`、
> `ValidationError` 多 `input`/`ctx`。这些是**环境差异**而不是契约变化，提交它们会让 CI 的漂移检查红。
> 导出后请确认 `git diff docs/api/openapi.json` 只包含本次真实新增的路由/描述。

### 13.6 停止报告（StopReport）也跑通了

`apps/web/e2e/agent-stop.spec.ts`（有头，真实模型）：发一轮「连续调工具」的长任务 →
徽标变「执行中」→ 点「停止」→ 界面出现

```text
已停止：sr_capabilities_list（已完成）…
可能仍在远端运行：…        （external_pending 非空时展示）
去任务里对账 →
```

并且「执行中」徽标**消失**（不会卡在运行态）。本轮结果 **1 passed (26.5s)**，
截图 `var/dev/shots/stop-03-stopped.png`、`stop-04-idle-after-stop.png`。

本轮修掉的相关缺陷：`turn.stopped` 之前只由 API 进程的 `broker.publish` 广播，
worker 进程的 `agent_events` 落库事件到不了浏览器（A-3）；改成表驱动后停止报告才真的可见。

### 13.7 本轮仍未闭环（口径不变）

- **非 Anthropic 协议族**（openai / google 两条 `_probe_request` 分支）只有单测桩，
  没有真实上游凭证——按规矩保持「待验证」，不因代码在就改状态。
- §13.1 / §13.6 的两个 e2e **默认 skip**（`SR_LIVE_AGENT=1` 才跑），CI 不跑真实模型。
  这与「e2e skipped ≠ verified」的既有口径一致：**证据在本文档，不在流水线**。
- 本轮新增的两条 e2e 需要 `SR_E2E_ADMIN_PASSWORD`（已改密的密码）；密码丢失时用
  `python scripts/bootstrap.py reset-admin-password --user admin` 重置后再跑。

### 13.8 顺带修掉的一条既有用例自相矛盾

`apps/web/e2e/core-loop.spec.ts` 里先硬断言「能力状态含『测试通过』」，紧接着又假设
「『未测试』时发布按钮禁用」——任何未测试的能力都会让它必红（本轮实测红过一次）。
现已改为：`已保存` 必断言 ± `测试通过|未测试` 二选一，仅在「未测试」时才要求发布禁用。
同一文件里还有一处 `getByText('执行中')` 的歧义（徽标 vs 输入框提示），已改 `exact: true`。

### 13.9 复审（同一批未提交改动）补修的 6 个问题

把 §13.1–13.8 的改动整体复审了一遍，又找出并修掉下面这些。它们都不是新功能，而是
「同一类缺陷换了个状态字 / 换了个出错位置」。

| # | 位置 | 问题 | 修复 |
|---|---|---|---|
| B-1 | `agent/sessions.py` + `services/agent_queue.py` | `queue_turn()` 会把「执行中又发一条」记成 `queued`，输入框也明确写着「会排队」，但**没有任何代码**把 `queued` 推进成 `running` → 永久「排队中（第 N 条）」，且它会继续挡住之后的新消息（queued 也算 busy）。与 A-2 的「永久 running」同类 | 新增 `sessions.dequeue_next()`（CAS：`WHERE status='queued'`，不会双跑）+ `services/agent_queue.start_next()`；worker 收尾、模型未配置失败、用户停止三处都调用 |
| B-2 | `api/admin/agent_chat.py::retry` | 重试不看有没有在跑的轮次，**无条件** `enqueue`：同一会话会有两轮并发（顺序被打破，网关里两个 agent 抢同一份上下文） | 只有 `queue_position == 0` 才入队，否则等 B-1 的推进 |
| B-3 | `worker/handlers/agent_turn.py::ingest_stream` | `httpx.Timeout(read=None)`：网关「TCP 还在但不再说话」时读会一直阻塞，直到 handler 1800s 超时被取消——那一轮就没人写终态，正好违反「绝不留永久 running」 | 读空闲上限 `STREAM_IDLE_TIMEOUT_S=90`（网关每 15s 一条 `: keep-alive`，90s 无字节即连接已死）；超时走 `AGENT_STREAM_LOST` 落 `failed` |
| B-4 | `services/agent_model.py::check` | 解析之后凭证可能刚好被停用/删除，`materialize()` 抛 `ApiError` → `POST /agent/config/test` 直接 500，把「凭证不可用」误报成平台故障 | 包成 `{ok:false, checked:false, problems:[...]}`；`secret` 为空同样给结论 |
| B-5 | `apps/web/.../settings/agent/page.tsx` | 保存新配置后，上一次「连通性测试通过」的徽标**留在页面上**（旧结论冒充当前配置的结论）；`温度 / max_tokens` 标成「当前生效」，但网关只读自己的 `AGENT_MODEL_MAX_TOKENS`，平台这两个值**根本不发送** | 保存成功即清空测试结果；两处文案改为「只保存不生效」 |
| B-6 | `docs/api/openapi.json` + `packages/contracts/src/api.d.ts` | 用 `> docs/api/openapi.json` 导出，cmd.exe 按 GBK 落盘：整份构建产物变成**非 UTF-8 + CRLF**（git 里 232 行全变），`openspec`/`openapi-typescript` 之类消费方按 UTF-8 读会直接抛错；而且只改了 `openapi.json`、没重新生成 `api.d.ts` → CI 的 `contracts diff` 作业必红 | 用 Python 自己以 `encoding='utf-8', newline='\n'` 写回，并抹平本地版本漂移（见 §13.5 的告示）；`npm run gen` 重新生成 `api.d.ts`。现在 `openapi.json` 的 `git diff` 只剩 **+25 行**（新路由 + 两处 docstring 变成 `description`），`api.d.ts` 只剩 **+55/−2** |

顺带的两条小修：`POST /agent/config/test` 里 `result["request_id"] = None` 是空操作（删）；
`validate_patch` 对 `max_test_calls_per_flow` / `allowed_window_hours` / `max_cost_usd_per_flow`
补了下界（负数没有意义，之前能存进去）。

新增回归 `apps/api/tests/test_agent_queue.py`（**11 例**）：`dequeue_next` 在轮次运行时不推进 /
按 ordinal 推进最老的一条 / 重编号剩余队列、`start_next` 真的入队、推进后排不进队列时
把轮次收成 `failed`（不留没人执行的 running）、worker 正常收尾后推进、模型未配置时也推进
（否则修好配置会被一条旧消息挡住）、停止后推进、重试在排队时**不**入队、
`check()` 对停用凭证给结论而不是抛异常、`validate_patch` 拒绝负数。

```text
cd apps/api && python -m pytest tests -q      → 285 passed（31 文件，原 274 + 11）
cd apps/api && python -m ruff check .         → All checks passed!
cd apps/api && python -m mypy skillrouter_api → Success: no issues found in 139 source files
cd apps/web && npx tsc --noEmit               → 0 error
cd apps/web && npx next lint                  → No ESLint warnings or errors
cd packages/contracts && npm run gen:check    → 检查通过（api.d.ts 已随 openapi.json 重新生成）
```

---

## 14. LLM 服务配置（四协议 · 实时连通 · 一键取模型）

对应契约：`docs/plans/llm-provider-config/spec.md` v2.0（§3.7 的改动前后对比、§7.2 的 E-01 / E-02）。
口径不变：**本节只登记真的跑过的命令与输出**，跑不了的一律进 `### 14.4` 的待验证表，
不因为「代码看起来对」升级成已验证。

| 项 | 值 |
|---|---|
| 记录时间 | 2026-10-20（文档与脚本节点） |
| 环境 | Windows 10.0.26200；Python 3.11.7；ruff 0.15.20；**无任何第三方 LLM 凭证** |
| 结论 | 文档 + `scripts/check_llm_protocols.py` **已交付并自检通过**；四协议矩阵（E-01 / E-02）**待 `api/admin/llm.py` 合并后重跑**（卡点见 14.2 的逐字输出） |

### 14.1 `credentials_verify` 的协议感知修复：改动前 / 改动后

同一张 **Anthropic 凭证**（`base_url = https://api.anthropic.com`、`auth_type = api_key`、
`meta.llm_protocol = anthropic`），点「检测」时发生了什么：

| 环节 | 改动前（当前代码事实） | 改动后（spec §3.7 规定行为） |
|---|---|---|
| 取模型地址 | `GET {base_url}/models` → `https://api.anthropic.com/models`（`services/credentials_verify.py:19` 的 `PROBES["llm"]`；`_probe_for()` 只做 `{base_url}` 字符串替换） | `GET {base_url}/v1/models?limit=1000` → `https://api.anthropic.com/v1/models?limit=1000`（`services/llm/protocols.build_models_request()`；`anthropic` 的 `strip_version_suffix="v1"` + `models_path="/v1/models"`） |
| 认证头 | 只有通用四形态（`api_key` → 固定头名），**没有** `anthropic-version` | `x-api-key: <secret>` + `anthropic-version: 2023-06-01`（`protocols.build_auth_headers()` 委托 `executors.auth` 后叠加协议级头） |
| 上游响应 | `404`（路径不存在） | `200 {"data":[…],"has_more":false,"last_id":"…"}` |
| 落库 / 结论 | `credential_verifications.ok = 0`，`detail_redacted` 记 404 → 页面「**无效**」 | `ok = 1` + `stage="auth"` + `model_count = N`，**不发计费请求** → 页面「**有效**」 |
| 计费 | 0 次 | 0 次（`mode=auth`） |

**这张表的证据等级（重要）**：

- 「改动前」两行是**代码事实**，逐字读自 `apps/api/skillrouter_api/services/credentials_verify.py:18-35`，
  不是推断；
- 「改动后」两行是 spec 的**规定行为**，不是本机观测结果；
- 本机**没有**任何真实 Anthropic key，所以「同一把钥匙 404 → 200」这条端到端对比**没有跑过**。
  它的必备证据是 `apps/api/tests/test_credentials_verify.py` 的 T-28（断言请求的是 `/v1/models`
  而不是 `/models`）——该用例未随本节点交付，状态 **待验证**。

### 14.2 `scripts/check_llm_protocols.py`：命令与输出形状

设计目标：**不依赖任何第三方凭证**也能证明「四协议各自打到正确的地址、带正确的认证头、
能解析模型清单、能完成一次生成」。做法（脚本头部有完整说明）：4 个
`http.server.ThreadingHTTPServer` 假上游（openai / anthropic / google / OpenAI 兼容的 custom 子路径网关）
+ 真实 uvicorn（临时 `SKILLROUTER_DATA_DIR` / `SKILLROUTER_DB_PATH`，不碰 `var/`）+ 真实管理端会话。

```bash
# E-01 全矩阵：4 协议 × 3 阶段（auth / completion / models），落 var/reports/llm-protocols.md
python scripts/check_llm_protocols.py

# E-02 变异：假上游一律回 401，断言结论是 UPSTREAM_AUTH 且 hint 提到「API key」
python scripts/check_llm_protocols.py --only anthropic --auth-401
```

#### 14.2.1 本轮实测（逐字输出）

```text
$ python scripts/check_llm_protocols.py --only openai
──────────────────────────────────────────────────────────────────────
 LLM 四协议矩阵校验
──────────────────────────────────────────────────────────────────────
 假上游 openai     http://127.0.0.1:61043
 假上游 anthropic  http://127.0.0.1:61044
 假上游 google     http://127.0.0.1:61045
 假上游 custom     http://127.0.0.1:61046/openai
 ✓ bootstrap 初始化（临时库 C:\Users\…\Temp\sr-llm-mexu8bsz）
 ✓ API 就绪 http://127.0.0.1:64165（真实 uvicorn 进程）
 ✓ 管理端登录（首次登录已自动完成强制改密）
 ✗ GET /api/admin/llm/protocols → HTTP 404 NOT_FOUND：检查标识是否正确
 ✗ 后端新端点还没就绪（`apps/api/skillrouter_api/api/admin/llm.py`），
   本脚本已按 spec 写好；等实现合并后直接重跑即可。

$ echo %errorlevel%
2
```

即：**假上游 → 真实 uvicorn → bootstrap → 登录 → CSRF → 探测端点** 这条链路的前半段已经通了，
卡点只有一个——`/api/admin/llm/*` 那批端点还没合入（`GET /api/admin/llm/protocols` 直接 404）。
脚本因此以退出码 **2**（环境不可用）收场，而不是把没跑的矩阵伪装成绿的。

静态检查（同一份文件，实跑）：

```text
python -c "import ast; ast.parse(open('scripts/check_llm_protocols.py',encoding='utf-8').read())"
                                                        → AST OK
python scripts/check_llm_protocols.py --help            → 打印 usage，退出码 0
python -m ruff check scripts/check_llm_protocols.py     → All checks passed!
python -m ruff format --check scripts/check_llm_protocols.py → 1 file already formatted
```

> **既有基线（不是本轮引入）**：`python -m ruff format --check scripts` 在今天这个仓库里本来就会报
> 13 个脚本「would reformat」——`scripts/` 下没有自己的 ruff 配置，ruff 按默认 88 列判定，
> 而现有脚本是 100 列风格。`make lint-py` 的第二步因此本来就对 `scripts/` 红。
> 本轮新增的脚本按**默认配置**格式化（88 列），所以它自己那一份是干净的；重排 13 个既有脚本
> 超出本轮范围，未动。

#### 14.2.2 假上游形状自检（14 项全过，临时脚本在 `%TEMP%`，不属于仓库）

在端点未就绪前先把「值得怀疑的部分」单独验一遍：假上游会不会因为自己写错而让矩阵变成假绿。

```text
$ python %TEMP%\check_fakes.py
 PASS openai GET /v1/models
 PASS openai 缺 Bearer → 401
 PASS openai POST chat
 PASS anthropic 第一页 has_more=true
 PASS anthropic 第二页 has_more=false
 PASS anthropic POST /v1/messages
 PASS anthropic 缺 max_tokens → 400
 PASS anthropic 缺 anthropic-version → 400
 PASS google GET /v1beta/models
 PASS google generateContent
 PASS google 缺 x-goog-api-key → 401
 PASS custom GET /openai/models
 PASS custom POST /openai/chat/completions
 PASS 变异模式 anthropic 回 401
ALL PASS
```

#### 14.2.3 判定逻辑自检（20 项全过，同样是 `%TEMP%` 里的临时脚本）

矩阵的「绿/红」由判定函数决定，所以先把它们拿 spec §3.4 / §5.3 的样例响应钉一遍
（含反例：判定函数不许把坏结果放过去）。摘录：

```text
 PASS auth 成功 → ok
 PASS 通过但零上游请求 → 抓假成功（INV-2）
 PASS 短路时 completion 行判失败并说明          # auth 失败 → completion「未执行」
 PASS 变异：200+ok=false+UPSTREAM_AUTH+hint(API key) → 通过
 PASS 变异：hint 不含 API key → 判失败          # E-02 的判据就是这句
 PASS 变异：401 却判通过 → 抓假成功
 PASS anthropic 未续页 → 判失败                 # 只请求 1 次即判红（T-19）
 PASS refresh 未绕过缓存 → 判失败
 PASS models 503 → 判失败并带状态码
 PASS 报告含 request_id / 通过计数 / 待验证段
ALL PASS
```

#### 14.2.4 期望的输出形状（**尚未实测**，不是结果）

端点就绪后，全矩阵会打印并落 12 行（4 协议 × 3 阶段），形状如下（`var/reports/llm-protocols.md`）：

| 协议 | 阶段 | 端点 | 结论 | HTTP | 模型数 | 上游请求 | 耗时 (ms) | request_id | 备注 |
|---|---|---|---|---|---|---|---|---|---|
| `openai` | `auth` | `POST /llm/test-connection` | ✓ | 200 | ≥1 | 1 | … | `…` | `resolved_url` = `…/v1/models` |
| `openai` | `completion` | `POST /llm/test-connection` | ✓ | 200 | — | 1 | … | `…` | `…/v1/chat/completions` |
| `openai` | `models` | `POST /llm/models` | ✓ | 200 | ≥1 | 1 | … | `…` | `source=upstream` |
| `anthropic` | `auth` | `POST /llm/test-connection` | ✓ | 200 | ≥1 | 2（`has_more` 续页） | … | `…` | `…/v1/models?limit=1000` |
| `anthropic` | `completion` | `POST /llm/test-connection` | ✓ | 200 | — | 1 | … | `…` | `…/v1/messages`（带 `max_tokens`） |
| `anthropic` | `models` | `POST /llm/models` | ✓ | 200 | ≥1 | ≥2 | … | `…` | 未续页即判失败（spec §3.5 / T-19） |
| `google` | `auth` | `POST /llm/test-connection` | ✓ | 200 | ≥1 | 1 | … | `…` | `…/v1beta/models?pageSize=1000` |
| `google` | `completion` | `POST /llm/test-connection` | ✓ | 200 | — | 1 | … | `…` | `…/v1beta/models/{model}:generateContent` |
| `google` | `models` | `POST /llm/models` | ✓ | 200 | ≥1 | 1 | … | `…` | `source=upstream` |
| `custom` | `auth` | `POST /llm/test-connection` | ✓ | 200 | ≥1 | 1 | … | `…` | `wire=openai` → `…/openai/models` |
| `custom` | `completion` | `POST /llm/test-connection` | ✓ | 200 | — | 1 | … | `…` | `…/openai/chat/completions` |
| `custom` | `models` | `POST /llm/models` | ✓ | 200 | ≥1 | 1 | … | `…` | `source=upstream` |

变异模式（E-02）只有 1 行（`--only anthropic`）：期望 `200 + ok:false`、`error_code=UPSTREAM_AUTH`、
`hint` 里出现「API key」。

> 上表是**契约形状**（每行该有什么、判定阈值是什么），不是本次结果。真跑绿之后要把逐行
> `request_id` 与耗时替换成实际值，并删掉这行说明。

### 14.3 本轮实际交付的文件

| 文件 | 动作 |
|---|---|
| `scripts/check_llm_protocols.py` | 新增（4 假上游 + 真实 uvicorn + 4×3 矩阵 + `--only` / `--auth-401` / `--mutate-auth` + Markdown 证据表 + 显式退出码 0/1/2） |
| `docs/api/examples.md` | 新增 §10：5 个端点的 curl / TS / JSON 示例（含一条 `ok:false` 失败样例） |
| `docs/runbook/troubleshooting.md` | 新增 §5.3（2 个新码）、§5.4（LLM 语境下的 5 个既有码）+ 三条取证命令 |
| `docs/runbook/verification-report.md` | 本节（§14） |
| `deploy/.env.example` | 新增 4 个变量（`SKILLROUTER_LLM_PROBE_TIMEOUT_S` / `_COMPLETION_TIMEOUT_S` / `_CATALOG_TTL_S` / `_PROBE_RATE_PER_MIN`，留空=默认 20 / 45 / 600 / 10） |
| `docs/README.md`、`README.md` | 文档地图各新增一行；`README.md` 的「核心概念」新增 `Protocol` / `Model Catalog` 两行 |

### 14.4 状态口径（已实现 vs 待验证）

> **更新（实现节点）**：本节 14.4 保留了文档/脚本节点当时的真实状态（后端尚未合并）。
> 后端与前端合并后的重跑证据在 **§14.5**；凡是 §14.5 已给出命令+输出的条目，
> 以 §14.5 为准，下表相应行已标注「→ §14.5」。

**已实现（本轮有命令与输出）**

| 项 | 证据 |
|---|---|
| `scripts/check_llm_protocols.py` 本体：4 个假上游、真实 uvicorn 启动、临时库 bootstrap、管理端登录/强制改密、Markdown 证据表、`--only` / `--auth-401`、退出码 0/1/2 | §14.2.1 的逐字输出（`--help` 退出 0；当时卡在新端点 404 → 退出码 2，符合设计） |
| 四种协议形状的假上游（anthropic `has_more` 分页、`max_tokens` 必填、`x-api-key` + `anthropic-version`、`x-goog-api-key`、Bearer） | §14.2.2 的 14 项自检全过 |
| 判定函数（成功 / 失败 / 短路 / 假成功 / 变异 hint / 未续页 / 缓存未绕过 / 503 / 报告渲染） | §14.2.3 的 20 项自检全过（用 spec §3.4 / §5.3 的样例响应） |
| 上表 §14.3 的 6 个文档改动 | 文件本身（`ruff` 与 Markdown 结构在 §14.2.1 有静态检查） |

**待验证（不许写成「已验证」）**

| 项 | 为什么 |
|---|---|
| ~~四协议 × 三阶段矩阵 12 行全绿（E-01）~~ | → §14.5.1：**已重跑，12/12 全绿** |
| ~~401 变异断言（E-02）~~ | → §14.5.2：**已重跑，1/1 符合预期** |
| ~~`credentials_verify` 改成协议感知（§14.1 的「改动后」）~~ | → §14.5.3：T-28 已交付并绿；真实 Anthropic key 仍无 |
| 真实上游凭证矩阵（Anthropic `/v1/models` 分页、Google `pageSize`、vLLM / One-API / LiteLLM） | 本机无第三方 key（与 spec §10 一致） |
| 多 worker 部署下的探测限流精度 | 限流是进程内 token bucket（前提：单 uvicorn 进程），无压测输出 |
| `custom + wire=anthropic` 的真实网关 | 本脚本的 custom 行是 OpenAI 兼容网关（`wire=openai`）；anthropic 兼容网关组合未验证 |
| 桌面端 18100 下的全流程 | 需出包后真机双击（`npm run smoke`），本轮无桌面端改动 |

### 14.5 实现节点补充证据（后端 + 前端合并后重跑）

| 项 | 值 |
|---|---|
| 记录时间 | E-01/E-02：`2026-10-20T09:48Z`（脚本报告头部自带 UTC 时间）；E-04/E-05：同日同一会话内（逐字输出见下） |
| 环境 | Windows 10.0.26200；Python 3.11.7；Node 22 + Next 15.5.7；Playwright 1.64.1 + **系统 Chrome**（`SR_E2E_CHANNEL=chrome`）；**无任何第三方 LLM 凭证** |

#### 14.5.1 E-01 四协议 × 三阶段矩阵（真实 uvicorn，12/12）

```text
$ python scripts/check_llm_protocols.py
 ✓ bootstrap 初始化（临时库 C:\Users\…\Temp\sr-llm-c6rkh1ti）
 ✓ API 就绪 http://127.0.0.1:52111（真实 uvicorn 进程）
 ✓ 管理端登录（首次登录已自动完成强制改密）
 ✓ GET /api/admin/llm/protocols 可达（协议注册表已实现）

 ✓ [openai]    auth       HTTP 200 · 2 个模型 · 上游 1 次 · 622ms · request_id=a9605b4517f24736
 ✓ [openai]    completion HTTP 200 · 上游 1 次 · 315ms · request_id=b9b31ec4a6f94eac
 ✓ [openai]    models     HTTP 200 · 1 个模型 · 上游 1 次 · 283ms · request_id=40409a3142134235
 ✓ [anthropic] auth       HTTP 200 · 2 个模型 · 上游 2 次 · 595ms · request_id=96b4f0b7de9545d7
 ✓ [anthropic] completion HTTP 200 · 上游 1 次 · 325ms · request_id=26588e4416d240ca
 ✓ [anthropic] models     HTTP 200 · 2 个模型 · 上游 2 次 · 600ms · request_id=17a776a17bbe4016
 ✓ [google]    auth       HTTP 200 · 2 个模型 · 上游 1 次 · 326ms · request_id=19a9ec8f59ca4955
 ✓ [google]    completion HTTP 200 · 上游 1 次 · 298ms · request_id=c767ea6997a342e5
 ✓ [google]    models     HTTP 200 · 1 个模型 · 上游 1 次 · 301ms · request_id=a25067b1131f4935
 ✓ [custom]    auth       HTTP 200 · 2 个模型 · 上游 1 次 · 264ms · request_id=c9ff1416ab2f4bd2
 ✓ [custom]    completion HTTP 200 · 上游 1 次 · 295ms · request_id=68a0077ca0f14f27
 ✓ [custom]    models     HTTP 200 · 1 个模型 · 上游 1 次 · 287ms · request_id=5f06538505e74a6b

 LLM 协议矩阵：12/12 行通过
 证据已写入 M:\takoAI\AragonSkillRouter\var\reports\llm-protocols.md
```

关键行解读（都是「地址拼对了吗」的直接证据）：

- `anthropic` 的 `resolved_url = http://…/v1/models?limit=1000` 且上游 2 次 → 走的是 `has_more` 续页；
- `google` 的 `resolved_url = …/v1beta/models?pageSize=1000`；
- `custom` 的 `resolved_url = …/openai/models`（子路径网关，wire=openai）。

#### 14.5.2 E-02 401 变异（true 上游拒绝）

```text
$ python scripts/check_llm_protocols.py --only openai --auth-401
 ✓ [openai] auth HTTP 200 · 上游 1 次 · 771ms · request_id=13768a0779074b24
   备注：UPSTREAM_AUTH · hint: 上游拒绝鉴权：检查 API key 是否属于该项目，或该 key 是否启用了对应 API
 变异断言：1/1 行符合预期（UPSTREAM_AUTH + hint 提到 API key）
```

注意 HTTP 是 **200** 而不是 5xx —— 这正是 spec §5.2 的约定：鉴权失败是「正常的坏结果」，
`200 + ok=false + error_code + hint`；只有请求本身不成立才走 4xx/5xx。

#### 14.5.3 `credentials_verify` 协议感知：T-28 与真实请求路径

```text
$ cd apps/api && python -m pytest tests/test_credentials_verify.py -q
............                                                             [100%]
```

`test_llm_credential_verify_uses_protocol_aware_endpoint` 断言（拿假 Anthropic 上游）：

- 请求 URL 以 `/v1/models?limit=1000` 结尾（**不是** `/models`）；
- 请求头带 `anthropic-version: 2023-06-01`；
- 响应 `ok=true`、`stage="auth"`、`model_count=1`、`protocol="anthropic"`。

即 §14.1 的「改动后」列已从「spec 规定」升级为「有断言守住的实现」。
**仍未验证**：真实 Anthropic key 的 404 → 200 对比（本机无 key）。

#### 14.5.4 E-04 前端联通（Next 反代 → FastAPI，含新端点）

```text
$ python scripts/check_web.py
 ✓ web /api/health
 ✓ 登录页渲染
 ✓ 强制改密页渲染
 ✓ Next.js /api 反代到 FastAPI（bootstrap）
 ✓ bootstrap 返回契约字段
 ✓ /api/v1/health 经反代可用
 ✓ 反代透传 X-Request-Id
 ✓ 反代透传 X-Frame-Options
 ✓ 未登录访问管理接口经反代返回 401

 前端联通性：9/9 项通过
```

#### 14.5.5 E-05 浏览器用例（Playwright + 系统 Chrome，5/5）

```text
$ npx playwright test e2e/llm-config.spec.ts        # SR_E2E_CHANNEL=chrome
Running 5 tests using 1 worker
  ok 1 … ① 切 4 个协议卡片：地址联动、方向键、切协议清空模型 (4.9s)
  ok 2 … ② 草稿密钥：测试连接出 auth 阶段，测试生成（计费 1 次）出 completion 阶段，且保存按钮变成跳凭证抽屉 (4.5s)
  ok 3 … ③ 获取模型 → 选择 → 保存 → 就绪徽标 (4.8s)
  ok 4 … ④ 失败态：整页只有 1 个 role="alert"，且文案含处置建议与 request_id (4.6s)
  ok 5 … data-theme="dark" 下 ①②③ 仍达标 (5.2s)
  5 passed (26.4s)
```

前提与限制（必须如实登记）：

- `npx playwright install chromium` 在本机**装不上**（`cdn.playwright.dev` TLS 被重置），
  所以走 `SR_E2E_CHANNEL=chrome` 用**系统 Chrome**；`playwright.config.ts` 早已支持这个兜底；
- 上游 LLM 调用由 `page.route()` 桩掉（用例不需要任何真实 key），但**登录、页面 SSR、
  Next 反代、`/api/admin/agent/config` 的真实形状**都是真的；
- 既有 4 个 spec 同时重跑（INV-4）：`5 passed / 4 skipped / 0 failed`
  （skip 的是既有的「需要真实模型/真实 Agent」用例，与基线口径一致）。

#### 14.5.6 后端全量回归

```text
$ cd apps/api && python -m pytest tests -q
372 passed
$ python -m ruff check apps/api scripts       → All checks passed!
$ cd apps/api && python -m mypy skillrouter_api → Success: no issues found in 147 source files
```

基线 285 → **372**（新增 87 条），DoD 门槛 `≥343` 满足。

> `python -m ruff format --check apps/api` 在**本仓库基线上本来就不绿**（109 个既有文件
> 「would reformat」）。本轮只保证**新增文件**格式干净（`ruff format` 已对
> `services/llm/*`、`schemas/llm.py`、`models/llm.py`、`api/admin/llm.py`、`tests/test_llm_*.py`、
> `tests/fixtures/fake_upstream.py` 跑过），未重排既有文件——见 spec
> `## 实施过程发现的方案缺陷` 的 D-6。

---

## 15. 终审复审节点（review / commit，round 1）

| 项 | 值 |
|---|---|
| 记录时间 | 2026-10-20（复审节点，同一台机器、同一份工作副本） |
| 环境 | Windows 10.0.26200；Python 3.11.7；Node 22 + Next 15.5.7；ruff 0.15.20；mypy（现有非 strict 配置）；**无任何第三方 LLM 凭证** |
| 结论 | 覆盖度对齐 spec §4 的三张清单（22 个新增 + 29 个改动，逐个核对存在性，**0 缺失**），外加 2 个清单外但必需的改动（`api/admin/providers.py` 挂 §5.4 的路由、`tests/conftest.py` 的夹具熵，见 spec D-13）；DoD 里的 `grep` 项逐条复核通过（`apps/web` 无 `if protocol ==`；`services/llm/**` 无自造认证头、`trust_env` 只出现在 `netpolicy` 判据之后；`credential_refs[` 只允许 `wire` 下标）；发现并**当场修掉** 1 个 P0（「起不来」级）+ 5 个 P1 + 13 个 P2（共 19 条，见 15.1），随后全门重跑绿（15.2） |

### 15.1 复审发现并已修复的问题

| # | 严重度 | 位置 | 问题（用户 / 维护者可见后果） | 修法 |
|---|---|---|---|---|
| R-1 | **P0（启动失败）** | `deploy/.env.example` | 四个 `SKILLROUTER_LLM_*` **数值**变量留空，注释还写「留空即用默认值」。实测 `SKILLROUTER_LLM_PROBE_TIMEOUT_S=` → `ValidationError: Input should be a valid number … input_value=''`，API / Worker **起不来**（`settings.py` 没有 `env_ignore_empty`） | 模板直接写默认值 `20 / 45 / 600 / 10`，并加一行说明「数值变量不能留空」；登记为 spec D-10 |
| R-2 | P1 | `providers/[id]/page.tsx` + `components/llm/useLlmDraft.ts` | 抽屉里采集的 `wire` / 路径覆盖**从不落库**（`meta` 只写 `llm_protocol` + `base_url_hint`），探测请求也带不上（`buildProbeTarget` 只认 `pathsModels`，RHF 给的是 `paths_models`）。后果：`custom` 配的线协议与路径覆盖永远不生效 | 落库 `meta.llm_wire` / `meta.llm_paths`（后端 `identity` 读的就是这两个键），并让 `buildProbeTarget` 认 snake_case |
| R-3 | P1 | `providers/[id]/credentials/[cid]/page.tsx` | 「重新探测」读 `http_status`，后端返回 `status_code` → HTTP 列恒为「—」（鉴权失败这种最需要看状态码的场景）；阶段时间线读 `stages`，该端点从不返回 → 死代码 | 前端改读 `status_code`；后端 `_verify_llm()` 增补 `stages`（只增不改），时间线变成真数据；登记为 spec D-12 |
| R-4 | P1 | `apps/web/e2e/llm-config.spec.ts` + `settings/agent/page.tsx` | 页面 ③ 的外层 `section` 与 `ConnectionTester` 内部**同名** `region[实时验证]`，Playwright 的 `name` 是子串匹配 → 解析成 2 个节点，`tester()` 一调用就 strict mode violation，E-05 的 4 条用例全红 | 去掉页面外层的重复 `aria-label`（同名 region 本来也没有语义收益） |
| R-5 | P1 | `components/llm/ModelPicker.tsx` | 输入即写父级：已选 `gpt-4o-mini` → 打 `claude` 想搜一下 → 按 Esc 放弃 → `values.model` 已被静默改成 `claude`，点保存就 PATCH 上去 | `close(commit)`：点选 / Enter / 失焦才提交，**Esc 丢弃**；输入只改本地草稿 |
| R-6 | P1 | `api/admin/agent_chat.py::test_config` | docstring 写着支持可选 body `protocol/model/mode`，代码把 `mode` 收下就**静默忽略**（白名单外的键同理）→ 调用方以为生效 | 白名单校验（`protocol/model/mode`），`mode != completion` 明确 400 并指向 `/api/admin/llm/test-connection` |
| R-7 | P2 | `tests/fixtures/fake_upstream.py` | CLI 的 `--models` / `--reject-auth` 收进 `argparse` 后没传给服务实例：`--reject-auth` 会**照常回 200** —— 一个专门用来证明「鉴权失败」的开关静默失效 | `_serve(..., model_ids=, reject_auth=)` 接通；命令补 `cd apps/api`（spec D-11） |
| R-8 | P2 | `scripts/check_llm_protocols.py` | ① 变异 / `--only` 运行会**覆盖**全矩阵报告；② completion 行的备注写的是「取清单地址」而不是上游真正收到的请求（等于没证明生成地址）；③ 响应里缺该阶段时只跳过断言、仍判绿；④ 临时数据目录从不清理；⑤ 没有「报告不含草稿密钥」的自检 | 报告名带模式后缀；备注改印 `GET /v1/models…` / `POST /v1/messages` / `:generateContent`（新增 `Row.upstream_paths`）；缺阶段判失败；成功后 `rmtree`（`--keep-data` 可留）；落盘前断言 `DRAFT_SECRET` 不出现 |
| R-9 | P2 | `settings/agent/page.tsx` + 凭证抽屉 | 新建凭证后只失效 `llm.all`，而设置页凭证下拉的前缀是 `credentials` → 「加完凭证回设置页看不到」 | 抽屉成功后同时失效 `queryKeys.credentials.all` |
| R-10 | P2 | `settings/agent/page.tsx` | 「值指纹」漏了额外请求头 / 查询参数 / api-version / max_tokens —— 改了这几项，旧的「测试通过」结论不失效（假绿） | 四项进指纹 |
| R-11 | P2 | `settings/agent/page.tsx` | 凭证下拉不按 `kind` 过滤，图 / 视频 / 语音凭证混进「模型服务」 | 请求改带 `kind=llm` |
| R-12 | P2 | `settings/agent/page.tsx` + 凭证抽屉 | 「去创建凭证并保存」只带 `protocol`，用户填过的地址丢掉 | 带上 `base_url`，抽屉据此预填 |
| R-13 | P2 | `services/agent_model.py` | 「`wire` / `paths` 只对 custom 生效」的判断写了两遍；错误文案 `max_cost_per_flow` 与字段名 `max_cost_usd_per_flow` 不一致；函数内 `import re` | 提取 `_protocol_in_effect()`；文案与字段名对齐；`re` 提到模块顶部 |
| R-14 | P2 | 多个文件 | 换行符与空白：`credentials_verify.py` / `docs/api/openapi.json` / `db/migrations/0001_init.sql` / `tests/conftest.py` / 凭证详情页是 **CRLF**（`.editorconfig` 要求 LF）；`settings.py` 有行尾空格；若干新代码行 >100 列 | 全部归一成 LF / 去行尾空白 / 拆长行（只动本轮新增与改动行，不重排既有文件） |
| R-15 | P2 | `schemas/llm.py` | 模块 docstring 自称「**不 import 任何 service**」，实际 import 了协议注册表的常量 | 说明改为「只 import 纯数据的注册表常量（`AUTH_STYLES` / `WIRE_KEYS`），不 import 任何有 I/O 的 service」 |
| R-16 | P2 | `docs/api/examples.md` | `SSRF_BLOCKED` 一行说「命中 `SKILLROUTER_SSRF_DENY`」，与 `netpolicy.validate_admin_target` 的实现和 `troubleshooting.md`（明确说它管抓取侧、不管这里）冲突 | 改为「`netpolicy` 硬编码拒绝云元数据；**不读** `SKILLROUTER_SSRF_DENY`」 |
| R-17 | P2 | `README.md` | 「本轮实测」表停在上一轮（285 passed / 49 表 56 索引 / `user_version=2` / 合计 386 / `OpenAPI-118 paths`），与 15.2 的实测不一致 | 更新为 372 passed（35 文件）/ 51 表 60 索引 / `user_version=3` / 合计 473 / 123 paths；Playwright 行只认实测口径（11 + 本轮新文件另见 §14.5.5） |
| R-18 | P2 | `docs/plans/llm-provider-config/spec.md` | §4.4 的「留空即用默认值」、§7.3 的命令 CWD、§5.7 的响应清单三处与实测不符 | 登记为 D-10 / D-11 / D-12（正文同步改成可照做的写法） |
| R-19 | P2 | `packages/contracts/src/api.d.ts` | 本轮新增 5 条管理端路由后，生成的契约文件没重新生成：`npm run gen:check` 实测「与 OpenAPI 生成结果不一致」，CI 的 `contracts diff` 作业必红 | `cd packages/contracts && npm run gen` 重新生成（`7.13.0` = `package-lock` 锁定版本，与 CI 一致）；`gen:check` 与 `typecheck` 均通过。spec §4.4「明确不改 `packages/**`」指的是不手改，生成物必须跟着 OpenAPI 走（见 D-14） |

### 15.2 重跑证据（本节点，命令即证据）

```text
$ cd apps/api && python -m pytest tests
372 passed in 111.55s (0:01:51)

$ python -m ruff check apps/api scripts
All checks passed!

$ cd apps/api && python -m mypy skillrouter_api
Success: no issues found in 147 source files

$ cd apps/web && npx tsc --noEmit
（无输出 = 0 错误）

$ cd apps/web && npx next lint
✔ No ESLint warnings or errors

$ cd apps/web && npm run build
✓ Compiled successfully（路由表含 /settings/agent 15.5 kB、/providers/[id] 11.8 kB）

$ python scripts/check_llm_protocols.py
LLM 协议矩阵：12/12 行通过
 证据已写入 M:\takoAI\AragonSkillRouter\var\reports\llm-protocols.md

$ cd packages/contracts && npm run gen:check
[contracts:gen] 检查通过：packages/contracts/src/api.d.ts 与生成结果一致
$ cd packages/contracts && npm run typecheck
（无输出 = 0 错误）

# R-1 的复现（修复前）：空数值变量让进程起不来
$ set SKILLROUTER_LLM_PROBE_TIMEOUT_S= && cd apps/api && python -c "from skillrouter_api.settings import get_settings; get_settings()"
ValidationError: 1 validation error for Settings
llm_probe_timeout_s
  Input should be a valid number, unable to parse string as a number [input_value='']
```

12 行矩阵（本轮重跑，备注列已改为**上游真正收到的请求**，因此每行的地址拼接都有证据）：

| 协议 | auth | completion | models |
|---|---|---|---|
| `openai` | ✓ `GET /v1/models` | ✓ `POST /v1/chat/completions` | ✓ `source=upstream` |
| `anthropic` | ✓ `GET /v1/models?limit=1000` + 续页 `after_id` | ✓ `POST /v1/messages` | ✓ 2 次上游（分页） |
| `google` | ✓ `GET /v1beta/models?pageSize=1000` | ✓ `POST /v1beta/models/{model}:generateContent` | ✓ `source=upstream` |
| `custom`(wire=openai) | ✓ `GET /openai/models` | ✓ `POST /openai/chat/completions` | ✓ `source=upstream` |

### 15.3 仍未验证（沿用 §14.4 与 spec §10，**不因本轮重跑而升级**）

- 真实第三方上游矩阵（Anthropic `/v1/models` 真实分页、Google `pageSize` 与配额、vLLM / One-API / LiteLLM、`custom + wire=anthropic`）—— 本机无 key；
- 多副本部署下的探测限流精度 —— 进程内 token bucket；
- 桌面端 18100 下的全流程 —— 需出包后真机双击；
- Playwright **自带 chromium** 通路 —— 本机 `cdn.playwright.dev` TLS 被重置，E-05 走系统 Chrome（§14.5.5）。



