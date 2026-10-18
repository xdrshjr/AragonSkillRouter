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
