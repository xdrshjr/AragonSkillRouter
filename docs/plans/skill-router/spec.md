# Skill Router 平台设计文档（spec）

| 项目 | 内容 |
|---|---|
| 文档 | `docs/plans/skill-router/spec.md` |
| 版本 | **v4.0**（= v2.0 实现契约 + §A 迭代 2 增量契约（经 §R2 评审修正）；v1.0 → v2.0 见 §0，v2.0 → v3.0 见 §A，v3.0 → v4.0 见 §R2） |
| 状态 | §1–§18 已实现（第 1 轮迭代，commit `1b5e39d`）；**本轮（迭代 2/2）实现范围 = §A（评审修正后版本）** |
| 编写时间 | 2026-10-18（第 1 轮迭代）；§A 编写于 2026-10-19（第 2 轮迭代） |
| 评审时间 | 2026-10-18（v1.0 设计评审：P0×3、P1×15、P2×8，23 项修正 / 3 项记录在案）；2026-10-19（实现审计：**P0×12、P1×14、P2×6**，见 §A）；2026-10-19（迭代 2 设计评审：**P0×10、P1×17、P2×8**，见 §R2） |
| 产品名 | Skill Router |
| 代号 slug | `skill-router` |
| 上游引擎 | AragonAgent，`github.com/xdrshjr/AragonAgent`，MIT |
| 上游锁定 | git `main` @ `0a119a64890717b0e3017edbaaf00944babf19a8`（tag `core-v0.2.34` / `cli-v0.6.22`） |
| 上游依赖 | `@aragon-agent/core` **0.2.34**（精确 pin），`@aragon-agent/cli` **0.6.22**（仅用于一致性校验） |
| 关联文档 | `docs/plans/skill-router/aragonagent-upstream.md`（上游事实与能力边界记录） |
| 本轮唯一实现范围 | **§A 迭代 2 增量契约**（§1–§18 为实现现状参照；改动只能落在 §A 允许的范围内） |
| 附件 | [`iteration-2-audit.md`](iteration-2-audit.md)（审计证据：每条缺陷的 file:line 与复核状态） |

> 本文档是**实现契约**：下游工程师不需要再做任何产品/技术决策，只需按 §7 的文件清单与 §8、§9、§10 的接口与表结构逐行实现。所有标识符（表名、字段名、路由、事件名、环境变量）在实现时必须与本文一致；如需改动，先改本文。

---

## R2. 评审记录（Review Notes，v3.0 → v4.0）

评审对象是 §A「迭代 2 增量契约」（v3.0）。评审方法与 §0 一致：逐节核对，四个维度——**可行性**（当前镜像/栈/数据能否实现）、**完整性**（边界、错误路径、上线风险是否覆盖）、**一致性**（是否与 §1–§18 的既有契约、与仓库现存实现冲突）、**规模合理性**（过度设计 / 描述不足）。
一致性基准：仓库无 `CLAUDE.md` / `AGENTS.md`（已按 `README.md` 与 `docs/README.md` 的索引定位），故以 §5/§6 的栈与编码约定、`docs/README.md` 的「状态只有四种」纪律、以及**现有源码事实**为准。差别在于：§0 评审的是一份纯设计，本轮评审的是一份**改已交付代码**的增量契约——所以每条修法都必须在源码里能落地，凡是「文档这么说、代码不是这样」的都算缺陷。
级别定义沿用 §0：**P0** 不修正则本轮交付无法实现或必然出错；**P1** 不修正则实现歧义、契约不一致或验收无法核对；**P2** 可改进项。

处置原则：**P0 与 P1 全部在 §A 正文直接修正**（下表「处置」列给出修复位置与断言编号）；P2 仅记录在案，除标注「顺手」者外不改动。

> **版本标记说明**：被评审文档已是 v3.0，故本次评审把版本推进到 **v4.0**。任务模板里的「bump 到 v2」对应「推进到被评审版本的下一个版本」；`v2` 这一格已在 §0（v1.0 → v2.0）用掉，重复使用会让版本号回退，下游无法判断哪一版才是当前契约。

### R2.1 P0（10 项，全部已修）

| 编号 | 维度 | 问题（证据） | 处置（v4.0 修复位置） |
|---|---|---|---|
| R2-01 | 可行性 | **升级流水线没有执行环境。** `api`/`worker` 镜像只装了 `ca-certificates`（`deploy/Dockerfile.api:29-31`，全文件无 node/npm/git），而隔离构建要 `git clone` / `npm ci` / `npm run build`（`upgrade/build.py:50-84`）、导出清单校验要跑 node 脚本（`upgrade/verify.py:41`）。§A.3.7 又规定「无执行环境 → `blocked`」，于是**任何部署形态下验证都恒为 blocked**，`policy=auto` 永不生效——需求「按管理员设置的升级策略自动生效」直接落空。 | §A.3.7 新增「工具链归属」：构建/验证在**网关容器**执行（`node:22-slim` + 新增 `git`），Python 只做编排；新增 `POST /internal/admin/build`、`POST /internal/admin/verify-artifact`；`SKILLROUTER_ENGINE_TOOLCHAIN=gateway\|local` 供本地开发；§A.4.1/A.4.4 补文件；断言 T-41 |
| R2-02 | 可行性 | **`0003` 迁移在 `create_all` 建出的库上必然失败。** `0001_init`/`0002_engine_upgrade` 用 `Base.metadata.create_all(checkfirst=True)` 建表（`0001_init.py:47-49`、`0002_engine_upgrade.py:36`），而本轮模型元数据已含 6 个新列；`alembic upgrade head` 先跑 0001（建出含新列的表）再跑 0003 的 `ALTER TABLE … ADD COLUMN` → SQLite `duplicate column name` → **升级即不可启动**。 | §A.6.1 的 `0003` 改为「按 `PRAGMA table_info` 判决的条件加列」helper（幂等），`0002` 的守卫写法为范本；§A.3.8 写明「迁移必须可在 create_all 库与空库上各跑两遍」；断言 T-42 |
| R2-03 | 一致性 | **成功率的聚合键与实际写入值不一致。** `call_logs.status` 写入的是 `success`（`services/execution.py:384`：`"success" if result.status == "succeeded" else …`），而 §A.3.1/§A.7.1 T-02 用 `SUM(status='succeeded')` → `success_rate_24h` 恒为 `0` 而不是真实值，F-02 修完仍显示 0%，且与「从未调用显示 `null`」的语义混在一起。 | §A.3.1/A.3.5 定义唯一映射表 `LOGGED_STATUS`（对外 `succeeded`→落库 `success`），聚合与前端共用；§A.6.2 新增不变式 I7；T-02 改断言键并新增 T-43 |
| R2-04 | 一致性 | **`test_runs[].ok` 是布尔，文档按整数判定。** `get_capability()` 返回 `"ok": bool(t.ok)`（`services/capabilities.py:603`），而 §A.3.1 要求前端按 `ok === 1` 派生 `testedOk` → JS 严格相等恒 false → **发布按钮永久禁用，F-01 以另一种形式原样复发**。 | §A.3.1 改为 `ok === true`；A.4.3 要求 `test_runs` 进 `Capability` 类型；T-01 增「`test_runs[].ok` 必须是 JSON boolean」断言 |
| R2-05 | 可行性 | **状态机缺边。** `ALLOWED["queued"]` 只有 `{running, canceled, failed}`（`services/tasks.py:17-25`），而 §A.3.6 要求「提交前取消 → `queued` 转 `canceling`」→ 该路径抛 `INVALID_STATE_TRANSITION`（500），正是 F-07 的现场。 | §A.3.6 新增「状态机增量」表（`queued→canceling`、`canceling` 的判定顺序）；§10.2 不变式 4 同步；T-44 |
| R2-06 | 一致性 | **记账表与作用域都对不上。** §A.3.4 与 T-07 说「`rate_counters.cost_usd` 单调增」，但 `RateCounter` 只有 `calls/concurrent`（`models/access.py:44-62`），费用累计在 `quota_counters.cost_usd`（`:84-95`，`security/ratelimit.py::record_cost` 已在写它）；作用域枚举现状是 `token/capability/provider/global`（`services/execution.py:128-141`），§A 写成 `token/agent/provider/global`。 | §A.3.4 重写（`quota_counters` 日/月双 period；新增 `agent` 作用域作为第 2 层，插在 token 之后）；§A.5/A.6 同步枚举；T-07/T-45 |
| R2-07 | 完整性 | **「无价目 → `cost=0`」的前提是错的。** `usage.unit_price()` 有 `DEFAULT_PRICES` 兜底（`services/usage.py:17-23, 36-46`），现状就按默认价计费；因此 R2 的「只在有价目时计费，不会扰动存量」是错误的风险评估，而 T-08「无价目 → cost=0」永远不可能绿。 | §A.3.4 重写定价阶梯（显式价 → `price_book` → `DEFAULT_PRICES`（`source='estimated'`）→ 都没有才 `cost=0/source='unknown'`）；§A.8 R2 重写为「本轮起流式/异步开始计费，属于**发布前必须告知**的行为变更」；T-08 改断言 |
| R2-08 | 一致性 | **签名 URL 已存在，文档另造一套。** `services/files.py:39, 149-175` 已有 `SIGN_TTL_S=600`、`_sign()`、`signed_url()`、`verify_signature()`，`api/v1/files.py:42-56` 也已接受 `sig+exp`；真正的缺陷只是 `download` 上挂了 `Depends(require_token)`（`:44`），无令牌的请求根本到不了路由。§A 却要求新增 `files.sign()` + `b"file-url-v1"` 派生 → 上传给出的 URL 与下载校验算法不同，**免令牌下载永远 401**。 | §A.3.10 改为「复用既有 `signed_url()/verify_signature()`，只把鉴权改成可选令牌 + 签名优先」；新增 `deps.optional_token`；`save_bytes` 返回值复用 `describe()` 的字段；T-31 收紧紧为「同一签名既能上传响应给出、也能无令牌下载」 |
| R2-09 | 完整性 | **引擎指针的环境变量在网关侧不存在。** §A.3.7 让网关启动时读 `ENGINE_RUNTIME_DIR/current.json`，但 `agents/aragon-gateway/src/config.ts:209` 只定义了 `AGENT_ENGINE_HOME_ROOT`，`deploy/docker-compose.yml:134` 也只给了它；Python 侧 `settings.py:162-163` 只有 `engine_runtime_dir`，没有 home root。按文档实现会出现「Python 写到 A，网关从 B 读」，F-10「重启后指针丢失」照旧。 | §A.3.7 明确定义 `AGENT_ENGINE_RUNTIME_DIR`（网关读指针）与 Python 新字段 `agent_engine_home_root`（默认 `<data>/aragon-home/engines`），两者都进 `deploy/.env.example` 与 compose；断言 T-20 增「指针来源回显 `pin_source`」 |
| R2-10 | 一致性 | **内部端点路径写错。** 网关只有 `POST /internal/admin/reload`（`agents/aragon-gateway/src/main.ts:280`）与 `GET /healthz`（`:143`），没有 `/internal/engine/reload`，也没有 `/internal/engine/status`；`upgrade/switch.py:94` 经适配层调的就是前者。按 §A.5.3 实现会两端改不一致 → 切换必然失败。 | §A.5.3 按现状重写：`POST /internal/admin/reload` 只增字段（`engine_home`、回显 `pin_source`/`previous_version`），状态信息走 `/healthz` 而不是新造端点；§A.4.4 补 `main.ts`/`health.ts` 行 |

### R2.2 P1（17 项，全部已修）

| 编号 | 维度 | 问题（证据） | 处置（v4.0 修复位置） |
|---|---|---|---|
| R2-11 | 一致性 | §A.3.1 说「现状断在两处：返回体形状与通过判定」，但后端 `test_capability()` 早已返回冻结后的形状（`services/capabilities.py:807-821`，键集合与 §A 完全一致），断的只有前端类型（`apps/web/lib/types.ts:302-317`）。按原文改后端等于白改，还会推翻一个正确实现。 | §A.3.1 现状段改写为「后端已是目标形状，前端类型与判定仍旧」，A.2.1 F-01 的修法列同步 |
| R2-12 | 一致性 | 函数名与源码不符：全文写 `run_test()`，实际是 `services/capabilities.py::test_capability()`（`:724`）；`_brief()` 在（`:507`），但 `list_capabilities()` 在（`:545`）。契约里的标识符必须是可 grep 的真名。 | §A.3.1/A.3.4/A.4.1 全部改为真实函数名；§A.3.1 开头新增「本节标识符以源码为准（grep 不到即文档错）」一句 |
| R2-13 | 完整性 | 凭证 meta 白名单是「凭想象列的」：`(provider_slug, model, region, notes, base_url_hint)` 中 `model/region/notes/base_url_hint` **全仓无人写入**，而真实存在的 `probe_path`/`probe_method`（`services/credentials_verify.py:30,33`）、`header_name`/`username`（`executors/auth.py:15,22`）、`mock`（`scripts/seed_demo.py:185`）会被白名单**静默丢弃**——而 A.2.1 F-03 的验收断言恰恰要求「元数据不再外泄但服务端仍可用」，丢弃会让排障信息消失。 | §A.3.2 白名单改为「真实键 + 预留键」显式集合，并补一条断言：`GET /credentials` 仍能看到 `probe_path`；同时明确「投影只影响输出，`materialize()`/`auth.py` 读原始 meta」 |
| R2-14 | 一致性 | §A.3.2 说「`GET /credentials*`、`GET /providers/{id}`、`/impact` 三个投影都原样透 JSON 列」。事实只有 `_brief()` 透（`services/credentials.py:30-47`）：`providers.py:101-114` 已用 `secret_mask` 且不带 `meta`，`impact` 只回 `{id,name,masked,status}`（`credentials.py:278-286`）。夸大缺陷范围会引导实现者去改两个本来就对的地方。 | §A.3.2 首段收窄为「只有 `_brief()`/详情透 meta」，另两个投影只需补字段名一致性 |
| R2-15 | 完整性 | F-02 的症状写「成功率 / 上次测试两列永远是『—』」，但能力列表**没有「上次测试」列**（`apps/web/app/(app)/capabilities/page.tsx:54-145` 只有成功率列），没有列就不会「永远显示 —」。照原文修不出可见变化，验收也无法目视核对。 | §A.3.1/§A.4.3 明确「先新增『上次测试』列（`last_test_ok` + 相对时间），再让它接真字段」；手测清单第 1 项补该列 |
| R2-16 | 一致性 | §A.3.11 让「`AppShell` 导航『提供商与凭证』组新增『凭证』项」，但导航是**扁平列表**（`apps/web/components/common/AppShell.tsx:56-103`），根本没有分组；照原文改会顺手做一次导航重构成隐藏范围。 | §A.3.11 改为「在『提供商』项之后新增顶层项『凭证』（`/credentials`）」，并明确「本轮不引入导航分组」 |
| R2-17 | 规模 / 完整性 | §A.4 的数量与实际行数不符：A.4.1 声称 27 个文件、实为 **37 行**（其中一行含两个 migration 文件）；A.4.2 声称「8 个新增 + 5 个改写」、实为 8 + **6**；A.4.3 13 ✓；A.4.4 声称 12、实为 **14 行**（且有一行用 `{a,b,c}.md` 折叠了 3 个文件）。数量错会让「复审按表核对 diff」这条纪律失效。 | §A.4 四张表重标数量并把折叠行拆开（评审后因补充缺失文件与新增 `test_upgrade_toolchain.py`，最终为 A.4.1 **43 行**、A.4.2 **9+6**、A.4.3 13、A.4.4 **23 行**）；表头写「表内即全部范围」 |
| R2-18 | 完整性 | 文件计划缺 8 个**必须改**的文件：`api/v1/tasks.py`（改 202 状态码）、`agents/aragon-gateway/src/main.ts`+`health.ts`（新端点与 `pin_source`）、`apps/api/.../settings.py` 的 `agent_engine_home_root`、`models/execution.py`（`needs_reconcile` 列）、`deploy/Dockerfile.gateway`（加 git）、`deploy/docker-compose.yml`（新变量）、`docs/skills/connect.md`（退出码表）、`services/overview.py`（I6 计数）。§A.4 首句却是「不在表内的文件不许动」→ 自相矛盾。 | §A.4.1/A.4.4 补齐 8 行；A.4 首句改为「表内即全部改动范围，表外改动需先改本表」 |
| R2-19 | 一致性 | `engine_upgrade_runs.result` 的枚举是 `success\|failed\|aborted`（§10.1），§A 多处把「验证三态 `passed\|blocked\|failed`」与「运行结果」混写（如 A.3.7-3 写 `result='failed'`、A.6.2 I5 写 `verify_status`）。两者是不同列，混写会让 UI 与断言各取一个。 | §A.6.1 明确两列语义（`result`＝运行结果，`verify_status`＝验证结论），A.3.7/A.6.2 分别用；§A.5.2 `GET /engine/status` 字段名同步 |
| R2-20 | 一致性 | §A.3.6 依赖 `queued→canceling`，而 §10.2 不变式 4 只允许 `queued→running→(…)`、`canceling→canceled\|status_unknown`——正文与不变式互相矛盾，实现时以哪个为准没有写。 | §10.2 不变式 4 改写为完整状态机（含 `queued→canceling`、`canceling→canceled/failed/status_unknown`），并注明「唯一实现入口 `services/tasks.py::transition()`」 |
| R2-21 | 完整性 | 「§1–§18 哪些段落在本轮作废」没有交代。§9.2 的 `cancel_scope` 只有 2 值（`:1847`）、§9.3 的 invoke 响应没有 `status_unknown`/`needs_reconcile`/`signed_url`、§10.1 的 `rate_counters`/`idempotency_keys`/`engine_candidates`（枚举里仍有 `skipped`）都要改，但 §A 只说「以本节为准」。下游读 §9 写代码会直接写错。 | 新增 **§A.10 与 §1–§18 的同步变更清单**（逐节列出要改的段落与改后取值），并在 §9.2/§9.3/§10.1/§10.2/§12.4 的对应位置加「（本轮由 §A.10 覆盖）」指针 |
| R2-22 | 可行性 | 幂等占位的**事务边界**没写清：现有 `_idempotency_lookup`/`_idempotency_store` 都是独立 `db_write()`（`services/execution.py:58-119`），占位若在调用方事务里 `INSERT` 后抛 `IntegrityError`，整个会话会被回滚，随后的「已有记录」查询会落在脏会话上。§A.3.5 只给了伪代码。 | §A.3.5 增「事务与冲突处理」三条：占位用独立事务、`IntegrityError` 后**新开**会话重查、完成态用 `UPDATE … WHERE state='in_flight'`（影响行数 0 说明被判过期，记一次 `idem_reservation_lost`） |
| R2-23 | 完整性 | 取消路径缺两类分支：①`submit()` 抛错时任务该落哪个终态（现在 `capability_invoke.py:47` 直接 `finish(failed)`，若此前已置 `canceling`，`ALLOWED["canceling"]` 不含 `failed` 之外的合法路径）；②`canceling` 且**有** upstream id 的悬挂（worker 崩在追取消途中）无人恢复——§A.3.6 只处理了「无 upstream id 且超 24h」。 | §A.3.6 补「提交失败」「悬挂 canceling（有 upstream id）」两条规则，超时阈值 24h → 30min 且可配（`SKILLROUTER_CANCEL_RECONCILE_MINUTES`）；T-13 扩为扫描三类行 |
| R2-24 | 完整性 | `in_flight` 的过期回收只写在风险 R4 里，没有落成设计（谁回收、多久一次、断言在哪），实现时最可能被跳过，留下「同键永久 409」。 | §A.3.5 落为设计：命中 `in_flight` 且 `expires_at < now` → 视为可重新占位（并发安全由唯一索引 + 条件 UPDATE 保证）；`worker/handlers/retention.py` 每小时清理过期行；断言 T-11 增一条「过期占位可被重新占用」 |
| R2-25 | 完整性 | §A.3.4 只写「异步提交不记账，只释放并发额度」——`submit_async` 走的 `_quota_gate` 会**先加**四层并发计数（`services/execution.py:122-141`），但终态释放的责任人没写；不释放就是并发额度泄漏，几小时后全平台 `429 QUOTA_EXCEEDED`。 | §A.3.4 表内「异步终态」行补「四层 `release_concurrency`（与提交侧对称，幂等：只释放一次）」，并给断言 T-45b |
| R2-26 | 完整性 | CI 的 `migrations` 作业只有命令没有骨架：`python -m alembic` 需要工作目录 `apps/api`、`requirements-dev.txt` 安装、`SKILLROUTER_*` 环境（`SKILLROUTER_ENV`/`SKILLROUTER_DATA_DIR`），否则作业第一次跑就红，F-21 会变成「CI 长期红」。 | §A.3.12 给出作业骨架（checkout / setup-python / pip install / env / 三步 alembic + 子集 pytest），并写明「本机等价命令」已在 A.7.2 |
| R2-27 | 完整性 | 测试计数口径不可核对：A.7.1 有 32 条 pytest 断言、A.4.2 说 8 新 + 6 改、A.7.4 说「≥197+32」。实际新增用例函数是 **34** 个（含 T-31 两个函数、auth_flow 一条），按原文核对必然对不上。 | §A.7.1 末尾给出「新增用例 ≥34 → 总数 ≥231」的算法与逐文件清单；A.7.4 引用同一数字 |

### R2.3 P2（8 项：3 项顺手改，5 项记录在案）

| 编号 | 维度 | 问题 | 处置 |
|---|---|---|---|
| R2-28 | 一致性 | 日志脱敏正则含 `exp=`，会把签名 URL 的有效期也打码，排障时反而看不出「链接是否过期」。 | **顺手改**：§A.3.3 的规则改为「`token\|access_token\|sig\|signature\|api_key\|secret` 必脱敏，`exp` 保留」 |
| R2-29 | 契约细节 | 敏感 meta 折叠成 `{"redacted": true}` 会占用一个真实键名，前端无法区分「被折叠」与「本来就是这个值」。 | **顺手改**：§A.3.2 改为「敏感键不出现在 `meta` 里，改由 `meta_redacted_keys: string[]` 列出键名」 |
| R2-30 | 完整性 | `capability_test_runs` 只有 `route JSON`（§10.1），没有 `output`/`dropped` 列，所以 §A.3.1 的「测试结果面板可回看历史」只能靠实时响应，刷新后历史详情缺失。 | **记录在案**：本轮不扩表，`test_runs[]` 只保证 `ok/mode/route_id/http_status/latency_ms/error_code/at`；UI 文案不得暗示可回看完整响应 |
| R2-31 | 规模 | A.9 第 1 步「迁移先做」与 A.4 的模型改动分属两步，但 `0003` 的条件加列 helper 读的是**模型元数据**——模型不改，迁移改不动。 | **顺手改**：A.9 第 1 步合并为「模型字段 + `0003` + bootstrap 单轨 + adopt 脚本」 |
| R2-32 | 规模 | §A.1 说「不新增页面级功能」，但 A.4.3 把 `/tasks` 换成 `DataTable` 属于页面重构，口径不一致会让复审判越界。 | **顺手改**：§A.1 改为「不新增路由（`/credentials` 除外），已有页面的信息密度与交互一致性属于本轮范围」 |
| R2-33 | 可行性 | 交付面断言用 `grep`（T-35/T-36），Windows 本机没有 grep，与「本机可复现」冲突。 | **记录在案**：A.7.1 的两条命令改为 Python 一行（已在表中给出等价写法） |
| R2-34 | 完整性 | §A.3.7 的「事件映射契约 = 平台期望的 13 类」把数量写死；网关侧权威集合是 `ALL_EVENT_NAMES`（`agents/aragon-gateway/src/engine.ts:1358`），写死数字必然漂移。 | **记录在案**：check 改为「候选事件名集合 ⊇ 网关导出的 `ALL_EVENT_NAMES`；缺的列出名字」，数量由源码派生 |
| R2-35 | 规模 | `SKILLROUTER_ALLOW_QUERY_TOKEN` 标注「计划在 1.1 移除」但没有移除判据，容易变成永久逃生阀。 | **记录在案**：改为「下一个 minor 版本移除；移除前置条件＝`docs/api/examples.md` 与 `docs/skills/connect.md` 中 `?token=` 引用为 0（由 T-35 的脚本断言）」 |

### R2.4 未发现问题的维度（供下游参考）

- **需求覆盖**：§A 的 12 个 P0 确实逐条对应需求里的硬约束（预算/幂等/取消/租约/升级/凭证隔离），没有发现「为了修缺陷而放宽需求」的地方；`blocked` 三态与需求的「明确标记待验证」一致。
- **范围控制**：新增依赖为 0、新增页面 1 个、新增表 0 张（只加列），规模判为**合适**；§A.1 的非目标与 v2 §2.2 一致。
- **凭证隔离主线**：F-03 的「投影白名单 + `meta_sealed` + 回调解密」与 §11.2 的五个检查点同向，且没有引入「把密钥交给前端」的捷径。
- **可验证性纪律**：§A.7.3「本轮不新增『已验证』声明」与 `docs/runbook/verification-checklist.md` 的权威地位一致，未发现自我加冕。

---

## A. 迭代 2 增量契约（v4.0，本轮唯一实现范围）

> **本节的强制力**：本轮（迭代 2/2）的代码改动**只能**来自本节 §A.2（`F-xx` 实现缺陷 + `R2-xx` 设计缺陷）
> 与 §A.4 的文件变更计划；改了 §A.10 里列出的旧段落，就等于改了契约——必须先改本节再改代码。
> 第 1–18 章描述的是**已交付实现**（commit `1b5e39d`），作为现状参照；与本节冲突时**以本节为准**。
> 证据出处见 [`iteration-2-audit.md`](iteration-2-audit.md)（审计的 file:line）与本节 §R2（评审复核的 file:line）。
> 编号规则：实现缺陷用 `F-xx`（沿用审计编号），评审新发现的设计缺陷用 `R2-xx`，断言用 `T-xx`。
> **本节已经过设计评审**（2026-10-19）：35 项发现（P0×10 / P1×17 / P2×8）已全部在正文修正，逐条见 §R2，结论见 §V2。

### A.0 概述（Overview）

第 1 轮迭代把 251 个设计文件全部落地，197 项后端测试 + 58 项端到端联调 + 9 项前端联通 + 42 项网关测试 +
37 项 CLI 测试全绿，`git log` 只有一个干净的 `feat:` 提交。**问题不在于「代码没写完」，而在于「写完了但语义是错的」**：
测试全绿覆盖的是设计里写到的分支，而需求承诺的是**用户能据此做决定的行为**。迭代 2 的审计换了视角——不看「有没有实现」，
只看「这个行为在真实使用中会不会给出错误结论」——结果在四个域里找出 12 个 P0：真实测试成功后 UI 仍显示「未通过」
（发布按钮永久灰）、预算只有同步 token 路径记账（流式与异步完全不计钱）、读超时后自动重投非幂等 POST（重复计费）、
取消与提交竞态导致上游任务成为孤儿、租约丢失后仍能覆写别人的任务、引擎升级的验证闸门在容器里必然 skipped 而
skipped 不计失败（升级「永远绿」）、以及升级产物根本不进运行解析路径（换版本必然启动失败）。这些都是**纸面完整、
线上致命**的缺陷，且全部可以在没有第三方凭证的情况下，用本地真实 HTTP 上游与真实进程复现并回归。

因此本轮不做新功能，只做一件事：**把「看起来完整」变成「可验证正确」**。做法是给每条缺陷配一个可执行断言，
并让「通过/失败/阻塞」三种状态在数据层、接口层、UI 层用同一套语义表达——这是需求里反复强调的那条纪律
（「明确区分『配置已保存』『测试通过』『已发布』『可调用』，不得使用一个模糊状态代替整个流程」）。
同时修掉一类**可验证性缺陷**：CI 的 e2e 作业按现状必挂、Alembic 迁移从不执行、runbook 里的容器命令必然 `UNKNOWN_ROLE`、
「切换前自动备份」只存在于文档。**验证流程本身必须是会被触发的**，否则「待验证」会永远待验证。

本轮的验收标准也随之改变：不再以「测试数量」计，而以**「每条 P0 都有一个会红的回归断言」**计。
不允许新增任何「跳过即通过」的检查项；允许并且鼓励把无法执行的环境明确标成 `blocked`，
因为需求要求「缺少第三方凭证或测试条件的项目明确标记为待验证」——`blocked` 是一个诚实的状态，
`passed` 不是。凡本轮无法闭环的（真实第三方凭证、真实 Docker、目标硬件性能、真实 MCP 客户端），
继续留在 `docs/runbook/verification-checklist.md` §F，**不得因为本轮修了代码就改状态**。

### A.1 本轮范围与非目标

**范围内**：§A.2 的 12 个 P0 全部修复并配回归断言；14 个 P1 全部修复（其中 F-26 允许以「状态语义 + UI 文案」的最小实现收口）；
P2 只在顺手处处理（F-32 的 AAD 注释随 F-03 一起做），其余登记。

**明确不做**（写进实现记录，避免复审误判为遗漏）：

1. 不新增**路由**（唯一例外是 F-19 的 `/credentials` 列表页，它是需求「凭证增删改查」的直接缺口）；
   已有页面的信息密度与交互一致性（列表搜索/筛选/分页/排序、状态徽标）属于本轮范围。
2. 不改上游 AragonAgent 版本（仍为 `@aragon-agent/core@0.2.34`），不改网关工具面（仍 12 个 `sr_*`）。
3. 不引入新依赖（`alembic`、`httpx`、`sqlalchemy`、`pydantic` 已在 `requirements.txt`；前端不加组件库）。
4. 不做多租户/RBAC、计费结算、分布式队列（v2 §2.2 N1–N3 仍为非目标）。
5. 不为「通过验收」而放宽任何闸门；不把 `blocked`/`skipped` 计入 `passed`。

### A.2 缺陷清单与处置

> 「验收断言」列是本轮 DoD 的最小单位：**没写断言 = 没修**。断言必须能在 CI 里以 `pytest -q` / `vitest run` 自动执行。
> 「复核」列为 `✔` 的条目，主代理已在源码中逐行确认（见 `iteration-2-audit.md` §2）；标 `[待复核]` 的必须先确认再动手，
> 确认不成立就在实现记录里写「报告不成立 + 证据」，**不要为了凑数改代码**。

#### A.2.1 P0（12 项，全部必须修）

| ID | 现象（用户可见） | 根因（证据） | 修法（文件 → 函数） | 验收断言 |
|---|---|---|---|---|
| F-01 ✔ | 真实测试成功后仍显示「测试未通过」，发布按钮永久禁用 | 前端类型 `{ok,route}` 与后端返回 `{status,used_route}` 不一致（`apps/web/lib/types.ts:302-317` ↔ `services/capabilities.py:807-821`；后端**早已是**目标形状）；且 `test_runs[].ok` 是布尔（`capabilities.py:603`） | `apps/web/lib/types.ts` 改 `CapabilityTestResult`；`capabilities/[id]/page.tsx` 用 `status==='succeeded'` 且由 `test_runs[].ok === true` 派生；`TestPanel.tsx` 读 `used_route` | T-01（含「`test_runs[].ok` 必须是 JSON boolean」）+ T-33 |
| F-02 ✔ | 能力列表「成功率」永远是「—」（**「上次测试」列根本不存在**） | `_brief`/`list_capabilities` 不产出 `last_test_ok`/`last_test_at`/`success_rate_24h`（`services/capabilities.py:507-574`）；前端只有成功率列（`capabilities/page.tsx:54-145`） | `services/capabilities.py::_brief` + 批量聚合（一次 `GROUP BY`，禁止 N+1）；前端**新增**「上次测试」列 | T-02（造 3 次调用 2 成功 → `0.666…`；无调用 → `null`；未测试 → `None`） |
| F-03 ✔ | 凭证掩码空白、指纹「—」；且 `credentials.meta` 明文外泄（含回调共享密钥） | `services/credentials.py:30-47` 只给 `secret:{masked}` 且原样透 `meta`；`api/v1/callbacks.py:92-99` 把 HMAC 密钥存在 `meta.webhook_secret` | `services/credentials.py` 增 `_public_meta()` 白名单 + 输出 `secret_mask`/`secret_fingerprint`；敏感 meta 存 `meta_sealed`；`callbacks._webhook_secret` 改解密读取 | `test_credentials_projection.py`：① `GET /credentials/{id}` 响应 JSON 序列化后不含密钥原文；② 含 `webhook_secret` 的 meta 写入后 `meta` 里没有该键且 `meta_sealed` 非空；③ 回调验签仍通过 |
| F-04 ✔ | 平台令牌可放进 URL，进而进反代/浏览器历史/日志 | `deps.py:75-83` 接受 `?token=`；`logging.py:20-28` 不脱敏查询串 | `deps.require_token` 仅 Bearer；`logging.redact` 增 query 脱敏；`main.py` 日志只记 `url.path` | `test_token_transport.py`：`?token=` 默认 `400 TOKEN_INVALID` 且提示改用头；`SKILLROUTER_ALLOW_QUERY_TOKEN=1` 时可用但产生一次 warn；日志断言：含 `token=` 的 URL 落盘后不含原值 |
| F-05 ✔ | 预算永不触发（流式/异步不记账） | `record_cost` 全仓仅 `services/execution.py:405` 一处；`check_budget` 只查 token 层；成本落在 `quota_counters.cost_usd`（`rate_counters` 没有 `cost_usd` 列） | 新增 `services/usage.account()` 统一记账（五作用域）；stream/async/worker/测试四路接入；终态四层 `release_concurrency` | T-07/T-08/T-45/T-46：小预算令牌第二次 `402 BUDGET_EXCEEDED`；`quota_counters.cost_usd` 日/月两行递增；定价阶梯与并发释放各有断言 |
| F-06 ✔ | 读超时后自动重投 POST → 上游可能已受理，重复计费 | `executors/errors.py::map_error` 对 `transport_error="timeout"` 返回 `retryable=True`（`:54-63`），`executors/http_executor.py:174-176` 据此继续重试 | `map_error` 区分 `connect_timeout`/`read_timeout`；`http_executor` 读超时立即返回 `status_unknown`，不重试不切换 | T-09（上游计数 == 1、`status_unknown=true`、`needs_reconcile=1`）、T-10 |
| F-07 ✔ | 取消后上游任务成孤儿：平台显示已取消，实际在跑且在计费 | `tasks.cancel:278-294` 无 upstream id 也直接 `canceled`（且把 `cancel_capability` 判在 `upstream_task_id` 之前）；`ALLOWED["queued"]` 不含 `canceling`（`services/tasks.py:17-25`）；`runner.py:132-134` 把 `INVALID_STATE_TRANSITION` 当成功 | `tasks.cancel` 判定顺序改为「终态 → 无 upstream id → cancel_capability」；`ALLOWED` 增 `queued→canceling`；`mark_running` 支持 `cancel_pending`；handler 立即追取消；runner 不再盲判成功 | T-12、T-44：提交中取消 → 终态 `canceled` 且 `upstream_task_id` 非空、上游收到一次 cancel、`error_code` 不含 `INVALID_STATE_TRANSITION` |
| F-08 ✔ | 租约过期后原 worker 仍能覆写任务 → 同任务双执行 / 重复下单 | `worker/leases.py:75-94` release 无 owner 校验；`runner.py:148-153` 续租失败不取消 handler | `leases.release` 增 owner 条件更新并返回 bool；`renew` 失败 → 取消 handler、按 owner 条件释放为 `queued` | `test_lease_ownership.py`：模拟租约被抢 → 原 worker 的 `release(ok=True)` 返回 False 且队列项状态/owner 不变；`renew` 失败后 handler 收到 `CancelledError`（用事件计数断言） |
| F-09 ✔ | 引擎升级「验证通过」是假绿：容器里 3 项 skipped 仍判 passed，且验证的是旧源码 | `upgrade/verify.py:41-78`（相对路径 → 镜像内不存在）、`:127-134`（skipped 不计 failed）、`:106-126`（硬编码 passed）；且 `api` 镜像没有 node/npm/git（`deploy/Dockerfile.api:29-31`），`build.py:50-84` 的命令必然 127 | `upgrade/verify.py` 重写为「只信 `artifact_dir`」；**工具链归属**：build/verify 在网关容器执行（§A.3.7-0）；无执行环境 → `blocked` | T-16、T-17、T-41（网关不可达 → `blocked` 且不产生候选） |
| F-10 ✔ | 换任何非 0.2.34 版本必然启动失败；网关重启后版本指针丢失 | 构建产物不进解析路径（`upgrade/build.py:78-99`）；`engine-runtime/current.json` 网关无人读（`engine-host.ts:516-535` 只给 `ARAGON_HOME`）；网关侧**没有** runtime dir 变量（`config.ts:209` 只有 `AGENT_ENGINE_HOME_ROOT`） | 新增 `upgrade/install_home.py`；网关读 `AGENT_ENGINE_RUNTIME_DIR/current.json` 并回显 `pin_source`；Python 新增 `settings.agent_engine_home_root`；`engine-host` 按 `home/node_modules` 解析 | T-20（解析到候选版本 + `pin_source == 'pointer'`）、T-47（vitest） |
| F-11 ✔ | 有迁移文件但没有迁移机制：`alembic_version` 恒空，`create_all` 才是真相 | `services/bootstrap.py:24-28`（create_all）、`:166-168`（缺版本静默 return）；`assert_migrated()` **全仓零调用点**；`0001`/`0002` 用 `create_all(checkfirst=True)` 建表，故 `0003` 的 `ADD COLUMN` 必须幂等 | `bootstrap` 改 `migrate_or_adopt()` 单轨（空库 → 存量库 adopt → `upgrade head`）；`create_all` 仅测试用；`assert_migrated()` 真正被调用；`0003` 条件加列 | T-23、T-24、T-42（连续两次 upgrade、以及在已含新列的库上 upgrade 都不报 duplicate column） |
| F-12 [待复核] | 并发同幂等键会双打上游并抛 500 | `services/execution.py:58-119` 查后写、`uq_idem_scope_key` 冲突未捕获 | `idempotency_keys.state` 先占位 `in_flight`；命中 in-flight → `409 IDEMPOTENCY_IN_PROGRESS`；完成后 `UPDATE` 为 `done` | `test_execution_sync_async_stream.py::test_concurrent_same_idem_key_calls_upstream_once`：并发 5 个同键请求 → 上游计数 == 1，其余 4 个 409 或回放同一 `request_id` |

#### A.2.2 P1（14 项，全部必须修）

| ID | 现象 | 证据 | 修法 | 验收断言 |
|---|---|---|---|---|
| F-13 [待复核] | 设置页保存成功但策略不生效（敏感数据策略、保留天数、抓取白名单、SSRF 黑名单、升级策略） | 写 `services/settings.py:57-83`；读仍取 `settings.*`（`execution.py:179`、`retention.py:17`、`fetch_guard.py:41,53`、`api/admin/engine.py:44,84`） | 新增 `services/settings_runtime.py`（白名单键 + 60s TTL + `invalidate()`）；替换上述读取点 | `test_settings_runtime.py`：PATCH 后同进程下一次调用即生效（TTL 内也生效，因为 update 会 invalidate）；跨进程场景以「重启后生效」断言 |
| F-14 [待复核] | `GET /settings/backup/now` 有副作用，可被跨站触发 | `api/admin/settings.py:81-97`；`security/csrf.py:11-15` 只校验写方法 | 新增 `POST /api/admin/settings/backup/now`（CSRF 保护）；旧 GET 返回 `405` + hint（不再有副作用） | `test_admin_files.py::test_backup_now_requires_post_and_csrf`：无 CSRF 头 → `403 CSRF_INVALID`；GET → 405 且不产生备份文件 |
| F-15 ✔ | 签名 URL 文档说免令牌，实现强制 Bearer；上传响应不返回签名 URL | `api/v1/files.py:42-56` 的 `download` 上挂着 `Depends(require_token)`（`:44`），签名分支不可达；`services/files.py:39,149-175` 已有 `SIGN_TTL_S/_sign/signed_url/verify_signature`；`save_bytes` 返回体没有 `signed_url` | `deps.optional_token` + `download` 改「签名优先、令牌兜底」；`save_bytes` 复用 `signed_url()` 补字段；**不新增第二套签名算法** | T-31（上传给出的签名可无令牌下载；过期 401 + hint）、T-48（单一算法） |
| F-16 [待复核] | 调用记录按时间筛选无效 | 前端 `calls/page.tsx:18,135-154` 发 `from/to`；后端只认 `date_from/date_to`（`api/admin/calls.py:23-24`） | 后端接受两个别名（`from/to` 兼容并返回 `X-Deprecated-Param`）；前端改发 `date_from/date_to` | `test_api_contract.py::test_calls_filter_aliases`：两种键名结果集相同；前端由 `tsc` + 手测清单兜住 |
| F-17 [待复核] | 列表表头标可排序，后端固定 `created_at desc` 且不接受 `sort` | `api/admin/calls.py:17-40`、`services/calls.py:51`、`api/admin/tasks.py:14-27` | 两端接 `sort`（字段白名单 + `sort_items`）；不支持的字段返回 `400 VALIDATION_FAILED` | `test_api_contract.py::test_calls_sort_whitelist`：`sort=-duration_ms` 生效；`sort=password` → 400 |
| F-18 [待复核] | 任务页无搜索/筛选/分页，固定拉 100 条 | `apps/web/app/(app)/tasks/page.tsx:45-49` | 后端补 `q/status/capability_key/page/page_size`；前端换 `DataTable` + `useTableState` | `test_api_contract.py::test_tasks_list_filters`（3 条断言）；前端手测清单第 4 项 |
| F-19 ✔ | 凭证没有独立列表页（无法搜索/筛选/分页） | `apps/web/app/(app)` 无 `credentials/` | 新增 `apps/web/app/(app)/credentials/page.tsx` + `AppShell` 导航项；后端确认 `GET /api/admin/credentials` 支持分页 | `next build` 通过 + 手测清单第 5 项（列表可筛 `status=expiring`） |
| F-20 [待复核] | CI 的 e2e 作业必挂（`E2E_BASE_URL` vs `SR_BASE_URL`；未 build 就 start） | `.github/workflows/ci.yml:268-293`、`playwright.config.ts:15,21-28` | workflow 注入 `SR_BASE_URL` + 先 `npm run build`；无浏览器环境时 `if:` 跳过并在日志写明原因 | workflow 语法校验（`python -c "import yaml,sys;yaml.safe_load(open(...))"`）纳入 `scripts` 作业 |
| F-21 ✔ | CI 从不执行 Alembic；也不做容器 entrypoint 冒烟 | `ci.yml` 8 个作业，无 alembic step | 新增 `migrations` 作业（upgrade → downgrade → upgrade + 快速 pytest 子集） | 该作业在本地可复现执行（`alembic upgrade head` 等命令逐条跑过并记录到 verification-report） |
| F-22 [待复核] | runbook 里 `docker compose run --rm api python …` 必然 `[FATAL] UNKNOWN_ROLE` | `Dockerfile.api:65` + `entrypoint.api.sh:20-21,112-114` + `runbook/backup-restore.md:24-25`、`init.md:129-130` | entrypoint 增 `run` 角色透传；文档改 `--entrypoint python`（两种写法都给） | `bash -n` + 文档命令清单逐条核对（`docs/runbook/` 内无可执行镜像的命令） |
| F-23 [待复核] | 「切换前自动备份」只存在于文档，`backup_file_id` 永不写 | `upgrade/switch.py:66-118`、`models/engine.py:55`、`runbook/engine-upgrade.md:16,83` | `switch()` 先 `backup_job.quick_backup(label="pre-upgrade")`，失败即 abort；成功写 `backup_file_id` | `test_engine_upgrade.py::test_switch_creates_backup_and_aborts_on_failure` |
| F-24 ✔ | 状态兼容检查形同虚设：`needs_migration` 硬编码 False | `upgrade/verify.py:106-126,132` | 新增 `upgrade/compat.py`（按版本比较 `aragon-home/sessions` 结构与 `schemaVersion`）+ `upgrade/gc.py`（保留 3 个已验证构建） | `test_engine_upgrade.py::test_compat_detects_session_shape_change`（改字段 → `needs_migration=True` 且切换被拦） |
| F-25 [待复核] | 维护模式不闭环：恢复期间 API 照常写入 | `scripts/restore.py` 写 `.restore-in-progress`；`Code.MAINTENANCE` 无消费点 | 新增 `services/maintenance.py`；`deps` 在写方法前拦 `503 MAINTENANCE`；`/healthz` 暴露 `maintenance`; UI 横幅改读 healthz | `test_maintenance_mode.py`：创建标记文件 → `POST /api/admin/providers` 返回 503、`GET /api/admin/providers` 正常；`/healthz.maintenance === true` |
| F-26 [待复核] | 升级结果只有 passed/failed，无法表达「阻塞/待人工验证」，与三态要求不齐 | `api/admin/engine.py`、`settings/engine/page.tsx:181-421` | 校验结果与运行记录统一为 `passed` / `blocked` / `failed` 三态；`blocked` 禁止自动切换并在 UI 显示原因与下一步 | `test_engine_upgrade.py::test_blocked_blocks_auto_policy`；前端手测清单第 6 项 |

#### A.2.3 P2（6 项：本轮登记，不实现）

F-27 生产性能基线（需 4C8G + 50 万 `call_logs`）；F-28 附录 C 兼容矩阵实测（需真实 Agent 环境）；
F-29 `docs/api/examples.md` 无漂移检查；F-30 日志保留策略未进 runbook；
F-31 无障碍无自动门禁（`axe`）；F-32 恢复后凭证可解密抽查未自动化（**其前置的 AAD 注释随 F-03 完成**）。

#### A.2.4 评审新增的设计级缺陷（R2-01…R2-10，均已在 §A 正文修正）

§A.2 的 F-xx 是**实现缺陷**（第 1 轮代码写错了）；本节 R2-xx 是**设计契约缺陷**（文档描述的机制在现有代码/镜像里不存在，
或已经存在另一套）。两类都属于本轮必须关闭的范围，逐项证据与修复位置见 §R2.1，落地位置如下：

| ID | 一句话 | 落地位置 |
|---|---|---|
| R2-01 | 构建/验证没有执行工具链 | §A.3.7-0、§A.4.1 `upgrade/toolchain.py`、§A.4.4 `Dockerfile.gateway`/compose |
| R2-02 | `0003` 在 `create_all` 库上必失败 | §A.6.1 条件加列、§A.3.8-2 |
| R2-03 | `call_logs.status='success'` 与聚合键不一致 | §A.3.1-2 `LOGGED_STATUS`、§A.6.2 I7 |
| R2-04 | `test_runs[].ok` 是布尔 | §A.3.1 前端判定、T-01 |
| R2-05 | 状态机缺 `queued→canceling` | §A.3.6 表、§A.10 的 §10.2 同步 |
| R2-06 | 记账表/作用域错位 | §A.3.4、§A.10 的 §10.1 同步 |
| R2-07 | 「无价目 → cost=0」前提错误 | §A.3.4 定价阶梯、§A.8 R2 |
| R2-08 | 签名 URL 已存在却被重新设计 | §A.3.10、T-31/T-48 |
| R2-09 | 引擎指针环境变量不存在 | §A.3.7-2、§A.4.1 `settings.py`、§A.4.4 compose |
| R2-10 | 内部端点路径写错 | §A.5.3、§A.4.4 `main.ts`/`health.ts` |

### A.3 技术设计（Technical Design）

本节按「一条链一条链地修」组织。每条给出**现状 → 目标语义 → 关键代码路径 → 边界与失败路径**。
所有新增字段/字段名即为契约，实现时不得改名。

#### A.3.1 能力测试与「发布证据」链路（F-01 / F-02）

> **标识符纪律（从本节开始适用）**：本节出现的函数名、字段名、环境变量都必须在仓库里 grep 得到。
> grep 不到，说明文档或代码有一边是错的；按 §A（首段）的规矩，**先修文档**，不允许「按文档新建一个同名函数」。

现状必须分环节看，否则会去改一个本来就正确的实现：

| 环节 | 现状（源码事实） | 本轮动作 |
|---|---|---|
| 后端返回体 | **已经是**冻结形状：`services/capabilities.py::test_capability()`（`services/capabilities.py:724`）返回 `test_run_id/status/http_status/latency_ms/response_preview/output/error/used_route/dropped`（`:807-821`），键集合与本节完全一致 | **不改形状**，只把它写成断言（T-01），防止未来漂移 |
| 前端类型 | 仍旧是 `{ok, route, at}`（`apps/web/lib/types.ts:302-317`），与后端键集合**没有交集** | 按返回体重写类型，删除 `ok`/`route` |
| 通过判定 | 页面 `setTestedOk(result.ok)`（`apps/web/app/(app)/capabilities/[id]/page.tsx:288`）→ `undefined` → 发布按钮永久 `disabled`（`:405`） | 判定改 `status === 'succeeded'`；`testedOk` 从 `detail.test_runs` 派生 |
| 列表指标 | `_brief()`（`services/capabilities.py:507`）与 `list_capabilities()`（`:545`）不产出 `last_test_ok`/`last_test_at`/`success_rate_24h` | 批量聚合一并产出（见下表） |

目标语义一句话：**「测试通过」必须由一条落库的、带真实上游响应的 `capability_test_runs.ok=1` 记录驱动，且 UI 与发布闸门读同一个来源。**

**1) 返回体（冻结，已与实现一致）**

```json
{
  "test_run_id": "ctr_...", "status": "succeeded|failed|status_unknown",
  "http_status": 200, "latency_ms": 412,
  "response_preview": "…脱敏前 4KB…",
  "output": {"…解析后输出…"},
  "error": {"code": "UPSTREAM_AUTH", "message": "…", "hint": "…", "request_id": "req_…"} | null,
  "used_route": {"route_id": "rt_primary", "provider_id": "prv_x", "credential_id": "crd_y"} | null,
  "dropped": [{"route_id": "rt_backup", "reason": "priority"}]
}
```

**2) 状态口径（本轮最容易写错的地方）**

平台内部有二套「结果」词汇，必须显式映射，禁止各处自造：

| 层 | 取值 | 出处 |
|---|---|---|
| 执行器 `ExecutionResult.status` | `succeeded` / `failed` / `status_unknown` | `executors/base.py:12` |
| 落库 `call_logs.status` | `success` / `failed` / `status_unknown` | `services/execution.py:384` |
| 任务 `tasks.status` | `queued` / `running` / `succeeded` / `failed` / `canceling` / `canceled` / `status_unknown` | `services/tasks.py:17-25` |

- 唯一映射常量 `LOGGED_STATUS = {"succeeded": "success", "failed": "failed", "status_unknown": "status_unknown"}`，
  定义在 `services/execution.py`，是 `call_logs.status` 的**唯一**写入口（现状已是 `success`，本轮不改这个历史取值，避免全库数据迁移）。
- `_brief()`/`list_capabilities()` 的成功率聚合必须用同一常量拼进 SQL（`SUM(status = :ok_status)`），
  **不得字面写 `'succeeded'`**：写错不会报错，只会让 `success_rate_24h` 恒为 `0`。
- `status_unknown` 记为「未成功但不算失败」：`success_rate_24h` 的分子只算 `success`，分母是全部终态（含 `status_unknown`）。

**3) 列表指标（4 个字段，批量计算）**

| 字段 | 语义 | 来源 | 为空时 |
|---|---|---|---|
| `last_test_ok` | 最近一次测试是否成功（JSON boolean，不是 0/1） | `capability_test_runs` 按 `at DESC LIMIT 1` 的 `ok` | `null`（从未测试） |
| `last_test_at` | 最近一次测试时间 | 同上 | `null` |
| `success_rate_24h` | 近 24h 调用成功率 | `call_logs`（`capability_key` + `created_at >= now-24h`，分子 `status = LOGGED_STATUS['succeeded']`） | `null`（无调用，**不是 0**） |
| `call_count_24h` | 近 24h 调用次数 | 同上 | `0` |

实现要点：`ix_call_logs_cap(capability_key, created_at)` 已存在（`models/execution.py`）；用一次
`SELECT capability_key, COUNT(*), SUM(status = :ok) … GROUP BY capability_key` 预取到字典，再在 `_brief()` 里查表。
**禁止**在循环里查询（v2 §7.10 的性能约束）。`last_test_ok` 同样用一次
`SELECT capability_id, ok, at FROM capability_test_runs WHERE at = (SELECT MAX(at) …)` 的窗口查询批量取，不做 N+1。

前端：

- `apps/web/lib/types.ts::CapabilityTestResult` 按上面的 JSON 逐字段声明；删除 `ok`/`route`/`at`。
- `Capability` 类型增 `last_test_ok?: boolean | null`、`last_test_at?: string | null`、`success_rate_24h?: number | null`、
  `call_count_24h?: number`，并**新增 `test_runs`**（后端 `get_capability()` 已经返回，`:600-617`，类型里目前没有）。
- 判定通过统一为 `result.status === 'succeeded'`；「测试结果」面板展示 `used_route.provider_id` 与 `dropped[]`。
- `capabilities/[id]/page.tsx` 的 `testedOk` 改为从 `detail.test_runs` 派生：
  `test_runs.some(r => r.ok === true && Date.parse(r.at) >= Date.now() - 86400_000)`。
  **`ok` 是布尔**（后端 `"ok": bool(t.ok)`，`services/capabilities.py:603`）——写成 `r.ok === 1` 会让严格相等恒 false，
  发布按钮永久禁用，F-01 以另一种形式原样复发。这一点由 T-01 卡住。
- 能力列表**新增「上次测试」列**（现状没有这一列，只有成功率列，`capabilities/page.tsx:54-145`）：
  单元格显示 `last_test_ok === null ? '—' : (last_test_ok ? '通过' : '未通过') + ' · ' + 相对时间`。
- 三态徽标语义固定：`测试通过` = 24h 内有成功测试；`已发布` = `published_version_id` 非空；
  `可调用` = 已发布 + 所有 route 的凭证 `status='active'` 且能力 `status='published'`。

#### A.3.2 凭证投影与 meta 密封（F-03）

**现状只有一处泄漏，别去改两处正确实现**：`services/credentials.py::_brief()`（`:30-47`）既只给 `secret:{masked}`
（前端读的是 `secret_mask`/`secret_fingerprint`，两个列都存在但没被投影出来），又把 `meta` 原样透出；
而 `services/providers.py:101-114` 的凭证摘要已经用 `secret_mask` 且**不带 `meta`**，`services/credentials.py::impact()`
（`:278-286`）只回 `{id, name, masked, status}`。本轮只收口 `_brief()`/详情与 providers 详情的字段一致性。

**原则**：`GET /credentials*`、`GET /providers/{id}` 的凭证摘要、`/impact` 三个投影**只允许**出现 `_PUBLIC_META_KEYS`
里的键；任何「原样返回 JSON 列」的写法都视为缺陷。

```python
# services/credentials.py
# 白名单 = 仓库里真实写入/读取的键 + 明确预留的键；新增键必须同时改本节
_PUBLIC_META_KEYS = (
    "provider_slug",     # api/v1/callbacks.py:97 按它找凭证
    "probe_path",        # services/credentials_verify.py:30
    "probe_method",      # services/credentials_verify.py:33
    "header_name",       # executors/auth.py:15
    "username",          # executors/auth.py:22（basic 认证用户名，非密钥）
    "mock",              # scripts/seed_demo.py:185（演示数据标记）
    "model", "region", "notes", "base_url_hint",   # 预留：UI 未来可填，当前无人写
)
_SENSITIVE_META = re.compile(r"(?i)(secret|token|passw|signature|webhook|api[_-]?key|private)")
def _public_meta(meta: dict) -> dict:
    """白名单投影：只回 _PUBLIC_META_KEYS 里的键；被剔除的键名交给 meta_redacted_keys。"""
```

写入/读取契约：

1. 写路径（`create_credential` / `update_credential`）：从 `payload.meta` 摘出命中 `_SENSITIVE_META` 的键 →
   `secretbox.seal(record_type="credential_meta", record_id="<凭证 id>", plaintext=...)` → 写 `meta_sealed`；
   非敏感键留在 `meta`。**`probe_path`/`header_name`/`username` 不属于敏感键**，不得被密封（否则认证会失效）。
2. 投影（`_brief`、详情、providers 详情）：`meta` 只回白名单键；被剔除的敏感键名以 `meta_redacted_keys: string[]` 返回，
   **不要**折叠成 `{"redacted": true}`（那会占用一个真实键名，前端无法区分「被折叠」与「本来就是这个值」）。
3. `meta_sealed` 只输出布尔 `bool(row.meta_sealed)`；`secret_mask`/`secret_fingerprint` 直接投影（列已存在：
   `models/providers.py:35-36`）。
4. 服务端读原始 meta 的地方（`services/credentials.py::materialize()`、`executors/auth.py`、`services/credentials_verify.py`）
   **不走投影**，本轮不改；只有回调验签改成「先解密 `meta_sealed`，再回落旧 `meta['webhook_secret']`」。
5. `meta_sealed` 的 AAD 固定 `record_type="credential_meta"`，并在 `crypto/secretbox.py` 顶部 docstring 登记全部
   `record_type` 取值（`credential`、`credential_meta`、`provider_secret`…）——这同时关掉 P2 F-32 的前置。
6. 回填：`scripts/migrate_seal_meta.py`（幂等，`--dry-run` 默认）：扫描 `meta` 含敏感键的行 → 重新 seal → 删键 → 打印统计；
   回填后旧回落分支保留，命中时记一次 `deprecated_meta_secret` 告警。

#### A.3.3 令牌传输与日志脱敏（F-04）

- `deps.require_token`（`deps.py:75-83` 现状接受 `?token=`）：

  ```python
  raw = header[7:].strip() if header.lower().startswith("bearer ") else None
  if not raw and settings.allow_query_token:          # 默认 False
      raw = request.query_params.get("token")
      if raw: log.warning("query_token_used", extra={"object": request.url.path})
  if not raw:
      raise ApiError(Code.TOKEN_INVALID, message="缺少 Authorization: Bearer <token>",
                     hint="平台令牌只能通过 Authorization 头传递；查询串通道默认关闭")
  ```

  新设置项 `SKILLROUTER_ALLOW_QUERY_TOKEN`（默认 `0`，进 `settings.py` 与 `deploy/.env.example`，标注 `[compat]`）。
  **移除判据**（避免逃生阀变永久阀）：下一个 minor 版本删除；删除前置条件＝`docs/api/examples.md` 与
  `docs/skills/connect.md` 里 `?token=` 的出现次数为 0（由 T-35 的脚本断言），写进 `troubleshooting.md` 该错误码的处置段。
- `logging.redact()`（`logging.py:34-56`）新增查询串规则：`(?i)\b(token|access_token|sig|signature|api_key|key|secret)=([^&\s"']+)` → `\1=***`。
  **`exp` 不脱敏**：签名 URL 的 `exp` 是排障关键信息（「链接是否过期」），打码只会让问题更难定位；`sig` 已足以防止重放。
- `main.py` 的请求日志与异常日志一律使用 `request.url.path`（**不含 query**）；401/403 与 5xx 走同一格式化函数
  `_log_request(...)`，禁止在别处拼 `str(request.url)`。
- `docs/api/examples.md` 与 `docs/skills/connect.md` 里所有 `?token=` 示例改为 `-H "Authorization: Bearer $SR_TOKEN"`。

#### A.3.4 用量与预算记账（F-05）

目标语义：**任何一次真实触达上游的调用（同步/流式/异步/测试面板/凭证探测）都必须产生一条 `usage_records`，
并把成本累加到 `quota_counters.cost_usd`（当日 + 当月两个 period），五个作用域统一走一个函数。**

先纠正三处事实（写错任何一处，断言都不可能绿）：

1. 成本累加在 **`quota_counters.cost_usd`**（`models/access.py:84-95`，`security/ratelimit.py::record_cost` 已在写日/月两行）；
   `rate_counters` 只有 `calls`/`concurrent`（`models/access.py:44-62`），**没有也不加** `cost_usd`。
2. 作用域是 `token / agent / capability / provider / global`：现状 `_quota_gate()` 跑的是
   `token → capability → provider → global`（`services/execution.py:122-141`）。本轮在 `token` 之后插入 **`agent`**
   （`scope_id = token_ctx.agent_id`），以满足需求「按…接入 Agent 设置速率、并发和预算」。
   `quota_limits`/`quota_counters` 的 `scope_type` 是 TEXT，**无需迁移**，只需在 §10.1 的枚举里补 `agent`。
3. 定价阶梯（`services/usage.py:17-23, 36-46` 现状有 `DEFAULT_PRICES` 兜底）：
   调用/route 显式 `unit_price_usd` → `price_book` 当前有效行 → **`DEFAULT_PRICES`（记为 `source='estimated'`）** →
   都没有才 `cost_usd=0`、`source='unknown'`。**不存在「无价目就 cost=0」的世界**：默认价表在，就一直有成本估算。

新增 `services/usage.py::account(...)`：

```python
def account(*, call_id, provider_id, capability_key, token_id, agent_id,
            unit, qty, source, unit_price_usd=None, currency="USD") -> dict:
    """写 usage_records + 推进 quota_counters.cost_usd（token/agent/capability/provider/global 五层）。
    返回 {unit, qty, cost_usd, source, price_source}；不抛错、不阻断调用。"""
```

接入点（全部要改）：

| 路径 | 位置 | 记账内容 | 并发额度 |
|---|---|---|---|
| 同步 | `services/execution.py::invoke_sync` | 已有 `usage.record` → 改 `usage.account`（`record_cost` 由它内部调一次） | `_quota_gate` 已 +1，调用结束 `release_concurrency` |
| 流式 | `services/execution.py::stream` 结束处 | 用流末尾的 usage；上游未给 → `qty=0, source='unknown'` | 同上（异常路径也要释放，`finally`） |
| 异步提交 | `services/execution.py::submit_async` | **不记账**（未产生上游用量） | `_quota_gate` 已 +1，**交给终态释放**，提交侧不得释放 |
| 异步终态 | `worker/handlers/capability_invoke.py` | 用 `async_poll` 解析出的 usage；无 → 按 `kind` 默认单位（`image=images`、`video/audio/asr=seconds`、`text=tokens`），`source='estimated'` | **四层 `release_concurrency`（与提交侧对称，只释放一次）**：用任务行上的 `released_at` 幂等标记 |
| 能力测试 | `services/capabilities.py::test_capability` | 现有 `usage.record`（`:800-806`）→ 改 `usage.account`（token/agent 作用域为空） | 同同步 |
| 凭证探测 | `services/credentials_verify.py` | 不计费，但记 `usage_records.source='probe'`（`qty=0`）便于对账 | 不占额度（不经 `_quota_gate`） |

预算判定：`check_budget`（`security/ratelimit.py`）扩展为五层，任一超限 → `402 BUDGET_EXCEEDED`，`details` 给出
`{"scope":"token","limit_usd":…, "used_usd":…, "hint":"提高该令牌预算或在设置页调整默认预算"}`。

**明确的已知限制（必须写进实现记录与 UI 提示）**：预算在终态记账，因此单次调用可能超过剩余预算（超额上限 = 本次调用成本）；
不做预占（`reserved_cost_usd`）是刻意取舍，避免为未完成的异步任务冻结额度。**行为变更告知**：本轮之前只有同步 token 路径计费，
本轮起流式/异步/测试/（探测除外）都计费，存量小预算令牌可能第一次出现 `402`——发布说明与设置页文案必须写明，
发布前用 `scripts/perf_smoke.py` + `scripts/seed_demo.py` 造的压力数据核对一次概览金额（§A.8 R2）。

#### A.3.5 超时、重试与幂等三态（F-06 / F-12）

**超时分类**（`executors/errors.py::map_error` 增加 `transport` 细分）：

| 分类 | 触发 | `retryable` | 动作 |
|---|---|---|---|
| `connect_timeout` / `dns_error` / `connection_refused` | 未建立连接 | `True` | 退避重试（受 `max_attempts` 与预算约束），可 failover |
| `read_timeout` / `upstream_hung` | 已发出请求、未拿到完整响应 | **`False`** | **禁止重投**；立即返回 `UPSTREAM_TIMEOUT` + `status_unknown: true` + `needs_reconcile: true`；**不 failover**（同一请求可能已在对方执行） |
| `connection_reset` 且方法 ∈ {GET, HEAD} | 幂等方法 | `True` | 允许重试 |
| `connection_reset` 且方法 ∈ {POST, PUT, PATCH} | 非幂等 | **`False`** | 同 read_timeout |

同步调用返回体增两个布尔字段（`invoke_sync` 的 `status` 取值集合扩为 `succeeded | failed | status_unknown`），
并把 `needs_reconcile=1` 写进 `call_logs`（新列，见 §A.6.1）；`call_logs.status` 仍按 `LOGGED_STATUS` 映射（§A.3.1-2）。

**幂等三态**（`idempotency_keys.state ∈ {in_flight, done}`）：

```
请求带 Idempotency-Key
  ├─ SELECT … WHERE scope+idem_key
  │    ├─ 无记录 → 独立事务 INSERT(state='in_flight', request_id=<新 id>)   ← 唯一索引做互斥
  │    │      └─ IntegrityError → 并发对手赢了 → 回滚该事务后**新开**会话重查，走「已有记录」分支
  │    └─ 有记录
  │         ├─ fingerprint 不同 → 409 IDEMPOTENCY_KEY_REUSED（已有语义，保留）
  │         ├─ state='in_flight' 且未过期 → 409 IDEMPOTENCY_IN_PROGRESS（新增错误码）
  │         ├─ state='in_flight' 且 expires_at < now → 视为陈旧占位，条件 UPDATE 抢占后继续执行
  │         └─ state='done' → 回放原 request_id 的结果（已有语义）
  └─ 调用结束后 UPDATE … SET state='done', request_id=… WHERE id=? AND state='in_flight'
```

**事务与冲突处理**（现在是 `services/execution.py:58-119` 的「查后写」，必须按这三条改，否则并发场景会 500）：

1. 占位用**独立事务**，不在调用方事务里：`db_write()` 抛出 `IntegrityError` 后会话已失效，必须退出该 `with` 再重查。
2. 「抢占陈旧占位」用条件更新：`UPDATE idempotency_keys SET request_id=:new, created_at=now WHERE id=:id AND state='in_flight' AND expires_at < now`；
   影响行数 0 说明别人先抢到 → 按「已有记录」分支处理。
3. 完成态更新同样带 `WHERE state='in_flight'`；影响行数 0 记一次 `idem_reservation_lost`（说明本次占位已被判过期并被他人接管），
   该次调用的结果照常返回，但不覆盖对手的 `request_id`。

**陈旧占位回收**：`worker/handlers/retention.py` 每小时删除 `state='in_flight' AND expires_at < now - 1h` 的行（`--dry-run` 不参与）；
`expires_at` 默认 `now + 24h`（沿用 `_idempotency_store` 现状）。

新增错误码 `IDEMPOTENCY_IN_PROGRESS`（409，`errors.REGISTRY` 必须是 4 元组，否则
`test_audit_redaction.py::test_every_registered_code_can_be_raised` 会红——这正是它存在的意义），
并同步 `docs/runbook/troubleshooting.md` 的码表。

上游幂等透传（可选但推荐）：若 route 定义含 `idempotency_header`（如 `Idempotency-Key`），把平台幂等键原样透传给上游；
否则不透传，并在 `route_decisions.reason` 注明「上游不支持幂等键，重试策略为保守」。

#### A.3.6 异步任务：取消竞态与租约所有权（F-07 / F-08）

**状态机增量**（`tasks` 表新增 `cancel_requested_at`；`services/tasks.py::ALLOWED` 是唯一实现入口）：

| 迁移 | 现状 `ALLOWED` | 本轮必须变成 |
|---|---|---|
| `queued → canceling` | **缺失**（`services/tasks.py:18` 只有 `running/canceled/failed`） | **新增**：提交前取消的唯一合法路径；不加这一条，提交前取消会抛 `INVALID_STATE_TRANSITION`（500）——正是 F-07 的现场 |
| `canceling → canceled / failed / status_unknown` | 已有（`:21`） | 保留；`failed` 用于「提交失败/追取消时上游报错」 |
| 其余 | 不变 | 不变 |

```
queued ──claim──> (submitting) ──submit ok──> running ──> succeeded | failed | status_unknown
   │                   │                          │
   └─cancel()──────────┤                          └─cancel()──> canceling ──> canceled
                       └─cancel()──> canceling（保留意图，不直接 canceled）
```

规则（逐条实现，缺一条都会留下孤儿任务）：

1. `tasks.cancel(task_id)` 的**判定顺序**（顺序错了会互相遮蔽，现状 `:285` 把 `cancel_capability != 'remote'` 判在
   `upstream_task_id` 之前，必须先改顺序）：
   1) 终态（`succeeded|failed|canceled`）→ 原样返回（`cancel_scope: "none"`，`ok: false`）；
   2) `upstream_task_id` **为空** → 落 `cancel_requested_at=now()`，`queued|running → canceling`，
      返回 `{ok: true, status: "canceling", cancel_scope: "pending_submit", note: "已请求取消；提交中的请求会被追取消"}`，
      **不再**直接置 `canceled`；
   3) `upstream_task_id` 非空且 `cancel_capability == "remote"` → `canceling`（现状保留）；
   4) `upstream_task_id` 非空且 `cancel_capability != "remote"` → 保留 `local_only` 语义（转 `canceled` +
      note「上游不支持取消，可能继续执行并产生费用」），但**必须**把 `upstream_task_id` 留在行里供人工追踪。
2. `tasks.mark_running(task_id, upstream_task_id)`：读行 → `cancel_requested_at` 非空 → 写 `upstream_task_id`
   并保持 `canceling`（返回 `{"cancel_pending": True}`），否则 `queued → running`。
3. `worker/handlers/capability_invoke.py`：`submit()` 返回后先看 `mark_running` 的返回值；`cancel_pending` 为真 →
   立刻 `async_poll.cancel(...)`（尽力）→ `tasks.mark_canceled(upstream_ok=...)` → 发布 `tasks/task.updated` 事件 → `return`。
   **不再**落到轮询循环。
4. **提交失败分支**：`submit()` 抛错时先读任务当前状态——若已是 `canceling`（用户已请求取消），
   `mark_canceled(upstream_ok=False)`；否则 `finish(failed/status_unknown)`。禁止直接 `finish(status=...)` 而不看状态，
   否则会被 `transition()` 判非法（`canceling → failed` 合法，但 `canceled → failed` 不合法）。
5. `worker/runner.py`：`INVALID_STATE_TRANSITION` 不再一律 `release(ok=True)`。改为：handler 返回 `{"terminal": True}`
   时才 `ok=True`；否则 `release(ok=False, error="STATE_RACE")`，并按 `attempts/max_attempts` 决定 `queued` 或 `dead`。
6. `leases.release(queue_id, *, worker_id, ok, error=None, retry_delay_s=0) -> bool`：`worker_id` **成为必填参数**，
   SQL 用条件更新 `WHERE id=:id AND status='leased' AND lease_owner=:wid`；返回是否真的释放成功。
   owner 不匹配 → 不写库、记 `log.warning("lease_release_rejected")`、返回 `False`。
7. `_renew_loop` 失败：`self._lost_leases.add(queue_id)` 并 `handler_task.cancel()`；`_execute` 捕获 `CancelledError`
   （区分「租约丢失」与「优雅停机」两个 reason），按 owner 条件把队列项放回 `queued`（若已被他人领取则不动），
   并把任务标 `failed/TIMEOUT` 之前先查状态，避免覆盖别人正在跑的结果。

**重启恢复**（`tasks.recover_on_startup` 已有，本轮补两条 + 一个阈值）：

1. 租约过期的队列项复位（已有）。
2. `status='canceling'` 且 `upstream_task_id IS NULL` 且 `cancel_requested_at` 早于
   `SKILLROUTER_CANCEL_RECONCILE_MINUTES`（默认 **30**，新设置项）→ 置 `canceled` + `resume_policy='abandon'`。
3. `status='canceling'` 且 `upstream_task_id IS NOT NULL`（worker 崩在追取消途中）→ 重新入队一次
   `capability_invoke_async` 的 `cancel_only` 任务；若该任务已是第 2 次，直接置 `canceled(local_only)` 并写明
   「上游确认失败，远端可能仍在执行」。**不处理这一条，任务会永久停在 `canceling`。**

#### A.3.7 引擎升级流水线真实化（F-09 / F-10 / F-23 / F-24 / F-26）

目标语义：**「验证通过」必须是候选产物的性质，而不是当前环境的性质；不能验证时必须说「不能验证」。**

**0) 工具链归属（本轮新增，决定整条链能不能跑）**

现状：`upgrade/build.py` 直接 `subprocess` 调 `git clone`/`npm ci`/`npm run build`（`:50-84`），
`upgrade/verify.py` 直接跑 node 脚本（`:41`）；而 `api`/`worker` 镜像**只有 `ca-certificates`**
（`deploy/Dockerfile.api:29-31`，全文件无 node/npm/git）。照现状实现，「无环境 → `blocked`」会把**每一次**升级都判成
`blocked`，`policy=auto` 永不生效——需求「按管理员设置的升级策略自动生效」直接落空。

固定决策：

| 项 | 决策 |
|---|---|
| 构建与验证在哪跑 | **网关容器**（`node:22-slim`，本轮 `deploy/Dockerfile.gateway` 增加 `git`），因为它本来就有 node，且与 api/worker 共享 `/data` 卷 |
| Python 侧角色 | **只做编排**：状态机、审计、并发限制、结果落库；不再自己 spawn npm/node（`SKILLROUTER_ENGINE_TOOLCHAIN=gateway`，生产默认） |
| 本地开发 | `SKILLROUTER_ENGINE_TOOLCHAIN=local` 时保留原 `subprocess` 路径（开发机有 git/npm 才能跑），且仅 `SKILLROUTER_ENV=development` 可用；生产环境该值被忽略并记 `log.warning("toolchain_local_ignored")` |
| 工具链不可用 | 构建阶段即返回 `blocked`（`detail`＝缺哪个命令），**不产生候选行**；不写 `failed`（那不是候选的问题） |

对应内部端点（见 §A.5.3）：`POST /internal/admin/build`、`POST /internal/admin/verify-artifact`。
两个端点都在网关侧实现、都用 `X-Internal-Token` 鉴权、都只接受 `/data` 下的绝对路径（路径前缀校验，防越权读宿主目录）。
Python 侧新增 `upgrade/toolchain.py`：`run_build(candidate) -> dict`、`run_verify(artifact_dir, expected) -> dict`，
超时 15 min（与 `BUILD_TIMEOUT_S` 一致），网络失败/网关不可达 → `blocked`（`detail="网关不可达：…"`）。

**1) `upgrade/verify.py` 重写规则**

1. **唯一输入是 `artifact_dir`**（`engine-builds/<version>-<provider>/`，`upgrade/build.py:113-117` 已经是绝对路径），
   不再读 `agents/aragon-gateway` 源码（`:41` 的相对路径在镜像里不存在）。
2. 检查项（每项 `passed | failed | blocked`，`detail` 必填；`skipped` 这个取值**从类型里删掉**，历史行按 `blocked` 读取）：

   | check | 通过条件 | 不可执行时 |
   |---|---|---|
   | `构建产物完整性` | `packages/core/dist/index.js` 存在且非空 | `failed` |
   | `构建清单一致` | `build-manifest.json` 的 `version/commit/pruned_lock_sha256` 与候选一致 | `failed` |
   | `导出清单兼容` | 网关侧跑 `scripts/verify_artifact.mjs <dist>`，导出符号集合 ⊇ 适配层 `EXPECTED` 集合 | `blocked`（网关不可达/脚本缺失） |
   | `事件映射契约` | 候选事件名集合 ⊇ 网关导出的 `ALL_EVENT_NAMES`（`agents/aragon-gateway/src/engine.ts:1358`），缺的列出名字 | `blocked` |
   | `工具清单` | 平台白名单 12 个 `sr_*` 全部可由候选注册（静态断言，不是文案） | `failed` |
   | `状态兼容` | `upgrade/compat.py` 对 `aragon-home/sessions/*` 结构与 `schemaVersion` 的断言 | `blocked`（无 home） |
   | `冒烟调用` | 有模型凭证时真实一次推理；否则 `blocked` | `blocked` |

   注意「事件映射契约」不写死数量（原写 13 类）：数量由网关源码派生，写死必然漂移。
3. `verify_status = failed if 任何 failed else (blocked if 任何 blocked else passed)`；`needs_adapter_review` = blocked 项列表。
   **`verify_status` 与 `engine_upgrade_runs.result` 是两列**：`result ∈ {success, failed, aborted}` 表示「这次运行怎么结束的」，
   `verify_status ∈ {passed, blocked, failed}` 表示「候选验证结论」；UI 与接口分别展示，不得互相顶替。
4. 自动切换条件收紧为：`policy == "auto" and verify_status == "passed"`；`blocked` → 进「待升级」并要求管理员确认
   （UI 明确写「未能验证：<原因>；确认风险后可手动切换」，请求带 `acknowledge_blocked=true`）。

**2) 环境变量与设置（F-10 的真正缺口）**

现状：网关只认 `AGENT_ENGINE_HOME_ROOT`（`agents/aragon-gateway/src/config.ts:209`，compose 已给 `/data/aragon-home/engines`）；
Python 侧只有 `settings.engine_runtime_dir`（`settings.py:162-163`，`<data>/engine-runtime`），**没有 home root**，
而网关**没有** runtime dir。文档原先让网关读 `ENGINE_RUNTIME_DIR/current.json`，这个变量在两侧都不存在。

本轮固定：

| 变量 | 侧 | 值（compose） | 用途 |
|---|---|---|---|
| `AGENT_ENGINE_HOME_ROOT` | 网关（已有） | `/data/aragon-home/engines` | 已安装引擎根目录；`<root>/<version>/node_modules/@aragon-agent/core` |
| `AGENT_ENGINE_RUNTIME_DIR` | 网关（**新增**） | `/data/engine-runtime` | 读 `current.json`（Python 写、网关读） |
| `SKILLROUTER_ENGINE_HOME_ROOT` | Python（**新增** `settings.agent_engine_home_root`） | 默认 `<data>/aragon-home/engines` | `install_home` 写入目标，必须与网关同值 |
| `SKILLROUTER_ENGINE_TOOLCHAIN` | Python（**新增**） | `gateway` | 见本节 0) |
| `SKILLROUTER_CANCEL_RECONCILE_MINUTES` | Python（**新增**） | `30` | 见 §A.3.6 重启恢复 |

三个变量都要进 `deploy/.env.example` 与 `deploy/docker-compose.yml`（api/worker 与 gateway 两侧给同值），
否则「装了但没生效」会以最难排查的形式复发。

**3) `upgrade/switch.py`**

1. **先备份**：`backup_job.quick_backup(label="pre-upgrade")`，失败 → `ENGINE_SWITCH_FAILED` 并 abort（不写指针）；
2. `install_home.install(artifact_dir, version)` → `AGENT_ENGINE_HOME_ROOT/<version>/node_modules/@aragon-agent/core`（新增文件）；
3. 调网关 `POST /internal/admin/reload`（**已是既有路径**，`agents/aragon-gateway/src/main.ts:280`），请求体带
   `{version, engine_home}`；失败 → 把 `current.json` 写回上一版本（`write_pointer` 已有），`result='failed'`；
4. 成功 → 写 `current.json` + `engine_upgrade_runs.backup_file_id` + `system_state.engine_pin` + `/healthz` 连续 3 次
   `engine.version` 匹配（已有 `health_check()`，`switch.py:50-63`）。

**4) 网关侧（`config.ts` / `engine-host.ts` / `main.ts` / `health.ts`）**

- 启动时若 `AGENT_ENGINE_RUNTIME_DIR/current.json` 存在且 `schema_version` 可读 → 用其中 `version/pin` 覆盖
  `ENGINE_PIN`（env 作为 fallback），并在 `/healthz` 回显 `pin_source: "pointer" | "env"` 与 `resolved_path`。
- `engine-host` spawn 时把 `AGENT_ENGINE_HOME_ROOT/<version>/node_modules` 放进解析路径
  （`NODE_PATH` + 显式 `require.resolve` 兜底，`engine-host.ts` 负责）。
- 指针损坏（JSON 解析失败/版本目录不存在）→ 回落 env 并记 `log.error("engine_pointer_invalid")`，**不静默**。

**5) `upgrade/compat.py` 与 `upgrade/gc.py`（新增）**

- `compat.py`：读 `aragon-home/sessions/*.json`（最多 50 个采样 + 1 次全量计数），断言顶层键集合与候选 `schemaVersion`
  对应；不一致 → `needs_migration=True` + `migration_hint`（人读的下一步），**并在切换被拦时把 hint 显示在 UI 的阻断原因里**。
- `gc.py`：保留当前指针版本 + 最近 3 个 `passed` 构建，其余删除；`switch` 成功后异步触发一次，失败只记日志，不影响切换结果。

#### A.3.8 迁移单轨（F-11）

现状三件事必须先说清，否则「改成单轨」会直接让服务起不来：

1. `0001_init`/`0002_engine_upgrade` **不是字面 DDL**，它们调用 `Base.metadata.create_all(checkfirst=True)`
   （`0001_init.py:47-49`、`0002_engine_upgrade.py:36`），并且**都已有真实 `downgrade()`**（`0001_init.py:60-65`、
   `0002_engine_upgrade.py:39-43`）。所以「补 downgrade」这项无需再做，CI 往返会证明。
2. 因为迁移读的是**当前模型元数据**，`alembic upgrade head` 在存量库上会先建出含新列的表，
   随后 `0003` 的 `ALTER TABLE ADD COLUMN` 会以 `duplicate column name` 失败。**`0003` 必须是幂等的**（见 §A.6.1）。
3. `assert_migrated()` 全仓**零调用点**（定义在 `services/bootstrap.py:153-175`，只在 `__all__` 里出现）；
   缺 `alembic_version` 时直接 `return`（`:166-168`）。F-11 的根因是「没被调用」，不是「写得不对」。

固定实现（顺序即契约）：

1. `services/bootstrap.py::bootstrap()` 的步骤顺序改为：
   `ensure_dirs → master_key → file_signing_key → migrate_or_adopt() → create_triggers() → seed_system_state → initialize_admin → cleanup_expired`。
   **迁移必须在 seed/admin 之前**，否则首次启动会在没有表的库上写种子。
2. 新增 `services/bootstrap.py::migrate_or_adopt()`：

   ```
   若 SKILLROUTER_ENV == "test" 或显式 create_all 开关 → Base.metadata.create_all() + PRAGMA user_version = SCHEMA_VERSION（快，单测用）
   否则：
     a. 空库（无 sqlite_master 表）              → alembic upgrade head
     b. alembic_version 缺失且有业务表（存量库）  → adopt_alembic.py 指纹校验 → stamp **0002_engine_upgrade** → alembic upgrade head
     c. alembic_version 存在                    → alembic upgrade head
   ```

   b 之所以 stamp 到 `0002` 而不是 head：存量库缺的是本轮新增的 6 个列，stamp 到 head 会让 `0003` 永远不执行；
   stamp 到 `0002` 后由幂等的 `0003` 补齐，若该库恰好已由新元数据 `create_all` 建过（列已存在），`0003` 的条件加列会跳过。
3. `scripts/adopt_alembic.py`（新增）：比对 `PRAGMA user_version` 与「表名集合指纹」
   （对 `sqlite_master` 里 `type='table' and name not like 'sqlite_%'` 排序后 sha256，与 `Base.metadata` 生成集合比对）；
   一致 → `alembic stamp 0002_engine_upgrade`；不一致 → 打印差异表并退出 3（**绝不强制 stamp**）。
4. `assert_migrated()` 在 `migrate_or_adopt()` 之后**必须被调用**：校验 `alembic_version` 存在且等于 head、
   `PRAGMA user_version == SCHEMA_VERSION`；`production` 下任一不满足 → `[FATAL] MIGRATION_NOT_ADOPTED`（新增启动期常量，
   只用于启动日志，不进 HTTP 码表）；`development` 下打印差异并继续。
5. `deploy/entrypoint.api.sh` 的迁移步骤**保持只调 `scripts/bootstrap.py`**（`entrypoint.api.sh:58-75` 已是这个顺序），
   不再像原文那样改成直接 `alembic upgrade head`——bootstrap 内部已包含迁移、种子与管理员初始化，
   直接跑 alembic 会跳过初始化，首启会得到一个空的平台。失败仍然 `exit 1`（`fatal()` 已如此）。
6. `migrations/versions/0003_iteration2.py`（新增，见 §A.6），`0001`/`0002` 不动。

#### A.3.9 设置项运行时生效（F-13 / F-14）

新增 `services/settings_runtime.py`：

```python
KEYS = {  # 只允许这些键从 DB 覆盖 environment；安全相关键永不进 DB
  "payload_policy", "retention_days", "fetch_allowlist", "ssrf_deny_extra",
  "engine_policy", "default_budget_usd", "agent_model",
}
def get(key: str, default=None): ...      # 60s TTL 内存缓存 → settings 表 → env
def invalidate() -> None: ...             # services/settings.update 后调用
```

- 读取点替换：`services/execution.py:179`（`payload_policy`）、`worker/handlers/retention.py:17`（`retention_days`）、
  `security/fetch_guard.py:41,53`（`fetch_allowlist`/`ssrf_deny`）、`api/admin/engine.py:44,84`（`engine_policy`）。
- 缓存一致性：单进程内 TTL 60s；`update()` 立即 `invalidate()` → **同进程内下一次请求即生效**；
  跨进程（api↔worker）最长延迟 60s，这个数字要写进设置页的说明文案（「worker 最多 60 秒内生效」）。
- **安全底线**：主密钥路径、`SKILLROUTER_AGENT_GATEWAY_SECRET`、`db_path`、`data_dir`，以及本轮新增的
  `agent_engine_home_root`/`engine_toolchain`/`cancel_reconcile_minutes` **永不从 DB 读**——它们是部署事实，不是可调策略。
  `settings_runtime` 只实现白名单，未列入的键 `get()` 直接抛 `KeyError`（防止未来有人顺手加键）。

`POST /api/admin/settings/backup/now`（F-14）：走 `require_admin`（含 CSRF）→ 同步执行 `backup_job.run_now()`；
**删除旧的 `GET` 路由**（`api/admin/settings.py:81`，不再保留任何有副作用的 GET；调用方会收到框架级
`405 Method Not Allowed`，这是可接受且诚实的失败方式），`docs/api/examples.md` 与 `apps/web` 同步改为 `apiPost`。

#### A.3.10 文件访问与签名 URL（F-15）

**签名能力已经存在，不要再造第二套**（造了就是「上传给的链接下载必失败」）：

现状：`services/files.py:39` `SIGN_TTL_S = 600`、`:149-162` `_sign()/signed_url()`、`:165-175` `verify_signature()`；
`api/v1/files.py:42-56` 的 `download` 已经同时接受 `sig+exp` 与 Bearer。真正的缺陷只有一个：
`download` 的签名参数上仍写着 `ctx: TokenContext = Depends(require_token)`（`api/v1/files.py:44`），
**无令牌请求在进入路由前就被 401**，所以文档里「签名 URL 免令牌」是假的。

本轮改动（最小且不引入第二套算法）：

1. `deps.py` 新增 `optional_token(request) -> TokenContext | None`：有 Authorization 就校验，没有返回 `None`，非法则照旧 401。
2. `api/v1/files.py::download` 改为 `ctx = Depends(optional_token)`，鉴权顺序：
   - `sig + exp` 有效（`files.verify_signature`）→ 放行，不再要求令牌；
   - 否则要求 `ctx` 且作用域含 `files:read`，且 `owner_type='token'` 时 `owner_id == ctx.token_id`（现状逻辑保留）；
   - 都没有 → `401 UNAUTHENTICATED`，`hint` 区分「链接已过期，请重新获取」与「缺少 Authorization 头」。
   **注意这是语义变更**：此前无效签名返回 `404`（`Code.NOT_FOUND`，「签名无效或已过期」），本轮统一为 401 + 明确 hint
   （404 会被误读成「文件不存在」），写进 §A.5.1 与 `troubleshooting.md`。
3. `services/files.py::save_bytes()` 返回值补 `signed_url` 与 `expires_at`（秒），直接复用 `signed_url()` 的字段
   （`describe()` 已有该字段，`:126-138`）；`api/v1/files.py::upload` 因此自动返回它们。
4. `invoke` 响应里 `output_files[]` 每项都带 `signed_url` 与 `expires_at`（由 `files.signed_url()` 统一生成，
   **不得**在 `services/execution.py` 里另写 HMAC）；`docs/api/examples.md` 同步示例。

#### A.3.11 列表契约：筛选 / 排序 / 分页（F-16 / F-17 / F-18 / F-19）

统一约定（三个端点都遵守；`deps.py::page_params`（`:90-102`）已经返回 `page/page_size/q/sort`，端点只负责用）：

| 参数 | 说明 |
|---|---|
| `q` | 前缀/包含匹配，字段由各端点声明（见下） |
| `page`, `page_size` | `page_size` 上限 100（已有 `page_params`） |
| `sort` | 逗号分隔，`-` 前缀降序；**字段白名单**，越界 → `400 VALIDATION_FAILED` |
| `date_from` / `date_to` | ISO8601；`from`/`to` 作为别名接受，命中时响应头 `X-Deprecated-Param: from,to` |

| 端点 | 新增/变化 | `q` 覆盖字段 | `sort` 白名单 |
|---|---|---|---|
| `GET /api/admin/calls` | 接受 `from/to` 别名；支持 `sort` | `request_id`、`capability_key`、`caller_id` | `created_at`、`duration_ms`、`status` |
| `GET /api/admin/tasks` | 新增 `q`、`status`、`capability_key`、`page`、`page_size`、`sort` | `request_id`、`capability_key`、`upstream_task_id` | `created_at`、`updated_at`、`status` |
| `GET /api/admin/credentials` | 确认分页 + `sort`；`status` 允许 `expiring`（`expires_at` 非空且 ≤7 天） | `name`、`kind`、`provider_name`（需要 `JOIN providers`） | `name`、`created_at`、`expires_at`、`last_verified_at` |

前端：

- `calls/page.tsx`：`FILTER_KEYS` 改 `date_from`/`date_to`；表头排序接后端 `sort`；若后端不支持的列要显式去掉 `sortable`
  （宁可不可排序，也不要假排序）。
- `tasks/page.tsx`：改用 `DataTable` + `useTableState`，与 `/calls` 完全一致的交互（D1 设计一致性）。
- `credentials/page.tsx`（新增）：`DataTable` + 状态筛选（`active/disabled/expiring`）+「即将过期」徽标 + 行内入口跳提供商详情。
- `AppShell.tsx` 导航是**扁平列表**（`:56-103`，没有「提供商与凭证」分组，本轮也不引入分组重构）：
  在「提供商」项之后新增顶层项 `{ href: '/credentials', label: '凭证' }`。

#### A.3.12 交付面：CI / entrypoint / runbook（F-20 / F-21 / F-22）

- `.github/workflows/ci.yml`（现状 8 个作业：scripts / api / gateway / cli / web / contracts / deploy / e2e）：
  - `e2e` 作业：注入 `SR_BASE_URL`（保留 `E2E_BASE_URL` 双写），先 `npm run build` 再 `npm run start`；
    `if: vars.E2E_ENABLED == '1'`，并在跳过分支 `echo "::warning::e2e skipped ≠ verified；P10 仍待验证"`。
  - **新增 `migrations` 作业**（F-21），骨架如下（照抄可用，缺任何一步作业第一次跑就会红）：

    ```yaml
    migrations:
      name: migrations (py3.11)
      runs-on: ubuntu-latest
      defaults:
        run:
          working-directory: apps/api
      env:
        SKILLROUTER_ENV: development
        SKILLROUTER_DATA_DIR: ${{ runner.temp }}/sr-data
        SKILLROUTER_DB_PATH: ${{ runner.temp }}/sr-data/mig.db
      steps:
        - uses: actions/checkout@v4
        - uses: actions/setup-python@v5
          with: { python-version: '3.11', cache: pip }
        - run: pip install -r requirements-dev.txt
        - run: python -m alembic -c migrations/alembic.ini upgrade head
        - run: python -m alembic -c migrations/alembic.ini downgrade base
        - run: python -m alembic -c migrations/alembic.ini upgrade head
        - run: python -m alembic -c migrations/alembic.ini upgrade head   # 幂等：第二次必须也是 0 退出
        - run: python -m pytest tests/test_api_contract.py tests/test_auth_flow.py -q
    ```

  - `scripts` 作业新增：workflow YAML 可解析
    （`python -c "import yaml,io;yaml.safe_load(io.open('.github/workflows/ci.yml',encoding='utf-8'))"`）+
    `bash -n deploy/entrypoint.api.sh` + 「`entrypoint.api.sh` 含 `run)` 分支」的字符串断言。
- `deploy/entrypoint.api.sh`：新增角色 `run`（`SKILLROUTER_ROLE=run` 或首参 `run`）→ `exec "${@:2}"`；
  校验顺序必须是「先判 `run`，再判 `api|worker`」，并把用法文案改成 `entrypoint.api.sh api|worker|run`（`:95-114`）。
  迁移步骤保持**只调 `python scripts/bootstrap.py`**（见 §A.3.8-5）。
- `docs/runbook/backup-restore.md`、`init.md`：把会 `UNKNOWN_ROLE` 的 `docker compose run --rm api python …`
  改为两种写法之一（都给出）：`--entrypoint python` 或 `--entrypoint /app/deploy/entrypoint.api.sh` + `run` 角色；
  并保留「先停 api/worker 再恢复」的正式流程。`engine-upgrade.md` 的备份声明与 F-23 实现对齐，
  并补「`blocked` 是什么意思、怎么消掉」一段（与 §A.3.7-4 一致）。

#### A.3.13 维护模式（F-25）

- `services/maintenance.py::is_maintenance()`：`settings.data_dir/".restore-in-progress"` 存在 → `True`；
  mtime 超过 24h → 视为陈旧标记，返回 `True` 但记 `log.error("stale_maintenance_marker")`
  （宁可 503 也不让半恢复的库被写）。
- `deps.require_admin` 之后、业务处理之前：写方法（`POST/PUT/PATCH/DELETE`）→ `503 MAINTENANCE`；
  读方法放行。`/healthz` 增 `maintenance: bool`；`/api/v1/invoke`（含 MCP `tools/call`）→ `503 MAINTENANCE`。
- UI：`AppShell` 已有横幅，改为读 `/healthz.maintenance` 并显示「维护中：正在恢复备份，请稍候（写操作已暂停）」。

### A.4 文件 / 模块变更计划（File / Module Change Plan）

> 动作：`C` 新建、`M` 修改。**表内即本轮全部改动范围**；表外的改动必须先改本表再动代码（复审按此表核对 diff）。
> 数量已按行数与实际文件核对（2026-10-19 评审）：A.4.1 43 行/43 文件（新建 7）、A.4.2 15 行（9 新 + 6 改）、A.4.3 13 行（新建 1）、A.4.4 23 行（新建 4）。
> 「意图」列是评审依据：一个文件一行话，写不出来就说明这项改动没想清楚。

#### A.4.1 后端 `apps/api/skillrouter_api`（43 个文件；其中新建 7 个）

| 文件 | 动作 | 意图 | 关闭 |
|---|---|---|---|
| `errors.py` | M | 新增 `IDEMPOTENCY_IN_PROGRESS`（409）、`MIGRATION_NOT_ADOPTED`（启动期常量）；保持 4 元组 | F-12 / F-11 |
| `deps.py` | M | `require_token` 只认 Bearer；新增 `optional_token`（签名 URL 用）；维护模式写拦截；`require_admin` 顺序不变 | F-04 / F-15 / F-25 |
| `logging.py` | M | `redact()` 增查询串规则（token/sig/signature/key/secret；`exp` 保留） | F-04 |
| `main.py` | M | 请求日志只记 `url.path`；`/healthz` 增 `maintenance`；移除任何 `str(request.url)` | F-04 / F-25 |
| `settings.py` | M | 新增 `allow_query_token`、`agent_engine_home_root`、`engine_toolchain`、`cancel_reconcile_minutes` | F-04 / F-10 / F-07 |
| `crypto/secretbox.py` | M | 顶部登记全部 `record_type` 取值（`credential`/`credential_meta`/…） | F-03 / F-32 |
| `models/execution.py` | M | 三列进模型：`call_logs.needs_reconcile`、`idempotency_keys.state`、`tasks.cancel_requested_at`（**模型与迁移必须同一提交**） | F-06 / F-07 / F-12 |
| `models/access.py` | M | `SCOPE_TYPES = ("token","agent","capability","provider","global")` 常量（枚举真相来源） | F-05 |
| `services/settings_runtime.py` | C | 白名单键 + 60s TTL 缓存 + `invalidate()` | F-13 |
| `services/settings.py` | M | `update()` 后调 `invalidate()`；返回生效范围说明 | F-13 |
| `services/maintenance.py` | C | `.restore-in-progress` → `is_maintenance()`（含陈旧标记告警） | F-25 |
| `services/credentials.py` | M | `_public_meta()` 白名单；`_brief` 增 `secret_mask`/`secret_fingerprint`/`meta_redacted_keys`/`meta_sealed`；写路径密封敏感 meta | F-03 |
| `services/providers.py` | M | 详情里的凭证摘要复用同一投影（消除两套字段名） | F-03 |
| `services/capabilities.py` | M | `_brief`/`list_capabilities` 增 4 个指标（批量聚合）；`test_capability` 返回体冻结为断言；`usage.account` 接入 | F-01 / F-02 / F-05 |
| `services/execution.py` | M | `LOGGED_STATUS` 映射；读超时不再重试/failover；幂等占位三态；五作用域 `account()`；`status_unknown`/`needs_reconcile` 返回体 | F-05 / F-06 / F-12 |
| `services/usage.py` | M | 新增 `account()`（五作用域记账 + 定价阶梯 + 返回成本）；`record()` 保留为薄封装 | F-05 |
| `services/tasks.py` | M | `ALLOWED` 增 `queued→canceling`；`cancel` 判定顺序；`mark_running` 支持 `cancel_pending`；恢复补「悬挂 canceling」两条 | F-07 / R2-05 / R2-23 |
| `services/files.py` | M | `save_bytes` 返回 `signed_url`/`expires_at`（复用 `signed_url()`，不新增算法） | F-15 |
| `services/overview.py` | M | 「待处理问题」增 `needs_reconcile` 计数 + 跳转到 `/calls?needs_reconcile=1`（I6 的落地） | F-06 |
| `executors/errors.py` | M | `map_error` 增 `transport` 细分（connect/read/reset）与 `retryable` 语义表 | F-06 |
| `executors/http_executor.py` | M | 按新语义决定是否重试；`Idempotency-Key` 透传；`needs_reconcile` 上报 | F-06 / F-12 |
| `security/ratelimit.py` | M | `check_budget` 五作用域；`record_cost` 返回累计值（写 `quota_counters`） | F-05 |
| `security/fetch_guard.py` | M | 白名单/黑名单改读 `settings_runtime` | F-13 |
| `worker/handlers/capability_invoke.py` | M | 提交后检查 `cancel_pending` 并追取消；终态记账 + 四层 `release_concurrency`（幂等） | F-05 / F-07 |
| `worker/handlers/retention.py` | M | `retention_days` 改读 `settings_runtime`；每小时清理过期 `in_flight` 幂等行 | F-13 / F-12 |
| `worker/runner.py` | M | 租约丢失取消 handler；`INVALID_STATE_TRANSITION` 不再盲判成功；`release` 传 `worker_id` | F-07 / F-08 |
| `worker/leases.py` | M | `release` 增 `worker_id` 必填 + 条件更新 + 返回 bool；`renew` 语义不变 | F-08 |
| `api/admin/calls.py` | M | `from/to` 别名 + `sort` 白名单 + `X-Deprecated-Param` | F-16 / F-17 |
| `api/admin/tasks.py` | M | 增 `q/status/capability_key/page/page_size/sort` | F-18 |
| `api/admin/providers.py` | M | `GET /credentials`（`:86`）补分页/`sort`/`status=expiring`/`q`（含 `provider_name` JOIN） | F-19 |
| `api/admin/settings.py` | M | `backup/now` 改 POST（旧 GET 删除）；权限/CSRF 不变 | F-14 |
| `api/admin/engine.py` | M | `engine_policy` 读运行时；`verify_status` 三态透出 + `blocked_reasons` + `acknowledge_blocked` | F-13 / F-26 |
| `api/v1/files.py` | M | 签名优先、`optional_token` 兜底；过期/缺头分别给 hint | F-15 |
| `api/v1/tasks.py` | M | cancel 在 `pending_submit` 时返回 `202`；响应透出 `cancel_scope`/`cancel_requested_at` | F-07 |
| `services/bootstrap.py` | M | `migrate_or_adopt()` 单轨；`assert_migrated()` **真正被调用**；缺版本即 FATAL（生产） | F-11 |
| `upgrade/toolchain.py` | C | 工具链归属：`run_build()`/`run_verify()`；`gateway`（生产）与 `local`（仅开发）两条实现 | F-09 / R2-01 |
| `upgrade/build.py` | M | 改为经 `toolchain.run_build()` 调用；无工具链 → `blocked` 且不产生候选 | F-09 / R2-01 |
| `upgrade/verify.py` | M | 重写为 artifact-first；三态；删除 `skipped`；事件集合取自网关 | F-09 / F-24 / F-26 |
| `upgrade/switch.py` | M | 先备份再切换；`install_home`；reload 走 `/internal/admin/reload`；写 `backup_file_id` | F-23 |
| `upgrade/compat.py` | C | 会话/状态兼容断言 + `needs_migration` + `migration_hint` | F-24 |
| `upgrade/install_home.py` | C | 把产物装到 `agent_engine_home_root/<version>/node_modules` | F-10 |
| `upgrade/gc.py` | C | 保留当前 + 最近 3 个 `passed` 构建 | F-24 |
| `migrations/versions/0003_iteration2.py` | C | 幂等加列（见 §A.6） | 多个 |

> `migrations/versions/0001_init.py`、`0002_engine_upgrade.py` **不改动**：两者已有真实 `downgrade()`
> （`0001_init.py:60-65`、`0002_engine_upgrade.py:39-43`），原文「需逐一确认、缺失就补」一项已确认为「已具备」。

#### A.4.2 后端测试 `apps/api/tests`（9 个新增 + 6 个改写）

| 文件 | 动作 | 覆盖 |
|---|---|---|
| `test_credentials_projection.py` | C | F-03 三条断言（无明文、密封、回调仍验签）+ `probe_path` 不被丢弃 |
| `test_token_transport.py` | C | F-04（query 通道默认关闭 + 日志脱敏 + `exp` 保留） |
| `test_budget_accounting.py` | C | F-05（四模式 + 五作用域 + `DEFAULT_PRICES` 阶梯 + 并发额度释放） |
| `test_task_cancel_race.py` | C | F-07（提交中取消 + `queued→canceling` 合法） |
| `test_lease_ownership.py` | C | F-08（越权 release、续租丢失取消 handler） |
| `test_maintenance_mode.py` | C | F-25 |
| `test_settings_runtime.py` | C | F-13（TTL + invalidate + 白名单 KeyError） |
| `test_migrations_roundtrip.py` | C | F-11（upgrade/downgrade/upgrade + **重复 upgrade 幂等** + adopt 脚本） |
| `test_upgrade_toolchain.py` | C | R2-01（网关不可达 → `blocked`；`local` 在 production 被忽略；build 产物路径在 `/data` 下） |
| `test_execution_sync_async_stream.py` | M | F-06 读超时不重投；F-12 并发同键只打一次 + 过期占位可重占 |
| `test_engine_upgrade.py` | M | F-09/F-10/F-23/F-24/F-26 六条新断言（含 `verify_status` 与 `result` 分离） |
| `test_api_contract.py` | M | F-16/F-17/F-18 + `LOGGED_STATUS` 聚合口径 + openapi 端点集合更新 |
| `test_admin_files.py` | M | F-15 签名 URL（同一签名既能上传响应给出、也能无令牌下载）；F-14 backup POST/CSRF |
| `test_capabilities_validation.py` | M | F-02 指标字段 + `test_runs[].ok` 必须是布尔 |
| `test_auth_flow.py` | M | 维护模式下的登录可用性（登录不属于业务写） |

#### A.4.3 前端 `apps/web`（13 个文件）

| 文件 | 动作 | 意图 | 关闭 |
|---|---|---|---|
| `lib/types.ts` | M | `CapabilityTestResult` 按真实体（删 `ok`/`route`/`at`）；`Capability` 增 4 指标 + `test_runs`；`Credential` 增 `secret_mask/secret_fingerprint/meta_redacted_keys/meta_sealed`；`Task` 增 `cancel_requested_at/cancel_scope`；`CallLog` 增 `needs_reconcile`；`TaskListParams` | F-01 / F-02 / F-03 / F-18 |
| `app/(app)/capabilities/[id]/page.tsx` | M | `testedOk` 从 `test_runs` 派生（`ok === true`）；发布按钮依据同源 | F-01 |
| `components/capabilities/TestPanel.tsx` | M | 显示 `used_route`/`dropped`；通过判定改 `status` | F-01 |
| `app/(app)/capabilities/page.tsx` | M | 成功率列接真字段；**新增「上次测试」列**（`null` 显示「—」） | F-02 |
| `app/(app)/providers/[id]/credentials/[cid]/page.tsx` | M | 掩码/指纹接 `secret_mask`/`secret_fingerprint`；`meta` 区展示 `meta_redacted_keys` | F-03 |
| `app/(app)/credentials/page.tsx` | C | 凭证列表：搜索/状态筛选（含 `expiring`）/排序/分页/即将过期徽标 | F-19 |
| `components/common/AppShell.tsx` | M | 顶层导航「提供商」之后新增「凭证」；维护横幅改读 `/healthz.maintenance`（不引入导航分组） | F-19 / F-25 |
| `app/(app)/calls/page.tsx` | M | 时间键名 `date_from/date_to`；排序真生效或去掉 `sortable`；`needs_reconcile` 筛选 | F-16 / F-17 / F-06 |
| `app/(app)/tasks/page.tsx` | M | 换 `DataTable`（搜索/筛选/分页/排序）；`canceling` 显示「已请求取消」 | F-18 / F-07 |
| `app/(app)/settings/page.tsx` | M | 备份改 POST；设置保存后提示「worker 60s 内生效」；计费口径变更提示 | F-14 / F-13 / F-05 |
| `app/(app)/settings/engine/page.tsx` | M | 三态徽标（`passed/blocked/failed`）+ 阻断原因 + `blocked` 禁自动升级 + `acknowledge_blocked` 二次确认 | F-26 |
| `lib/api.ts` | M | 新增/调整请求函数（tasks 列表参数、credentials 列表、backup POST） | 多个 |
| `lib/hooks/useTableState.ts` | M | 支持 `sort` 白名单与 `date_from/date_to` 命名 | F-16 / F-17 |

#### A.4.4 网关、交付与文档（23 个文件；其中新建 4 个）

| 文件 | 动作 | 意图 | 关闭 |
|---|---|---|---|
| `agents/aragon-gateway/src/config.ts` | M | 读 `AGENT_ENGINE_RUNTIME_DIR/current.json` 覆盖 `ENGINE_PIN`，回显 `pin_source` | F-10 / R2-09 |
| `agents/aragon-gateway/src/engine-host.ts` | M | 按 `AGENT_ENGINE_HOME_ROOT/<version>` 解析引擎包（`NODE_PATH` + `require.resolve`） | F-10 |
| `agents/aragon-gateway/src/main.ts` | M | `/internal/admin/reload` 增字段；新增 `/internal/admin/build`、`/internal/admin/verify-artifact` | F-09 / R2-01 / R2-10 |
| `agents/aragon-gateway/src/health.ts` | M | `/healthz` 增 `pin_source`、`resolved_path`、`maintenance` 透传字段 | F-10 / R2-10 |
| `agents/aragon-gateway/src/types.ts` | M | `ReloadResult`/`BuildResult`/`VerifyResult` 类型（客户端与服务端共享） | F-09 / R2-10 |
| `agents/aragon-gateway/test/pointer.test.ts` | C | 指针覆盖 env / 指针损坏回落 env / `pin_source` 回显 | F-10 |
| `scripts/verify_artifact.mjs` | C | 从 `dist` 读导出符号集合（供网关侧 verify 调用） | F-09 |
| `scripts/adopt_alembic.py` | C | 存量库 stamp `0002`（指纹比对，不一致退出 3） | F-11 |
| `scripts/migrate_seal_meta.py` | C | 回填 `meta_sealed`（幂等，`--dry-run` 默认） | F-03 |
| `deploy/entrypoint.api.sh` | M | 新增 `run` 角色透传；迁移保持只调 `bootstrap.py` | F-22 / F-11 |
| `deploy/Dockerfile.gateway` | M | 增 `git`（隔离构建需要）；`node:22-slim` 固定 digest 占位不变 | R2-01 |
| `deploy/docker-compose.yml` | M | api/worker/gateway 三侧注入 `AGENT_ENGINE_HOME_ROOT`/`AGENT_ENGINE_RUNTIME_DIR`/`SKILLROUTER_ENGINE_TOOLCHAIN` | F-10 / R2-01 |
| `deploy/.env.example` | M | 增 `SKILLROUTER_ALLOW_QUERY_TOKEN`、`SKILLROUTER_ENGINE_HOME_ROOT`、`AGENT_ENGINE_RUNTIME_DIR`、`SKILLROUTER_ENGINE_TOOLCHAIN`、`SKILLROUTER_CANCEL_RECONCILE_MINUTES` | F-04 / F-10 |
| `.github/workflows/ci.yml` | M | e2e 修正；新增 `migrations` 作业（含重复 upgrade）；YAML 语法自检 | F-20 / F-21 |
| `docs/api/examples.md` | M | 令牌只用头；签名 URL（上传响应 + 下载）；tasks 列表参数；backup POST；`needs_reconcile` | F-04 / F-15 / F-18 |
| `docs/runbook/troubleshooting.md` | M | 新增 `IDEMPOTENCY_IN_PROGRESS`、`MAINTENANCE`、`MIGRATION_NOT_ADOPTED`、`LEASE_LOST` 处置；签名过期 hint | 多个 |
| `docs/runbook/init.md` | M | 容器命令改 `--entrypoint python`；初始化与 `bootstrap.py` 单轨说明 | F-22 / F-11 |
| `docs/runbook/backup-restore.md` | M | 容器命令修正；`0003` 回滚注意事项 | F-22 / R2-02 |
| `docs/runbook/engine-upgrade.md` | M | 升级备份与实现一致；`blocked` 语义与「怎么消掉」 | F-23 / F-26 |
| `docs/runbook/verification-report.md` | M | 追加「迭代 2」章节：本轮命令与结果（含新增测试计数） | DoD |
| `docs/runbook/verification-checklist.md` | M | 仅允许按 §A.7.3 的口径改动状态行，新增「待验证（评审新增）」登记 | DoD |
| `docs/skills/connect.md` | M | `invoke.mjs` 退出码表补 `4 = 结果不确定`；移除 `?token=` 示例 | F-04 / F-15 |
| `docs/plans/skill-router/implementation-notes.md` | M | 追加「迭代 2 偏差与已关闭缺陷」表（含 §R2 的取舍） | DoD |

### A.5 接口设计（Interface Design）

#### A.5.1 对外 `/api/v1`（变化点）

| 端点 | 变化 | 兼容性 |
|---|---|---|
| `POST /capabilities/{key}/invoke` | 响应 `status` 取值集合固定为 `succeeded \| failed \| status_unknown`；顶层增 `status_unknown: bool`、`needs_reconcile: bool`；`output_files[]` 每项增 `signed_url`、`expires_at` | **向后兼容**（新增字段；`status` 是新取值） |
| `GET /tasks/{id}` | 增 `cancel_requested_at`、`cancel_scope`（`none/remote/local_only/pending_submit`） | 兼容 |
| `POST /tasks/{id}/cancel` | 提交中取消返回 **`202`** + `{ok: true, status: "canceling", cancel_scope: "pending_submit"}`（原先 `200 + canceled`）；上游已受理时仍 `200` | **语义变更**，已在 `docs/api/examples.md` 与 `troubleshooting.md` 同步；客户端应把 `canceling` 当作「已受理、等待上游」 |
| `GET /files/{id}` | `?sig=&exp=` 有效则免令牌；否则 Bearer（作用域 `files:read`）；两者都无效 → `401 UNAUTHENTICATED`（过期单独 hint） | **语义变更**（无效签名从 `404` 改为 `401`，避免被读成「文件不存在」） |
| `POST /mcp`（`tools/call`） | 维护模式 → `503 MAINTENANCE`（JSON-RPC error `-32000`） | 行为新增 |
| `GET /healthz` | 增 `maintenance: bool` | 兼容 |

> `cancel_scope` 的四个取值是本轮确定的完整枚举（§9.2 旧文只写了 `remote|local_only`，由 §A.10 同步）。

幂等错误（新）：

```http
POST /api/v1/capabilities/text.chat/invoke
Idempotency-Key: 8f2c…

HTTP/1.1 409 Conflict
{"error": {"code": "IDEMPOTENCY_IN_PROGRESS",
           "message": "同一幂等键的请求正在执行中",
           "hint": "等待首个请求完成，或换用新的 Idempotency-Key",
           "request_id": "req_01J…"}}
```

#### A.5.2 管理端 `/api/admin`（变化点）

| 端点 | 变化 |
|---|---|
| `GET /capabilities` | `items[]` 增 `last_test_ok`、`last_test_at`、`success_rate_24h`、`call_count_24h` |
| `POST /capabilities/{id}/test` | 返回体**冻结**为 §A.3.1 的形状（`used_route`、`dropped`、`status`） |
| `GET /credentials` | 明确分页 + `sort` + `status=expiring` + `q`；`items[]` 字段见 §A.3.2 |
| `GET /credentials/{id}` | 同投影；不再出现 `meta` 原文，改出 `meta_redacted_keys` |
| `GET /providers/{id}` | `credentials[]` 使用同一投影（字段名一致） |
| `GET /calls` | `from/to` 别名；`sort` 白名单 `created_at,duration_ms,status`；`needs_reconcile` 筛选 |
| `GET /tasks` | 新增 `q/status/capability_key/page/page_size/sort` |
| `POST /settings/backup/now` | **新增**（CSRF 必需）；旧 `GET` 删除 |
| `POST /settings` | 返回 `{"saved_at": …, "effective": {"api": "immediate", "worker": "≤60s"}}`；不可热更的键（部署事实）在 `readonly` 列表里 |
| `GET /engine/status` | 增 `pin_source`（`pointer\|env`）、`verify_status`（`passed/blocked/failed`）、`blocked_reasons[]`、`resolved_path` |
| `POST /engine/upgrade` | 请求可带 `acknowledge_blocked: bool`（`verify_status == blocked` 且要切换时必填，否则 `409 VALIDATION_FAILED`） |

#### A.5.3 内部接口（Python ↔ 网关）

网关侧只有这三个既有路径：`/healthz`、`/internal/turns*`、`/internal/admin/reload`（`main.ts:143-302`）。
本轮**沿用既有路径、只加字段与两个新端点**，不重命名（重命名会让 Python 与网关必须同版本发布）：

| 端点 | 变化 |
|---|---|
| `POST /internal/admin/reload` | 请求体 `{version, engine_home}`（`engine_home` 新增，绝对路径且必须在 `/data` 下）；响应增 `pin_source`、`previous_version` |
| `GET /healthz` | 增 `pin_source`、`resolved_path`、`engine.version`（已有）——「装了但没生效」的排查入口 |
| `POST /internal/admin/build` | **新增**：`{version, provenance, commit_sha}` → `{ok, stage, detail, artifact_dir, manifest, steps}`；在网关容器内执行 clone/npm ci/build/prune |
| `POST /internal/admin/verify-artifact` | **新增**：`{artifact_dir, expected_symbols}` → `{status, checks[{check,status,detail}]}`；只做 node 侧能做的检查（导出符号、事件名） |
| `POST /internal/tools/{name}` | 不变（12 个 `sr_*` 工具签名冻结，本轮不改工具面） |

两个新端点与其他内部端点一样要求 `X-Internal-Token`，且对 `artifact_dir` 做 `/data` 前缀校验（防目录穿越）。

#### A.5.4 CLI / 安装包

不改 CLI 契约（`install/verify/list/update/uninstall/doctor` 与 `invoke.mjs` 子命令保持冻结）。
唯一变化：`invoke.mjs` 对 `status_unknown` 响应的退出码固定为 `4`（已有码表：4 = 结果不确定），
并在 `docs/skills/connect.md` 的退出码表里补一行说明「不确定 ≠ 失败，请用 `tasks` 子命令复查」；
同时删除该文档里的 `?token=` 示例（§A.3.3）。

### A.6 数据模型（Data Model）

#### A.6.1 迁移 `0003_iteration2`（幂等；up → down 必须对称）

`0001`/`0002` 是 `create_all` 型迁移，会按**当前模型元数据**建表（见 §A.3.8）；因此 `0003` 的每一个
`ADD COLUMN` 都必须先探测是否存在，否则「模型已含新列 + 迁移再 ALTER」会以 `duplicate column name` 失败。

```python
# migrations/versions/0003_iteration2.py（要点，不是全文）
def _columns(bind, table: str) -> set[str]:
    return {r[1] for r in bind.exec_driver_sql(f"PRAGMA table_info({table})")}

def _add(bind, table: str, column: str, ddl: str) -> None:
    if column not in _columns(bind, table):
        bind.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {ddl}")

def upgrade() -> None:
    bind = op.get_bind()
    # 1) 幂等三态（F-12）
    _add(bind, "idempotency_keys", "state", "state TEXT NOT NULL DEFAULT 'done'")
    bind.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_idem_state ON idempotency_keys(state, expires_at)")
    # 2) 取消意图（F-07）
    _add(bind, "tasks", "cancel_requested_at", "cancel_requested_at TEXT NULL")
    bind.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_tasks_cancel_req ON tasks(status, cancel_requested_at)")
    # 3) 敏感 meta 密封（F-03）
    _add(bind, "credentials", "meta_sealed", "meta_sealed TEXT NULL")
    # 4) 调用记录的对账标记（F-06）
    _add(bind, "call_logs", "needs_reconcile", "needs_reconcile INTEGER NOT NULL DEFAULT 0")
    # 5) 升级运行记录的三态与阻断原因（F-23 / F-26）
    _add(bind, "engine_upgrade_runs", "verify_status", "verify_status TEXT NULL")
    _add(bind, "engine_upgrade_runs", "blocked_reasons", "blocked_reasons TEXT NULL")   # JSON 数组

def downgrade() -> None:
    # 对称：先校验可安全回滚，再按存在性删列/删索引
    ...
```

改动共 **6 列 + 2 个索引**（原文写「5 个新列」是漏算）。`down()`：

1. **前置校验**：`SELECT COUNT(*) FROM credentials WHERE meta_sealed IS NOT NULL` > 0 → 中止并提示先跑
   `scripts/migrate_seal_meta.py --unseal --out <file>`（不回滚会**永久丢失**已密封的敏感 meta）。
2. 依次 `DROP INDEX IF EXISTS` + 条件 `DROP COLUMN`（列不在就跳过）。
3. SQLite ≥ 3.35 支持 `DROP COLUMN`（`python:3.11-slim` 的 sqlite 为 3.40+，满足）；若目标环境 < 3.35，
   用「建新表 → 拷数据 → 改名」标准做法，并在迁移文件顶部注释写明检测方式（`PRAGMA` + 版本比较）。

#### A.6.2 不变式（本轮新增/收紧）

| # | 不变式 | 落地方式 |
|---|---|---|
| I1 | `idempotency_keys.state ∈ {in_flight, done}` | `CHECK` 无法后加（SQLite 限制），改为写路径断言 + `test_execution_sync_async_stream.py` |
| I2 | `tasks.status='canceling'` ⇒ `cancel_requested_at IS NOT NULL`（含 `queued→canceling` 路径） | `services/tasks.py::cancel()` 唯一入口 + `test_task_cancel_race.py` 全表扫描断言 |
| I3 | 同一幂等键最多一条 `in_flight` | `uq_idem_scope_key` 唯一索引 + `IntegrityError` 转 409（§A.3.5-1/2） |
| I4 | `credentials.meta` 不含敏感键（`_SENSITIVE_META` 匹配） | 写路径过滤 + `test_credentials_projection.py` 正则遍历断言 |
| I5 | `engine_upgrade_runs.verify_status='passed'` ⇒ 至少 1 条 `passed` 且 **0 条 blocked** 的检查记录 | `upgrade/verify.py` 写入时保证 + 测试；`result` 与 `verify_status` 不得互相顶替 |
| I6 | `call_logs.needs_reconcile=1` 的行必须能被发现（概览「待处理问题」计数 + `/calls?needs_reconcile=1`） | `services/overview.py` 计数 + `test_api_contract.py` |
| I7 | `call_logs.status` 只能由 `LOGGED_STATUS` 写入；`success_rate_24h` 的分子必须是 `success` | `services/execution.py` 单点写 + `test_api_contract.py` 断言聚合结果 |

#### A.6.3 数据回填与回滚方案

| 场景 | 方案 |
|---|---|
| 存量库接入 Alembic | `scripts/adopt_alembic.py`（指纹一致才 `stamp 0002_engine_upgrade`；不一致退出 3 并打印差异） |
| 回填 `meta_sealed` | `scripts/migrate_seal_meta.py`（幂等；`--dry-run` 默认只报告；`--unseal` 供回滚前导出） |
| 幂等行 `state` 默认值 | 迁移默认 `'done'`（历史记录都是已完成），无需回填 |
| 已计费口径变化 | 本轮起 `source` 可能是 `provider \| estimated \| unknown \| probe`（§10.1 枚举同步）；历史行不重算 |
| 回滚 | `0003` 的 `down()` 删除 6 个新列与 2 个索引；`meta_sealed` 非空时**中止**并提示先导出；写进 `docs/runbook/backup-restore.md` 的「回滚注意事项」 |

### A.7 测试与验收标准（Testing & Acceptance Criteria）

#### A.7.1 断言矩阵（P0：一条缺陷至少一条会红的断言）

| 断言 ID | 测试文件 :: 用例 | 断言内容（必须是「行为」，不是「实现细节」） |
|---|---|---|
| T-01 | `test_capabilities_validation.py::test_test_result_shape_matches_frontend_contract` | `POST /capabilities/{id}/test` 的响应键集合 == 前端类型键集合；含 `used_route`、`dropped`；不含 `ok`/`route`；`GET /capabilities/{id}` 的 `test_runs[].ok` 必须是 **JSON boolean**（不是 1） |
| T-02 | `test_capabilities_validation.py::test_brief_exposes_test_and_success_metrics` | 造 3 次调用 2 成功（落库 `status='success'`）→ `success_rate_24h == 0.666…`；从未调用 → `null`；从未测试 → `last_test_ok is None`；测试通过 → `last_test_ok is True` |
| T-03 | `test_credentials_projection.py::test_no_plaintext_secret_in_any_credential_view` | `GET /credentials`、`/credentials/{id}`、`/providers/{id}`、`/impact` 四个响应序列化后不含密钥原文与 `webhook_secret` 值；且 `GET /credentials` 仍能看到 `probe_path`（白名单不误伤） |
| T-04 | `test_credentials_projection.py::test_webhook_secret_is_sealed_and_still_verifies` | 写入含 `webhook_secret` 的 meta → `meta` 无该键、`meta_redacted_keys` 含它、`meta_sealed` 非空、回调验签通过 |
| T-05 | `test_token_transport.py::test_query_token_rejected_by_default` | `?token=` → 400 `TOKEN_INVALID` + hint；`SKILLROUTER_ALLOW_QUERY_TOKEN=1` 时通过且日志出现一次 `query_token_used` |
| T-06 | `test_token_transport.py::test_logs_never_contain_token_value` | 用日志 handler 捕获，断言记录里不含令牌原值（含 URL 中的）；签名 URL 的 `exp` **仍可读**（不被脱敏） |
| T-07 | `test_budget_accounting.py::test_stream_and_async_charge_budget` | 小预算令牌：同步成功 → 流式（同步路径）再调 → `402 BUDGET_EXCEEDED`；异步场景必须**等到任务终态**再发第 2 次（预算终态记账），此时也 402；`quota_counters.cost_usd`（当日与当月两行）单调增 |
| T-08 | `test_budget_accounting.py::test_price_ladder` | 显式价 → 用显式价；`price_book` 有行 → 用该行；只有 `DEFAULT_PRICES` → `source='estimated'` 且 `cost>0`；`unit` 不在 `DEFAULT_PRICES` 且无价目 → `cost_usd=0`、`source='unknown'`、调用仍成功 |
| T-09 | `test_execution_sync_async_stream.py::test_read_timeout_does_not_retry_post` | 假上游「收到后 sleep 到超时」→ 上游计数 == 1；响应 `status_unknown=true`、`UPSTREAM_TIMEOUT`、`call_logs.needs_reconcile=1` |
| T-10 | `test_execution_sync_async_stream.py::test_connect_timeout_retries_then_fails` | 连接失败可重试：上游计数 == `max_attempts` |
| T-11 | `test_execution_sync_async_stream.py::test_concurrent_same_idem_key_calls_upstream_once` | 并发 5 个同键 → 上游计数 == 1；其余 409 `IDEMPOTENCY_IN_PROGRESS` 或回放同一 `request_id`；另断言「过期的 `in_flight` 可被重新占位」 |
| T-12 | `test_task_cancel_race.py::test_cancel_during_submit_reaches_upstream` | 提交中取消 → 终态 `canceled`、`upstream_task_id` 非空、上游 cancel 被调用 1 次、`error_code` 不含 `INVALID_STATE_TRANSITION` |
| T-13 | `test_task_cancel_race.py::test_canceling_state_follows_invariant` | 扫描全表：任何 `canceling` 行都有 `cancel_requested_at`；且「无 upstream id 超阈值」「有 upstream id 超阈值」两类悬挂行会被 `recover_on_startup` 处理掉 |
| T-14 | `test_lease_ownership.py::test_stale_worker_release_is_rejected` | 租约被抢后原 worker `release(worker_id=…, ok=True)` → False，队列行 owner/status 不变 |
| T-15 | `test_lease_ownership.py::test_lease_lost_cancels_handler` | `renew` 失败后 handler 收到取消（事件标志置位）；队列项回到 `queued` 或保持新 owner |
| T-16 | `test_engine_upgrade.py::test_verify_blocked_without_artifact` | 无 `artifact_dir` 或网关不可达 → `verify_status='blocked'`；`needs_adapter_review` 非空；**永不 `passed`** |
| T-17 | `test_engine_upgrade.py::test_verify_fails_on_bad_manifest` | manifest 版本不符 → `failed`，`detail` 指出不一致字段 |
| T-18 | `test_engine_upgrade.py::test_blocked_blocks_auto_switch` | `policy=auto` + `blocked` → 不切换；手动切换缺 `acknowledge_blocked` → `409` |
| T-19 | `test_engine_upgrade.py::test_switch_backs_up_first_and_aborts_on_backup_failure` | 备份失败 → 无指针变更、`result='failed'`、`verify_status` 不变、`backup_file_id is None` |
| T-20 | `test_engine_upgrade.py::test_installed_home_is_resolved` | 伪造 dist 装入 home → 解析出的 `package.json.version` == 候选；`/healthz.pin_source == 'pointer'`；指针损坏时回落 `env` |
| T-21 | `test_engine_upgrade.py::test_compat_detects_session_shape_change` | 会话结构改字段 → `needs_migration=True` + `migration_hint` 非空，且切换被拦 |
| T-22 | `test_engine_upgrade.py::test_calls_during_upgrade`（保留并收紧） | 升级全程已发布能力成功率 100%（已有用例，断言不得减弱） |
| T-23 | `test_migrations_roundtrip.py::test_upgrade_downgrade_upgrade` | 空库 → head → 表数/关键列（6 列各在）→ base → 表清空 → head 再成功 |
| T-24 | `test_migrations_roundtrip.py::test_adopt_alembic_requires_fingerprint_match` | 指纹不一致 → 退出 3 且未 stamp；一致 → stamp 到 `0002` 后 `upgrade head` 补齐新列 |
| T-25 | `test_maintenance_mode.py::test_writes_blocked_reads_allowed` | 标记存在 → 写 503 `MAINTENANCE`、读 200、`/healthz.maintenance=true` |
| T-26 | `test_settings_runtime.py::test_update_takes_effect_immediately` | PATCH 后同进程下一次调用即用新值（TTL 内也生效） |
| T-27 | `test_settings_runtime.py::test_unknown_key_raises` | 白名单外的键 `get()` 抛 `KeyError`；`agent_engine_home_root`/`engine_toolchain` 不在白名单内 |
| T-28 | `test_api_contract.py::test_calls_filter_aliases` | `from/to` 与 `date_from/date_to` 结果一致，且返回 `X-Deprecated-Param` |
| T-29 | `test_api_contract.py::test_calls_sort_whitelist` | `-duration_ms` 生效；非法字段 400 |
| T-30 | `test_api_contract.py::test_tasks_list_filters` | `q`/`status`/`capability_key`/分页各一条断言 |
| T-31 | `test_admin_files.py::test_signed_url_download_without_token` | 上传响应里的 `signed_url` 不带令牌即可下载成功（**同一个签名算法**）；过期签名 401 + 「链接已过期」hint |
| T-32 | `test_admin_files.py::test_backup_now_requires_post_and_csrf` | GET 405；POST 无 CSRF 403；POST 有 CSRF 200 且产物带 sha256 |
| T-41 | `test_upgrade_toolchain.py::test_build_blocked_when_gateway_unreachable` | `ENGINE_TOOLCHAIN=gateway` 且网关不可达 → build 返回 `blocked`（不是 `failed`），且**不产生候选行** |
| T-42 | `test_migrations_roundtrip.py::test_upgrade_is_idempotent` | 连续两次 `upgrade head` 均 0 退出；在「已由 create_all 建过含新列的库」上跑也不报 `duplicate column name` |
| T-43 | `test_api_contract.py::test_call_log_status_mapping` | `LOGGED_STATUS` 覆盖执行器三种状态；`success` 落库；成功率聚合用同一常量（改字面值会让本用例红） |
| T-44 | `test_task_cancel_race.py::test_queued_cancel_is_allowed` | `queued` 任务取消 → `canceling`（不抛 `INVALID_STATE_TRANSITION`），最终 `canceled` |
| T-45 | `test_budget_accounting.py::test_agent_scope_budget` | 给 `agent` 作用域设预算并超限 → `402 BUDGET_EXCEEDED`，`details.scope == 'agent'` |
| T-46 | `test_budget_accounting.py::test_async_releases_concurrency_once` | 异步任务终态后四层 `concurrent` 归零；重复调用释放不会变负数 |
| T-47 | `agents/aragon-gateway/test/pointer.test.ts`（vitest） | `AGENT_ENGINE_RUNTIME_DIR/current.json` 覆盖 `ENGINE_PIN`；损坏回落 env；`/healthz.pin_source` 与实际来源一致 |
| T-48 | `test_admin_files.py::test_single_signature_algorithm` | `save_bytes()` 与 `describe()` 给出的 `signed_url` 用同一算法与同一 TTL 常量，`verify_signature()` 对两者都通过 |

前端与交付的断言（不进 pytest，但必须记录命令与输出；Windows 上不可用的 `grep` 已换成 Python 一行）：

| 断言 ID | 命令 | 通过标准 |
|---|---|---|
| T-33 | `cd apps/web && npx tsc --noEmit` | 0 错误（**类型即契约**：F-01/F-02/F-03/R2-04 的漂移由它兜住） |
| T-34 | `cd apps/web && npx next lint && npm run build` | 0 warning / 0 error；21+1 路由构建成功 |
| T-35 | `python -c "import yaml,io;d=yaml.safe_load(io.open('.github/workflows/ci.yml',encoding='utf-8'));s=io.open('.github/workflows/ci.yml',encoding='utf-8').read();print('SR_BASE_URL' in s, 'alembic upgrade head' in s, 'migrations' in d['jobs'])"` | 输出 `True True True` |
| T-36 | `bash -n deploy/entrypoint.api.sh` + `python -c "s=open('deploy/entrypoint.api.sh',encoding='utf-8').read();print('run)' in s)"` | 语法通过；`run)` 分支存在 |
| T-37 | `python scripts/e2e_live.py` | **≥ 58/58** 通过（既有断言不得减少），并新增 3 项：测试通过→发布按钮可用链路、维护模式 503、签名 URL 下载 |
| T-38 | `cd agents/aragon-gateway && npx tsc --noEmit && npx vitest run` | 通过；含新增 `pointer.test.ts` |
| T-39 | `cd apps/api && python -m pytest tests/ -q` | 全绿；用例总数 **≥ 197 + 47 = 244**（见下面的计数口径），0 新增 skipped |
| T-40 | `python scripts/check_file_plan.py` | 设计文件清单覆盖率仍为 100%（新增文件要同步进 §A.4 与清单脚本的期望集合） |

**计数口径（避免「≥197+32」这种对不上的说法）**：本轮新增的 pytest 用例函数数按下表逐文件累加，**≥ 47**：

| 文件 | 新增用例数 | 对应断言 |
|---|---|---|
| `test_credentials_projection.py` | 3 | T-03、T-04、`probe_path` 可见性 |
| `test_token_transport.py` | 3 | T-05、T-06、`exp` 保留 |
| `test_budget_accounting.py` | 4 | T-07、T-08、T-45、T-46 |
| `test_task_cancel_race.py` | 3 | T-12、T-13、T-44 |
| `test_lease_ownership.py` | 2 | T-14、T-15 |
| `test_maintenance_mode.py` | 1 | T-25 |
| `test_settings_runtime.py` | 2 | T-26、T-27 |
| `test_migrations_roundtrip.py` | 3 | T-23、T-24、T-42 |
| `test_upgrade_toolchain.py` | 3 | T-41 + 两条工具链选择 |
| `test_execution_sync_async_stream.py` | 4 | T-09、T-10、T-11、过期占位 |
| `test_engine_upgrade.py` | 7 | T-16…T-22 |
| `test_api_contract.py` | 4 | T-28、T-29、T-30、T-43 |
| `test_admin_files.py` | 4 | T-31（2 个函数）、T-32、T-48 |
| `test_capabilities_validation.py` | 3 | T-01、T-02、`ok` 布尔 |
| `test_auth_flow.py` | 1 | 维护模式下的登录 |
| **合计** | **47** | 断言 ID 可一对多，故断言条数（40）少于用例数 |

#### A.7.2 执行顺序（本地一次跑完，用于 `verification-report.md`）

```bat
cd apps\api
python -m alembic -c migrations/alembic.ini upgrade head
python -m alembic -c migrations/alembic.ini downgrade base
python -m alembic -c migrations/alembic.ini upgrade head
python -m alembic -c migrations/alembic.ini upgrade head
python -m pytest tests/ -q
cd ..\..
python scripts\e2e_live.py
python scripts\check_web.py
python scripts\check_gateway.py
```

> 每一步的输出（含新增测试计数）必须原样贴进 `docs/runbook/verification-report.md` 的「迭代 2」章节。
> **不接受**「全部通过」这种一句话结论。第 4 条 `upgrade head` 是幂等性证据（R2-02），不许省。

#### A.7.3 需求 → 证据 → 状态口径

本轮不新增任何「已验证」声明，只把**可在本机闭环**的部分从「待验证」升级为「已验证」，其余保持「待验证」。
状态口径不变（`docs/runbook/verification-checklist.md` 是唯一权威）：

| 需求条款 | 本轮能给出的证据 | 本轮后的状态 |
|---|---|---|
| 凭证安全（不泄漏明文 / 掩码 / 指纹） | T-03、T-04 | 可升级为**已验证（本机）** |
| 令牌安全（不在 URL、不进日志） | T-05、T-06 | 可升级为**已验证（本机）** |
| 费用与预算控制 | T-07、T-08、T-45、T-46 | 可升级为**已验证（模拟上游）**；真实厂商计价仍待验证 |
| 重试与幂等 | T-09、T-10、T-11、T-43 | 可升级为**已验证（本机）** |
| 异步任务与恢复 / 取消语义 | T-12、T-13、T-14、T-15、T-44 | 可升级为**已验证（本机）** |
| AragonAgent 升级可靠性 | T-16…T-22、T-41、T-47 | 升级为**部分验证**（脚本化流水线 + 单元/契约级证据 + 工具链归属确定）；**真实候选版本升级仍待验证（P11）** |
| 数据库迁移 | T-23、T-24、T-42 | 可升级为**已验证（本机）** |
| 文件签名访问 | T-31、T-48 | 可升级为**已验证（本机）** |
| 完整状态设计（维护/离线/无权限） | T-25 + 前端手测清单 | 部分升级 |
| 真实联调三件套（文本/语音/异步，两个提供商） | 无真实凭证 | **保持待验证（P1/P2/P3）** |
| 外部 Agent 全闭环 / MCP 实测 | 仅本机脚本（echo 上游） | **保持待验证（P5/P6）** |
| 性能基线（4C8G） | 无目标硬件 | **保持待验证（P8）** |

前端手测清单（8 项，每项记录截图或文字结果到 verification-report）：

1. 能力详情：跑一次测试 → 徽标变「测试通过」→ 发布按钮可用 → **刷新后仍可用**。
2. 能力列表：「成功率」与新增的「上次测试」两列都有真实值；无数据时显示「—」而不是 0%。
3. 凭证详情：掩码非空、指纹非「—」、`meta` 里看不到共享密钥、`meta_redacted_keys` 列出被密封的键名。
4. `/credentials` 列表：搜索 + `status=expiring` 筛选 + 分页 + 排序可用。
5. `/tasks`：搜索 `request_id`、按状态筛选、翻页可用；`canceling` 行显示「已请求取消」与 note。
6. 设置页：备份按钮（POST）返回真实产物名与 sha256；保存策略后提示「worker 60s 内生效」；计费口径变更提示可见。
7. 引擎页：`blocked` 显示阻断原因与下一步，「立即升级」需 `acknowledge_blocked` 二次确认。
8. 概览：「待处理问题」包含「待对账调用」（`needs_reconcile`），点击可跳到 `/calls` 的筛选结果。

#### A.7.4 本轮 Definition of Done

- [ ] §A.2 的 12 个 P0 与 §R2.1 的 10 个设计级 P0 全部关闭，且每个都有 §A.7.1 中对应的**通过**断言（断言曾在修复前红过）。
- [ ] 14 个 P1 与 §R2.2 的 17 个 P1 全部关闭或（仅 F-26）以最小实现收口，并在 `implementation-notes.md` 写明取舍。
- [ ] `pytest` 全绿且用例总数 ≥ 244（新增 ≥ 47，见 §A.7.1 计数口径）、`e2e_live.py` ≥58/58、
      `check_web`/`check_gateway` 全绿、前端 `tsc`+`lint`+`build` 全绿、网关与 CLI 的 `tsc`+`vitest` 全绿。
- [ ] Alembic 往返（upgrade → downgrade → upgrade → **再 upgrade**）实测通过；存量 `create_all` 库经 `adopt_alembic.py`
      后能起服务；`docs/api/openapi.json` 重新导出且无漂移。
- [ ] `docs/runbook/verification-report.md` 有「迭代 2」章节，逐条贴出命令与输出；
      `verification-checklist.md` 的状态变更只发生在 §A.7.3 允许的行。
- [ ] §A.10 列出的正文同步项全部落地（否则 §1–§18 与本节的契约仍然互相打架）。
- [ ] `git status` 里只有 §A.4 列出的文件；无构建产物、无 `var/`、无临时脚本（`_r2_edit*.py` 已删）。
- [ ] 提交信息以 `fix:` 前缀（本轮是缺陷修复，不是新功能），正文列出关闭的 F 编号与 R2 编号。
### A.8 风险与缓解（Risks & Mitigations）

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| R1 | 改 `require_token` 会让历史 curl/脚本失效 | 外部集成方 400 | 默认关闭 query 通道但保留 `SKILLROUTER_ALLOW_QUERY_TOKEN=1` 逃生阀；文档与示例同步；troubleshooting 给一行修法 |
| R2 | **本轮起流式/异步/测试路径开始计费**（`DEFAULT_PRICES` 一直存在，不是「只在有价目时计费」） | 存量小预算令牌第一次可能出现 `402` | 发布说明与设置页写明「计费口径已扩到流式/异步」；发布前用 `perf_smoke.py` + 概览金额核对一次；`402` 的 hint 直接给「提高该令牌预算」的跳转 |
| R3 | `cancel` 改 `canceling` 后前端显示「取消中」更久 | 用户以为没生效 | UI 文案「已请求取消，等待上游确认」+ `SKILLROUTER_CANCEL_RECONCILE_MINUTES`（默认 30）后自动转 `canceled(local_only)` 并说明 |
| R4 | 幂等先占位可能留下永久 `in_flight`（进程崩溃） | 同键后续请求被 409 卡死 | 占位写 `expires_at`（默认 24h）；命中 `in_flight` 且已过期 → 条件 UPDATE 抢占；`retention` 每小时清理；T-11 有断言 |
| R5 | Alembic 单轨与既有 `create_all` 库结构有微小差异 | 启动 FATAL 或漏列 | `adopt_alembic.py` 指纹比对 + `stamp 0002` + 幂等 `0003`；T-42 覆盖「已含新列的库」；先在 e2e 库上实测再写进 runbook |
| R6 | 严格三态让「升级」在无 node/无凭证环境永远 `blocked` | 用户觉得「功能退化」 | 工具链固定在网关容器（有 node），只有**无模型凭证**才 blocked；UI 写清原因 + 「确认风险后手动切换」；这是需求要的诚实，不是退化 |
| R7 | `verify.py` 重写引入回归 | 升级路径整体失效 | 保留旧检查项作为 `blocked` 的 detail 证据；`test_engine_upgrade.py` 20 项既有断言不得删减 |
| R8 | 前端 `types.ts` 改动连带多处编译错误 | 交付延期 | 先改类型、再 `tsc --noEmit` 驱动修复（编译器即清单），最后跑 build |
| R9 | 时间/人力不足导致 P1 未清 | 契约仍漂移 | 优先级固定：P0（含 §R2.1）全部 → F-13/F-16/F-17/F-18/F-19（契约一致性）→ 其余 P1；未完成的在实现记录里逐条写「未做 + 原因」 |
| R10 | `DOWNGRADE` 丢 `meta_sealed` 数据 | 不可恢复 | `down()` 前校验 `meta_sealed IS NULL`，否则中止并提示先 `--unseal` 导出；写进 runbook |
| R11 | 新增内部端点（`build`/`verify-artifact`）扩大了网关的攻击面 | 越权读写宿主目录 | 只接受 `/data` 前缀的绝对路径；`X-Internal-Token` 必填；网关不发布端口；两个端点都写审计 |
| R12 | 「工具链在网关」让网关成为升级单点 | 网关不可用时无法升级 | 降级为 `blocked` 并保留「手动 `--entrypoint` 构建」的 runbook 路径；已发布能力调用不受影响（不经网关） |

### A.9 实施顺序（每步可独立验证）

| 步 | 内容 | 完成判据（可见证据） |
|---|---|---|
| 1 | 数据层：模型 3 列 + `0003`（幂等）+ `bootstrap.migrate_or_adopt` + `adopt_alembic.py` | `test_migrations_roundtrip.py` 绿（含 T-42）；手工 `upgrade→downgrade→upgrade→upgrade` 输出贴报告 |
| 2 | 契约对齐（F-01/F-02/F-03 + R2-03/R2-04）：后端投影与状态映射 + 前端类型 | `tsc --noEmit` 0 错误；T-01…T-04、T-43 绿 |
| 3 | 安全面（F-04）：令牌通道 + 日志脱敏 | T-05/T-06 绿 |
| 4 | 资金面（F-05 + R2-06/R2-07）：`usage.account` 五作用域接入 | T-07/T-08/T-45/T-46 绿；概览费用卡片有数 |
| 5 | 执行语义（F-06/F-12 + R2-22/R2-24）：超时分类 + 幂等三态 | T-09…T-11 绿 |
| 6 | 任务语义（F-07/F-08 + R2-05/R2-23）：取消竞态 + 租约所有权 + 状态机补边 | T-12…T-15、T-44 绿 |
| 7 | 升级流水线（R2-01/R2-09/R2-10 先行）：工具链归属 + 内部端点 + verify/switch/compat/install_home/gc | T-16…T-22、T-41、T-47 绿；网关 `pointer.test.ts` 绿 |
| 8 | 运行时设置与副作用（F-13/F-14/F-25） | T-25…T-27、T-32 绿 |
| 9 | 文件与列表（F-15 + R2-08/F-16/F-17/F-18/F-19） | T-28…T-31、T-48 绿；`/credentials` 页可访问 |
| 10 | 交付面（F-20/F-21/F-22）：CI / entrypoint / runbook | T-35/T-36 绿；runbook 命令逐条核对 |
| 11 | 正文同步（§A.10）+ 全量回归 + 报告 | §A.10 清单全勾；T-37…T-40 绿；报告「迭代 2」章节成文 |

> 顺序不可调整：1→2 是硬依赖（schema 与契约），5→6 是硬依赖（重试语义影响取消判定），7→11 里的工具链必须在 verify 之前。
> 7 与 8 可以并行（不同文件），9 可与 8 并行，10 只依赖 1 与 9 的产物。

### A.10 与 §1–§18 的同步变更清单（本轮必须一并改，否则契约自相矛盾）

> 本轮只允许改 `spec.md`，因此这些「旧段落」由**本轮评审直接落在本文里**：下表左边是旧文位置，
> 右边是改后取值。实现时以改后值为准；若发现还有未列出的旧段落与 §A 冲突，先补这张表再改代码。

| 位置 | 旧文（会误导实现） | 改后（以 §A 为准） |
|---|---|---|
| §9.2 `POST /tasks/{id}/cancel` | 「返回 `cancel_scope: remote\|local_only`」 | `cancel_scope ∈ {none, remote, local_only, pending_submit}`；`pending_submit` 时 HTTP `202` |
| §9.3 同步调用响应 | 只有 `request_id/status/capability/provider/output/output_files/usage/timings/error`，无状态枚举 | 增 `status_unknown`、`needs_reconcile`；`status ∈ {succeeded, failed, status_unknown}`；`output_files[]` 增 `signed_url`/`expires_at` |
| §9.3 错误与幂等 | 无 `IDEMPOTENCY_IN_PROGRESS` | 增 409 `IDEMPOTENCY_IN_PROGRESS`（见 §A.5.1） |
| §10.1 `rate_counters` | `scope_type(token\|provider\|capability\|global)`，无 `cost_usd` | 增 `agent` 作用域；**成本在 `quota_counters.cost_usd`**，`rate_counters` 只记 `calls/concurrent` |
| §10.1 `usage_records.source` | `source(provider\|estimated)` | `source ∈ {provider, estimated, unknown, probe}` |
| §10.1 `idempotency_keys` | 无 `state` | 增 `state(in_flight\|done)` |
| §10.1 `tasks` | 无 `cancel_requested_at` | 增 `cancel_requested_at TEXT NULL` |
| §10.1 `credentials` | 无 `meta_sealed` | 增 `meta_sealed TEXT NULL`（AAD `credential_meta`） |
| §10.1 `call_logs` | 无 `needs_reconcile` | 增 `needs_reconcile INTEGER NOT NULL DEFAULT 0` |
| §10.1 `engine_upgrade_runs` | `result(success\|failed\|aborted)` | 增 `verify_status(passed\|blocked\|failed)`、`blocked_reasons JSON`；`result` 与 `verify_status` 语义分离 |
| §10.1 `engine_candidates` | `verification_status(pending\|passed\|failed\|skipped)` | 删 `skipped`，历史行按 `blocked` 读取 |
| §10.2 不变式 4 | `queued→running→(…)`，不含 `queued→canceling` | 完整状态机（见 §A.3.6 表）+ I1…I7（见 §A.6.2） |
| §11.2 凭证隔离检查点 | 只要求「不回显明文」 | 增「投影白名单 + 敏感 meta 密封 + `meta_redacted_keys`」检查点（§A.3.2） |
| §12.4 升级流水线 | 验证读 `agents/aragon-gateway` 源码、`skipped` 计入、无工具链归属、无 `install_home` | artifact-first + 三态 + 工具链在网关 + `install_home` + 先备份（§A.3.7） |
| §13.1/§13.2 信息架构 | 无 `/credentials` 页面、无三态徽标 | 增 `/credentials`（顶层导航）与 `passed/blocked/failed` 徽标 |
| §15.3 异常矩阵 | 升级验证只有「成功/失败」 | 增 `blocked` 行与「skipped 不计 passed」纪律 |
| 附录 A 错误码 | 无 `IDEMPOTENCY_IN_PROGRESS`/`MAINTENANCE`/`MIGRATION_NOT_ADOPTED`/`LEASE_LOST` | 四个都补进附录 A 与 `docs/runbook/troubleshooting.md` |
| §14.1/§14.3 运维 | 容器内 `docker compose run --rm api python …` | 改 `--entrypoint python`（或 `run` 角色），见 §A.3.12 |

---

## 0. 评审记录（Review Notes，v1.0 → v2.0）

评审方法：逐节核对 §1–§17 与需求提示词、技术栈约束、`CLAUDE.md`（本节评审时仓库尚无该文件，故以 §5/§6 的栈与编码约定、以及 `docs/plans/skill-router/aragonagent-upstream.md` 的实测事实作为一致性基准）。四个维度：**可行性**（当前栈能否实现）、**完整性**（边界/错误路径/上线风险是否覆盖）、**一致性**（是否与既有约定或本文其它章节冲突）、**规模合理性**（过度设计 / 描述不足）。

处置原则：**P0 与 P1 全部在 v2 正文直接修正**（下表「处置」列给出修复位置）；P2 仅记录，除标注「已顺手修正」者外不改动。级别定义：**P0** 不修正则核心交付无法实现或必然出错；**P1** 不修正则实现歧义、契约不一致或验收无法核对；**P2** 可改进项。

### 0.1 P0（3 项，全部已修）

| 编号 | 维度 | 问题 | 处置（v2 修复位置） |
|---|---|---|---|
| R01 | 一致性 / 可行性 | 「多提供商路由」在数据模型上不成立：`capabilities.key UNIQUE` 且 `provider_id`/`credential_id` 为单值，而 §7.7 却说候选集是「同一 `capability.key` 下**可跨提供商**的已发布版本行」，D12 又要求等价性靠 `compatibility_group`。需求「同一标准能力能够配置多个兼容的提供商或模型，支持固定指定、优先级选择、健康检查和受限故障切换」因此无法实现。另外 v1 从未说明**可编辑草稿**存在哪张表（`capability_versions` 是不可变快照）。 | §7.6 能力定义改为「标准能力 + `routes[]`（每条 route 绑定 provider/credential + 请求/映射/限制）」；§7.7 候选集改为 route 级；§7.5 凭证引用方式同步；§10.1 新增 `capability_routes` 表、从 `capabilities` 移除单值 provider/credential、并新增 `capabilities.draft_definition JSON` 明确「草稿态存 `capabilities`，发布时冻结进 `capability_versions`」 |
| R02 | 一致性 / 可行性 | §7.9 导出的 `SKILL.md` 写 `allowed-tools: [sr_invoke_capability, sr_capabilities_list]`，但 `sr_*` 是**网关内部**工具，外部 Agent（AragonAgent / Claude Code）环境里不存在；上游事实明确「无法解析的工具名会让整份声明失效（不是变窄）」。同时全文未定义外部 Agent 实际用什么通道调用平台（HTTP 需要 shell，MCP 尚未实测）。核心闭环（安装 → 发现 → 调用）因此不可按文档执行。 | §7.9 重写导出契约：frontmatter 只声明目标环境真实存在的工具（`bash`），调用由随包安装的 `invoke.mjs` 助手完成；MCP 通道未实测前不进 frontmatter；§9.8 定义安装产物与 `--transport`；§8.5 新增模板文件 |
| R03 | 可行性 | §7.8 写「同步调用也入队以便审计与限并发」，与 §4.4(c) 的同步直返路径、§15.4 的「同步调用平台开销 ≤150ms」「管理接口 P95 ≤500ms」冲突；且 API 进程无法把 worker 的执行结果作为同步响应体返回。 | §7.8 明确：同步/流式在 **API 进程内**执行（令牌/提供商/能力三级信号量 + 直接写 `call_logs`），队列只承载异步调用与长任务（`file_parse`、`agent_turn`、`skill_import`、`engine_upgrade` 等） |

### 0.2 P1（15 项，全部已修）

| 编号 | 维度 | 问题 | 处置（v2 修复位置） |
|---|---|---|---|
| R04 | 完整性 | 异步提交的时机自相矛盾：§4.4(d) 是「先建任务并返回 202，worker 再提交上游」，§7.6 执行器步骤 6 是「异步：提交 → 写 `upstream_task_id` → 返回 202」。直接影响 §15.4 的「异步提交 1s 内返回」与 `status_unknown` 判定。 | §4.4(d) 与 §7.6 统一为：请求内只做鉴权/校验/幂等/预算/持久化/入队（不触达上游），上游提交与轮询全部在 worker；`queued` 表示「平台已受理，尚未提交上游」 |
| R05 | 完整性 / 安全 | `idempotency_keys(idem_key)` 全局唯一：令牌 A 用过的幂等键会被令牌 B 命中并返回**他人**的 `request_id` 与状态，且可被用来探测他人请求。 | §10.1 改为唯一索引 `(scope, idem_key)`，`scope = token:<id>` / `admin:<id>`；§7.6 幂等步骤同步 |
| R06 | 一致性 | D11 说 MCP 工具名是 `capability.<slug>`，§9.7 说 `capability__<key>`；前者含 `.`，违反 MCP 工具名 `[a-zA-Z0-9_-]` 约束。 | §3 D11 与 §9.7 统一为 `capability__<key 归一化>`（`.` → `_`） |
| R07 | 可行性 | §12.4 隔离构建执行 `npm ci --omit=dev --ignore-scripts` 后再 `npm run build -w packages/core`，但 `typescript`/构建脚本在 `devDependencies` 中 → 干净目录下构建必然失败，升级流水线的首个可执行步骤就断。 | §12.4 拆为「构建阶段 `npm ci --ignore-scripts`（含 dev）+ `npm run build`」与「产物阶段单独 `npm ci --omit=dev` 裁剪运行时依赖」 |
| R08 | 可行性 | §12.4 切换步骤要求「重启网关进程（`POST /internal/admin/restart` 或容器重启）」，但 API 进程没有容器/进程管理权限；给它挂 Docker socket 会把平台域权限扩大到可控制宿主机，违反 §4.3 信任域。 | §7.12 与 §12.4 引入网关内 `engine-host` 子进程：`/internal/admin/reload` → 起新子进程 → 健康探测 → `abort()+waitForIdle()` 排空旧子进程 → 退出；无需 Docker socket、不依赖信号（规避 Windows 信号差异） |
| R09 | 可行性 | §7.4/§10.3 的备份命令是 `sqlite3 ... ".backup ..."`，而运行镜像是 `python:3.11-slim`（不含 `sqlite3` CLI），生产镜像里备份脚本直接不可用；`tar` 在 Windows 无对应命令。 | §7.4/§10.3 改为 Python `sqlite3.Connection.backup()` + `zipfile` 打包（跨平台、无外部二进制依赖） |
| R10 | 完整性 | 「接入流程」（资料解析 → 草案 → 校验 → 测试 → 样例 → 结果检查 → 发布）与 `sr_request_approval` 的审批记录没有任何持久化模型，`/agent` 页要求的「步骤级状态、失败可从该步继续」无法实现；审批超时拒绝也无处记录。 | §10.1 新增 `agent_flows`、`agent_flow_steps`、`agent_approvals`；§7.12/§9.2 绑定到 `agentic_flow.py` 与 `/agent/flows*` |
| R11 | 完整性 | 配额维度不足：`quota_limits` 声明了 provider/capability 级 `rpm`/`concurrency`，但计数器只有 token 级窗口计数与日/月计数 → provider 级「每分钟请求数」和「并发数」无法统计，需求「按提供商、能力和接入 Agent 设置速率、并发和预算」落不了地。 | §10.1 新增通用 `rate_counters(scope_type, scope_id, window_start, window_seconds, calls, concurrent)`，`token_rate_counters` 收敛为它的一个 scope |
| R12 | 一致性 | 错误码前后不一致：正文 §7.6/§9.3 用 `INPUT_SCHEMA_INVALID`，附录 A 写 `CAPABILITY_SCHEMA_INVALID`；正文使用但附录缺失 `DB_BUSY`、`INVALID_STATE_TRANSITION`、`PROVIDER_IN_USE`、`NOT_INITIALIZED`、`SKILL_DEPENDENCY_MISSING`。前端约定「只根据 `code` 分支」，缺失即失配。 | 附录 A 统一为 `INPUT_SCHEMA_INVALID` 并补齐全部正文使用的错误码（§附录 A） |
| R13 | 完整性 | 「未初始化」状态下其它接口的行为未定义。需求要求「生产环境完成初始化后才开放正常业务功能」，但 §4.4(a) 只描述了初始化与改密两个分支，没有说明初始化闸门与「改密未完成」闸门的区别。 | §7.3 增加统一闸门依赖：`admin_initialized` 未置位 → 除 `GET /healthz`、`GET /api/admin/auth/bootstrap`、`POST /api/admin/auth/login` 外一律 `409 NOT_INITIALIZED`；改密未完成 → `403 PASSWORD_CHANGE_REQUIRED`。初始化由启动流程完成（§7.1），**不提供匿名初始化接口** |
| R14 | 完整性 | §15.3 引用 `test_credentials_verify.py`、§16 引用 `docs/runbook/verification-checklist.md`，但 §8 文件计划中没有这两个文件 → §15.5 的 DoD 无法逐项核对。 | §8.2 补 `tests/test_credentials_verify.py`；§8.5 补 `docs/runbook/verification-checklist.md`；§15.3 引用同步 |
| R15 | 完整性 | 能力定义没有 `examples` 字段，而需求要求发布内容包含「调用示例」，§13.2 `/capabilities/[id]` 也要展示示例；发布证据只有测试记录，无法证明「示例真实可执行」。 | §7.6 能力定义增加 `examples[]`，发布校验要求 ≥1 条示例且该示例在 `capability_test_runs` 中有 `ok=1` 记录；§9.2/§9.3 输出同步 |
| R16 | 完整性 | 影响面只覆盖凭证。需求要求「变更或删除时展示受影响对象」，但能力/版本删除、停用时没有「依赖我的 SKILL / 令牌作用域」影响面；§10.2 不变式也只约束凭证。 | §7.7 增加 `capabilities.impact()`（SKILL 依赖、令牌作用域、幂等/任务在途引用）；§10.2 增加能力侧不变式；§9.2 `/capabilities/{id}/impact` |
| R17 | 完整性 | SKILL 缺少「启用/停用」（需求明确列出），`skills.status` 只有 `draft/published/deprecated`，「停用」与「废弃」语义混在一起，且停用不应改变已发布版本。 | §7.9/§10.1 为 `skills` 增加独立 `enabled` 开关（停用只影响分发与执行，不改变 `status` 与版本不可变性）；`/api/v1/skills` 只返回 `enabled=1` |
| R18 | 完整性 / 一致性 | 能力定义只有单值 `mode`，无法表达「同一能力同时支持 sync 与 stream」（如 chat 能力）或「文件输入/输出」；而需求明确要求「每项能力需要明确支持同步、异步、流式、文件输入、文件输出中的哪些方式」，§9.2 的能力详情也已经承诺返回复数 `modes`。 | §7.6 改为 `modes: string[]` + `file_io: {input, output}`；§7.6 校验规则、§7.7 路由过滤、§9.3 调用校验同步改为集合语义 |

### 0.3 P2（8 项：7 项已按记录修正，1 项保留）

| 编号 | 维度 | 问题 | 处置 |
|---|---|---|---|
| P01 | 一致性 | §9.2「管理端 REST」表格里混入了一整块对外 `/api/v1` 端点（表头却写「统一调用（对外）」），章节归属错位。 | 已修正：拆为 `#### 9.2.1 对外统一接口（/api/v1）` 小节标题 |
| P02 | 完整性 | 文件签名 URL 用 HMAC，但签名密钥来源未定义（用主密钥会牵连轮换，用会话密钥会导致重启后链接失效）。 | 已修正：§7.1 增加 `SKILLROUTER_FILE_SIGNING_SECRET`，§7.11 明确其独立性与轮换行为 |
| P03 | 一致性 | `SKILLROUTER_TELEMETRY_EGRESS` 名字与语义不符（它控制的是版本检测/构建的出站策略，无遥测上报）。 | 已修正：新增规范名 `SKILLROUTER_EGRESS_POLICY`（`allow\|deny`），旧名保留为别名以兼容部署；§12.4 同步 |
| P04 | 规模 | `sse_events` 与 `agent_events` 双写同一批事件，写入放大与一致性风险。 | 已修正：§10.1 明确 `agent_events` 为 Agent 流权威源、`sse_events` 仅承载非 Agent 通道重放，实现时不得双写同一事件 |
| P05 | 完整性 | MCP 侧无 `notifications/tools/list_changed`，能力新增后客户端只能靠重连或轮询感知。 | **保留（记录在案）**：本阶段用 `tools/list` 的 `ETag` 语义 + 客户端刷新；协议通知待 MCP 通道实测后补，不在本阶段承诺 |
| P06 | 一致性 | 浮窗快捷键 `Ctrl+J` 与 Chrome「下载」冲突且无法被页面拦截，属于「宣传了却按不动」的交互。 | 已修正：§13.3 改为 `Alt+J`，并在 `Ctrl+K` 面板内暴露同一动作；`Ctrl+J` 不再作为承诺 |
| P07 | 一致性 | §10.3 允许 `0002_engine_upgrade` 与 `0001_init` 合并，与 §7.2「每次 schema 变更必须新增 revision」轻微冲突。 | 已修正：§10.3 限定「首次交付且无部署实例时可合并；一旦有部署，任何变更必须新增 revision」 |
| P08 | 完整性 | 凭证过期提醒依赖定时任务，但 §7.8 的 handler 清单里没有对应作业，`retention_sweep` 是否兼任未说明。 | 已修正：§7.8 新增 `expiry_scan` handler（同时负责审批超时置 `expired`），概览侧仍实时计算 |

### 0.4 未发现问题的维度（供下游参考）

- **凭证隔离**：D5（`getApiKey` 单点注入）、§11.2 五个检查点与上游实测事实（`getApiKey` 返回值不进消息历史）一致，无需改动。
- **Agent 权限边界**：D4 + `policy.ts` 白名单 + §12.2 四态能力状态自洽，且与「工具即权限边界」的上游事实吻合。
- **规模合理性**：整体规模与需求条目一一对应（16 条需求域 → 14 个模块 → 300+ 文件），未发现明显过度设计；M0–M10 里程碑可独立验证，规模判为**合适**。

---

## 1. 概述

Skill Router 是一个面向智能体的「能力中枢」：管理员在一个 Web 控制台里集中管理**上游 AI 服务的 API 凭证**，把每个上游接口描述成一个带输入输出 Schema、认证引用、调用模式与错误映射的**能力（capability）**，再用**平台内置 Agent** 完成接口理解、配置生成、真实联调与能力校验；验证通过的能力与组合而成的 **SKILL** 通过版本化发布对外提供，外部 Agent（AragonAgent / Claude Code / 任意支持 MCP 或 HTTP 的运行时）只需安装一次连接包、拿到一个平台访问令牌，即可按授权范围**动态发现并调用**平台已发布的能力，而永远接触不到上游真实密钥。

之所以要做这件事：今天每个团队都在同时使用多个模型厂商与生成式服务，密钥散落在 `.env`、CI 变量和每个人的本地配置里；「某个 SDK 能不能用某个模型」「这张图该走哪家」「上次为什么失败」这类问题没有统一答案；把「理解一个接口 → 配好 → 验证 → 交给别的 Agent 用」这条链路上的每一步都手工做完，成本高且不可复现。Skill Router 把这条链路产品化：**凭证集中、能力结构化、联调可追溯、分发标准化、调用可观测**，并让平台内置 Agent 承担其中最耗人力的接口理解与联调工作。

本阶段（Iteration 1 的实现目标）交付一个可真实运行的单机生产级系统，完成完整闭环：`初始化管理员 → 配置凭证 → 接入 API 能力 → 真实联调验证 → 发布 SKILL → 外部 Agent 安装连接包 → 发现能力 → 调用 → 查看调用结果`。管理员在首次登录后必须修改默认密码，服务重启或版本升级不得复活默认凭证；平台的普通 API 能力在执行时**不依赖 Agent 进程**，Agent 停止、崩溃或升级期间，已发布能力仍必须可调用。

---

## 2. 目标与非目标

### 2.1 目标

| 编号 | 目标 | 验收锚点 |
|---|---|---|
| G1 | 单管理员安全初始化与登录体系 | 默认 `admin/adminjr` 首次登录强制改密；`argon2id` 哈希；安全 Cookie 会话；失败限流；CSRF 双提交 |
| G2 | 提供商 / 凭证 / 能力三层资产模型 | 一提供商多凭证、多能力；关联关系可视化；引用计数与影响面提示 |
| G3 | 凭证零泄露 | 上游密钥信封加密落库，主密钥在库外；前端只显示掩码；聊天、日志、导出包、npm 包中零明文 |
| G4 | 能力配置与真实执行引擎 | 结构化 Schema、参数映射、同步/异步/流式/文件四种模式、结果解析、错误映射、超时与重试 |
| G5 | 平台内置 Agent（AragonAgent） | 独立 `AragonAgentAdapter`；流式事件、工具进度、停止、排队、重试、会话历史；凭证经受控执行层注入 |
| G6 | Agent 驱动接入闭环 | 资料解析 → 草案 → 结构校验 → 连接测试 → 最小样例 → 结果检查 → 发布，每步有状态、可续跑、发布有真实证据 |
| G7 | SKILL 管理与分发 | CRUD/版本/导出/导入（Git、URL、本地）、沙箱检查、依赖能力、npm CLI 安装器、MCP 与 HTTP 复用同一套能力定义 |
| G8 | 对外统一接口 `/api/v1` | 发现、详情、调用、异步任务查询、取消、文件访问 + 与实际一致的 OpenAPI |
| G9 | 任务、审计与可观测 | 持久化任务队列（租约/超时/恢复）、调用记录、费用与配额、管理操作审计（脱敏） |
| G10 | 引擎长期演进 | 双通道版本检测（git tag + npm dist-tag）、隔离构建、兼容性/回归验证、原子切换、失败回滚、版本与依赖记录 |
| G11 | 顶级交互与视觉 | 与 Anthropic 产品同级的信息架构、状态设计与克制的视觉语言；键盘可达；窄窗可用 |

### 2.2 非目标（本阶段明确不做）

- **N1** 多租户 / 组织 / 团队 RBAC（数据模型预留 `workspace_id` 字段但固定为 `default`）。
- **N2** 计费、发票、真实扣费；费用只做「上游返回的实际用量」与「平台按价目表估算」两种展示。
- **N3** 分布式多机部署（队列与事件基于 SQLite；接口层已抽象，后续可换 Postgres + Redis）。
- **N4** 上游 OAuth 授权码全流程与动态签名（如 AWS SigV4）的内置实现：仅提供 `auth_adapter` 扩展位与明确的支持范围说明，本阶段落地 `api_key / bearer / basic / header` 四种。
- **N5** 移动端专属布局（响应式到 768px 即可）。
- **N6** Agent 引擎的文件系统沙箱：上游明确「没有文件系统沙箱」，本平台的对策是**不给 Agent 任何文件/网络工具**（见 D4），而不是假装隔离。

---

## 3. 关键决策（ADR）

| 编号 | 决策 | 选择 | 理由 | 代价 / 缓解 |
|---|---|---|---|---|
| D1 | 认证模型 | 单管理员账号 + 服务端会话（`admin_sessions` 表），不用 JWT | 需要「立即失效会话」「改密后踢掉旧会话」「审计」，服务端会话最直接 | 每请求一次索引查询；已由 SQLite 索引覆盖 |
| D2 | Agent 引擎接入方式 | **SDK 内嵌**：Node 边车 `agents/aragon-gateway` 引入 `@aragon-agent/core@0.2.34`，Python 通过内部 HTTP/SSE 访问 | 需求要求「Agent 通过平台注册的受控工具访问凭证/能力/SKILL」；上游 CLI 只暴露固定 16 个宿主工具，无法注册平台工具；SDK 允许 `defineTool` 注入工具并 `subscribe()` 收事件 | 多一个 Node 运行时与边车进程；用 Docker Compose 单镜像多进程与健康检查覆盖 |
| D3 | CLI 角色的界定 | `@aragon-agent/cli` 只用于**一致性校验与冒烟**（`aragon info --json` / `aragon doctor --json` / `aragon exec` 契约测试），不作为业务通道 | 上游明确「ignore unknown event types」的契约与 CLI 的 `schemaVersion:1`，用它验证升级兼容性最便宜 | 需要在 CI 安装 CLI；作为 devDependency + 升级流水线依赖 |
| D4 | Agent 权限边界 | 网关**只注册白名单内的 `sr_*` 平台工具**，一个上游内置工具都不注册 | 上游 SDK 的 `tools` 数组即最终工具集：不注册 `bash`/`write_file`，模型看不到也规划不到；升级新增的上游工具默认不注册，必须管理员显式开启 | 平台工具能力边界需要设计到位（§12.3） |
| D5 | 凭证注入点 | `getApiKey` 钩子在**上游请求发出的瞬间**从凭证代理取明文，仅在网关进程内存存活 | 模型上下文/聊天记录/日志/导出包都不可能包含密钥 | 网关需短时令牌访问凭证代理（同机回环 + 共享密钥 + 60s TTL） |
| D6 | 数据库 | SQLite + WAL + `foreign_keys=ON` + `busy_timeout=5000` + `synchronous=NORMAL`，单写者串行化（写事务经 `db/writer.py` 排队） | 单机部署要求，读写并发足够（10 并发 P95 < 500ms 可达成） | 长事务会阻塞写；规定写事务 ≤ 200ms，长任务一律异步化 |
| D7 | 任务队列 | 数据库租约队列（`task_queue` + `lease_expires_at` + 心跳），不用 Celery/Redis | 重启恢复必须真实：状态即数据；单机无需额外中间件 | 吞吐上限约 50 task/s，满足本阶段 |
| D8 | 版本模型 | 「草稿 → 测试 → 发布 → 停用」四态 + 不可变版本行（`capability_versions` / `skill_versions`），`published_version_id` 指针 | 需求要求已发布版本不可变、可 diff、可回退、依赖可追溯 | 每次修改多一次插入；用行级 `content_hash` 去重 |
| D9 | 幂等 | `idempotency_keys(idem_key, fingerprint)` + 唯一索引；超时且无法判定上游是否受理时任务进入 `status_unknown` 并优先查询 | 收费请求不能盲目重投；需求明确「不能把停止本地等待当作远端已取消」 | 需要每个执行器实现 `query(task)` 能力探测 |
| D10 | 推流通道 | SSE（`text/event-stream`），事件带 `id`（单调序号）+ `Last-Event-ID` 断线重放；不用 WebSocket | 单向推送足够；断线恢复语义天然 | Agent 输入走普通 POST，不需要双向帧 |
| D11 | MCP 版本 | MCP `2025-06-18`，Streamable HTTP（`POST /api/v1/mcp` + `GET` 用于服务端消息），工具名 = `capability__<key 归一化>`（`.` → `_`，满足 MCP 工具名 `[a-zA-Z0-9_-]`），另暴露 `capabilities_list` / `skills_list` / `skills_read` / `task_status` / `task_cancel` | 与外部 Agent 生态一致；与 HTTP 复用同一套能力定义/鉴权/路由/记录；工具名不允许 `.`（v2 修正 v1 的 `capability.<slug>` 写法） | 需要维护协议版本矩阵与兼容测试 |
| D12 | 路由等价判定 | 能力「可路由等价」必须由**审批过的 schema 指纹 + 显式声明**决定，绝不按名称相似匹配 | 需求明确禁止名称相似即互换 | 每个能力多维护一个 `compatibility_group` |
| D13 | 上游版本来源 | 双通道：GitHub tag（`cli-v*` / `core-v*`）+ npm `dist-tags`；记录 `provenance`（`tagged` / `npm-only` / `branch-commit`） | 实测存在偏差：npm `latest` 为 core 0.2.37 / cli 0.6.25，而 git `main@0a119a6` 的 tag 只到 core 0.2.34 / cli 0.6.22 → 只信一侧会漏版本或失去可追溯性 | 需要「无 tag 的 npm 版本」走隔离构建 + 完整性校验后才能成为候选 |
| D14 | 前端技术选择 | Next.js 15 App Router + React 19 + TypeScript 严格模式 + TanStack Query v5 + react-hook-form + zod + Tailwind + Radix primitives + lucide-react | 一致的表单/校验/请求状态机制；Radix 提供可访问性原语 | 组件需自建样式层（`components/ui`），不引入重型 UI 套件 |
| D15 | npm 包策略 | 包名 `@skillrouter/cli`，发布前以**本地 tarball**（`npm pack` 产物）+ `npx ./skillrouter-cli-<ver>.tgz` 形式分发；页面上的安装命令由后端根据 `install_mode` 配置生成 | 需求禁止展示无法执行的公共安装方式 | 发布链路一旦就绪只需切一个配置项 |
| D16 | 外部资料处理 | 外部网页/文件/SKILL 内容一律标记为 `untrusted`，在提示中包裹为数据块；抓取受 `ssrf_guard`（DNS 解析后 IP 白名单校验、大小/耗时/重定向上限） | 防止提示注入与内网访问 | 白名单需管理员显式配置，默认拒绝私网 |

---

## 4. 总体架构

### 4.1 运行时拓扑

```
                    ┌──────────────────────────────────────────────┐
  Browser           │  apps/web  (Next.js 15, App Router)          │
  (管理员)  ───────► │  概览/提供商/凭证/能力/SKILL/Agent/调用/设置  │
                    │  全局浮窗 Agent 面板 (SSE)                    │
                    └───────────────┬──────────────────────────────┘
                                    │ HTTPS, HttpOnly cookie, X-CSRF-Token
                                    ▼
                    ┌──────────────────────────────────────────────┐
                    │  apps/api  (FastAPI)                         │
                    │  /api/admin/*  (cookie+CSRF, 管理员)          │
                    │  /api/v1/*     (Bearer 平台令牌, 外部 Agent)  │
                    │  安全层: 会话/CSRF/限流/令牌/审计             │
                    │  服务层: 凭证/能力/路由/执行/SKILL/版本       │
                    └───┬───────────────┬──────────────┬───────────┘
                        │               │              │
              ┌─────────▼──────┐  ┌─────▼──────┐  ┌────▼─────────────┐
              │ SQLite (WAL)   │  │ Worker 进程 │  │ 凭证代理(同进程)  │
              │ + 持久化文件目录│  │ 租约/重试/流│  │ AES-256-GCM 开封  │
              └────────────────┘  └─────┬──────┘  └────┬─────────────┘
                                        │              │
                      内部 HTTP + SSE   │              │ 出站 HTTP(受控)
                    ┌───────────────────▼───┐   ┌──────▼─────────────────┐
                    │ agents/aragon-gateway │   │ 上游 AI 服务            │
                    │ Node 22 + core@0.2.34 │──►│ Anthropic/OpenAI/Google │
                    │ 平台工具 sr_* (白名单) │   │ 兼容端点/图像/视频/语音 │
                    │ SSE → Python          │   └────────────────────────┘
                    └───────────────────────┘
                                        ▲
                                        │ 引擎升级流水线（隔离构建 + 验证 + 切换 + 回滚）
                                        │
        ┌───────────────────────────────┴───────────────────────────────┐
        │ 外部 Agent 运行时: AragonAgent / Claude Code / 任意 MCP 客户端 │
        │ 安装 @skillrouter/cli → 写 SKILL.md → 连通性检查 → 动态发现    │
        └───────────────────────────────────────────────────────────────┘
```

### 4.2 进程与端口

| 进程 | 技术 | 端口（默认） | 职责 | 健康检查 |
|---|---|---|---|---|
| `web` | Next.js standalone | 3000 | 管理控制台 SSR/CSR、浮窗 Agent | `GET /api/health` |
| `api` | uvicorn + FastAPI | 8000 | 所有 REST/SSE、鉴权、执行编排、文件服务 | `GET /healthz`（含 DB 与迁移版本） |
| `worker` | `python -m skillrouter_api.worker` | — | 任务领取、上游长调用、流式转发、文件解析、引擎升级作业 | 心跳写 `worker_heartbeats` |
| `gateway` | Node 22 + Fastify | 8500（仅回环） | AragonAgent SDK 宿主、平台工具、事件转发 | `GET /healthz`（含引擎版本、工具清单摘要） |
| `proxy`（可选） | nginx / Caddy | 443 | TLS 终结、SSE 关闭缓冲 | — |

约定：`api` 与 `worker` 共用同一 Docker 镜像不同入口命令；`gateway` 单独镜像（Node 基础镜像），通过 `AGENT_GATEWAY_SHARED_SECRET`（32 字节随机）与 `X-Internal-Token` 头认证，且 `AGENT_GATEWAY_BIND=127.0.0.1`。

### 4.3 信任域

1. **浏览器域**：只能拿到掩码凭证、能力定义、调用记录（脱敏）。所有管理接口后端鉴权，前端路由守卫只是体验层。
2. **平台域（api/worker）**：唯一持有主密钥（`SKILLROUTER_MASTER_KEY`）与解密能力的域。所有上游密钥在此域内解密后立即注入请求或交给网关的短时令牌通道。
3. **网关域（Agent 引擎）**：只持有「平台工具」与「模型 API Key 的瞬时值」。不持有数据库连接、不持有上游业务密钥、不能读文件系统（未注册文件工具）。
4. **上游域**：任意第三方服务；一切响应都按不可信数据处理（大小/时长/类型上限）。
5. **外部 Agent 域**：只持有平台访问令牌（可撤销、可限流、可限预算）。

### 4.4 关键时序

**(a) 初始化与首次登录**

1. `api` 启动时读 `SKILLROUTER_ADMIN_USER` / `SKILLROUTER_ADMIN_PASSWORD`（默认 `admin` / `adminjr`）与 `system_state.admin_initialized`。
2. 若未初始化：写入 `admin_users`（`must_change_password=1`，`password_hash=argon2id(初始密码)`），置 `system_state.admin_initialized=1` 与 `initialized_at`。
3. 若已初始化：**绝不**再创建或重置账号（即使环境变量被改回默认）——这是需求「重启/升级不得重新创建默认密码」的硬约束。
4. 登录成功但 `must_change_password=1` → 会话标记 `scope=password_change`，除 `POST /api/admin/auth/change-password`、`GET /api/admin/auth/me` 外的所有管理接口返回 `403 PASSWORD_CHANGE_REQUIRED`。
5. 改密成功：清 `must_change_password`，吊销该管理员其它会话，写审计 `admin.password_changed`，签发正常会话。
6. **初始化闸门**（`deps.py` 中按顺序判定，覆盖 `/api/admin/*` 与 `/api/v1/*`）：
   - `system_state.admin_initialized != '1'` → `409 NOT_INITIALIZED`。白名单仅 `GET /healthz`、`GET /api/admin/auth/bootstrap`、`POST /api/admin/auth/login`。初始化只由 `bootstrap.py`/启动流程完成，**不暴露匿名初始化接口**，因此「生产环境完成初始化后才开放正常业务功能」由该闸门保证。
   - `must_change_password = 1` → 除 `POST /api/admin/auth/change-password`、`GET /api/admin/auth/me`、`POST /api/admin/auth/logout` 外一律 `403 PASSWORD_CHANGE_REQUIRED`。
   - 两者都通过才进入业务路由。

**(b) Agent 接入一个上游 API 并发布能力**

1. 管理员在浮窗面板上传资料（URL / OpenAPI / cURL / 文件）并选择凭证引用 `credential_id`。
2. `POST /api/admin/agent/sessions/{id}/messages` 持久化消息 → 任务 `agent_turn` → 网关 `POST /internal/turns`。
3. 网关用 `Agent`（`systemPrompt=平台策略`，`tools=sr_* 白名单`）执行 `prompt()`；平台工具 `sr_fetch_source` / `sr_parse_document` 把资料转成结构化摘要回注上下文。
4. Agent 调 `sr_propose_capability_draft` 提交草案（含 `routes[]` 中的 `request`/`param_map`/`response_map`、标准层的 `input_schema`/`output_schema`/`modes`/`file_io`/`examples`/`limits` 与 `evidence[]`）；后端 `validate_draft()` 做结构与安全校验，落 `agent_artifacts` 供审阅。
5. 管理员点「连接测试」→ `POST /api/admin/capabilities/{draft}/test` → 执行器真实请求上游（凭证由凭证代理注入），结果脱敏后回写 `capability_test_runs`。
6. 最小样例调用 → 结果检查 → `POST /api/admin/capabilities/{id}/publish`（或自动发布，若接入流程被预先授权且全部检查通过）。
7. 发布产生不可变 `capability_versions` 行 + `published_version_id`，写审计、写发布证据（测试记录 id 列表）。

**(c) 外部 Agent 同步调用**

1. CLI 已写入 `~/.aragon/skills/skill-router/SKILL.md` 与平台配置（`base_url`、`token`）。
2. `GET /api/v1/capabilities` 带 `Authorization: Bearer <platform token>` → 令牌校验（sha256 比对）→ 权限/配额/预算检查 → 返回该令牌可见的能力目录（`ETag` + `Cache-Control: private, max-age=60`）。
3. `POST /api/v1/capabilities/{key}/invoke` with `{capability_version, input, options}`：
   - 校验 Schema（`jsonschema`，拒绝未声明字段）；
   - 幂等：有 `Idempotency-Key` 则查 `idempotency_keys`，命中返回原 `request_id` 与状态；
   - 路由：`router.select()` 产出 `route_decision`（候选、兼容性、配额、预算、最终选择与理由）；
   - 执行：`http_executor` 通过凭证代理注入密钥 → 上游 → 解析 → 统一结果；
   - 记录：`call_logs`（脱敏）+ `usage_records`；返回 `request_id`。
4. 同步返回 `{request_id,status,output,usage,provider,error}`；异步返回 `202 {request_id,task_id,status:"queued"}`；流式返回 SSE。

**(d) 异步任务与恢复**

1. `POST .../invoke` 选择 `mode=async`：请求内只做「鉴权 → Schema 校验 → 幂等/预算/配额检查 → 创建 `tasks` 行（`status=queued`）+ `task_queue` 队列项」，**不触达上游**，随即返回 202（满足 §15.4「异步提交 1s 内返回任务编号」）。上游提交、`upstream_task_id` 回填与轮询全部在 worker 内完成。
2. worker `claim()`：`UPDATE task_queue SET status='leased', lease_expires_at=now+60s, lease_owner=? WHERE id=? AND status='queued'`，执行中每 20s 心跳续租。
3. 上游返回任务标识 → 写 `tasks.upstream_task_id` + `poll_config`；worker 按 backoff 轮询 `query_spec`。
4. worker 崩溃：租约过期（≤60s）后其它 worker 重新领取；`attempts` 递增，超过 `max_attempts` 进入 `failed` 并写 `failure_class`。
5. 重启恢复：`worker.startup()` 先把 `leased` 且租约过期的队列项复位为 `queued`，再对 `status in ('queued','running','canceling')` 的任务按 `resume_policy` 恢复轮询。
6. 取消：`POST /api/v1/tasks/{id}/cancel` → 若上游支持取消则转发并进入 `canceling`；上游不支持则返回 `409 CANCEL_UNSUPPORTED` 并把任务标为 `canceled_local`（明确文案：仅停止平台等待，远端仍可能继续）。

**(e) SKILL 安装与动态发现**

1. 管理员创建/导入 SKILL → 校验结构/路径/权限声明 → 版本行 `draft` → 测试 → `publish`。
2. `POST /api/admin/skills/{id}/export?format=npm|zip` 生成连接包：`SKILL.md`（AragonAgent/Claude Code 兼容 frontmatter：`name`/`description`/`allowed-tools`/`activation`）+ `skillrouter.config.json`（`base_url`，**不含 token**）+ `README.md` + 安装脚本。
3. 管理员复制页面上的真实命令：`npx @skillrouter/cli install --base-url ... --token ...`（未发布时 `npx ./downloads/skillrouter-cli-0.1.0.tgz install ...`）。
4. CLI 探测目标 Agent（`.aragon/skills`、`.claude/skills`、`~/.aragon-agent`），写入 SKILL 文件，`--verify` 执行 `/api/v1/health` + `capabilities.list` 连通性检查。
5. 外部 Agent 每次调用 `capabilities.list` 拿到最新目录（服务端 `ETag` 变化即失效），因此平台新增能力无需重装连接包；破坏性变更仅通过主版本号体现并在详情里给出迁移说明。

**(f) 引擎升级**

1. 调度器每 `ENGINE_CHECK_INTERVAL_HOURS`（默认 24h）执行 `upgrade.check()`：并行查 GitHub tags/releases 与 npm `dist-tags`。
2. 差异 → 建 `engine_candidates`（`version`、`commit_sha`、`provenance`、`source_url`、`integrity`）。
3. 按策略 `auto | notify | manual`：`auto` 进入验证流水线；`notify` 只置 `pending_upgrade` 并在概览/设置页提示。
4. 验证：隔离目录 `var/engine-builds/<version>/` → `npm ci` → 构建 core → 跑**适配层一致性套件**（导出符号差异、工具清单差异、事件字段差异、SSE 契约、简单 prompt 冒烟）。
5. 通过 → `swap`：更新 `var/engine-runtime/current` 指针与 `engine_deployments` 行，重启 gateway，健康检查（`/healthz` + 一次真实 minimal prompt）。
6. 失败 → 指针回滚到 `previous_version`，重启 gateway，记录 `engine_upgrade_runs.result=failed` 与失败检查项；期间平台已发布能力不受影响（它们不经过网关）。

---

## 5. 技术栈与版本锁定

| 层 | 组件 | 版本策略 |
|---|---|---|
| 前端 | Next.js `15.x`、React `19.x`、TypeScript `5.6+`（`strict`）、TanStack Query `5.x`、react-hook-form `7.x`、zod `3.x`、Tailwind `3.4.x`、Radix primitives、lucide-react | `package-lock.json` 锁定；`next.config.mjs` 输出 `standalone` |
| 后端 | Python `3.11`、FastAPI `0.115.x`、uvicorn `0.32.x`、SQLAlchemy `2.0.x`、Alembic `1.13.x`、pydantic `2.9.x`、httpx `0.27.x`、jsonschema `4.23.x`、argon2-cffi `23.x`、cryptography `43.x`、PyYAML `6.x`、itsdangerous `2.x` | `requirements.txt` + `requirements-dev.txt` 全部 `==` 精确锁定；CI 用 `pip install --require-hashes` |
| Agent 引擎 | Node `22.x`，`@aragon-agent/core` **`0.2.34`（精确，不带 `^`）**，Fastify `5.x`，`ajv`（core 可选依赖） | `agents/aragon-gateway/package-lock.json` 提交；`package.json` 中 `core` 版本由升级流水线改写并记录 |
| 校验用 CLI | `@aragon-agent/cli` `0.6.22`（devDependency） | 仅一致性测试与冒烟使用 |
| npm 包 | `@skillrouter/cli` `0.1.0`，Node `>=18` | 同时产出 `.tgz` 供本地安装 |
| 数据库 | SQLite `3.4x`（`sqlite3` CLI 备份） | `PRAGMA user_version` 由 Alembic 管理 |
| 测试 | pytest `8.x`、pytest-asyncio、httpx AsyncClient、respx；Vitest `2.x`；Playwright `1.4x` | CI 三层：lint/typecheck → unit+contract → e2e（可选） |
| 部署 | Docker Compose v2、镜像基础 `python:3.11-slim` / `node:22-slim`，镜像 digest 固定 | 见 §14 部署章节 |

环境事实（实测）：Windows 10.0.26200；Python 3.11.7；Node v22.18.0；npm 10.9.3。

---

## 6. 仓库布局

```
M:/takoAI/AragonSkillRouter/
├─ apps/
│  ├─ api/                        # FastAPI 服务（api + worker 共用镜像）
│  │  ├─ skillrouter_api/
│  │  │  ├─ main.py               # create_app()、路由挂载、生命周期
│  │  │  ├─ settings.py           # pydantic-settings，全部 env 前缀 SKILLROUTER_
│  │  │  ├─ deps.py               # 依赖注入：db session、当前管理员、当前令牌
│  │  │  ├─ errors.py             # ApiError + 错误码表 + 统一异常处理器
│  │  │  ├─ db/                   # engine.py session.py writer.py base.py seed.py
│  │  │  ├─ models/               # SQLAlchemy ORM，按域分文件
│  │  │  ├─ schemas/              # pydantic 请求/响应模型，按域分文件
│  │  │  ├─ security/             # passwords.py sessions.py csrf.py ratelimit.py tokens.py
│  │  │  ├─ crypto/               # masterkey.py secretbox.py keyring.py
│  │  │  ├─ services/             # 业务服务层（唯一持有事务的地方）
│  │  │  ├─ executors/            # base.py http_executor.py mapping.py parsers.py async_poll.py stream.py
│  │  │  ├─ router/               # selector.py health.py compatibility.py
│  │  │  ├─ agent/                # client.py events.py prompting.py sessions.py toolspec.py
│  │  │  ├─ sse/                  # broker.py events.py
│  │  │  ├─ api/admin/            # 管理端路由（cookie+CSRF）
│  │  │  ├─ api/v1/               # 对外路由（Bearer 令牌）
│  │  │  ├─ worker/               # runner.py leases.py handlers/ upgrade_job.py
│  │  │  └─ upgrade/              # detect.py build.py verify.py switch.py rollback.py
│  │  ├─ tests/                   # unit / contract / e2e / fixtures
│  │  ├─ migrations/              # Alembic
│  │  ├─ requirements.txt
│  │  └─ Dockerfile
│  └─ web/                        # Next.js 15 管理控制台
│     ├─ app/                     # 路由段（含浮窗 Agent provider）
│     ├─ components/              # ui/ 域组件、agent/ 面板、common/
│     ├─ lib/                     # api.ts sse.ts auth.ts query.ts format.ts
│     ├─ styles/                  # tokens.css globals.css
│     ├─ public/
│     └─ Dockerfile
├─ agents/
│  └─ aragon-gateway/             # Node 边车：AragonAgent SDK 宿主
│     ├─ src/{main.ts,config.ts,engine.ts,policy.ts,tools/,platform-client.ts,sse.ts,health.ts}
│     ├─ test/{conformance.test.ts,tools.test.ts,policy.test.ts}
│     ├─ package.json
│     └─ Dockerfile
├─ packages/
│  ├─ skillrouter-cli/            # npm 包 @skillrouter/cli（TS）
│  └─ contracts/                  # 由 OpenAPI 生成的 TS 类型（CI 校验一致）
├─ db/migrations/                 # 初始化 SQL 快照（与 Alembic 对齐，便于审阅）
├─ deploy/
│  ├─ docker-compose.yml
│  ├─ nginx.conf
│  └─ .env.example
├─ scripts/                       # bootstrap.py backup.py restore.py perf_smoke.py seed_demo.py
├─ docs/
│  ├─ README.md                   # 运维/部署/接口/SKILL 接入索引
│  ├─ api/                        # openapi.json（构建产物）+ 调用示例
│  ├─ runbook/                    # 初始化、备份恢复、升级回滚、故障处理
│  └─ plans/skill-router/         # 本文档
└─ var/                           # 运行时持久化目录（gitignore）
   ├─ skillrouter.db
   ├─ files/                      # 上传与产物
   ├─ credentials-master.key      # 仅当未用 env 注入主密钥时（0600）
   ├─ aragon-home/                # ARAGON_HOME（按引擎版本分子目录）
   └─ engine-builds/              # 隔离构建目录
```

编码约定：

- Python 模块禁止从 `api/*` 导入到 `services/*`（单向依赖：`api → services → models/db`）。
- 事务边界只在 `services/*`：API 层不 `commit()`。
- 所有对外错误必须走 `errors.ApiError(code, message, hint, request_id)`，前端只根据 `code` 做分支。
- TypeScript `strict: true`、`noUncheckedIndexedAccess: true`；禁止 `any`（`@typescript-eslint/no-explicit-any: error`）。

## 7. 模块设计

### 7.1 配置与启动（`settings.py` / `bootstrap.py`）

`settings.py` 用 `pydantic-settings`（`env_prefix="SKILLROUTER_"`）定义下表全部变量，缺失时给出明确中文提示并以退出码 2 结束（不静默降级）。

| 变量 | 默认 | 说明 |
|---|---|---|
| `SKILLROUTER_ENV` | `production` | `production` 时强制 HTTPS cookie、禁止 `--reload`、要求主密钥来自 env 或 0600 文件 |
| `SKILLROUTER_DB_PATH` | `var/skillrouter.db` | SQLite 路径 |
| `SKILLROUTER_DATA_DIR` | `var` | 持久化根（文件、引擎、home） |
| `SKILLROUTER_MASTER_KEY` | — | base64 32 字节；缺失则从 `var/credentials-master.key` 读，仍缺失则首次启动生成并 0600 落盘 |
| `SKILLROUTER_MASTER_KEY_ID` | `mk1` | 密钥轮换标识，随密文保存 |
| `SKILLROUTER_ADMIN_USER` / `_PASSWORD` | `admin` / `adminjr` | 仅首次初始化生效 |
| `SKILLROUTER_SESSION_TTL_MINUTES` | `480` | 会话滑动过期 |
| `SKILLROUTER_LOGIN_MAX_FAILURES` / `_WINDOW_SECONDS` / `_LOCK_SECONDS` | `5` / `300` / `300` | 登录限流 |
| `SKILLROUTER_AGENT_GATEWAY_URL` / `_SECRET` | `http://127.0.0.1:8500` / — | 边车地址与共享密钥（必填） |
| `SKILLROUTER_ENGINE_POLICY` | `auto` | `auto` / `notify` / `manual` |
| `SKILLROUTER_ENGINE_CHANNEL` | `stable` | `stable`（`core-v*` tag）、`branch:<name>` |
| `SKILLROUTER_ENGINE_PIN` | `0.2.34@0a119a6...` | 当前部署版本+commit |
| `SKILLROUTER_ENGINE_CHECK_INTERVAL_HOURS` | `24` | 检测周期 |
| `SKILLROUTER_FETCH_ALLOWLIST` | 空 | 逗号分隔的 CIDR/域名白名单（外部抓取） |
| `SKILLROUTER_SSRF_DENY` | 默认私网+元数据 | `127.0.0.0/8,10/8,172.16/12,192.168/16,169.254.169.254,::1,fc00::/7` |
| `SKILLROUTER_MAX_UPLOAD_MB` / `SKILLROUTER_FETCH_TIMEOUT_S` | `50` / `20` | 文件与抓取上限 |
| `SKILLROUTER_RETENTION_DAYS` | `30` | 调用正文/文件保留期 |
| `SKILLROUTER_TELEMETRY_EGRESS` | `deny` | 出站策略：控制引擎版本检测与隔离构建的出站请求（`allow` / `deny`）；`deny` 时仅支持手动上传 tarball 升级。v2 起同时接受别名 `SKILLROUTER_EGRESS_POLICY` |
| `SKILLROUTER_DEFAULT_BUDGET_USD` | `0` | 0 表示不限制 |
| `SKILLROUTER_FILE_SIGNING_SECRET` | — | 文件签名 URL 的独立 HMAC 密钥（base64 32 字节）；缺失则首次启动生成并 0600 落盘。**与主密钥分离**，轮换只使旧签名链接失效，不影响凭证解密 |

  `bootstrap.py` 顺序：初始化 `settings` → `db.init_engine()`（设 PRAGMA）→ `alembic upgrade head`（或 `create_all` + version 断言）→ `seed_system_state()` → **首次管理员初始化（若 `admin_initialized != '1'`，用 `SKILLROUTER_ADMIN_USER` / `_PASSWORD` 创建并置位；已置位则跳过，绝不重建）** → `security.cleanup_expired()` → `files.ensure_dirs()` → 打印启动摘要（版本、引擎 pin、迁移版本、监听地址）。任何一步失败都打印 `[FATAL] <code> <hint>` 并退出。**主密钥与文件签名密钥必须在此步可用**（否则凭证永远无法解密，且不存在「先启动后补密钥」的降级路径）。

### 7.2 数据访问层（`db/`）

- `engine.py`：`create_engine(sqlite+pysqlite:///<path>, connect_args={"timeout": 5, "check_same_thread": False})`；`event.listens_for(Engine, "connect")` 中执行 `PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000; PRAGMA synchronous=NORMAL; PRAGMA temp_store=MEMORY;`；`pool_pre_ping=True`。
- `writer.py`：进程级 `asyncio.Lock` + `threading.Lock` 的写序列化器 `async def write(fn)`，把写事务排队，超时 5s 抛 `DB_BUSY`。所有 `INSERT/UPDATE/DELETE` 必须经它（`services` 层统一用 `with db_write() as s:`）。
- `session.py`：请求作用域 `Session`（`expire_on_commit=False`），FastAPI 依赖 `get_db()`。
- 迁移：Alembic 单 head；`0001_init` 覆盖 §10 全部表；后续每次 schema 变更必须新增 revision 且 `upgrade`/`downgrade` 双向可用（升级回滚要求）。

### 7.3 安全层（`security/`）

- `passwords.py`：`hash_password()` 用 `argon2.PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)`；`verify_password()` 恒定时间比对；`needs_rehash()` 登录时自动升级。
- `sessions.py`：`admin_sessions(id, user_id, token_hash, csrf_hash, scope, created_at, last_seen_at, expires_at, ip, user_agent, revoked_at)`。Cookie `sr_session=<32B base64url>`，`HttpOnly; Secure(生产); SameSite=Lax; Path=/; Max-Age=TTL`。每次请求 `token_hash = sha256(cookie)` 查表；`last_seen_at` 与 `expires_at` 滑动续期（每 >60s 才写，减少写放大）。
- `csrf.py`：双提交。登录时同时下发 `sr_csrf`（可被 JS 读取的 cookie）与会话内 `csrf_hash`；所有 `POST/PATCH/PUT/DELETE` 管理接口要求 `X-CSRF-Token` 且与会话 `csrf_hash` 比对；比较用 `secrets.compare_digest`。SSE/GET 不需要。
- `ratelimit.py`：`login_attempts(username, ip, at, success)` 表 + 固定窗口计数；超限返回 `429 LOGIN_THROTTLED` 并带 `retry_after`；成功登录清窗口。业务限流统一走 `rate_counters(scope_type, scope_id, window_start, window_seconds, calls, concurrent)`：`scope_type ∈ token|provider|capability|global`，60s 滑动窗口用于 rpm 与并发，`quota_counters` 负责日/月累计（见 §10.1）。
- `tokens.py`：平台令牌格式 `srk_<keyid>_<secret>`，`secret` 32 字节 base64url；库中只存 `sha256(secret)`（`platform_tokens.token_hash`）、`prefix`（前 8 字符便于识别）。创建时**仅一次**返回完整令牌。
- `gates.py`（v2 新增，闸门顺序固定）：`require_initialized()` → `require_admin()`（cookie 会话）→ `require_password_changed()` → `require_csrf()`（写方法）→ 业务依赖。`/api/v1/*` 走 `require_initialized()` → `require_token(scope)`。顺序不可调换，否则未初始化时可能先返回 401 而非 `409 NOT_INITIALIZED`，前端无法区分「没登录」与「还没初始化」。

### 7.4 加密与主密钥（`crypto/`）

- `secretbox.py`：`AESGCM(master_key)`，随机 12 字节 nonce，AAD = `f"{record_type}:{record_id}:{key_id}"`。密文封装 `v1.<key_id>.<b64url(nonce)>.<b64url(ct)>`。`seal()` / `open()` / `rotate()`。
- `keyring.py`：支持 `SKILLROUTER_MASTER_KEY` 与 `SKILLROUTER_MASTER_KEY_PREVIOUS`（轮换期双读）。`POST /api/admin/settings/rotate-master-key` 触发全表重加密（`credentials.secret_ciphertext`、`provider_headers` 中的敏感值、`agent_model_keys`），过程分批（每批 50 行）+ 事务 + `master_key_rotations` 记录（`from_key_id,to_key_id,rows,started_at,finished_at,status`）；失败可回滚（旧密钥仍在 `PREVIOUS` 中可用）。
- 恢复方案：`scripts/backup.py` 导出 DB + `var/files` + 主密钥指纹（`sha256(master_key)[:16]`，**不导出密钥本体**）；`scripts/restore.py` 校验指纹一致后才恢复，不一致则拒绝并提示「主密钥与备份不匹配，需同时恢复密钥」。
  - **实现方式（v2 修正）**：不依赖 `sqlite3`/`tar` 等外部二进制（运行镜像 `python:3.11-slim` 不含 `sqlite3` CLI，Windows 无 `tar`）。DB 用标准库 `sqlite3.Connection.backup(dst)` 做一致性热备（WAL 下安全）；文件目录用 `zipfile` 打包；`manifest.json` 用 `json` 写出。整个备份在 worker 的 `backup` handler 内执行，产物登记为 `file_objects(kind=artifact)`，可下载、可核对 sha256。

### 7.5 提供商与凭证（`services/providers.py` / `credentials.py`）

- 提供商 `providers`：`id, slug, name, category, base_url, docs_url, tags(json), environment(prod|staging|dev), notes, status, created_at, updated_at`。`slug` 唯一。删除为软删（`deleted_at`）以保持历史调用可追溯。
- 凭证 `credentials`：
  - 字段：`id, provider_id, name, kind(llm|image|video|speech|asr|generic), auth_type(api_key|bearer|basic|header), header_name, secret_ciphertext, meta(json: region/model_hints), status(active|disabled|expired), expires_at, last_verified_at, last_verify_result(json), rotation_of, created_at, deleted_at`。
  - 认证注入（`executors/auth.py`）：`api_key`→ `X-API-Key`（按凭证 `meta.header_name` 覆盖）；`bearer` → `Authorization: Bearer <secret>`；`basic` → `Authorization: Basic base64(user:pass)`（`meta.username` + secret）；`header` → 自定义头名 + secret。
  - 有效性检测：`services/credentials_verify.py` 每类 `kind` 定义 `probe`（如 LLM 调 `GET {base_url}/models` 或最小 chat；图像走一次 1 图最小生成，若 `meta.probe=k`），结果写 `credential_verifications(id,credential_id,ok,status_code,latency_ms,error_code,detail_redacted,at)` 并更新 `last_verified_at/last_verify_result`。`detail_redacted` 经 `redact()` 处理（掩码 `sk-***abcd`、移除 `Authorization` 回显）。
  - 生命周期：禁用（`status=disabled`）→ 所有引用它的能力路由阶段直接跳过并在概览产生 `CREDENTIAL_DISABLED` 待办；过期前 7 天在概览提示；替换（`POST /credentials/{id}/replace` 复用 id、写入新密文、`rotation_of` 记录旧 id 供审计）；轮换同理但生成新凭证行并把引用切换到新行（原子事务）。
  - 影响面：`services/credentials.py::impact(credential_id)` 返回 `{providers, capabilities:[{id,key,name,version,status}], skills:[...], external_tokens:[...]}`，用于删除/停用确认弹窗。
- 引用关系（v2 修正）：能力**不直接**持有单值 `provider_id`/`credential_id`，而是通过 `capability_routes[]` 引用（见 §7.6/§10.1 `capability_routes`）；同一标准能力可有多条 route，各自绑定 `provider_id` + `credential_id`。SKILL 通过 `dependencies` 引用能力版本。因此「密钥轮换无需逐个修改 SKILL」自然成立，且「同一能力多提供商」在数据层成立。

### 7.6 能力（`services/capabilities.py` + `executors/`）

**能力定义（草稿态可编辑，发布后不可变）**

v2 起能力 = **标准能力（对外契约，稳定）** + **`routes[]`（每条约定的上游通道，可多条）**。这是「同一标准能力配置多个兼容提供商/模型 + 固定指定/优先级/健康检查/受限故障切换」唯一可行的建模方式；`input_schema`/`output_schema` 在标准能力层（对外承诺不变），`request`/`param_map`/`response_map`/`limits`/`errors` 在 route 层（各提供商不同）。**存储位置**：草稿态整份 JSON 存 `capabilities.draft_definition`（`capability_routes` 是它的冗余投影，用于列表筛选与路由查询）；发布时整份冻结为 `capability_versions.definition`，此后不可变。

```jsonc
{
  // ── 标准能力层（对外契约，发布后不可变）──
  "key": "text.generate.gpt4o-mini",         // 全局唯一、小写点分
  "name": "GPT-4o mini 文本生成",
  "kind": "text.generate",                    // text.generate | image.generate | video.generate | audio.speech | audio.transcribe | generic.http
  "modes": ["sync", "stream"],                // 子集，取自 sync | async | stream（v2：复数）
  "file_io": {"input": ["image/png"], "output": []},   // 声明式：文件输入/输出能力（v2）
  "input_schema": {"$schema": "...", "type": "object", "properties": {"...": {}}, "required": [], "additionalProperties": false},
  "output_schema": {"...": {}},
  "examples": [                               // v2：至少 1 条，发布前必须有一条在测试记录中 ok=1
    {"title": "短提示", "input": {"prompt": "你好", "max_tokens": 64}}
  ],
  "compatibility_group": "text.chat.openai-v1",   // 路由等价性依据（仅同组能力可互为候选）
  "failover": {"enabled": true, "max_switches": 2},   // v2：故障切换开关（能力级），与 §7.7 一致
  "requires_agent_reasoning": false,              // true 时详情页明确标识「需实时 Agent 推理」
  "tags": ["chat", "cheap"], "docs_url": "...",

  // ── 路由层（可多条；每条独立绑定 provider + credential）──
  "routes": [
    {
      "id": "rt_primary",
      "provider_id": "prv_openai", "credential_id": "crd_...",
      "priority": 0,                              // 越小越优先；固定指定 = 只有一条 enabled route
      "enabled": true,
      "model_hint": "gpt-4o-mini",
      "request": {
        "method": "POST", "url_template": "{base_url}/v1/chat/completions",
        "headers": {"Content-Type": "application/json"},
        "query": [], "body_template": {"model": "${model}", "messages": "${messages}"},
        "encoding": "json"                        // json | form | multipart
      },
      "param_map": {
        "messages": {"from": "input.messages", "transform": "passthrough"},
        "model": {"from": "route.model_hint", "default": "gpt-4o-mini"},
        "max_tokens": {"from": "input.max_tokens", "transform": "int", "max": 4096}
      },
      "response_map": {"format": "json", "jsonpath": "$.choices[0].message.content", "usage": {"in": "$.usage.prompt_tokens", "out": "$.usage.completion_tokens"}},
      "stream": {"protocol": "sse", "delta_path": "$.choices[0].delta.content", "done_markers": ["[DONE]"]},
      "async": {"submit": {"task_id_path": "$.id"}, "query": {"url_template": "{base_url}/v1/tasks/{task_id}", "status_path": "$.status", "success": ["succeeded"], "failure": ["failed"], "cancel": {"method": "POST", "url_template": ".../cancel"}}, "poll": {"initial_ms": 2000, "max_ms": 30000, "timeout_s": 900}},
      "limits": {"max_input_bytes": 262144, "max_output_bytes": 10485760, "max_duration_ms": 120000, "allowed_mime": ["image/png", "image/jpeg"], "max_file_mb": 20},
      "errors": [
        {"match": {"status": 401}, "code": "UPSTREAM_AUTH", "retryable": false, "hint": "检查凭证是否有效"},
        {"match": {"status": 429}, "code": "UPSTREAM_RATE_LIMIT", "retryable": true, "backoff_ms": 2000},
        {"match": {"json": "$.error.type", "equals": "insufficient_quota"}, "code": "UPSTREAM_QUOTA", "retryable": false}
      ],
      "timeout_ms": 60000,
      "retry": {"max_attempts": 3, "base_ms": 500, "max_ms": 8000}
    },
    {
      "id": "rt_backup",                          // 第二条候选：不同提供商，同 compatibility_group
      "provider_id": "prv_azure", "credential_id": "crd_...",
      "priority": 10, "enabled": true, "model_hint": "gpt-4o-mini",
      "request": {"method": "POST", "url_template": "{base_url}/openai/deployments/{model_hint}/chat/completions?...", "encoding": "json"},
      "param_map": {"...": "..."}, "response_map": {"...": "..."},
      "limits": {"...": "..."}, "errors": [ "..." ], "timeout_ms": 60000, "retry": {"max_attempts": 2, "base_ms": 500, "max_ms": 4000}
    }
  ]
}
```

**校验规则（`capabilities.validate_definition()`，失败返回结构化 `issues[]`，每项含 `path`/`code`/`message`/`hint`）**

1. `key` 正则 `^[a-z0-9]+(\.[a-z0-9][a-z0-9-]*)+$`，唯一性（草稿内与已发布版本内均校验）。
2. `modes` 必须是非空集合，元素 ∈ `{sync, async, stream}`，无重复；`file_io.input/output` 元素必须是合法 MIME 或 `*/*`。
3. 每条 route 的 `request.url_template` 只允许 `http/https`，必须含 `{base_url}` 或以绝对 URL 开头并命中 `egress_allowlist`；禁止 `file://`、`gopher://`、内网 IP（除白名单）。`request.encoding=multipart` 时 route 的 `file_io.input` 不得为空。
4. `input_schema`/`output_schema` 必须是合法 JSON Schema（`jsonschema.Draft202012Validator.check_schema`），`additionalProperties` 必须为 `false`（需求：不支持的参数必须显式报错）。
5. route 的 `param_map` 中引用的 `input.*` 必须在 `input_schema.properties` 中存在；引用 `route.*` 必须在该 route 的声明字段中（`model_hint`/`id`/`provider_id`）；引用未知命名空间 → `PARAM_MAP_UNRESOLVED`。
6. `modes` 含 `stream` → **至少一条 enabled route** 提供 `stream.protocol ∈ {sse, ndjson}` 且 `delta_path` 能在该 route 的测试响应中命中一次（发布前由 `capability_test_runs` 提供证据）。
7. `modes` 含 `async` → **至少一条 enabled route** 同时提供 `async.submit.task_id_path` 与 `async.query.url_template`；否则 `issues` 增加 `ASYNC_QUERY_MISSING`。
8. `errors[]` 的 `match` 字段必须能被解析（`status` 整数 100–599 或 `json` JSONPath）；`errors[]` 必须覆盖 401/403/429 三类中该提供商实际会返回的状态（缺 429 映射时 `warn` 而非 `error`）。
9. 每条 route 的 `limits.max_file_mb` ≤ `SKILLROUTER_MAX_UPLOAD_MB`；`limits.allowed_mime` 必须是 `file_io.input` 的子集。
10. `routes` 至少 1 条且至少 1 条 `enabled=true`；`id` 在能力内唯一；`(provider_id, credential_id)` 指向的凭证必须存在且 `status=active`（保存草稿时是 `warn`，发布时是 `error`）。
11. `examples` 至少 1 条，且每条 `input` 必须通过 `input_schema` 校验。发布额外要求：至少一条 example 在 `capability_test_runs` 中有 `ok=1` 且 `at > now-24h` 的记录（与该能力的某个已发布版本对应）。
12. `compatibility_group` 非空时，该组必须已存在（`compatibility_groups` 行），且组内其它成员 `kind` 与新成员一致；不允许校验阶段自动建组（必须走 `POST /capabilities/{id}/compatibility`）。

**执行引擎（`executors/http_executor.py`）**

```
invoke(ctx) ->                                   # ctx: {capability_version, input, options, caller}
  1. 模式判定：options.mode（默认 capabilities.modes 的第一项）必须 ∈ capability.modes，否则 400 UNSUPPORTED_MODE
  2. input_schema 校验（jsonschema，收集全部错误 → 400 INPUT_SCHEMA_INVALID，带字段路径）；options.extensions 之外未声明字段 → 400 UNSUPPORTED_PARAM
  3. 路由选择（router.select，§7.7）→ route_decision（候选 route 列表、过滤原因、最终 route）
  4. param_map 求值（mapping.evaluate，严格模式：未声明字段报错）
  5. auth 注入（credential_proxy.materialize(route.credential_id) → 内存 dict，作用域仅本次请求）
  6. 幂等（若 options.idempotency_key）：按 (scope, idem_key) 查 idempotency_keys → 命中且指纹一致返回已有 request_id；指纹不一致 → 409 IDEMPOTENCY_KEY_REUSED
  7. 同步（mode=sync，**在 API 进程内执行**）：httpx.AsyncClient(timeout=route.timeout_ms, follow_redirects=False)
     → 记录 request_fingerprint（method+url(去密钥)+body 的 sha256）
     → 映射状态/错误 → 解析 response_map → 产出 ExecResult → 直接作为 HTTP 响应体返回
  8. 流式（mode=stream，**在 API 进程内执行**）：httpx.stream → 逐块解析 delta → 写入 sse_broker → 结束写 usage
  9. 异步（mode=async）：**本函数不触达上游**，只做 校验/幂等/预算/配额 → 创建 tasks(queued) + task_queue → 返回 202
     （真正的上游提交、upstream_task_id 回填与轮询由 worker 的 async_poll 完成，见 §7.8 / §4.4(d)）
```

> v2 修正：v1 第 6 步「异步：提交 → 写 upstream_task_id → 返回 202」与 §4.4(d)、§15.4 的 1s 提交预算冲突，已统一为「请求内不触达上游」；v1 的「同步调用也入队」也与 150ms 平台开销冲突，已统一为进程内执行（§7.8）。

- 幂等记录 `idempotency_keys(scope, idem_key, fingerprint, request_id, created_at, expires_at)`，唯一索引 **`(scope, idem_key)`**（`scope = token:<id>` / `admin:<id>`，防止跨令牌命中他人请求，v2 修正）；指纹不一致 → `409 IDEMPOTENCY_KEY_REUSED`。
- 网络异常分类：`connect_timeout/read_timeout → UPSTREAM_TIMEOUT`；若「请求已发出但无响应」→ 任务/调用标记 `status_unknown` 并写 `needs_reconcile=1`，执行器在 `query_spec` 可用时优先查询上游，不重投。
- 重试：只对 `retryable=true` 的错误重试，且遵守 `max_attempts/base_ms/max_ms` 与 `budget` 累计上限；流式请求一旦产生首个 delta 不再重试、不切换提供商（需求硬约束）。
- 结果归一：`ExecResult{request_id, status, output, output_files[], usage{in,out,total,source:'provider'|'estimated'}, error{code,message,hint,retryable,upstream_status}, provider_id, capability_version_id, route_decision, timings{queue_ms,upstream_ms,total_ms}}`。

### 7.7 路由（`router/`）

- **候选集（v2 修正为 route 级）**：目标能力版本下所有 `enabled=true` 的 `routes[]` 条目。除此之外，仅当两个能力属于**同一个 `compatibility_group`** 时，才允许把另一能力的 route 纳入候选（跨能力等价切换），且必须由管理员显式确认过（§D12）。
- 过滤：
  1. **模式**：请求的 `mode` ∈ 该能力 `modes`，且该 route 能承载该模式（`stream` 需 `stream.*`，`async` 需 `async.*`）。
  2. **输入/输出兼容**：`input_schema` 可接受该输入（必填可满足、`additionalProperties=false` 下无多余字段）；`file_io.input/output` ⊇ 本次请求实际带/需要的 MIME。
  3. **限制**：`limits` 满足（文件大小/时长/格式/输出大小上限）。
  4. **凭证**：`credential.status=active` 且未过期（`expires_at` 为空或 > now+10min 安全余量）。
  5. **授权**：令牌 `scopes` 命中能力 key 或其 tag；`allowed_modes` 含该模式。
  6. **配额/预算**：`rate_counters`（rpm、并发）与 `quota_counters`（日/月、预算）未超；`options.preferred_provider` 若指定，则只保留该 provider 的 route（找不到 → `409 PROVIDER_NOT_AVAILABLE` 并列出可用 provider）。
- 排序（确定性，同分时按 `route.id` 字典序兜底）：显式 `priority`（小者优先）→ 健康分（近 5 分钟错误率）→ 估算单价（`price_book`）→ 最近 P95 延迟。
- 故障切换：仅当 `capability.failover.enabled`（默认 `true`）且错误 `retryable` 且**未产生任何输出** 且**尚未向该 route 提交过异步任务**时切换；切换事件写 `call_logs.route_events`（`{"from":"rt_primary","to":"rt_backup","reason":"UPSTREAM_429"}`）。一次调用最多切换 `failover.max_switches`（默认 2）次，累计预算不得超 `options.max_cost_usd`。
- 决策记录：`route_decisions(id, call_id, candidates_json, chosen_route_id, chosen_capability_version_id, chosen_provider_id, reason, created_at)`，调用详情页可展开查看「为什么选了它 / 为什么排除了其它候选」。
- 兼容性判定：`compatibility.py::equivalent(a,b)` 需要 (1) 同 `kind`；(2) `input_schema` 必填字段集合是超集关系且类型一致；(3) `output_schema` 顶层类型一致；(4) `modes` 有交集；(5) 管理员显式确认过同一 `compatibility_group`。**禁止**名称相似即等价。
- **影响面（v2 新增，需求「变更或删除时展示受影响对象」）**：`capabilities.impact(capability_id)` 返回 `{skills:[{id,name,version}], tokens:[{id,name,scope_match}], published_versions:[...], inflight_tasks:int, route_credentials:[{credential_id,name,status}]}`。停用/删除/发布新版本前必须展示；存在 `inflight_tasks>0` 时只允许「停止路由新请求」，不允许直接删除。
- 反向依赖查询：`GET /api/admin/skills/{id}/dependencies` 给出「我依赖谁」，`GET /api/admin/capabilities/{id}/impact` 给出「谁依赖我」，两者共同覆盖需求中的双向依赖可见性。

### 7.8 任务与队列（`worker/`）

- 队列表 `task_queue(id, kind, payload(json), priority, status(queued|leased|done|dead), available_at, attempts, max_attempts, lease_owner, lease_expires_at, created_at)`；索引 `(status, available_at)`。
- `leases.py::claim(kinds, n)`：单条 SQL `UPDATE ... SET status='leased', lease_owner=:w, lease_expires_at=:exp WHERE id=(SELECT id FROM task_queue WHERE status='queued' AND available_at<=:now AND kind IN (...) ORDER BY priority DESC, available_at ASC LIMIT 1) RETURNING id`；SQLite 3.35+ 支持 `RETURNING`（若目标环境不支持，回退为事务内 `SELECT ... LIMIT 1` + 条件 UPDATE 双重校验）。
- 心跳：执行中的 handler 每 20s 调 `renew(lease)`；续租失败（被抢占）→ 触发 `handler.cancel()`。
- 超时：按上表 `kind` 默认超时（`agent_turn` 30min，`capability_invoke_async` 30min，`file_parse` 5min，`engine_upgrade` 60min），到期写 `failed` + `failure_class=TIMEOUT`。超时判定由 `runner` 的心跳循环执行（`lease_expires_at < now` 或 handler 自身超时），不依赖外部调度器。
- **执行位置划分（v2 修正）**：
  - **API 进程内同步执行**：`mode=sync` 与 `mode=stream` 的调用。它们必须在请求生命周期内返回结果或事件流，而 worker 无法把结果交回 HTTP 响应体。限并发用进程内 `asyncio.Semaphore`（三级：令牌 → 提供商 → 能力），超限立即返回 `429 QUOTA_EXCEEDED`（带 `retry_after`），**不排队**，从而保住 §15.4 的「同步平台开销 ≤150ms」。
  - **队列执行**：`mode=async` 的调用与所有长任务。队列只承载「可以在后台完成、结果靠任务状态查询」的工作。
- 任务类型 handler（v2 清单）：
  | handler | 触发 | 默认超时 |
  |---|---|---|
  | `capability_invoke_async` | 异步能力调用（含上游提交 + 轮询 + 结果落库） | 30min（受 `route.timeout_ms` 与 `async.poll.timeout_s` 约束） |
  | `agent_turn` | Agent 一轮对话 | 30min |
  | `skill_import` | Git/URL/文件导入流水线 | 10min |
  | `file_parse` | 文档解析（pdf/txt/md/json） | 5min |
  | `credential_verify` | 凭证有效性探测 | 2min |
  | `engine_upgrade` | 引擎检测（`check`）/构建/验证/切换 | 60min |
  | `retention_sweep` | 每日过期文件与调用正文清理 | 10min |
  | `expiry_scan` | 每日凭证过期扫描 + 概览待办生成（v2 新增，P08） | 5min |
  | `backup` | 手动/定时备份（§7.4、§10.3） | 30min |
  > 注意 v1 中的 `capability_invoke_sync` 已移除（见上文执行位置划分）；同步调用的审计与用量由 API 进程直接写 `call_logs`/`usage_records`，不依赖队列。
- 并发限制（队列侧）：全局 `WORKER_CONCURRENCY`（默认 4）+ 每提供商 `max_concurrency` + 每令牌 `max_concurrency`（信号量，超限任务回到 `available_at=now+1s`，不失败）。

### 7.9 SKILL 管理与导入（`services/skills.py` / `skillimport.py`）

- SKILL 定义：`name(kebab,≤64), title, description, when_to_use, inputs[], outputs[], dependencies[{capability_key, version_range}], instructions(md), examples[], permissions{allowed_tools[], needs_agent_reasoning:bool, network:bool}, version, source{type:git|url|file|manual, ref, commit, url, sha256}, activation(auto|always|manual), tags[]`。
- **启用 / 停用（v2 新增，需求明确列出的操作）**：`skills.enabled` 是**独立于 `status`** 的开关。
  - `enabled=0`：`/api/v1/skills` 与 `/api/v1/skills/{name}` 不再返回；SKILL 执行计划拒绝执行（`409 SKILL_DISABLED`）；不改变 `status`、不改变已发布版本行、不影响历史调用记录。
  - `enabled=1` + `status=published` 才是「外部 Agent 可发现可调用」。
  - 与「废弃（`deprecated`）」的区别：废弃是语义化版本层面的处置（对外公告即将下线，仍可见但标注），停用是即时分发层面的开关（立即不可见）。两者可组合，UI 必须分别呈现，不得合并成一个「停用」。
  - 该开关可逆、不产生新版本、写审计 `skill.enabled` / `skill.disabled`。
- **导出契约（v2 重写，修正 v1 的致命错误）**：v1 的 `allowed-tools: [sr_invoke_capability, ...]` 不可实现——`sr_*` 是**平台网关内部**工具，外部 Agent 环境里根本不存在；而上游事实明确「无法解析的工具名会让整份声明失效（不是变窄）」，这会让导出的 SKILL 在目标环境里直接失配。v2 规则：

  1. **frontmatter 只声明目标环境真实存在的工具**。JSON-over-HTTP 调用需要一个能执行命令的工具，因此 `allowed-tools: [bash]`（AragonAgent 与 Claude Code 都提供 `bash`/`Bash` 并会自动映射）。**不声明**任何 `sr_*`、不声明未验证的工具名。
  2. **调用逻辑放在随包安装的无依赖助手脚本里**，不让 Agent 手写 HTTP：
     - 安装产物包含 `invoke.mjs`（Node ≥18，仅用内置 `fetch`，零依赖），子命令：
       - `node ./invoke.mjs capabilities.list [--refresh]`
       - `node ./invoke.mjs capabilities.get <key>`
       - `node ./invoke.mjs invoke --capability <key> [--version <v>] [--mode sync|async|stream] --input '<json>'`
       - `node ./invoke.mjs task.status|task.cancel <task_id>`
       - `node ./invoke.mjs skills.list|skills.read <name>`
     - 它读取同目录 `skillrouter.config.json` 的 `base_url` 与 `token_ref`，令牌从 `~/.skillrouter/credentials.json`（0600）或 OS 凭据库解析；输出统一为单行 JSON，便于 Agent 解析。
     - 因此 Agent 只需一条稳定命令，不再需要理解认证头与错误映射，也不会有把令牌写进命令行的风险（令牌不在 argv 中）。
  3. **`allowed-tools` 的作用域**：上游明确其效力是「一轮」且只约束写类工具、只读工具恒可用；同时上游明确「`allowed-tools` 是影响面控制，不是安全边界」。因此 SKILL 正文必须写清「只用 `invoke.mjs` 访问平台」，并且平台侧的真正边界是**令牌作用域 + 配额 + 预算**（§7.10/§9.3），不依赖 SKILL 声明做安全。
  4. **MCP 通道**：平台同时提供 MCP 适配（§9.7）。但 MCP 客户端通路尚未实测（附录 C 全部为「待验证」），因此**默认不进 SKILL frontmatter**；只有在 `docs/skills/connect.md` 登记实测通过后，才可由 `--transport mcp` 生成 MCP 变体（CLI 写客户端配置文件 + 一份不含 `bash` 的 SKILL 变体）。未实测就写进 frontmatter 属于「宣传无法执行的能力」，禁止。
  5. **导出包不含任何密钥**；`sha256` 清单与 `install_command` 写入 `skill_exports`；构建完成后跑 `secret_scan`。

  导出产物（`export_skill(format=skill_md|zip)`）：

  ```markdown
  ---
  name: video-short-clip
  description: 用一个文本提示生成 5 秒竖版短视频。当任务需要短视频素材时使用。
  version: 1.2.0
  license: proprietary
  allowed-tools: [bash]          # 仅声明目标环境真实存在的工具（v2）
  activation: auto               # 导入来源未审查时禁止 always
  metadata:
    skillrouter:
      base_url: https://router.example.com
      helper: ./invoke.mjs       # 所有平台调用都经它
      config: ./skillrouter.config.json
      required_capabilities: [video.generate.kling-v2]
  ---
  # 使用说明
  1. 列出可用能力（平台新增能力会即时出现在结果里，无需重装）：
     `node ./invoke.mjs capabilities.list`
  2. 调用能力（输入 Schema 见 `capabilities.get` 的 `input_schema`）：
     `node ./invoke.mjs invoke --capability video.generate.kling-v2 --mode async --input '{"prompt":"雨中的柴犬","duration":5}'`
     若返回 `task_id`，用 `node ./invoke.mjs task.status <task_id>` 轮询。
  3. 错误体含 `code`/`hint`/`request_id`；把 `request_id` 报给管理员可定位问题。
  4. 不要尝试读取或转述任何密钥：平台令牌已在 `skillrouter.config.json` 中通过 `token_ref` 间接引用。
  ```
- 导入（`/api/admin/skills/import`，`kind=git|url|file`）：
  1. 预检：来源解析 → 抓取（`fetch_guard`：大小/超时/重定向 ≤3/内容类型白名单/SSRF 校验）或 `git archive --depth=1` 到临时目录（不执行仓库内任何脚本）。
  2. 展示：`ImportPreview{source, ref, commit, files[{path,size,sha256,type}], declared_permissions, dependencies, warnings[]}`。
  3. 校验：必须有 `SKILL.md`；frontmatter `name/description` 必填；路径不得含 `..`、绝对路径、符号链接、Windows 保留名（`CON/PRN/AUX/NUL/COM1..9/LPT1..9`）；单文件 ≤ 2MB，总 ≤ 20MB，条目 ≤ 200；含脚本（`*.sh|*.py|*.js|*.ps1`）或安装钩子（`hooks/*`,`postinstall`）→ `requires_sandbox_review=true`。
  4. 危险内容审查：`instruction_scanner` 检测「要求导出全部凭证 / 覆盖平台指令 / 提升权限 / 访问元数据地址」等模式 → `warnings[]` 且禁止 `activation: always`。
  5. 落地：写入 `skills`（draft）+ `skill_files` 清单 + `skill_imports` 记录；需要沙箱的脚本**不执行**，仅静态展示，标注「本阶段不执行脚本」。
- SKILL 与 API 整合：`SkillExecutionPlan{steps:[{id, type: 'capability' | 'agent_reasoning' | 'transform', capability_key?, version?, inputs{mapping}, output_as, on_error: 'stop'|'continue'|'fallback', fallback?}]}`，发布进 `skill_versions.execution_plan`；普通 step 由平台执行器按已发布配置执行（不经过网关），`agent_reasoning` step 明确标识、仅在网关可用时执行并在结果中标注 `used_agent=true`。

### 7.10 调用记录、用量、配额与审计

- `call_logs`：`id, request_id(uuid), caller_type(admin|token|agent|skill), caller_id, capability_key, capability_version_id, provider_id, credential_id, mode, status(success|failed|canceled|status_unknown), http_status, error_code, latency_ms, queue_ms, upstream_ms, retries, request_fingerprint, payload_policy, request_preview(脱敏,可选), response_preview(脱敏,可选), request_id_parent, agent_session_id, task_id, created_at`。索引：`(created_at desc)`、`(caller_id, created_at)`、`(capability_key, created_at)`、`(status)`。
- 正文保存策略 `settings.payload_policy ∈ {none, redacted_preview, full(仅管理员显式开启)}`，默认 `redacted_preview`（≤2KB，密钥/邮箱/手机号/长 base64 掩码）。
- 用量：`usage_records(id, call_id, provider_id, capability_key, token_access_id, unit(tokens|images|seconds|chars), qty, cost_usd, source(provider|estimated), at)`；费率表 `price_book(provider_id, capability_key, unit, unit_price_usd, currency, updated_at)` 用于估算。
- 配额（v2 补齐计数器维度）：限制项 `quota_limits(scope_type: global|provider|capability|token, scope_id, rpm, concurrency, daily_calls, monthly_budget_usd)`；速率/并发计数 `rate_counters(scope_type, scope_id, window_start, window_seconds, calls, concurrent)`（60s 窗口，四类 scope 共用）；日/月与费用累计 `quota_counters(scope_type, scope_id, period, calls, cost_usd)`。超限返回 `429 QUOTA_EXCEEDED` / `402 BUDGET_EXCEEDED`（含 `retry_after` 与 `hint`，例如「预算已用 100%，请在系统设置调整月预算」）。检查顺序：令牌 → 能力 → 提供商 → 全局，任一层超限即拒绝并说明是哪一层。
- 审计：`audit_logs(id, at, actor_type, actor_id, action, object_type, object_id, summary(json 脱敏), request_id, ip)`。必须记录的动作（v2 补齐，与 §9.2 端点一一对应）：`admin.password_changed`、`credential.created/replaced/rotated/disabled/deleted`、`capability.published/deprecated/rolled_back/route_changed/compatibility_confirmed`、`skill.imported/published/deleted/enabled/disabled`、`token.created/revoked`、`agent_flow.started/published`、`agent_approval.requested/approved/rejected/expired`、`route.changed`、`engine.upgrade_started/verified/switched/rolled_back`、`master_key.rotated`、`backup.created`、`restore.completed`、`settings.updated`。审计表与运行日志物理/逻辑分离（不同表、不同导出接口），只允许管理员读取，不可通过业务 API 删除。

### 7.11 文件与产物（`services/files.py`）

- `file_objects(id, sha256, mime, size, storage_path, kind(upload|output|artifact), owner_type, owner_id, expires_at, ref_count, created_at)`；内容按 `var/files/<sha256[0:2]>/<sha256>` 存储（内容寻址，天然去重）。
- 上传：类型白名单（图片 `png/jpeg/webp`、音频 `mp3/wav/m4a`、视频 `mp4/webm`、文档 `pdf/txt/md/json/yaml`）、大小上限、流式落盘 + 边写边算 sha256，超限立即中断并删除。
- 访问：`GET /api/v1/files/{id}` 需令牌 + 归属校验；返回 `X-Content-Type-Options: nosniff`、`Content-Disposition: inline|attachment`；签名 URL（`/api/v1/files/{id}?sig=&exp=`，HMAC-SHA256 over `file_id|exp|owner`，10 分钟）供 `<img>/<video>` 预览，不暴露令牌。签名密钥使用独立的 `SKILLROUTER_FILE_SIGNING_SECRET`（v2 明确来源）：与主密钥分离，轮换它只会让已发出的预览链接失效，不会牵连凭证解密；反过来轮换主密钥也不会让用户的预览链接突然失效。
- 保留：`retention_sweep` 每日清理 `expires_at < now` 且 `ref_count=0` 的记录与内容；清理时同步失效签名（`files.deleted_at`）并写审计摘要。
- 回调：`POST /api/v1/callbacks/{provider_slug}` 校验 `signature`（HMAC-SHA256，密钥在凭证 `meta.webhook_secret`）、时间戳窗口 ±300s、`event_id` 去重（`callback_events` 唯一索引）、来源 IP 命中白名单。

### 7.12 Agent 接入层（`agent/` Python 侧 + `agents/aragon-gateway` Node 侧）

Python 侧 `AragonAgentAdapter`（`agent/client.py`）只暴露 6 个方法，内部全部转成对网关的 HTTP/SSE 调用：

```python
class AragonAgentAdapter(Protocol):
    async def health(self) -> EngineHealth            # 版本、pin、工具清单、能力状态
    async def capabilities(self) -> list[AgentCapabilityStatus]  # 已可用/待适配/受权限限制
    async def start_session(self, spec: SessionSpec) -> str
    async def send(self, session_id: str, turn: TurnRequest) -> str      # 返回 turn_id，事件走 SSE
    async def stop(self, session_id: str, turn_id: str) -> StopReport
    async def history(self, session_id: str, limit: int) -> list[TurnRecord]
```

- 事件统一为 `AgentEvent{seq, turn_id, type, payload}`，`type` 取值：`session.started`、`turn.started`、`assistant.delta`、`thinking.delta`、`tool.started`、`tool.progress`、`tool.finished`、`todo.updated`、`turn.queued`、`turn.finished`、`turn.stopped`、`turn.failed`、`engine.notice`。
- 网关（`agents/aragon-gateway/src`）职责：
  1. `engine.ts`：构造 `new AragonAgent({systemPrompt, model, tools, providerRegistry, getApiKey, maxTokens, timeouts})`；`getApiKey` 只从 `platform-client.ts` 的短时令牌通道取（`POST /internal/credentials/materialize`，TTL 60s，审计一次）；`subscribe()` 把 12 类 `AgentEvent` 映射为平台事件；`abort()` 实现停止。
  2. `policy.ts`：读 `var/agent-policy.json`（由平台设置页写入），决定注册哪些 `sr_*` 工具；**上游内置工具一律不注册**。
  3. `tools/*.ts`：平台工具实现（见 §12.3），全部 `defineTool` + JSON Schema 参数校验 + 超时 + 审计条目。
  4. `sessions`：上游 SDK 无持久化 → 网关自己保存 `turns.jsonl`，Python 侧 `agent_sessions`/`agent_turns` 是权威记录；重启后用 `replaceMessages()` 把最近 N 轮（默认 20，超阈值先做摘要）重放给引擎。
  5. 停止语义：`interrupt` 上游 `abort()` → 汇总「已完成的工具调用 / 已提交但可能仍在运行的上游请求」（来自工具返回值里的 `upstream_task_id`）→ `StopReport{stopped_steps[], external_pending[]}`。
  6. **`engine-host.ts`（v2 新增，解决「切换引擎需要重启网关」而又不能给 API 进程容器权限的问题）**：
     - 引擎运行在网关进程派生出的**子进程**里（`node engine-host.js --pin <version> --home var/aragon-home/engines/<version>`），父进程只做 HTTP 面与工具代理，二者通过 `process.send`/`stdout` NDJSON 通信。
     - `POST /internal/admin/reload {target_version}` 由父进程执行四步：① 用目标 pin 起新子进程；② 等它自报 `ready`（含 `engine.version` 与工具清单摘要）；③ 用 `abort()` + `waitForIdle()` 排空旧子进程（活跃轮次先收尾，超时 60s 后强制 `abort` 并标记该轮 `turn_stopped`）；④ 关闭旧子进程并更新 `/healthz` 报告版本。
     - 任一步失败 → 关闭新子进程，保持旧子进程继续服务，返回 `{ok:false, stage, detail}` 且**不改变** `engine_pin`。因此「切换失败时能够恢复上一可用版本」在进程内完成，不需要重启容器，也不需要 Docker socket。
     - 不使用信号终止（规避上游实测的 Windows 信号差异），改用子进程自退 + 父进程超时兜底。
- Python 侧 `services/agentic_flow.py`（状态机与证据）+ `api/admin/agentic_flow.py`（路由，见 §8.2）——v2 新增，对应需求「Agent 联调流程」：
  - 状态机 `parse → draft → validate → test → sample → check → publish`，每步持久化在 `agent_flow_steps`，字段 `status(pending|running|passed|failed|skipped)`、`detail`、`evidence`（测试记录 id、文档 id、工具调用 id）、`started_at/finished_at`。
  - 「从失败步骤继续」= 只重跑 `status != passed` 的步骤，前置步骤的 `evidence` 原样复用；重跑前校验证据是否过期（如测试记录 >24h 则一并重跑 `test`）。
  - 「预先授权的自动执行范围」存 `agent_flows.budget`（`allowed_providers[]`、`max_test_calls`、`max_cost_usd`、`window_hours`）；超出即转 `agent_approvals` 审批（`status=pending`，10 分钟未决置 `expired` 并拒绝，全程写审计）。
  - 发布依据必须是 `evidence` 里的真实记录（测试记录 + 样例调用记录），不允许「Agent 声称通过」直接发布；自动发布仅在 `agent_flows.auto_publish=1` 且全部步骤 `passed` 时触发。

### 7.13 引擎升级管理器（`upgrade/`）

见 §12.4，模块拆分：`detect.py`（双通道版本发现）、`build.py`（隔离构建）、`verify.py`（一致性套件）、`switch.py`（指针切换 + 重启 + 健康检查）、`rollback.py`。

### 7.14 可观测性与错误约定

- 每个请求生成 `request_id`（`uuid4().hex[:16]`），写入响应头 `X-Request-Id` 与所有日志/错误体/调用记录；前端错误提示固定展示「请求编号」并支持一键复制。
- 日志：`skillrouter.access` / `skillrouter.run` / `skillrouter.audit` 三个 logger，JSON 行格式，字段 `ts,level,event,request_id,actor,object,latency_ms,code`；日志写入 `var/logs/*.log` 轮转（`RotatingFileHandler`，10MB × 5）。
- 指标：`/metrics`（Prometheus 文本）暴露 `sr_calls_total{status,capability}`、`sr_call_latency_ms` histogram、`sr_queue_depth`、`sr_worker_lease_expired_total`、`sr_agent_turn_active`、`sr_engine_version_info{version}`。

---

## 8. 文件 / 模块变更计划

> 「新增」表示从零创建；「修改」仅用于本仓库已存在文件的后续迭代。当前仓库除 `.agentmesh/`、`docs/` 外为空，因此本表几乎全是**新增**。行数按实现顺序给出，便于并行开发切分（owner 列：A=后端核心，B=执行/任务，C=Agent/引擎，D=前端，E=工具链）。

### 8.1 仓库根与部署

| 文件 | 动作 | 意图 | owner |
|---|---|---|---|
| `README.md` | 新增 | 产品简介、5 分钟本地启动、目录导航、状态说明 | E |
| `docs/README.md` | 新增 | 文档索引：部署、接口、SKILL 接入、运维 runbook | E |
| `.gitignore` | 新增 | 忽略 `var/`、`node_modules/`、`.next/`、`*.db`、日志、`.env` | E |
| `.editorconfig` | 新增 | 统一 LF/UTF-8/缩进 | E |
| `deploy/docker-compose.yml` | 新增 | web/api/worker/gateway/proxy 五服务、卷、健康检查、依赖顺序 | E |
| `deploy/.env.example` | 新增 | 全部 `SKILLROUTER_*` 变量模板（无真实密钥） | E |
| `deploy/nginx.conf` | 新增 | TLS、`proxy_buffering off`（SSE）、上传体积、安全头 | E |
| `deploy/Dockerfile.api` | 新增 | python:3.11-slim，非 root 用户，`uvicorn` | E |
| `deploy/Dockerfile.web` | 新增 | node:22-slim 多阶段 → `next start` standalone | E |
| `deploy/Dockerfile.gateway` | 新增 | node:22-slim，`npm ci --omit=dev` 后运行网关 | E |
| `scripts/bootstrap.py` | 新增 | 初始化 DB/迁移/管理员/目录，打印摘要 | A |
| `scripts/backup.py` | 新增 | SQLite `.backup` + 文件目录打包 + 主密钥指纹 | E |
| `scripts/restore.py` | 新增 | 校验后恢复，指纹不匹配即拒绝 | E |
| `scripts/perf_smoke.py` | 新增 | 10 并发压测管理接口与任务提交，输出 P95 | E |
| `scripts/seed_demo.py` | 新增 | 可选演示数据（明确标注 mock，不用于验收） | E |
| `Makefile` / `make.ps1` | 新增 | `make dev/test/lint/e2e/up` 统一入口 | E |

### 8.2 后端 `apps/api`

| 文件 | 动作 | 意图 | owner |
|---|---|---|---|
| `apps/api/pyproject.toml` | 新增 | 包元数据、ruff/mypy/pytest 配置 | A |
| `apps/api/requirements.txt` | 新增 | 运行时依赖（精确 pin） | A |
| `apps/api/requirements-dev.txt` | 新增 | 测试/静态检查依赖 | A |
| `apps/api/skillrouter_api/__init__.py` | 新增 | 版本常量 `__version__` | A |
| `apps/api/skillrouter_api/settings.py` | 新增 | 全部环境变量与校验（§7.1） | A |
| `apps/api/skillrouter_api/main.py` | 新增 | `create_app()`、中间件（request_id、安全头、CORS 关闭同源）、路由挂载、异常处理器、`/healthz` | A |
| `apps/api/skillrouter_api/deps.py` | 新增 | `get_db`、`require_admin`、`require_password_changed`、`require_token(scope)` | A |
| `apps/api/skillrouter_api/errors.py` | 新增 | `ApiError`、错误码枚举（§附录 B）、统一响应封装 | A |
| `apps/api/skillrouter_api/logging.py` | 新增 | JSON 日志、request_id 上下文、`redact()` | A |
| `apps/api/skillrouter_api/db/__init__.py` | 新增 | 导出 engine/session/write | A |
| `apps/api/skillrouter_api/db/engine.py` | 新增 | 引擎与 PRAGMA（WAL/外键/超时） | A |
| `apps/api/skillrouter_api/db/session.py` | 新增 | 会话工厂、`db_session()` 上下文 | A |
| `apps/api/skillrouter_api/db/writer.py` | 新增 | 写序列化器（单写者） | A |
| `apps/api/skillrouter_api/db/base.py` | 新增 | `Base`、`TimestampMixin`、`uuid_pk()` | A |
| `apps/api/skillrouter_api/db/seed.py` | 新增 | `system_state` 初始化、价格表种子、默认技能策略 | A |
| `apps/api/skillrouter_api/models/__init__.py` | 新增 | 汇总导入（供 Alembic autogenerate） | A |
| `apps/api/skillrouter_api/models/auth.py` | 新增 | `AdminUser`、`AdminSession`、`LoginAttempt` | A |
| `apps/api/skillrouter_api/models/providers.py` | 新增 | `Provider`、`Credential`、`CredentialVerification` | A |
| `apps/api/skillrouter_api/models/capabilities.py` | 新增 | `Capability`、`CapabilityRoute`（v2 新增）、`CapabilityVersion`、`CapabilityTestRun`、`CompatibilityGroup` | A |
| `apps/api/skillrouter_api/models/skills.py` | 新增 | `Skill`、`SkillVersion`、`SkillFile`、`SkillImport`、`SkillExport` | A |
| `apps/api/skillrouter_api/models/execution.py` | 新增 | `CallLog`、`RouteDecision`、`UsageRecord`、`PriceBook`、`IdempotencyKey`、`Task`、`TaskQueue`、`CallbackEvent` | B |
| `apps/api/skillrouter_api/models/access.py` | 新增 | `PlatformToken`、`RateCounter`（v2，含 token/provider/capability/global 四类 scope）、`QuotaLimit`、`QuotaCounter` | A |
| `apps/api/skillrouter_api/models/agent.py` | 新增 | `AgentSession`、`AgentTurn`、`AgentEventRow`、`AgentArtifact`、`AgentToolCall`、`AgentFlow`、`AgentFlowStep`、`AgentApproval`（v2 后三者） | C |
| `apps/api/skillrouter_api/models/engine.py` | 新增 | `EngineDeployment`、`EngineCandidate`、`EngineUpgradeRun`、`EngineCapabilityStatus` | C |
| `apps/api/skillrouter_api/models/system.py` | 新增 | `SystemState`、`Settings`、`AuditLog`、`FileObject`、`MasterKeyRotation`、`WorkerHeartbeat` | A |
| `apps/api/skillrouter_api/schemas/common.py` | 新增 | `Page[T]`、`ApiErrorBody`、`IdResponse` | A |
| `apps/api/skillrouter_api/schemas/auth.py` | 新增 | 登录/改密/会话 DTO + 校验 | A |
| `apps/api/skillrouter_api/schemas/providers.py` | 新增 | 提供商/凭证 DTO（响应中 `secret` 只出 `masked`） | A |
| `apps/api/skillrouter_api/schemas/capabilities.py` | 新增 | 能力定义 DTO、`DraftRequest`、`TestRequest`、`PublishRequest` | A |
| `apps/api/skillrouter_api/schemas/skills.py` | 新增 | SKILL DTO、`ImportRequest`、`ExportRequest`、`ExecutionPlan` | A |
| `apps/api/skillrouter_api/schemas/execution.py` | 新增 | `InvokeRequest`/`InvokeResponse`、`TaskResponse`、`CallLogItem` | B |
| `apps/api/skillrouter_api/schemas/agent.py` | 新增 | `AgentMessageRequest`、`AgentEventDTO`、`StopReport` | C |
| `apps/api/skillrouter_api/schemas/engine.py` | 新增 | 引擎状态/候选/升级运行 DTO | C |
| `apps/api/skillrouter_api/security/passwords.py` | 新增 | argon2 哈希/校验/重哈希 | A |
| `apps/api/skillrouter_api/security/sessions.py` | 新增 | 会话签发/校验/滑动续期/吊销 | A |
| `apps/api/skillrouter_api/security/csrf.py` | 新增 | 双提交校验依赖 | A |
| `apps/api/skillrouter_api/security/ratelimit.py` | 新增 | 登录限流、令牌限流、并发信号量 | A |
| `apps/api/skillrouter_api/security/tokens.py` | 新增 | 平台令牌生成/哈希/校验/作用域 | A |
| `apps/api/skillrouter_api/security/fetch_guard.py` | 新增 | SSRF 防护、DNS/IP 校验、大小/超时/重定向限制 | B |
| `apps/api/skillrouter_api/security/instruction_scanner.py` | 新增 | 外部内容提示注入扫描 | B |
| `apps/api/skillrouter_api/crypto/masterkey.py` | 新增 | 主密钥加载/生成/指纹 | A |
| `apps/api/skillrouter_api/crypto/secretbox.py` | 新增 | AES-256-GCM 封装/解封 | A |
| `apps/api/skillrouter_api/crypto/keyring.py` | 新增 | 轮换与分批重加密 | A |
| `apps/api/skillrouter_api/services/__init__.py` | 新增 | — | A |
| `apps/api/skillrouter_api/services/bootstrap.py` | 新增 | 首次初始化、迁移断言 | A |
| `apps/api/skillrouter_api/services/providers.py` | 新增 | 提供商 CRUD、关联视图 | A |
| `apps/api/skillrouter_api/services/credentials.py` | 新增 | 凭证 CRUD、掩码、影响面、替换/轮换、凭证代理 `materialize()` | A |
| `apps/api/skillrouter_api/services/credentials_verify.py` | 新增 | 各类凭证有效性探测 | A |
| `apps/api/skillrouter_api/services/capabilities.py` | 新增 | 能力 CRUD、校验、草稿/测试/发布/停用、diff、回退、`impact()`（v2） | B |
| `apps/api/skillrouter_api/services/agentic_flow.py` | 新增 | 接入流程状态机、步骤证据、从失败步骤继续、自动发布判定（v2） | C |
| `apps/api/skillrouter_api/services/versions.py` | 新增 | 不可变版本行、`content_hash`、版本 diff 算法 | B |
| `apps/api/skillrouter_api/services/routing.py` | 新增 | 候选筛选/排序/故障切换/决策落库 | B |
| `apps/api/skillrouter_api/services/execution.py` | 新增 | 统一执行编排（同步/异步/流式）、幂等、重试、记录 | B |
| `apps/api/skillrouter_api/services/tasks.py` | 新增 | 任务创建/查询/取消/恢复、`status_unknown` 处理 | B |
| `apps/api/skillrouter_api/services/files.py` | 新增 | 内容寻址存储、签名 URL、保留清理 | B |
| `apps/api/skillrouter_api/services/skills.py` | 新增 | SKILL CRUD/版本/导出/执行计划/调用测试 | A |
| `apps/api/skillrouter_api/services/skillimport.py` | 新增 | Git/URL/文件导入、预览、结构校验、风险审查 | A |
| `apps/api/skillrouter_api/services/access.py` | 新增 | 平台令牌 CRUD、作用域、撤销、配额绑定 | A |
| `apps/api/skillrouter_api/services/usage.py` | 新增 | 用量与费用（实际 vs 估算）、价格表 | A |
| `apps/api/skillrouter_api/services/audit.py` | 新增 | 审计写入与查询（脱敏） | A |
| `apps/api/skillrouter_api/services/overview.py` | 新增 | 概览聚合（能力数、成功率、失败、耗时、待办） | A |
| `apps/api/skillrouter_api/services/settings.py` | 新增 | 设置读写（含 `engine_policy`、`payload_policy`、白名单） | A |
| `apps/api/skillrouter_api/executors/base.py` | 新增 | `Executor` 协议、`ExecResult`、错误分类枚举 | B |
| `apps/api/skillrouter_api/executors/mapping.py` | 新增 | `${path}` 模板求值、类型转换、严格模式 | B |
| `apps/api/skillrouter_api/executors/auth.py` | 新增 | 四种认证注入 + 扩展位 | B |
| `apps/api/skillrouter_api/executors/http_executor.py` | 新增 | 同步/异步/流式执行核心 | B |
| `apps/api/skillrouter_api/executors/parsers.py` | 新增 | JSON/JSONPath/文本/二进制/base64 解析与 `response_map` | B |
| `apps/api/skillrouter_api/executors/async_poll.py` | 新增 | 上游任务轮询、取消能力探测 | B |
| `apps/api/skillrouter_api/executors/errors.py` | 新增 | `errors[]` 匹配 → 平台错误码映射 | B |
| `apps/api/skillrouter_api/router/selector.py` | 新增 | 路由选择与决策 | B |
| `apps/api/skillrouter_api/router/health.py` | 新增 | 健康分计算（滑动窗口） | B |
| `apps/api/skillrouter_api/router/compatibility.py` | 新增 | 等价性判定 | B |
| `apps/api/skillrouter_api/agent/client.py` | 新增 | `AragonAgentAdapter` 实现（HTTP/SSE → 网关） | C |
| `apps/api/skillrouter_api/agent/events.py` | 新增 | 事件模型与持久化（`agent_events`） | C |
| `apps/api/skillrouter_api/agent/sessions.py` | 新增 | 会话/轮次状态机、排队、停止报告 | C |
| `apps/api/skillrouter_api/agent/prompting.py` | 新增 | 平台系统提示、外部资料包裹、工具说明 | C |
| `apps/api/skillrouter_api/agent/toolspec.py` | 新增 | 平台工具清单（与网关 `policy.ts` 同源生成） | C |
| `apps/api/skillrouter_api/sse/broker.py` | 新增 | 事件总线（内存 pub/sub + DB 回放） | A |
| `apps/api/skillrouter_api/sse/events.py` | 新增 | 事件 `id` 分配、`Last-Event-ID` 重放、心跳注释 | A |
| `apps/api/skillrouter_api/api/admin/auth.py` | 新增 | 登录/退出/改密/me/CSRF 引导 | A |
| `apps/api/skillrouter_api/api/admin/overview.py` | 新增 | 概览数据与待办列表 | A |
| `apps/api/skillrouter_api/api/admin/providers.py` | 新增 | 提供商与凭证路由 | A |
| `apps/api/skillrouter_api/api/admin/capabilities.py` | 新增 | 能力 CRUD/测试/发布/版本/回退 | B |
| `apps/api/skillrouter_api/api/admin/skills.py` | 新增 | SKILL CRUD/导入/导出/测试 | A |
| `apps/api/skillrouter_api/api/admin/access.py` | 新增 | 外部 Agent 与令牌管理 | A |
| `apps/api/skillrouter_api/api/admin/calls.py` | 新增 | 调用记录查询/详情/导出 | B |
| `apps/api/skillrouter_api/api/admin/tasks.py` | 新增 | 任务列表/详情/取消/重试 | B |
| `apps/api/skillrouter_api/api/admin/agent_chat.py` | 新增 | 会话、消息、SSE、停止、历史 | C |
| `apps/api/skillrouter_api/api/admin/engine.py` | 新增 | 引擎状态/检测/升级/回滚/记录 | C |
| `apps/api/skillrouter_api/api/admin/settings.py` | 新增 | 设置、主密钥轮换、审计查询、备份触发 | A |
| `apps/api/skillrouter_api/api/admin/agentic_flow.py` | 新增 | 接入流程状态机（parse→draft→validate→test→sample→publish） | C |
| `apps/api/skillrouter_api/api/v1/meta.py` | 新增 | `/api/v1/health`、`/api/v1/whoami`、OpenAPI 说明 | A |
| `apps/api/skillrouter_api/api/v1/discovery.py` | 新增 | `GET /capabilities`、`/capabilities/{key}`（ETag） | B |
| `apps/api/skillrouter_api/api/v1/invoke.py` | 新增 | 统一调用（sync/async/stream）+ 幂等键 | B |
| `apps/api/skillrouter_api/api/v1/tasks.py` | 新增 | 任务查询/取消 | B |
| `apps/api/skillrouter_api/api/v1/files.py` | 新增 | 文件访问/签名 URL | B |
| `apps/api/skillrouter_api/api/v1/skills.py` | 新增 | SKILL 分发（列表/内容/连接包下载） | A |
| `apps/api/skillrouter_api/api/v1/mcp.py` | 新增 | MCP Streamable HTTP（initialize/tools.list/tools.call） | C |
| `apps/api/skillrouter_api/api/v1/callbacks.py` | 新增 | 第三方回调（签名/去重/校验） | B |
| `apps/api/skillrouter_api/worker/__main__.py` | 新增 | worker 入口、信号处理、优雅退出 | B |
| `apps/api/skillrouter_api/worker/runner.py` | 新增 | 领取循环、并发控制、心跳、超时 | B |
| `apps/api/skillrouter_api/worker/leases.py` | 新增 | 租约 acquire/renew/release/reap | B |
| `apps/api/skillrouter_api/worker/handlers/__init__.py` | 新增 | handler 注册表 | B |
| `apps/api/skillrouter_api/worker/handlers/capability_invoke.py` | 新增 | 同步/异步调用执行与恢复 | B |
| `apps/api/skillrouter_api/worker/handlers/agent_turn.py` | 新增 | 转发 Agent 轮次、事件落库、停止 | C |
| `apps/api/skillrouter_api/worker/handlers/skill_import.py` | 新增 | 导入流水线执行 | A |
| `apps/api/skillrouter_api/worker/handlers/file_parse.py` | 新增 | 文档解析（pdf/txt/md/json） | B |
| `apps/api/skillrouter_api/worker/handlers/credential_verify.py` | 新增 | 凭证验证批处理 | A |
| `apps/api/skillrouter_api/worker/handlers/retention.py` | 新增 | 过期清理与文件回收 | B |
| `apps/api/skillrouter_api/upgrade/detect.py` | 新增 | 双通道版本检测与 provenance 判定 | C |
| `apps/api/skillrouter_api/upgrade/build.py` | 新增 | 隔离构建（npm ci + build + 完整性校验） | C |
| `apps/api/skillrouter_api/upgrade/verify.py` | 新增 | 一致性套件执行与报告 | C |
| `apps/api/skillrouter_api/upgrade/switch.py` | 新增 | 指针切换、网关重启、健康检查 | C |
| `apps/api/skillrouter_api/upgrade/rollback.py` | 新增 | 回滚与状态恢复 | C |
| `apps/api/skillrouter_api/upgrade/state.py` | 新增 | 升级状态机与审计 | C |
| `apps/api/migrations/alembic.ini` | 新增 | Alembic 配置 | A |
| `apps/api/migrations/env.py` | 新增 | 迁移环境（SQLite batch mode） | A |
| `apps/api/migrations/versions/0001_init.py` | 新增 | 全量初始 schema（§10） | A |
| `apps/api/migrations/versions/0002_engine_upgrade.py` | 新增 | 引擎升级相关表（可与 0001 合并，保留以示演进） | C |
| `apps/api/Dockerfile` | 新增 | api/worker 镜像 | E |
| `apps/api/tests/conftest.py` | 新增 | 临时 DB、TestClient、认证 fixture、假上游 | A |
| `apps/api/tests/test_auth_flow.py` | 新增 | 初始化/强制改密/会话过期/限流/CSRF | A |
| `apps/api/tests/test_crypto_credentials.py` | 新增 | 加解密、掩码、轮换、无明文泄漏 | A |
| `apps/api/tests/test_credentials_verify.py` | 新增 | 凭证有效性探测：成功/401/429/超时，结果落库与 `last_verified_at`（v2 补，§15.3 引用） | A |
| `apps/api/tests/test_capabilities_validation.py` | 新增 | Schema/映射/限制/错误映射校验 + 多 route、`modes`、`file_io`、`examples` 校验（v2） | B |
| `apps/api/tests/test_execution_sync_async_stream.py` | 新增 | 三种模式与超时/重试/幂等（含「异步请求内不触达上游」断言，v2） | B |
| `apps/api/tests/test_routing.py` | 新增 | 等价性、优先级、健康切换、`preferred_provider`、route 级决策记录（v2） | B |
| `apps/api/tests/test_tasks_recovery.py` | 新增 | 租约过期、重启恢复、取消语义 | B |
| `apps/api/tests/test_skills_import_export.py` | 新增 | 导入校验、路径穿越、导出无密钥 | A |
| `apps/api/tests/test_platform_tokens.py` | 新增 | 令牌创建/撤销/限流/配额 | A |
| `apps/api/tests/test_api_contract.py` | 新增 | `/api/v1` 契约（对齐 openapi.json） | B |
| `apps/api/tests/test_mcp.py` | 新增 | MCP initialize/list/call 与错误 | C |
| `apps/api/tests/test_agent_adapter.py` | 新增 | 适配层事件映射、停止报告（假网关） | C |
| `apps/api/tests/test_engine_upgrade.py` | 新增 | 检测/验证失败回滚/成功切换 | C |
| `apps/api/tests/test_audit_redaction.py` | 新增 | 审计脱敏、日志无密钥 | A |
| `apps/api/tests/test_ssrf_guard.py` | 新增 | 私网/元数据/重定向/超限 | B |
| `apps/api/tests/fixtures/fake_upstream.py` | 新增 | 可编排的假上游（同步/异步/流式/错误） | B |
| `apps/api/tests/fixtures/fake_gateway.py` | 新增 | 假网关（NDJSON/SSE 事件脚本） | C |
| `apps/api/tests/perf/test_baseline.py` | 新增 | 10 并发 P95 断言（标记 `slow`） | E |

### 8.3 Agent 网关 `agents/aragon-gateway`

| 文件 | 动作 | 意图 | owner |
|---|---|---|---|
| `agents/aragon-gateway/package.json` | 新增 | `"@aragon-agent/core": "0.2.34"`（精确）、fastify、tsx、vitest | C |
| `agents/aragon-gateway/package-lock.json` | 新增 | 锁定依赖树（含 core 完整性哈希） | C |
| `agents/aragon-gateway/tsconfig.json` | 新增 | NodeNext ESM、strict | C |
| `agents/aragon-gateway/src/config.ts` | 新增 | env 校验（secret、bind、platform URL、policy 路径） | C |
| `agents/aragon-gateway/src/main.ts` | 新增 | Fastify 启动、内部认证钩子、路由挂载 | C |
| `agents/aragon-gateway/src/engine.ts` | 新增 | `Agent` 构造、`subscribe()` 映射、`abort()`、会话内存表 | C |
| `agents/aragon-gateway/src/engine-host.ts` | 新增 | 引擎宿主子进程（v2）：按 pin 加载引擎、向父进程上报 `ready`/事件、接收 `abort`/`shutdown`；使 `reload` 能在不重启容器的情况下切换版本 | C |
| `agents/aragon-gateway/src/policy.ts` | 新增 | 工具白名单加载与校验（拒绝未知工具） | C |
| `agents/aragon-gateway/src/platform-client.ts` | 新增 | 调平台内部接口（凭证 materialize、能力目录、审计） | C |
| `agents/aragon-gateway/src/sse.ts` | 新增 | 事件流（SSE 上行）、`seq` 分配、背压 | C |
| `agents/aragon-gateway/src/health.ts` | 新增 | `/healthz`：引擎版本、工具摘要、活跃轮次 | C |
| `agents/aragon-gateway/src/tools/index.ts` | 新增 | 工具注册表（按 policy 过滤） | C |
| `agents/aragon-gateway/src/tools/sr_capabilities.ts` | 新增 | `sr_capabilities_list` / `sr_capability_get` | C |
| `agents/aragon-gateway/src/tools/sr_sources.ts` | 新增 | `sr_fetch_source` / `sr_parse_document`（受 fetch_guard 约束） | C |
| `agents/aragon-gateway/src/tools/sr_draft.ts` | 新增 | `sr_propose_capability_draft` / `sr_update_capability_draft` | C |
| `agents/aragon-gateway/src/tools/sr_test.ts` | 新增 | `sr_test_capability` / `sr_sample_invoke`（沙箱化真实调用） | C |
| `agents/aragon-gateway/src/tools/sr_skill.ts` | 新增 | `sr_create_skill_draft` / `sr_validate_skill` | C |
| `agents/aragon-gateway/src/tools/sr_approval.ts` | 新增 | `sr_request_approval`（越权/预算外操作） | C |
| `agents/aragon-gateway/src/tools/sr_notes.ts` | 新增 | `sr_record_finding`（待补信息、缺口标注） | C |
| `agents/aragon-gateway/test/conformance.test.ts` | 新增 | 上游导出符号/事件字段/工具清单一致性 | C |
| `agents/aragon-gateway/test/tools.test.ts` | 新增 | 工具参数校验、超时、错误转换 | C |
| `agents/aragon-gateway/test/policy.test.ts` | 新增 | 白名单：上游新工具默认不注册 | C |
| `agents/aragon-gateway/Dockerfile` | 新增 | gateway 镜像 | E |
| `agents/aragon-gateway/README.md` | 新增 | 版本、边界、工具说明、升级注意 | C |

### 8.4 前端 `apps/web`

| 文件 | 动作 | 意图 | owner |
|---|---|---|---|
| `apps/web/package.json` / `package-lock.json` | 新增 | Next 15/React 19/Tailwind/Radix/Query/zod | D |
| `apps/web/next.config.mjs` | 新增 | standalone、`/api` 反代到 FastAPI、安全头 | D |
| `apps/web/tsconfig.json` | 新增 | strict、路径别名 `@/*` | D |
| `apps/web/tailwind.config.ts` | 新增 | 设计令牌映射（色阶/间距/圆角/字号） | D |
| `apps/web/styles/tokens.css` | 新增 | CSS 变量：中性色阶、语义色、焦点环、间距刻度 | D |
| `apps/web/styles/globals.css` | 新增 | 基础重置、排版、`prefers-reduced-motion` | D |
| `apps/web/app/layout.tsx` | 新增 | 根布局、字体、Providers（Query/Agent 浮窗） | D |
| `apps/web/app/(auth)/login/page.tsx` | 新增 | 登录页（含限流/错误/首次引导） | D |
| `apps/web/app/(auth)/change-password/page.tsx` | 新增 | 强制改密页（不可跳过） | D |
| `apps/web/app/(app)/layout.tsx` | 新增 | 侧边导航、顶栏（环境/引擎状态）、浮窗挂载点 | D |
| `apps/web/app/(app)/page.tsx` | 新增 | 概览：能力数、接入状态、近期成功率、失败任务、耗时、待办（全部可跳转） | D |
| `apps/web/app/(app)/providers/page.tsx` | 新增 | 提供商列表（搜索/筛选/排序/分页） | D |
| `apps/web/app/(app)/providers/[id]/page.tsx` | 新增 | 详情：凭证与能力关联图、标签、备注 | D |
| `apps/web/app/(app)/providers/[id]/credentials/[cid]/page.tsx` | 新增 | 凭证详情：掩码、验证历史、影响面、替换/轮换 | D |
| `apps/web/app/(app)/capabilities/page.tsx` | 新增 | 能力列表（状态徽标：草稿/测试/已发布/停用） | D |
| `apps/web/app/(app)/capabilities/new/page.tsx` | 新增 | 新建向导（手工配置 3 步 / 交给 Agent） | D |
| `apps/web/app/(app)/capabilities/[id]/page.tsx` | 新增 | 能力详情：结构化编辑器 + 示例 + 测试面板 + 版本/依赖 | D |
| `apps/web/app/(app)/capabilities/[id]/versions/page.tsx` | 新增 | 版本历史、diff、回退 | D |
| `apps/web/app/(app)/skills/page.tsx` | 新增 | SKILL 列表（分类/搜索/状态/导出） | D |
| `apps/web/app/(app)/skills/new/page.tsx` | 新增 | 创建/导入 SKILL（来源预览、权限申请、校验结果） | D |
| `apps/web/app/(app)/skills/[id]/page.tsx` | 新增 | SKILL 详情：说明、依赖能力、执行计划、调用测试 | D |
| `apps/web/app/(app)/agent/page.tsx` | 新增 | Agent 接入与全屏工作台（流程步骤 + 事件时间线） | D |
| `apps/web/app/(app)/access/page.tsx` | 新增 | 外部 Agent / 令牌管理（作用域、限流、预算、撤销） | D |
| `apps/web/app/(app)/calls/page.tsx` | 新增 | 调用记录（搜索/筛选/分页/详情抽屉） | D |
| `apps/web/app/(app)/tasks/page.tsx` | 新增 | 任务记录（状态、进度、恢复、取消） | D |
| `apps/web/app/(app)/settings/page.tsx` | 新增 | 设置分区分页（见 §13.2） | D |
| `apps/web/app/(app)/settings/engine/page.tsx` | 新增 | 引擎版本/候选/验证结果/升级与回滚记录 | D |
| `apps/web/components/ui/*.tsx` | 新增 | Button/Input/Select/Table/Dialog/Tabs/Toast/Badge/Tooltip/Drawer（Radix） | D |
| `apps/web/components/common/AppShell.tsx` | 新增 | 侧栏 + 面包屑 + 页面标题区（一致布局） | D |
| `apps/web/components/common/DataTable.tsx` | 新增 | 搜索/筛选/排序/分页/空态/加载/错误重试 | D |
| `apps/web/components/common/FormField.tsx` | 新增 | 就地校验、错误提示、必填标记 | D |
| `apps/web/components/common/StatusBadge.tsx` | 新增 | 统一状态语义词（草稿/测试通过/已发布/可调用） | D |
| `apps/web/components/common/DangerConfirm.tsx` | 新增 | 危险操作影响面清单 + 二次确认 | D |
| `apps/web/components/common/EmptyState.tsx` | 新增 | 首次使用/空数据/无权限/网络中断/服务不可用五种态 | D |
| `apps/web/components/capabilities/SchemaEditor.tsx` | 新增 | JSON Schema 编辑器（校验 + 格式化 + 字段路径错误） | D |
| `apps/web/components/capabilities/ParamMapper.tsx` | 新增 | 参数映射可视化编辑 | D |
| `apps/web/components/capabilities/RouteEditor.tsx` | 新增 | 多 route 编辑（提供商/凭证/优先级/启用/映射），展示当前主力与故障切换顺序（v2） | D |
| `apps/web/components/capabilities/TestPanel.tsx` | 新增 | 请求预览、发送、响应查看（脱敏）、耗时 | D |
| `apps/web/components/agent/AgentDock.tsx` | 新增 | 固定 Agent 入口（不遮挡主操作），可折叠 | D |
| `apps/web/components/agent/AgentPanel.tsx` | 新增 | 面板：消息、拖拽上传、预览、流式、进度、停止、重试、历史 | D |
| `apps/web/components/agent/AgentComposer.tsx` | 新增 | Enter 发送 / Shift+Enter 换行、排队状态、附件 | D |
| `apps/web/components/agent/AgentTimeline.tsx` | 新增 | 工具进度与步骤结果（读取资料/配置变更/测试结果/待补信息） | D |
| `apps/web/components/agent/FlowPanel.tsx` | 新增 | 接入流程七步状态、每步证据展开、从失败步骤继续、待审批卡片（v2） | D |
| `apps/web/components/agent/useAgentSession.ts` | 新增 | 会话状态机 + SSE 订阅 + 断线重连 + 页面切换保持 | D |
| `apps/web/lib/api.ts` | 新增 | fetch 封装（CSRF、request_id、错误映射、重试策略） | D |
| `apps/web/lib/sse.ts` | 新增 | EventSource 封装（`Last-Event-ID`、退避重连、心跳超时） | D |
| `apps/web/lib/query.ts` | 新增 | QueryClient + 统一 key 工厂 | D |
| `apps/web/lib/auth.ts` | 新增 | `me` 引导、强制改密重定向 | D |
| `apps/web/lib/format.ts` | 新增 | 时间/耗时/字节/费用格式化（统一中文文案） | D |
| `apps/web/e2e/core-loop.spec.ts` | 新增 | Playwright：配置→测试→发布→调用→查看记录 | D |
| `apps/web/e2e/agent-panel.spec.ts` | 新增 | 浮窗：发送/排队/停止/重连/历史 | D |
| `apps/web/Dockerfile` | 新增 | 多阶段构建 → standalone | E |

### 8.5 npm 包与契约

| 文件 | 动作 | 意图 | owner |
|---|---|---|---|
| `packages/skillrouter-cli/package.json` | 新增 | `@skillrouter/cli` 0.1.0，bin `skillrouter` | E |
| `packages/skillrouter-cli/src/cli.ts` | 新增 | 命令分发（install/verify/update/uninstall/list） | E |
| `packages/skillrouter-cli/src/detect.ts` | 新增 | 探测目标 Agent 与目录（`.aragon/skills`、`.claude/skills`、`~/.aragon-agent`） | E |
| `packages/skillrouter-cli/src/install.ts` | 新增 | 写 `SKILL.md` + `invoke.mjs` + 配置（token 存 OS 凭据或 0600 文件，配置只存 `token_ref`） | E |
| `packages/skillrouter-cli/src/verify.ts` | 新增 | `/api/v1/health` + `whoami` + `capabilities.list`（可选真实最小调用）连通性检查 | E |
| `packages/skillrouter-cli/src/update.ts` | 新增 | 重新拉取 SKILL 正文与能力目录快照（不重装，能力动态刷新） | E |
| `packages/skillrouter-cli/src/uninstall.ts` | 新增 | 清理文件与配置 | E |
| `packages/skillrouter-cli/src/templates/skill.md.tpl` | 新增 | SKILL 模板（frontmatter 兼容 AragonAgent/Claude Code，`allowed-tools:[bash]`） | E |
| `packages/skillrouter-cli/src/templates/invoke.mjs.tpl` | 新增 | 随包安装的零依赖调用助手模板（v2，见 §7.9；由后端 bundle 内容覆盖更新） | E |
| `packages/skillrouter-cli/test/*.test.ts` | 新增 | 探测/安装/验证在临时目录的行为 | E |
| `packages/skillrouter-cli/README.md` | 新增 | 安装、验证、更新、卸载、已验证 Agent 版本矩阵 | E |
| `packages/contracts/package.json` | 新增 | 生成类型包 | E |
| `packages/contracts/scripts/gen.ts` | 新增 | `openapi-typescript` 生成 `src/api.d.ts` | E |
| `packages/contracts/src/api.d.ts` | 新增 | 由 OpenAPI 生成（CI 校验无 diff） | E |
| `db/migrations/0001_init.sql` | 新增 | 与 Alembic 对齐的可审阅 SQL 快照 | A |
| `docs/api/openapi.json` | 新增 | 构建产物（CI 生成并校验） | B |
| `docs/api/examples.md` | 新增 | curl/TS/Python 调用示例（真实字段） | B |
| `docs/skills/connect.md` | 新增 | 外部 Agent 安装与发现说明（含已验证矩阵） | E |
| `docs/runbook/init.md` | 新增 | 初始化与首个能力接入 | E |
| `docs/runbook/backup-restore.md` | 新增 | 备份、恢复、主密钥匹配 | E |
| `docs/runbook/engine-upgrade.md` | 新增 | 升级/回滚/数据恢复方案 | C |
| `docs/runbook/verification-checklist.md` | 新增 | 真实联调与异常场景的复现步骤、所需最小凭证清单、「待验证」项的登记表（v2 补，§16 引用） | E |
| `docs/runbook/troubleshooting.md` | 新增 | 常见错误码与处置 | E |
| `.github/workflows/ci.yml` | 新增 | lint/typecheck/unit/contract/build/e2e(可选) | E |

## 9. 接口设计

> **本轮（迭代 2）修订说明**：本节部分取值已被 §A 覆盖（逐条对照见 §A.10）。实现或评审与本节正文冲突时**以 §A 为准**；本节保留原文，是为了让读者看到「改之前是什么样」。


### 9.1 通用约定

- 基址：管理端 `/api/admin`，对外 `/api/v1`。
- 认证：管理端 Cookie（`sr_session`）+ 写请求头 `X-CSRF-Token`；对外 `Authorization: Bearer srk_...`。
- 分页：`?page=1&page_size=20&sort=-created_at&q=&status=`；响应 `{items:[], page, page_size, total}`。
- 错误体（所有非 2xx）：

```json
{
  "error": {
    "code": "UPSTREAM_RATE_LIMIT",
    "message": "上游限流：请稍后重试或降低并发",
    "hint": "检查该提供商的 rate limit，或在设置中降低 concurrency",
    "retryable": true,
    "details": {"source": "executor", "candidates_dropped": 1},
    "request_id": "9f3c1a7b2e4d4a10"
  }
}
```

- 请求头：`X-Request-Id` 可选（客户端自定义追踪），响应始终回显。写操作支持 `Idempotency-Key`。

### 9.2 管理端 REST（`/api/admin`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/auth/bootstrap` | 是否需要初始化、是否强制改密（匿名可访问） |
| POST | `/auth/login` | `{username,password}` → 设置 cookie，返回 `{admin:{id,username,must_change_password}, csrf_token}`；限流 `429` |
| POST | `/auth/logout` | 吊销当前会话 |
| GET | `/auth/me` | 当前管理员、会话过期时间、引擎状态摘要 |
| POST | `/auth/change-password` | `{current_password,new_password}`；密码策略：≥12 字符、含字母+数字、不等于默认值；成功后吊销其它会话 |
| GET | `/overview` | 概览聚合（§9.6） |
| GET/POST | `/providers` | 列表（`q/status/category/tag`）、创建 |
| GET/PATCH/DELETE | `/providers/{id}` | 详情（含凭证/能力计数）、更新、软删（有关联时 `409 PROVIDER_IN_USE`，需 `?force=true`） |
| GET/POST | `/providers/{id}/credentials` | 列出该提供商下凭证、创建 |
| GET/PATCH/DELETE | `/credentials/{id}` | 详情（`secret.masked`）/更新/删除（被引用时 `409`，带影响面） |
| POST | `/credentials/{id}/verify` | 触发有效性检测（异步，返回 `verification_id`） |
| POST | `/credentials/{id}/disable` / `/enable` | 启停 |
| POST | `/credentials/{id}/replace` | 就地替换密文（保留 id 与关联） |
| POST | `/credentials/{id}/rotate` | 生成新凭证行并原子切换引用 |
| GET | `/credentials/{id}/impact` | 影响面：能力、SKILL、令牌、任务 |
| GET/POST | `/capabilities` | 列表（`q/kind/status/provider/modes`，`provider` 按 route 归属过滤）、创建草稿 |
| GET/PATCH/DELETE | `/capabilities/{id}` | 详情/更新草稿/删除（已发布禁止删除，只能停用） |
| POST | `/capabilities/{id}/validate` | 结构与安全校验，返回 `issues[]` |
| POST | `/capabilities/{id}/test` | 真实调用测试：`{input, route_id?, mode?, save_payload:false}` → `{test_run_id,status,http_status,latency_ms,response_preview,error}`（`route_id` 省略时按路由规则选主力，返回实际使用的 route） |
| POST | `/capabilities/{id}/publish` | `{version, changelog}`；要求最近一次测试通过且证据未过期（默认 24h） |
| POST | `/capabilities/{id}/deprecate` | 停用（不再被路由，历史可用） |
| GET | `/capabilities/{id}/versions` | 版本列表 |
| GET | `/capabilities/{id}/versions/{v}` | 版本详情 |
| GET | `/capabilities/{id}/diff?from=&to=` | 结构化 diff（字段级） |
| POST | `/capabilities/{id}/rollback` | `{to_version}`：创建新的发布指针（不修改历史版本行） |
| POST | `/capabilities/{id}/compatibility` | `{group, confirm:true}` 显式声明路由等价 |
| GET | `/capabilities/{id}/impact` | 影响面（v2）：依赖此能力的 SKILL、令牌作用域、在途任务、route 引用的凭证 |
| GET/POST | `/capabilities/{id}/routes` | 路由（多提供商通道）列表 / 新增（也可在定义 PATCH 中整体提交） |
| PATCH/DELETE | `/capabilities/{id}/routes/{route_id}` | 修改（优先级/启用/映射）/ 删除 route（在途任务引用时 `409`） |
| GET/POST | `/skills` | 列表、创建 |
| GET/PATCH/DELETE | `/skills/{id}` | 详情/更新/删除 |
| POST | `/skills/import/preview` | `{kind:git\|url\|file, url?, ref?, upload_id?}` → 预览（文件清单、commit、权限、警告） |
| POST | `/skills/import/commit` | 用 `preview_token` 落地为草稿 |
| POST | `/skills/{id}/test` | 按 `execution_plan` 试运行（含依赖能力校验） |
| POST | `/skills/{id}/publish` | 发布新版本（依赖必须指向已发布能力版本且兼容） |
| GET | `/skills/{id}/versions` / `/diff` / `/dependencies` | 版本、差异、反向依赖（谁依赖我） |
| POST | `/skills/{id}/export` | `{format: npm\|zip\|skill_md}` → 下载连接包（无密钥） |
| POST | `/skills/{id}/enable` / `/disable` | 启用 / 停用（v2；独立于 `status`，立即影响分发与执行，见 §7.9） |
| POST | `/skills/{id}/preview` | 渲染最终 `SKILL.md`（frontmatter + 正文）供发布前预览 |
| GET/POST | `/access/agents` | 外部 Agent 列表（名称、最后使用、状态）、创建 |
| GET/PATCH/DELETE | `/access/agents/{id}` | 详情/更新/删除 |
| POST | `/access/agents/{id}/tokens` | 创建令牌 → **仅此一次**返回完整 `token` 与 `install_command` |
| DELETE | `/access/tokens/{id}` | 立即撤销 |
| GET | `/calls` | 调用记录（`q/status/capability/provider/caller/from/to/min_latency`） |
| GET | `/calls/{request_id}` | 详情：路由决策、重试链、错误映射、耗时分解 |
| GET | `/tasks` / `/tasks/{id}` | 任务列表/详情（状态、进度、上游 id、恢复信息） |
| POST | `/tasks/{id}/cancel` | 取消（返回 `cancel_scope: none\|remote\|local_only\|pending_submit`；`pending_submit` 时 HTTP `202`，见 §A.5.1 / §A.10） |
| POST | `/tasks/{id}/reconcile` | 对 `status_unknown` 主动查询上游 |
| POST | `/agent/sessions` | `{title, model_profile_id, permission_profile}` → 会话 |
| GET | `/agent/sessions` / `/agent/sessions/{id}` | 历史列表/详情（含消息与工具时间线） |
| POST | `/agent/sessions/{id}/messages` | `{text, attachments[{file_id}], links[], queued_ack:true}` → `{turn_id, queue_position}` |
| GET | `/agent/sessions/{id}/stream` | SSE：`turn.started/assistant.delta/tool.*/todo.updated/turn.finished` |
| POST | `/agent/sessions/{id}/stop` | 停止 → `StopReport{stopped_steps[], external_pending[], note}` |
| POST | `/agent/sessions/{id}/retry` | 重试上一条（`{turn_id?}`） |
| POST | `/agent/flows` | 创建接入流程（v2）：`{capability_id?, credential_id, budget{...}, auto_publish}` → `agent_flow`（步骤持久化，见 §7.12） |
| GET | `/agent/flows` / `/agent/flows/{id}` | 流程列表 / 详情（每步状态、证据、失败原因） |
| POST | `/agent/flows/{id}/steps/{step}/run` | 从该步继续（复用已通过步骤的证据） |
| POST | `/agent/flows/{id}/publish` | 汇总证据并发布（要求全部步骤 `passed`） |
| GET | `/agent/approvals` | 待审批请求（越权/超预算操作），`POST /agent/approvals/{id}/decide {approve:bool}` 决策 |
| GET/PATCH | `/agent/config` | 模型配置（provider/model/base_url/温度/最大 token）、凭证引用（不含明文） |
| POST | `/agent/connectivity` | 检查网关与模型可用性（`aragon` 等价探测） |
| GET | `/engine/status` | 当前版本、pin commit、适配层版本、工具清单摘要、能力状态表 |
| GET | `/engine/candidates` | 候选版本（`version, provenance, published_at, notes`） |
| POST | `/engine/check` | 立即检测（双通道） |
| POST | `/engine/upgrade` | `{version, mode: verify_only\|deploy}` → 创建升级运行 |
| GET | `/engine/runs` / `/engine/runs/{id}` | 升级记录与检查项明细 |
| POST | `/engine/rollback` | `{to_version}` |
| GET | `/engine/capabilities` | 「已可用 / 待适配 / 受权限限制」清单（§12.2） |
| GET/PATCH | `/settings` | 通用设置（`payload_policy`、`retention_days`、`fetch_allowlist`、`budget`、`engine_policy`） |
| POST | `/settings/rotate-master-key` | 触发主密钥轮换（异步任务） |
| GET | `/settings/audit` | 审计列表（`action/actor/from/to`） |
| POST | `/settings/backup` | 触发备份，返回产物 `file_id` |
| GET | `/events` | 全局 SSE（管理端通知：任务完成、凭证过期、引擎候选） |

#### 9.2.1 对外统一接口（`/api/v1`，Bearer 令牌）

> 修正 v1 的结构问题：这块对外端点原来直接挂在「管理端 REST」表格尾部，容易误读为管理端路径。实际全部位于 `/api/v1`，认证方式与错误体与 §9.1 一致，区别是不接受 cookie。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/health` | `{status,version,engine:{version,status},time}`（无鉴权，可作连通性检查） |
| GET | `/api/v1/whoami` | 令牌主体、作用域、配额余量 |
| GET | `/api/v1/capabilities` | 目录；`ETag`/`If-None-Match`、`?refresh=1` 强制刷新 |
| GET | `/api/v1/capabilities/{key}` | 详情：`input_schema/output_schema/examples/limits/modes/file_io/version/requires_agent_reasoning` + `routes_summary[{provider,priority,available}]`（不暴露上游 URL 与凭证） |
| POST | `/api/v1/capabilities/{key}/invoke` | 统一调用（`mode` 由 `options.mode` 决定且必须 ∈ `modes`；`mode=stream` 时返回 SSE） |
| GET | `/api/v1/tasks/{task_id}` | 异步任务查询（含 `progress`、`status_detail`） |
| POST | `/api/v1/tasks/{task_id}/cancel` | 取消（明确返回 `cancel_scope`） |
| GET | `/api/v1/files/{file_id}` | 文件访问（归属校验）；`?sig=&exp=` 签名访问 |
| POST | `/api/v1/files` | 上传文件（v2 补齐，供 `{"$file": ...}` 输入引用）：`multipart/form-data`，受类型白名单/大小上限约束，返回 `{file_id, mime, size, sha256, expires_at}` |
| GET | `/api/v1/skills` | 已发布 SKILL 目录（name/description/version/required_capabilities） |
| GET | `/api/v1/skills/{name}` | SKILL 正文（frontmatter + instructions + 示例） |
| GET | `/api/v1/skills/{name}/bundle` | 连接包下载（`zip`，无密钥） |
| POST | `/api/v1/mcp` | MCP Streamable HTTP（JSON-RPC） |
| GET | `/api/v1/mcp` | MCP 服务端消息通道（SSE） |
| POST | `/api/v1/callbacks/{provider_slug}` | 第三方回调（签名校验） |

### 9.3 统一调用请求/响应

请求（`POST /api/v1/capabilities/{key}/invoke`）：

```json
{
  "capability_version": "1.2.0",
  "input": {"prompt": "一只在雨中的柴犬", "size": "1024x1024", "reference_image": {"$file": "fil_...", "mime": "image/png"}},
  "options": {
    "mode": "async",
    "timeout_ms": 30000,
    "stream": false,
    "preferred_provider": "prv_openai",
    "max_cost_usd": 0.05,
    "idempotency_key": "agent-a-94f2",
    "callback_url": "https://agent.example/hook",
    "extensions": {"openai": {"quality": "hd"}}
  }
}
```

- `options.mode` 必须 ∈ 能力 `modes`（v2：复数集合）。省略时取 `modes[0]`。`modes` 不含该模式 → `400 UNSUPPORTED_MODE`，`details.supported` 列出实际支持的模式。
- **文件输入**：`input` 中用 `{"$file": "<file_id>", "mime": "..."}` 引用已上传文件（先经 `POST /api/v1/files` 上传）。执行器解析为二进制并按该 route 的 `request.encoding`（`multipart`/`base64`/URL）注入。`file_io.input` 未声明该 MIME → `400 UNSUPPORTED_PARAM`（`details.field` 指向该字段）。**文件输出**：结果中的二进制一律产出 `output_files[{file_id, mime, size}]`，不内联 base64（避免响应体膨胀与日志泄漏）。
- `extensions` 是提供商特有参数的唯一合法入口；未在 `extensions` 中声明且 `additionalProperties=false` 的字段 → `400 INPUT_SCHEMA_INVALID`。
- 不支持的模式/参数必须显式报错：`400 UNSUPPORTED_MODE`、`400 UNSUPPORTED_PARAM`；`options.preferred_provider` 不可用 → `409 PROVIDER_NOT_AVAILABLE`（列出可用 provider）。

同步响应：

```json
{
  "request_id": "req_01J9Z8W3",
  "status": "succeeded",
  "capability": {"key": "text.generate.gpt4o-mini", "version": "1.2.0"},
  "provider": {"id": "prv_openai", "name": "OpenAI"},
  "output": {"text": "……"},
  "output_files": [{"file_id": "fil_...", "mime": "image/png", "size": 184320}],
  "usage": {"unit": "tokens", "input": 812, "output": 96, "total": 908, "cost_usd": 0.0004, "source": "provider"},
  "timings": {"queue_ms": 4, "upstream_ms": 1180, "total_ms": 1233},
  "error": null
}
```

异步：`202 {"request_id":"req_...","task_id":"tsk_...","status":"queued"}`；任务查询返回 `{status: queued|running|succeeded|failed|canceling|canceled|status_unknown, progress:{"percent":42,"stage":"upstream_polling"}, result?, error?}`。

失败体复用 §9.1 的 `error` 结构并补充 `upstream_status`、`provider`。

### 9.4 SSE 事件

统一事件包（`data:` 一行 JSON）：

```json
{"id": 1287, "type": "assistant.delta", "at": "2026-10-18T05:41:25.072Z",
 "turn_id": "trn_...", "seq": 12, "payload": {"text": "正在读取接口文档…"}}
```

| 域 | 事件 `type` |
|---|---|
| Agent | `turn.queued`(queue_position)、`turn.started`、`assistant.delta`、`thinking.delta`、`tool.started`(name,args_summary)、`tool.progress`(percent,message)、`tool.finished`(ok,duration_ms,result_summary)、`todo.updated`、`turn.stopped`(stopped_steps,external_pending)、`turn.finished`(usage,cost)、`turn.failed`(code,hint)、`engine.notice` |
| 任务 | `task.updated`(status,progress)、`task.log`(line)、`task.finished` |
| 调用 | `call.delta`(chunk)、`call.usage`、`call.done`、`call.error` |

实现要点：服务端每 15s 发送 `: keep-alive` 注释；客户端断线带 `Last-Event-ID` 重连，服务端从 `sse_events` 表（保留 24h）重放缺失事件后转入实时；**重放不会触发任何写操作或重复执行**（幂等，靠事件表只读回放）。

### 9.5 内部接口（Python ↔ 网关，仅回环）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | `{ok, engine:{package,version,commit,schema_version}, tools:[], active_turns, uptime_s}` |
| POST | `/internal/turns` | `{session_id, turn_id, messages[], system_prompt, model, budget{max_turns,max_duration_ms}}` → `200 {accepted:true, ipc_stream:"/internal/turns/{turn_id}/events"}` |
| GET | `/internal/turns/{turn_id}/events` | SSE 事件流（§7.12 的 `AgentEvent`） |
| POST | `/internal/turns/{turn_id}/interrupt` | 停止当前轮次 → `StopReport` |
| POST | `/internal/turns/{turn_id}/steer` | 追加用户消息到当前轮（排队） |
| POST | `/internal/credentials/materialize` | `{credential_ref, purpose, ttl_s<=60}` → `{provider_id, base_url, kind, auth_type, header_name, secret}`；网关只在内存持有 |
| GET | `/internal/capabilities` | 平台能力目录（供 `sr_*` 工具） |
| POST | `/internal/audit` | 工具级审计（动作、对象、结果摘要） |
| POST | `/internal/admin/reload` | 引擎热切换（v2）：`{target_version}` → 网关内部起新 `engine-host` 子进程、排空旧子进程；返回 `{ok, engine_version, stage, detail}`。仅管理员触发（见 §12.4-4） |

认证：`X-Internal-Token: <AGENT_GATEWAY_SHARED_SECRET>`；网关只监听 `127.0.0.1`；平台侧对 `/internal/credentials/materialize` 做速率限制（60 次/分钟）与审计。

### 9.6 概览数据形状

```json
{
  "capabilities": {"published": 12, "draft": 3, "failing": 1},
  "providers": {"total": 4, "with_active_credential": 3},
  "success_rate_24h": 0.982,
  "calls_24h": 1843,
  "latency": {"p50_ms": 740, "p95_ms": 2310},
  "failed_tasks": 2,
  "queue_depth": 1,
  "engine": {"version": "0.2.34", "status": "available", "candidate": "0.2.37", "policy": "auto"},
  "attention": [
    {"code": "CREDENTIAL_EXPIRING", "severity": "warning", "title": "OpenAI 主密钥 5 天后过期", "target": "/providers/prv_openai/credentials/crd_1"},
    {"code": "CAPABILITY_FAILING", "severity": "error", "title": "video.generate.kling 连续 3 次失败", "target": "/capabilities/cap_9"},
    {"code": "ENGINE_CANDIDATE", "severity": "info", "title": "发现引擎候选版本 0.2.37（npm-only）", "target": "/settings/engine"}
  ]
}
```

`attention[].target` 是前端可跳转的前端路由，保证「每项信息能够跳转到对应详情」。

### 9.7 MCP 适配

- 传输：Streamable HTTP（`POST /api/v1/mcp`，`Accept: application/json, text/event-stream`）；`Mcp-Session-Id` 由服务端在 `initialize` 响应中下发并在后续请求校验。
- 版本协商：客户端 `protocolVersion` ∈ `["2025-06-18","2025-03-26"]`，服务端优先 `2025-06-18`；不匹配时返回 `initialize` 结果中服务端支持的版本并让客户端决定（遵循规范）。
- 方法：`initialize`、`notifications/initialized`、`ping`、`tools/list`、`tools/call`。
- 工具映射：每个已授权能力 → 一个工具，`name = "capability__" + key.replace(".","_")`（MCP 工具名限制 `[a-zA-Z0-9_-]`），`description` 来自能力描述，`inputSchema` 直接复用能力 `input_schema`，`title` 来自 `name`。另提供 `capabilities_list`、`skills_list`、`skills_read`、`task_status`、`task_cancel` 五个元工具。
- `tools/call` 结果：`content:[{type:"text",text:"<JSON 结果>"}]` + `structuredContent`（同 `/api/v1` 的输出）+ `isError`。异步能力返回 `{"task_id":..., "status":"queued"}` 并提示用 `task_status` 轮询。
- 一致性约束：MCP、HTTP、SKILL、CLI 共用 `services/capabilities.py::describe()` 生成的同一份能力描述；契约测试断言四处输出 `input_schema` 完全一致。

### 9.8 CLI（`@skillrouter/cli`）

```
skillrouter install   --base-url <url> [--token <srk_...> | --token-stdin]
                      [--agent aragon|claude|generic] [--transport http|mcp]
                      [--dir <path>] [--skill <name>] [--no-verify] [--force]
skillrouter verify    [--base-url <url>] [--token <token>]
skillrouter list      [--dir <path>]
skillrouter update    [--dir <path>]        # 重新拉取 SKILL 正文与目录快照（不重装）
skillrouter uninstall [--dir <path>] [--keep-config]
skillrouter doctor                          # 探测本机 Agent 环境、Node 版本、凭据存储后端
```

行为约束：

- 探测顺序（`detect.ts`）：`--dir` → `./.aragon/skills`（若存在 `.aragon`）→ `./.claude/skills` → `~/.aragon-agent/skills`（`ARAGON_HOME` 覆盖）→ 通用目录 `./skillrouter-skill`。**必须打印最终选择与理由**（「检测到 .claude/skills，已写入此处」），避免用户找不到文件。
- 写盘产物（v2 明确清单，与 §7.9 导出契约一一对应）：
  1. `SKILL.md` — frontmatter `name/description/version/license/allowed-tools:[bash]/activation:auto/metadata.skillrouter`；正文含 `invoke.mjs` 用法。
  2. `invoke.mjs` — 零依赖 Node ESM 助手（内容由后端 `bundle` 下发，不硬编码在 CLI 里，保证与平台契约同步）。
  3. `skillrouter.config.json` — `base_url`、`transport`、`token_ref`、`installed_at`、`cli_version`、`server_version`。**不含令牌**。
  4. `README.md` — 安装/验证/更新/卸载说明 + 已验证 Agent 矩阵。
  5. `--transport mcp` 且平台侧 MCP 通路已实测时，额外写客户端 MCP 配置片段（`mcp.json`）并在 SKILL.md 中移除 `bash` 依赖。
- 令牌存储：优先 OS 凭据库（`keytar` 可用时），否则写 `~/.skillrouter/credentials.json`（0600；Windows 用 `icacls` 限制为当前用户）；`skillrouter.config.json` 只存 `token_ref`。`--token` 会进入 shell 历史，因此同时提供 `--token-stdin`（推荐），并在两者都缺时交互式隐藏输入。
- `verify`：`GET /api/v1/health` → `GET /api/v1/whoami` → `GET /api/v1/capabilities` → **一次真实最小调用**（可选 `--probe-capability <key>`，默认只做只读三项），逐项打印 `✓/✗` 与失败原因（含 `request_id`）。`--no-verify` 只跳过最后一步真实调用，前两项永远执行。
- 退出码：`0` 成功、`1` 一般失败、`2` 参数错误、`3` 连通性失败、`4` 无写入权限、`5` 目标 Agent 未探测到（需 `--dir` 指定）。
- 页面上的安装命令由后端 `GET /api/v1/skills/{name}/bundle` 返回的 `install_command` 字段提供，保证「复制的命令真实可执行」；未发布 npm 时该字段为 `npx ./skillrouter-cli-0.1.0.tgz install --base-url ... --token-stdin`（`.tgz` 由平台以 `file_objects` 提供真实下载链接，不是占位符）。

---

## 10. 数据模型

> **本轮（迭代 2）修订说明**：本节部分取值已被 §A 覆盖（逐条对照见 §A.10）。实现或评审与本节正文冲突时**以 §A 为准**；本节保留原文，是为了让读者看到「改之前是什么样」。


### 10.1 表清单与关键列

> 所有表：`id TEXT PRIMARY KEY`（`uuid4().hex`，36 位带前缀如 `crd_...`）、`created_at/updated_at TEXT`（ISO8601 UTC）。外键均 `ON DELETE RESTRICT`（软删优先），除显式标注。

**system_state** — `key TEXT PK, value TEXT, updated_at`。种子键：`admin_initialized`、`schema_version`、`engine_pin`、`engine_previous_pin`、`install_mode`、`master_key_id`。

**settings** — `key TEXT PK, value JSON, scope TEXT(global), updated_by, updated_at`。含 `payload_policy`、`retention_days`、`fetch_allowlist`、`ssrf_deny`、`engine_policy`、`default_budget_usd`、`agent_model`。

**admin_users** — `id, username UNIQUE, password_hash, must_change_password INT, password_changed_at, last_login_at, disabled INT, created_at`。

**admin_sessions** — `id, user_id FK, token_hash UNIQUE, csrf_hash, scope TEXT(normal|password_change), created_at, last_seen_at, expires_at, revoked_at, ip, user_agent`。索引：`(expires_at)`、`(user_id, revoked_at)`。

**login_attempts** — `id, username, ip, at, success INT`。索引 `(username, at)`、`(ip, at)`。

**providers** — `id, slug UNIQUE, name, category TEXT(llm|image|video|speech|asr|generic), base_url, docs_url, tags JSON, environment, notes, status(active|disabled), deleted_at, created_at, updated_at`。

**credentials** — `id, provider_id FK, name, kind, auth_type, header_name, secret_ciphertext TEXT, secret_mask TEXT, secret_fingerprint TEXT(sha256 前 12 位，用于变更检测), meta JSON, meta_sealed TEXT NULL（敏感 meta 的信封加密列，AAD `record_type="credential_meta"`；永不出现在投影里，见 §A.3.2）, status(active|disabled|expired), expires_at, last_verified_at, last_verify_ok INT, rotation_of FK NULL, created_at, updated_at, deleted_at`。索引 `(provider_id, status)`。**约束**：`secret_ciphertext` 永不出现在任何 API 响应中。

**credential_verifications** — `id, credential_id FK, ok INT, status_code INT, latency_ms INT, error_code, detail_redacted, at`。索引 `(credential_id, at DESC)`。

**capabilities** — `id, key UNIQUE, name, kind, modes JSON(["sync","stream"]), file_io JSON, compatibility_group NULL, failover JSON, enabled INT, **draft_definition JSON（v2：可编辑草稿的唯一存放处；发布时冻结为 `capability_versions.definition`）, draft_revision INT**, published_version_id NULL, latest_version TEXT, status(draft|testing|published|deprecated), requires_agent_reasoning INT, tags JSON, created_at, updated_at, deleted_at`。**v2 移除单值 `provider_id`/`credential_id`**（改由 `capability_routes` 承载），这样「同一标准能力多个提供商」才在数据层成立。`modes`/`file_io`/`failover`/`tags` 是 `draft_definition` 的冗余投影列，仅供列表筛选与路由过滤使用，**以 `draft_definition` 为准**（保存时同事务写入）。

**capability_routes（v2 新增）** — `id, capability_id FK, provider_id FK, credential_id FK, route_key TEXT（能力内唯一，如 rt_primary）, priority INT, enabled INT, model_hint, request JSON, param_map JSON, response_map JSON, stream JSON, async JSON, limits JSON, errors JSON, timeout_ms INT, retry JSON, health JSON(最近统计快照), created_at, updated_at`。唯一索引 `(capability_id, route_key)`；索引 `(capability_id, enabled, priority)`、`(credential_id)`、`(provider_id)`。
> 注意：`capability_routes` 是 `capabilities.draft_definition.routes` 的**投影表**，存在的目的是「按凭证/提供商反查引用」「按优先级过滤候选」和承载运行时 `health` 统计；写入时同一事务内维护，不单独作为事实来源。发布时 route 快照冻结进 `capability_versions.definition.routes`；`health` 是可变运行时数据，不参与版本不可变性约束。

**capability_versions** — `id, capability_id FK, version TEXT, content_hash TEXT, definition JSON（§7.6 全量，含 `routes[]` 冻结快照）, status(draft|testing|published|deprecated), published_at, published_by, changelog, test_run_ids JSON, created_at`。唯一索引 `(capability_id, version)`、`(capability_id, content_hash)`。已发布行不可 UPDATE（由 `versions.py` 断言 + 触发器 `BEFORE UPDATE ON capability_versions WHEN OLD.status='published' → RAISE(ABORT)`）。

**capability_test_runs** — `id, capability_id, capability_version_id NULL, route_id TEXT, version TEXT, mode, ok INT, http_status, latency_ms, request_preview_redacted, response_preview_redacted, error_code, error_detail_redacted, credential_id, route JSON, at`。发布时必须存在 `ok=1` 且 `at > now-24h` 的记录（除非 `settings` 关闭该要求，默认开启），且 `modes` 中的每个模式至少有一条对应用例的记录。

**compatibility_groups** — `id, slug UNIQUE, kind, notes, confirmed_by, confirmed_at`；禁止自动创建（必须显式 `POST /capabilities/{id}/compatibility`）。

**skills** — `id, name UNIQUE, title, description, category, tags JSON, status(draft|published|deprecated), enabled INT DEFAULT 1（v2 新增，独立于 status）, **draft_definition JSON（v2：草稿态 `skill_md`/`frontmatter`/`execution_plan`/`dependencies`，发布时冻结进 `skill_versions`）**, published_version_id, latest_version, source_type(manual|git|url|file), source_ref, source_commit, source_url, deleted_at, created_at, updated_at`。

**skill_versions** — `id, skill_id FK, version, content_hash, skill_md TEXT, frontmatter JSON, execution_plan JSON, dependencies JSON([{capability_key,version_range}]), permissions JSON, examples JSON, status, published_at, created_at`。不可变约束同上。

**skill_files** — `id, skill_version_id FK, path, size, sha256, mime, executable INT`。路径唯一 `(skill_version_id, path)`。

**skill_imports** — `id, skill_id NULL, kind, source_url, source_ref, resolved_commit, preview_token, files JSON, warnings JSON, requires_sandbox_review INT, status(previewed|committed|rejected), error_code, error_detail, created_at`。

**skill_exports** — `id, skill_id, version, format, file_id, sha256, created_by, created_at`。

**agent_sessions** — `id, title, model_profile JSON, permission_profile JSON, engine_version, status(active|stopped|archived), turn_count, created_by, created_at, updated_at`。

**agent_turns** — `id, session_id FK, ordinal INT, user_text, attachments JSON, status(queued|running|succeeded|failed|stopped), queue_position INT NULL, stop_report JSON NULL, usage JSON, error_code, error_detail_redacted, started_at, finished_at`。索引 `(session_id, ordinal)`。

**agent_events** — `id INTEGER PK AUTOINCREMENT, turn_id FK, seq INT, type TEXT, payload JSON, at`。唯一索引 `(turn_id, seq)`；保留 24h（清理任务）。

**agent_tool_calls** — `id, turn_id FK, tool_name, args_redacted JSON, result_summary TEXT, ok INT, duration_ms, upstream_task_id NULL, at`。

**agent_artifacts** — `id, turn_id FK, kind(capability_draft|skill_draft|finding|test_result), payload JSON, status(pending_review|accepted|rejected), reviewed_by, reviewed_at, created_at`。

**agent_flows（v2 新增）** — `id, title, capability_id NULL, skill_id NULL, credential_id NULL, status(running|passed|failed|publishing|published|aborted), auto_publish INT, budget JSON({allowed_providers[], max_test_calls, max_cost_usd, window_hours}), spent JSON({calls, cost_usd}), current_step TEXT, created_by, created_at, updated_at, finished_at`。索引 `(status, updated_at)`。

**agent_flow_steps（v2 新增）** — `id, flow_id FK, step TEXT(parse|draft|validate|test|sample|check|publish), ordinal INT, status(pending|running|passed|failed|skipped), detail TEXT, evidence JSON({test_run_ids[], document_ids[], tool_call_ids[], note}), error_code, started_at, finished_at`。唯一索引 `(flow_id, step)`。这是「每个步骤都有可查看的状态、结果和失败原因，支持从失败步骤继续」的持久化载体。

**agent_approvals（v2 新增）** — `id, flow_id NULL, turn_id NULL, tool_call_id NULL, action TEXT, target TEXT, impact JSON, requested_scope JSON, status(pending|approved|rejected|expired), decided_by, decided_at, expires_at, note, created_at`。`sr_request_approval` 写入此表；`expires_at` 到期由 `expiry_scan` 置 `expired` 并视为拒绝（需求「超时 10 分钟自动拒绝」）。索引 `(status, expires_at)`。

**platform_tokens** — `id, agent_id FK, name, token_hash UNIQUE, prefix, scopes JSON([capability_key 或 tag]), allowed_modes JSON, expires_at, rpm INT, concurrency INT, daily_calls INT, budget_usd REAL, revoked_at, last_used_at, created_by, created_at`。

**external_agents** — `id, name UNIQUE, description, contact, status(active|revoked), last_seen_at, created_at`。

**rate_counters（v2 新增）** — `id, scope_type(token|agent|provider|capability|global), scope_id, window_start TEXT, window_seconds INT, calls INT, concurrent INT, updated_at`；唯一索引 `(scope_type, scope_id, window_start, window_seconds)`。**成本不在这张表**：费用累计在 `quota_counters.cost_usd`（日/月两行，见 §A.3.4）。`agent` 作用域由 §A 新增（需求「按接入 Agent 设置速率、并发和预算」）。取代 v1 的 `token_rate_counters`（后者只是 `scope_type='token'` 的特例）。

**quota_limits** — `id, scope_type(global|provider|capability|token), scope_id, rpm, concurrency, daily_calls, monthly_budget_usd, updated_at`；唯一索引 `(scope_type, scope_id)`。

**quota_counters** — `id, scope_type, scope_id, period(YYYY-MM-DD|YYYY-MM), calls INT, cost_usd REAL`；唯一索引 `(scope_type, scope_id, period)`。

**call_logs** — `id, request_id UNIQUE, caller_type(admin|token|agent|skill), caller_id, capability_key, capability_version_id, provider_id, credential_id, mode(sync|async|stream), status(success|failed|canceled|status_unknown；写入值由 `LOGGED_STATUS` 单点决定，见 §A.3.1), needs_reconcile INT DEFAULT 0（读超时等「上游可能已受理」的调用置 1，供概览待办与 /calls 筛选）, http_status, error_code, latency_ms, queue_ms, upstream_ms, retries INT, idempotency_key NULL, request_fingerprint, payload_policy, request_preview, response_preview, agent_session_id NULL, agent_turn_id NULL, task_id NULL, created_at`。索引：`(created_at DESC)`、`(caller_id, created_at DESC)`、`(capability_key, created_at DESC)`、`(status)`、`(task_id)`。

**route_decisions** — `id, call_id FK, candidates JSON, chosen_route_id TEXT, chosen_capability_version_id, chosen_provider_id, reason TEXT, dropped JSON([{candidate, reason}]), failover_from NULL, created_at`（v2 增加 `chosen_route_id`，因为候选单位已从「能力版本」细化为「route」）。

**usage_records** — `id, call_id FK, provider_id, capability_key, token_id NULL, unit(tokens|images|seconds|chars|bytes), qty REAL, cost_usd REAL, source(provider|estimated|unknown|probe), at`。索引 `(provider_id, at)`、`(capability_key, at)`。

**price_book** — `id, provider_id, capability_key NULL, unit, unit_price_usd, currency, effective_from, updated_at`；唯一索引 `(provider_id, capability_key, unit, effective_from)`。

**tasks** — `id, request_id, capability_key, capability_version_id, mode, status(queued|running|succeeded|failed|canceling|canceled|status_unknown), progress_percent INT, progress_stage, upstream_task_id, upstream_payload_redacted JSON, attempts INT, max_attempts INT, failure_class NULL, error_code, error_detail_redacted, cancel_capability(none|remote|local_only), cancel_requested_at TEXT NULL（`status='canceling'` ⇔ 本列非空，不变式 I2）, result JSON, output_files JSON, token_id NULL, agent_session_id NULL, resume_policy(check|resubmit), created_at, updated_at, finished_at`。索引 `(status, updated_at)`、`(upstream_task_id)`。

**task_queue** — 见 §7.8。**幂等键表** `idempotency_keys`（v2 修正）：`scope TEXT(token:<id>|admin:<id>), idem_key TEXT, fingerprint TEXT, request_id TEXT, capability_key, state(in_flight|done, 默认 done 供历史行), created_at, expires_at`；唯一索引 **`(scope, idem_key)`**（v1 的全局 `(idem_key)` 会让不同令牌互相命中对方的请求，既是正确性问题也是信息泄漏）；索引 `(expires_at)`。

**file_objects** — 见 §7.11。**callback_events**：`id, provider_slug, event_id, signature_ok INT, payload_redacted JSON, at`；唯一索引 `(provider_slug, event_id)`。

**audit_logs** — 见 §7.10，仅追加；索引 `(at DESC)`、`(action, at DESC)`、`(object_type, object_id)`。

**worker_heartbeats** — `worker_id TEXT PK, started_at, last_seen_at, leases_held INT, engine_version`。

**master_key_rotations** — `id, from_key_id, to_key_id, rows_total, rows_done, status(running|done|failed), started_at, finished_at, error`.

**engine_deployments** — `id, version TEXT, commit_sha, provenance(tagged|npm-only|branch-commit), source_url, integrity TEXT, adapter_version, status(active|previous|rolled_back|failed), deployed_at, health JSON`。

**engine_candidates** — `id, version, commit_sha NULL, provenance, published_at, channel, notes, detected_at, verification_status(pending|passed|blocked|failed——`skipped` 已删除，历史行按 `blocked` 读取), verification_report JSON`；唯一索引 `(version, provenance)`.

**engine_upgrade_runs** — `id, from_version, to_version, mode(verify_only|deploy), policy(auto|notify|manual), started_at, finished_at, result(success|failed|aborted；表示「这次运行怎么结束的」), verify_status(passed|blocked|failed；候选验证结论，与 `result` 语义分离), blocked_reasons JSON（阻断/待验证检查项，UI 的阻断原因直接读它）, steps JSON([{name,status,detail,duration_ms}]), error, backup_file_id NULL, rollback_of NULL`.

**engine_capability_status** — `id, capability_key UNIQUE, state(available|needs_adapter|permission_limited|unsupported), detail, since_version, updated_at`（§12.2 的「已可用/待适配/受权限限制」持久化）。

**sse_events** — `sse_events(id INTEGER PK AUTOINCREMENT, stream_key TEXT, type, payload JSON, at)`；索引 `(stream_key, id)`；保留 24h。**v2 明确职责划分（P04）**：Agent 通道的权威事件源是 `agent_events`，`sse_events` 只承载**非 Agent** 通道（任务 `task.*`、调用 `call.*`、管理端通知 `engine.notice`）。两表**不得双写同一事件**——重放时 Agent 流读 `agent_events`，其它流读 `sse_events`，避免写入放大与顺序不一致。

### 10.2 不变式与触发器

1. 已发布版本行不可变：`capability_versions`、`skill_versions` 上 `BEFORE UPDATE` 触发器在 `OLD.status='published'` 时 `RAISE(ABORT, 'published_version_immutable')`；修改 = 新建行。
2. 能力发布必须指向存在且 `status='published'` 的版本行：应用层校验 + `FOREIGN KEY` 指向 `capability_versions.id`。
3. `credentials.secret_ciphertext` 非空且格式 `v1.<keyid>.<nonce>.<ct>`；写入前由 `secretbox.seal()` 保证。
4. 任务状态机合法迁移：`queued→(running|canceling|canceled|failed)`（**`queued→canceling` 由 §A.3.6 新增**）；`running→(succeeded|failed|canceling|canceled|status_unknown)`；`status_unknown→(running|failed|succeeded)`；`canceling→(canceled|failed|status_unknown)`。非法迁移由 `tasks.transition()` 抛 `INVALID_STATE_TRANSITION`。
5. 删除凭证前必须 `ref_count=0`，否则 `409 CREDENTIAL_IN_USE` 并返回影响面（`ref_count` 统计 `capability_routes.credential_id` 的引用数，v2 修正引用来源）。
6. 每个 `call_logs` 行必须有对应 `request_id`；`route_decisions.call_id` 可空（拒绝前置失败时）。
7. **能力侧影响面（v2 新增）**：删除能力必须同时满足「无 SKILL 依赖 && 无在途任务 && 无令牌作用域命中」，否则 `409 CAPABILITY_IN_USE` 并返回 `capabilities.impact()`；已发布能力禁止删除，只能 `deprecate`（与 §9.2 一致）。停用（`enabled=0`）只停止新路由，历史记录与版本仍可查。
8. **路由完整性（v2 新增）**：`enabled=1` 且 `status=published` 的能力必须至少有一条 `capability_routes.enabled=1` 且其凭证 `status=active`；不满足时能力自动降级为「不可调用」并在概览产生 `CAPABILITY_NO_ROUTE` 待办（**不静默失败**）。
9. **SKILL 分发一致性（v2 新增）**：`/api/v1/skills*` 只返回 `enabled=1 AND status='published'`；任一 SKILL 依赖的能力版本被 `deprecate` 时，该 SKILL 必须在概览产生 `SKILL_DEPENDENCY_DEPRECATED` 待办；依赖缺失时执行计划返回 `409 SKILL_DEPENDENCY_MISSING`（列出缺失项），而不是执行部分步骤。
10. **审批超时（v2 新增）**：`agent_approvals` 中 `status='pending'` 且 `expires_at < now` 的记录，由 `expiry_scan` 置为 `expired`，任何工具调用不得把它当作隐式批准。

### 10.3 迁移与备份

- Alembic revision 列表：`0001_init`（全量表 + 触发器 + 索引 + 种子）、`0002_engine_upgrade`（v2：**首次交付时允许并入 `0001_init`**，因为此时不存在已部署实例；一旦有任何部署，任何 schema 变更都必须新增 revision 且 `downgrade` 可用）。`env.py` 使用 `render_as_batch=True`，`PRAGMA user_version` 与 `alembic_version` 双向校验（不一致即启动失败）。
- 备份（v2 改为纯 Python，不依赖 `sqlite3`/`tar` 二进制）：`scripts/backup.py` 用 `sqlite3.Connection.backup()` 生成一致性快照 `backup/db-<ts>.sqlite`，用 `zipfile` 打包 `var/files`（保持相对路径与 sha256 清单），并写 `manifest.json`（`schema_version`、`migration_revision`、`engine_pin`、`master_key_fingerprint`、文件数、每个包的 sha256、创建时间、创建版本）。
- 恢复：停写（进入维护模式，管理写接口返回 `503 MAINTENANCE`）→ 校验 manifest 与当前主密钥指纹（不一致立即拒绝）→ 替换 DB 与文件 → `alembic upgrade head`（允许从更旧 schema 升级）→ 若备份 schema **更新**于当前程序则拒绝（禁止用新库跑旧代码）→ 重启并跑 `verify_restore()`（计数一致性：能力/凭证/SKILL/文件行数与 manifest 对比，并随机抽查 1 个凭证能否解密）。

## 11. 安全设计

> **本轮（迭代 2）修订说明**：本节部分取值已被 §A 覆盖（逐条对照见 §A.10）。实现或评审与本节正文冲突时**以 §A 为准**；本节保留原文，是为了让读者看到「改之前是什么样」。


### 11.1 威胁模型（本阶段必须覆盖）

| 威胁 | 攻击面 | 对策（实现位置） |
|---|---|---|
| 未授权访问管理接口 | 前端路由守卫被绕过 | 所有 `/api/admin/*` 走 `require_admin` 依赖；无 cookie 即 401（`deps.py`） |
| 会话劫持 / CSRF | 浏览器 | `HttpOnly+Secure+SameSite=Lax` cookie；写请求双提交 CSRF；会话表可即时吊销；改密后踢全部会话 |
| 暴力破解登录 | `/auth/login` | 固定窗口限流（5 次/5 分钟/IP+用户名），锁定 5 分钟，恒定时间密码比对（`ratelimit.py`） |
| 数据库被读走 → 密钥泄露 | 备份文件、误导出 DB | 上游密钥 AES-256-GCM 信封加密；主密钥只在 env 或 0600 文件；`secret_mask` 用于展示；导出接口不含密文 |
| 提示注入（外部网页/SKILL 内容） | Agent 上下文 | 外部内容包裹为 `<untrusted-data>` 数据块 + 显式系统约束「资料不得改变指令」；`instruction_scanner` 告警；导入 SKILL 默认禁用 `activation: always` |
| Agent 越权调用工具 | 引擎升级新增工具 | 网关只注册白名单 `sr_*`（`policy.ts`），上游内置工具（`bash`/`write_file`/`web` 等）**从不注册**；升级不改变白名单，新增工具需管理员在设置页显式开启 |
| SSRF / 内网探测 | 抓取网页、API 测试 URL、回调 | `fetch_guard`：DNS 解析后校验每个 A/AAAA 是否命中 `ssrf_deny`；禁止重定向跨域（≤3 次，逐跳校验）；禁止 `file://`/`ftp://`；云元数据地址硬编码拒绝；白名单由管理员显式配置 |
| 密钥出现在日志/聊天/导出包 | 日志、SSE、SKILL 导出、npm 包 | `redact()` 中间件对所有日志字段过滤（`sk-`/`srk_`/`Bearer`/`api[_-]?key` 模式）；SSE 事件只传摘要；导出包构建后跑 `secret_scan`（正则扫描最终 zip 与 npm 包 tarball），命中即失败并审计 |
| 令牌滥用 / 超量调用 | 外部 Agent | 令牌哈希存储、仅创建时展示一次、作用域限制、rpm/并发/日调用/预算四维限制、即时撤销（`revoked_at`）、`last_used_at` |
| 幂等缺失导致重复扣费 | 外部 Agent 重试 | `Idempotency-Key` + `request_fingerprint`；超时无法判定时 `status_unknown` + 先查询后决定（不盲目重投、不盲目切换提供商） |
| 路径穿越 / 恶意 SKILL 包 | 导入 | 路径白名单校验、拒绝符号链接与设备名、条目/大小/压缩比上限、脚本不执行 |
| 回调伪造 | 第三方回调 | HMAC 签名 + 时间戳窗口 + `event_id` 去重 + 来源 IP 校验 |

### 11.2 凭证隔离的落地检查点

1. 模型上下文：网关 `Agent` 的 `systemPrompt`/消息只含 `credential_ref`（如 `crd_7f3a`），不含任何密钥；`getApiKey` 返回的密钥只进入 HTTP 请求头。
2. 平台工具返回值：`sr_test_capability` 只回传 `{http_status, latency_ms, response_preview(≤2KB, 脱敏), error_code}`。
3. SSE/DB：日志与事件字段经 `redact()`；`call_logs.request_preview` 默认脱敏。
4. 导出包：`skill_md` / `skillrouter.config.json` 中只有 `base_url` 与令牌引用；CI 有 `test_skill_export_has_no_secret` 断言。
5. 崩溃转储/异常栈：`errors.py` 捕获异常时剔除 `request.headers`，只保留白名单头。

### 11.3 外部内容处理规则（Agent 视角）

- 抓取上限：单资源 ≤ 5MB、总资料 ≤ 20MB、超时 20s、最多 5 个 URL/轮。超限时明确告知「已截断，剩余 N 段未读取」。
- 文档解析：PDF 只取文本层（不 OCR），超出页数上限（50 页）时截断并声明。
- 事实与推测分离：Agent 工具 `sr_record_finding` 必须带 `confidence ∈ {verified, inferred, unknown}`；`inferred/unknown` 的内容不得进入发布能力的必填映射（发布校验会检查字段来源证据）。
- 越权请求：当流程需要「删除数据 / 付款 / 对外发消息 / 访问非白名单地址」时，Agent 必须调 `sr_request_approval`，UI 展示具体请求与影响面并等待管理员确认（超时 10 分钟自动拒绝）。

---

## 12. AragonAgent 集成与升级

> **本轮（迭代 2）修订说明**：本节部分取值已被 §A 覆盖（逐条对照见 §A.10）。实现或评审与本节正文冲突时**以 §A 为准**；本节保留原文，是为了让读者看到「改之前是什么样」。


### 12.1 接入版本与运行环境（实测记录）

| 项 | 值 |
|---|---|
| 仓库 | `https://github.com/xdrshjr/AragonAgent` |
| 许可证 | MIT（`LICENSE`，1102 字节） |
| 语言/运行时 | TypeScript（ESM-only）、Node ≥ 18；实测 Node v22.18.0 |
| 引擎包 | `@aragon-agent/core`，本仓库 pin `0.2.34`（git `main@0a119a6`，tag `core-v0.2.34`） |
| CLI 包 | `@aragon-agent/cli`，pin `0.6.22`（tag `cli-v0.6.22`），仅用于一致性校验 |
| 桌面端 | `desktop/`（Electron + Next.js），本平台不使用 |
| 机器契约 | `aragon exec` NDJSON，`schemaVersion: 1`；事件类型 `system/user/text_delta/thinking/assistant/tool_call/tool_result/todo/team/fast_review/retry/error/result`；只增不改、未识别事件必须忽略 |
| SDK 契约 | 公开导出 86 个（`packages/core/src/__tests__/public-api.test.ts` 冻结）；`Agent`/`AragonAgent` 同一类；`subscribe()` 事件、`abort()` 停止、无持久化 |
| 模型适配 | Anthropic / OpenAI / Google + 任意 OpenAI 兼容 `baseUrl`（DeepSeek、Qwen、Ollama 网关可用） |
| 已知偏差（实测 2026-10-18） | npm `dist-tags`：`@aragon-agent/cli` `latest=0.6.25`、`@aragon-agent/core` `latest=0.2.37`；而 git `main@0a119a6` 的 tag 只到 `cli-v0.6.22`/`core-v0.2.34`。→ 需要双通道检测与 `provenance` 分级（见 §12.4） |
| 上游自述边界 | 无文件系统沙箱；Windows 上仅 Ctrl-C 触发 `result`，`taskkill` 直接结束进程；`exec` 会话与 TUI `/save` 目录共享 |

> 详细采证（命令、输出、文件路径）见 `docs/plans/skill-router/aragonagent-upstream.md`。

### 12.2 能力边界与状态展示（已可用 / 待适配 / 受权限限制）

`engine_capability_status` 表持久化，设置页与概览读取；**不得因为引擎版本升级就宣称全部新能力可用**。

| 上游能力 | 平台状态 | 说明 |
|---|---|---|
| 多轮对话与流式输出（`assistant.delta`） | 已可用 | 网关 `subscribe()` → SSE `assistant.delta` |
| 工具调用与进度（`tool_call/tool_result`） | 已可用 | 映射为 `tool.started/tool.finished`，耗时来自上游 `durationMs` |
| 停止（`abort()`） | 已可用 | 停止报告列出已停止步骤与外部待确认操作 |
| TODO 规划（`todo`） | 已可用 | 映射 `todo.updated`，浮窗面板展示 |
| 子代理团队（`team`） | 待适配 | 上游事件可读，但平台暂不展示团队视图；事件被忽略并记录 `engine.notice` |
| 快速模型评审（`fast_review`） | 待适配 | 需要单独的模型配置与费用归属，本阶段不开启 |
| 上下文自动压缩（compaction） | 已可用（默认开启） | 由上游 `contextManager` 驱动；平台记录压缩事件 |
| 计划模式（plan mode） | 受权限限制 | 仅在「接入流程」中以只读模式启用；管理员可在设置中关闭 |
| 文件读取/写入/`bash` 内置工具 | 受权限限制（**禁用**） | 网关注册工具集不含它们；设置页标注「需管理员配置且本平台不支持」 |
| `skill_install` / `skill_create` 内置工具 | 受权限限制 | 平台用自身 SKILL 导入管线替代，凭证与文件不外露 |
| 沙箱 CodeAct（`isolated-vm`） | 不支持 | 可选依赖，本阶段不安装；状态标注 `unsupported` |
| 会话持久化 | 待适配（平台自实现） | 上游无持久化，平台自建 `agent_sessions/agent_turns` 并重放 |

状态机：`available` / `needs_adapter` / `permission_limited` / `unsupported`，每次升级验证后由 `verify.py` 依据「上游导出清单差异 + 工具清单差异 + 事件字段差异」自动重算并写 `since_version`。

### 12.3 平台工具清单（Agent 唯一能力面）

全部通过 `defineTool` 注册，参数 JSON Schema `additionalProperties:false`，每工具 20s 超时（`sr_test_capability` 60s），每次调用写 `agent_tool_calls` 与审计。

| 工具名 | 参数（要点） | 返回 | 权限 |
|---|---|---|---|
| `sr_capabilities_list` | `{query?, kind?, mode?}` | 能力列表（key/name/kind/mode/status） | 只读 |
| `sr_capability_get` | `{key}` | 能力详情（schema/limits/示例） | 只读 |
| `sr_fetch_source` | `{url, purpose}` | 截断后的文本 + `truncated` + `bytes`；受 `fetch_guard` | 网络（白名单） |
| `sr_parse_document` | `{file_id}` | 结构化摘要（标题/段落/示例片段） | 只读 |
| `sr_propose_capability_draft` | `{key,name,kind,modes[],file_io,input_schema,output_schema,examples[],routes[{provider_id,credential_id,priority,request,param_map,response_map,limits,errors,stream?,async?}],evidence[]}` | `{draft_id, issues[]}` | 写草稿（不发布） |
| `sr_update_capability_draft` | `{draft_id, patch}` | 同上 | 写草稿 |
| `sr_test_capability` | `{draft_id, input, route_id?, save_payload?}` | `{ok,http_status,latency_ms,response_preview,error_code,used_route}`（脱敏） | 受限执行（预算内） |
| `sr_sample_invoke` | `{capability_key, version?, input}` | 最小样例结果（脱敏） | 受限执行 |
| `sr_create_skill_draft` | `{name,description,instructions,execution_plan,permissions}` | `{skill_id, issues[]}` | 写草稿 |
| `sr_validate_skill` | `{skill_id}` | `{ok, issues[]}` | 只读 |
| `sr_record_finding` | `{kind, confidence, text, targets[]}` | `{finding_id}` | 写备注 |
| `sr_request_approval` | `{action, target, impact, requested_scope}` | `{approval_id, decision}` | 需要人工 |

**预算与自动执行范围**：`POST /api/admin/agent/config` 配置 `allowed_providers[]`、`max_test_calls_per_flow`（默认 10）、`max_cost_usd_per_flow`（默认 1.0）、`allowed_window`（默认 24h）。网关在执行 `sr_test_capability`/`sr_sample_invoke` 前向后端预检；超限 → 工具返回 `budget_exceeded` 并触发 `sr_request_approval`（不静默继续）。

### 12.4 升级流水线（检测 → 隔离构建 → 验证 → 切换 → 回滚）

**状态机**（`engine_upgrade_runs.steps` 顺序记录，每步 `status: pending|running|passed|failed|skipped`）：

```
detected → verify_only? ─┬─► built ─► verified ─► deployed ─► healthy (end)
                         └─► verified (未部署)
       any step failed ─► failed → (自动) rollback_if_deployed → previous_active
```

**1) 检测（`detect.py`，每 24h 或手动）**

- 通道 A（git，权威来源）：`GET https://api.github.com/repos/xdrshjr/AragonAgent/releases`（稳定通道取 `core-v*`/`cli-v*` tag）；失败回退 `git ls-remote --tags <repo>`（实测可用，输出 `core-v0.2.34 → 0a119a64…`）。记录 `version`、`tag_object`、`commit_sha`（tag 解引用）、`published_at`。
- 通道 B（npm，可部署工件）：`GET https://registry.npmjs.org/@aragon-agent/core` 取 `dist-tags.latest` 与对应 `dist.integrity`（sha512）；同样查 `@aragon-agent/cli`。
- 合并与 `provenance` 判定：
  - 两通道都有且 commit 一致 → `tagged`（可部署，最高可信）。
  - 只有 npm，无对应 git tag → `npm-only`（候选可用，但必须通过隔离构建 + 完整性校验，UI 标注「上游无 tag」）。
  - 配置 `ENGINE_CHANNEL=branch:<name>` 且无正式版本 → `branch-commit`（锁定 `git ls-remote` 的 commit，UI 标注分支名）。
- 平台默认 `ENGINE_CHANNEL=stable`；`SKILLROUTER_EGRESS_POLICY=deny`（别名 `SKILLROUTER_TELEMETRY_EGRESS`）时可关闭出站（此时仅支持手动上传 tarball 升级，UI 明示该限制）。

**2) 隔离构建（`build.py`）**

- 目录 `var/engine-builds/<version>-<provenance>/`；`tagged`/`branch-commit` → `git clone --filter=blob:none --depth 1 --branch <ref>`；`npm-only` → `npm pack @aragon-agent/core@<version>` 并校验 `integrity`（sha512）。
- **两阶段安装（v2 修正）**：v1 的 `npm ci --omit=dev --ignore-scripts` 后接 `npm run build` 必然失败——`typescript` 与构建脚本都在 `devDependencies` 里，`--omit=dev` 之后无编译器可用。正确顺序：
  1. **构建阶段**：`npm ci --ignore-scripts`（含 dev 依赖；`--ignore-scripts` 仍禁止 postinstall 执行任意代码）+ `npm run build -w packages/core`。
  2. **裁剪阶段**：在独立目录用 `npm ci --omit=dev --ignore-scripts` 生成仅运行时的 `node_modules`，与 `dist/` 一起打包为可部署产物（这样运行时镜像/目录不含编译器）。
- 产物：`dist/` + 裁剪后的 `node_modules/` + `build-manifest.json{version, commit, integrity, node, deps_lock_sha256, pruned_lock_sha256, built_at, adapter_version}`。
- 构建在子进程中运行，超时 15 分钟；stdout/stderr 脱敏后写入 `engine_upgrade_runs.steps[built].detail`（保留末尾 200 行）。

**3) 验证（`verify.py` + `agents/aragon-gateway/test/conformance.test.ts`）**

| 检查项 | 通过条件 |
|---|---|
| 导出清单兼容 | 上游导出符号集合与 `EXPECTED` 对比：新增符号 → `needs_adapter_review`（不阻断，但必须记录）；**删除/改名** → 直接 `failed` |
| 事件字段兼容 | `schemaVersion` 未变；`AgentEvent` 成员集合无删除；新增成员记录为待适配 |
| 工具清单 | `HOST_TOOL_NAMES` 若新增工具，确认平台白名单未自动包含（`policy.test.ts` 断言） |
| 适配层编译 | 网关 `tsc --noEmit` + `vitest run` 全绿 |
| 冒烟调用 | 使用配置的廉价模型完成 1 次 `prompt("reply with OK")`，断言收到 `assistant` 与完成事件，耗时 < 60s（无可用模型时标记 `skipped` 并**不阻断**，但 UI 明示「未做真实推理验证」） |
| CLI 契约（可选） | `aragon info --json` 的 `schemaVersion===1`、`features` 包含 `exec,sessions,permissions,budgets`；`aragon exec --output-format stream-json "hi"` 首行 `type=system` |
| 数据兼容 | 会话目录结构未变（`sessions/*.json` 可读）；若结构变化 → 标记 `needs_migration` 并禁止自动部署 |

报告落 `engine_candidates.verification_report`（每项 `{check, status, detail}`）。

**4) 部署切换（`switch.py`）**

1. 备份：`engine_upgrade_runs.backup_file_id` = 当前 DB + `var/aragon-home` + 当前网关 `package-lock.json` 的备份产物（§7.4 的纯 Python 备份实现）。
2. 写 `engine_deployments`（新版本 `status=pending`），旧版本暂保持 `active`；更新 `var/engine-runtime/current.json` 指针文件（Windows 上符号链接需要额外权限，因此**指针文件是唯一实现**，不依赖 symlink/junction）。
3. **切换通过网关自愈完成（v2 修正）**：调用 `POST http://gateway/internal/admin/reload {target_version}`，由网关的 `engine-host.ts`（§7.12 第 6 条）起新引擎子进程 → 排空旧子进程 → 返回实际运行版本。API 进程**不重启容器、不需要 Docker socket**（v1 的 `POST /internal/admin/restart` 在容器内不可实现，且挂 Docker socket 会把平台域权限扩大到宿主机）。
4. 健康检查：`GET /healthz` 连续 3 次通过（间隔 2s）且 `engine.version === 期望版本`；随后用当前 Agent 配置跑一次最小 `prompt`，断言收到完整事件流（无可用模型时标记 `skipped` 并在 UI 明示「未做真实推理验证」，但仍视为部署成功——与 §12.4-3 的判定保持一致）。
5. 成功：`engine_deployments` 新版本置 `active`、旧版本置 `previous`；更新 `system_state.engine_pin` / `engine_previous_pin`；`engine_upgrade_runs.result=success`；写审计 `engine.switched`；刷新 `engine_capability_status`。
6. 失败：立即 `rollback.py`——再调一次 `reload {previous_version}`（旧子进程若已被关闭则由新子进程按旧 pin 重建）→ 健康检查 → 新版本 `status=failed`、旧版本恢复 `active` → `result=failed`，写审计 `engine.rolled_back`，并在概览生成高优待办。
7. 无论成功失败，`var/aragon-home/engines/<version>/` 按版本隔离保留，供回滚与再次升级复用。

**5) 数据与回滚一致性**

- 引擎本身无数据库；平台侧唯一相关状态是 Agent 会话轨迹（平台自有表）与 `var/aragon-home/engines/<version>/`（上游会话缓存）。
- 回滚策略：平台会话轨迹与引擎版本无关（重放即可），上游会话缓存按版本分目录，**不跨版本复用**；若发生上游会话格式变化，回滚后旧版本读取自己目录，新版本目录保留待下次升级复用 → 无需数据迁移。
- 若未来上游引入需要迁移的状态，`verify.py` 的 `needs_migration` 会阻断自动部署，并要求 `engine_upgrade_runs` 附带 `migration_script` + `restore_script`（成对提供，缺一不可部署）。

**6) 崩溃与并发保护**

- 同一时间最多一个 `engine_upgrade_runs` 处于 `running`（唯一部分索引 `WHERE result IS NULL`）。
- 升级期间平台拒绝新的升级请求（`409 UPGRADE_IN_PROGRESS`）；**普通能力调用完全不受影响**（它们不经过网关）。
- 升级作业由 worker 的 `engine_upgrade` handler 执行，租约 60 分钟，崩溃后不会自动重复部署（`result=failed` 且需人工确认）。

---

## 13. 前端设计

> **本轮（迭代 2）修订说明**：本节部分取值已被 §A 覆盖（逐条对照见 §A.10）。实现或评审与本节正文冲突时**以 §A 为准**；本节保留原文，是为了让读者看到「改之前是什么样」。


### 13.1 信息架构与导航

左侧固定导航（宽度 240px，可折叠为 64px 图标栏），顺序：**概览 / 提供商 / 能力 / SKILL / Agent / 接入与令牌 / 调用记录 / 任务 / 设置**。每项显示计数徽标（如待发布草稿数、失败任务数）。顶栏：环境徽标（`production` 红点 / `staging`）、引擎状态点（`available`/`degraded`/`unavailable`，点击跳 `/settings/engine`）、全局搜索（`⌘K`/`Ctrl+K`）、管理员菜单（改密/退出）。

### 13.2 页面清单与关键状态

| 页面 | 核心区块 | 必须实现的真实状态 |
|---|---|---|
| `/login` | 表单、限流提示、首次使用引导 | 空/加载/校验失败/限流(带倒计时)/服务不可用 |
| `/change-password` | 当前密码、新密码（强度提示）、确认 | 成功跳转；失败就地提示；不可跳过（其它路由重定向回此页） |
| `/`（概览） | 指标卡（能力/成功率/调用量/P95/失败任务/队列）、接入状态流水线、待办列表、最近调用、引擎卡 | 首次使用（空数据引导三步）、加载骨架、部分失败（单卡错误不拖垮整页） |
| `/providers` | 表格（名称/分类/环境/凭证数/能力数/状态）、创建抽屉 | 空、搜索无结果、分页、批量筛选、删除确认（含影响面） |
| `/providers/[id]` | 头部信息、凭证列表、能力列表、关联图（凭证→能力→SKILL） | 无凭证提示「先添加凭证」；有关联时删除按钮禁用并说明原因 |
| `/capabilities` | 表格（key/名称/类型/模式/状态/发布版本/成功率） | 状态徽标四态区分（草稿/测试中/已发布/已停用） |
| `/capabilities/new` | 三选一入口：手工配置 / 从 OpenAPI 导入 / 交给 Agent | 每步校验、可返回上一步、离开有未保存提示 |
| `/capabilities/[id]` | 左：结构化编辑器 Tab（请求/路由/映射/Schema/限制/错误映射/示例）；右：示例与测试面板 | 「配置已保存」≠「测试通过」≠「已发布」三态独立显示；测试中禁用发布；密码类字段永不回显；**路由 Tab 明确展示每条 route 的提供商/凭证/优先级/启用状态与「谁是当前主力」**，并允许新增第二条 route 用于故障切换（v2） |
| `/capabilities/[id]/versions` | 版本时间线、diff（并排）、回退按钮 | 回退需二次确认（说明影响的外部 Agent） |
| `/skills` | 表格（名称/分类/版本/依赖数/状态/**启用开关**）、导入入口 | 导入中进度、失败原因可操作（如「缺少 SKILL.md，需在根目录提供」）；**「已发布」与「已启用」是两个独立列与独立操作**，停用不改变版本（v2） |
| `/skills/[id]` | 说明、适用范围、输入输出、依赖能力（版本区间）、执行计划树、权限要求、调用测试、**导出/预览 SKILL.md** | 依赖缺失时发布按钮禁用并列出缺失项；`agent_reasoning` 步骤用徽标标注；停用后顶部显示「当前对外不可见」横幅（v2） |
| `/agent` | 全屏工作台：左会话列表，中对话与时间线，右「接入流程步骤」+ 待审批卡片 | 步骤级状态（解析/生成/校验/测试/样例/检查/发布）、失败可从该步继续、**每步可展开证据（测试记录/文档/工具调用）**、审批请求显示具体影响并支持批准/拒绝（v2） |
| `/access` | Agent 列表、令牌表（前缀/作用域/限流/预算/最近使用）、创建令牌弹窗 | 令牌仅创建时完整展示 + 一次性复制 + 「我已保存」确认；撤销即时生效并显示影响 |
| `/calls` | 筛选器（时间/状态/能力/提供商/调用方/最小耗时）、表格、详情抽屉 | 空、无匹配、详情内部分区（路由决策/重试/错误映射/耗时分解） |
| `/tasks` | 状态分组（进行中/待确认/已完成/失败）、进度条、取消与对账按钮 | `status_unknown` 专门区块解释「可能仍在远端执行」 |
| `/settings` | 分区：通用/安全/预算与配额/外部抓取白名单/数据保留/审计/备份 | 保存成功 toast 带 `request_id`；危险区独立 |
| `/settings/engine` | 当前引擎版本与 pin、候选版本、验证结果明细、升级策略、升级记录、能力状态表 | 「已可用/待适配/受权限限制」三色标注；升级中实时进度；失败展示失败检查项 |

### 13.3 全局浮窗 Agent

- 入口：右下固定按钮（56×56，距边 24px），`aria-label="打开 Agent"`，不覆盖表格操作区与分页控件（内容区右侧预留 24px 安全边距）。
- 面板：420×640（≥1280px）/ 全宽抽屉（<1024px）；可拖拽调整尺寸（记忆到 `localStorage`）；`Esc` 关闭；焦点陷阱 + `aria-modal`。
- 会话保持：面板状态挂在 `AgentProvider`（`app/(app)/layout.tsx`），**跨页面切换不销毁**；SSE 由 `useAgentSession` 持有，路由变化只重渲染不改订阅。
- 发送：`Enter` 发送、`Shift+Enter` 换行（与需求一致）；输入框支持粘贴长文本（超过 8000 字符折叠为「粘贴内容（N 字）」）。
- 上传：支持拖拽与选择；预览（图片缩略、PDF 首页、文本前 20 行）；单文件 ≤ 50MB；上传中显示进度与可取消。
- 执行中：输入框保持可用，新消息显示「排队中（第 2 条）」，直到 `turn.started` 或 `turn.queued` 更新；`turn.queued` 必须来自后端真实 `queue_position`。
- 进度：`AgentTimeline` 展示「正在读取的资料 / 配置变更 / 测试结果 / 待补信息」四类条目，工具调用逐条显示状态（运行中/完成/失败 + 耗时）。
- 停止：点击后按钮进入 `stopping`，返回 `StopReport` 后展示「已停止：<步骤列表>；可能仍在远端运行：<上游任务/请求>」，并提供「去任务里对账」链接。
- 重试：失败或停止后出现「重试此步」，重新发送时会带上 `retry_of` 以便后端去重与审计。
- 历史：面板顶部会话选择器（最近 20 个，可搜索），切换即加载历史消息与工具时间线。
- 键盘可达：`Ctrl+K` 全局搜索、`Alt+J` 打开/关闭 Agent 面板（v2 修正：v1 用 `Ctrl+J`，但 Chrome 的下载快捷键无法被页面拦截，属于「宣传了却按不动」；`Alt+J` 在各主流浏览器可用，同时 `Ctrl+K` 面板内也提供同一动作入口）、面板内 `Tab` 循环、`Esc` 关闭。

### 13.4 视觉与设计令牌（`styles/tokens.css`）

| 令牌 | 值 | 用途 |
|---|---|---|
| `--sr-bg` | `#fbfbfa`（浅） / `#12110f`（深） | 页面底色（暖中性，避免纯白刺眼） |
| `--sr-surface` | `#ffffff` / `#1a1917` | 卡片/面板 |
| `--sr-border` | `#e6e3dd` / `#2a2825` | 1px 边框，**不使用阴影表达层级** |
| `--sr-text` / `--sr-text-muted` | `#1c1a17` / `#6b6660` | 正文/次要文本 |
| `--sr-accent` | `#b4532a`（赤陶橙） | 主操作、当前导航项 |
| `--sr-ok` / `--sr-warn` / `--sr-danger` / `--sr-info` | `#2f6f4f` / `#a56a12` / `#a3322b` / `#2b5d8a` | 状态语义 |
| `--sr-radius` | `6px`（卡片 8px、按钮 6px、输入 6px） | 克制的圆角（不使用 16px+ 大圆角） |
| `--sr-space` | `4/8/12/16/24/32/48` 七档 | 统一间距刻度 |
| 字号 | `12/13/14/16/20/24/32`（正文 14，行高 1.55） | 中文可读性优先 |
| 焦点环 | `0 0 0 2px var(--sr-bg), 0 0 0 4px var(--sr-accent)` | 键盘焦点必须可见 |
| 动效 | 仅 `opacity`/`transform`，150ms `ease-out`；`prefers-reduced-motion` 下 0ms | 减少装饰性动效 |

禁止：渐变背景、玻璃拟态、多重阴影、无意义大圆角、图标与文字混用不同风格（统一 lucide 24px/1.75 描边）。数据表格不使用斑马纹，靠 1px 分隔线与悬停底色区分。

### 13.5 交互最佳实践细节

- 列表统一：搜索（300ms 防抖）+ 筛选 chips + 排序表头 + 分页（20/50/100）+ URL 状态同步（可分享/刷新保持）。
- 表单：`react-hook-form` + `zod` 就地校验，提交按钮旁固定显示三种结果：「已保存」「测试通过」「已发布」分别用不同徽标，不合并成一个「成功」。
- 未保存离开：路由切换或关闭抽屉时 `beforeunload`/路由拦截确认。
- 危险操作：`DangerConfirm` 必须列出影响对象（可展开清单），确认按钮文案包含动作与对象数量（例如「停用 3 个能力」）。
- 加载：骨架屏与真实布局同尺寸，避免跳动；超过 2s 显示「仍在处理，可继续其它操作」。
- 错误：区分网络中断（「连接已断开，正在重试…」自动退避重连）、服务不可用（`503` + 维护页）、无权限（`403` + 说明需要什么角色/配置）、未知错误（展示 `request_id` + 复制按钮）。
- 无障碍：所有交互元素可 Tab 到达、`aria-*` 正确、对比度 ≥ 4.5:1、表格有 `<caption>`/`scope`、状态不仅靠颜色（同时有文字）。
- 窄窗（≥768px）：侧栏折叠为图标条，表格转卡片列表，Agent 面板全宽；<768px 显示「建议使用更宽窗口」提示但功能可用。

## 14. 部署与运维

### 14.1 本地开发

```
# 1) 依赖
cd apps/api && pip install -r requirements-dev.txt
cd agents/aragon-gateway && npm ci
cd apps/web && npm ci

# 2) 初始化（创建 DB + 管理员 + 目录）
python scripts/bootstrap.py

# 3) 三进程（三个终端）
uvicorn skillrouter_api.main:app --reload --port 8000   # apps/api 下
python -m skillrouter_api.worker                        # apps/api 下
npm run dev --prefix apps/web                           # 3000
npm run dev --prefix agents/aragon-gateway              # 8500

# 4) 首次登录
http://localhost:3000/login  → admin / adminjr → 强制改密
```
`deploy/.env.example` 复制为 `.env`，其中 `SKILLROUTER_AGENT_GATEWAY_SECRET` 用 `python -c "import secrets;print(secrets.token_urlsafe(32))"` 生成。

### 14.2 生产部署（Docker Compose）

- 服务：`proxy`（443/80）→ `web`（3000）→ `api`（8000）→ `gateway`（仅容器内 8500）+ `worker`（无端口）。
- 卷：`skillrouter-data:/data`（`SKILLROUTER_DATA_DIR=/data`）：包含 DB、`files/`、`engine-builds/`、`aragon-home/`、`logs/`。
- 健康检查：`api` `GET /healthz`；`web` `GET /api/health`；`gateway` `GET /healthz`；`worker` 通过 `worker_heartbeats.last_seen_at` 判定。
- 依赖顺序：`api` 启动完成迁移 → `web`/`worker`/`gateway` 才启动（`depends_on: condition: service_healthy`）。
- HTTPS：`nginx.conf` 强制 TLS1.2+、HSTS、`proxy_buffering off`（SSE）、`client_max_body_size 50m`、安全响应头（`X-Frame-Options: DENY`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、CSP 允许 `self` + `data:` 图片）。
- 日志保留：容器 `json-file` 轮转 `max-size=20m,max-file=5`；应用日志 `RotatingFileHandler`；审计表永久保留（导出 CSV 除外）。
- 升级：`docker compose pull && docker compose up -d`；启动前自动备份（entrypoint 调 `scripts/backup.py --quick`）；回滚 = 再次 `up -d` 到上一个镜像 tag。
- 环境变量模板中**不含任何真实密钥**；镜像以 digest 固定（`python:3.11-slim@sha256:...`）。

### 14.3 运维手册要点（`docs/runbook/*`）

| 场景 | 步骤 |
|---|---|
| 首次部署 | 复制 env → 生成主密钥与网关密钥 → `docker compose up -d` → 登录改密 → 添加提供商与凭证 → 接入第一个能力 |
| 忘记密码 | 停止 api → `python scripts/bootstrap.py reset-admin-password --user admin`（交互输入）→ 启动；写审计 |
| 主密钥轮换 | 设置页触发 → 后台分批重加密 → 完成后把 `PREVIOUS` 移除；期间不停服 |
| 备份 | 每日 cron `python scripts/backup.py`（含 DB+files+manifest）→ 异地存储；保留 14 份 |
| 恢复 | 维护模式 → `python scripts/restore.py <backup>`（校验指纹/schema）→ 重启 → `verify_restore()` 计数比对 |
| 引擎升级失败 | 查看 `/settings/engine` 的失败检查项 → 已自动回滚则修复后重试；未回滚则手动 `POST /engine/rollback` |
| 上游限流 | 概览待办点开 → 调低该提供商并发/提高优先级 → 或新增备用凭证 |
| 令牌泄露 | `/access` 立即撤销 → 检查 `call_logs` 中该令牌的异常调用 → 轮换对应上游凭证 |

---

## 15. 测试与验收标准

### 15.1 测试分层

| 层级 | 工具 | 覆盖 | 门禁 |
|---|---|---|---|
| 静态检查 | `ruff`, `mypy --strict`(services 层), `eslint`, `tsc --noEmit`, `prettier` | 全仓库 | CI 阻断 |
| 单元 | `pytest`（DB 用临时 SQLite 文件，非内存，以覆盖 WAL/锁）、`vitest` | 加密、密码、令牌、映射、解析、路由、租约、SSRF、导入校验 | 覆盖率 ≥ 80%（`services/`、`executors/`、`security/`） |
| 契约 | `pytest` + `respx` + OpenAPI 快照 | `/api/v1` 全部端点、错误码、SSE 事件 schema、MCP JSON-RPC、`packages/contracts` 生成无 diff | CI 阻断 |
| 集成 | 假上游 + 假网关（`tests/fixtures`） | 同步/异步/流式、幂等、重试、限流、任务恢复、Agent 事件映射 | CI 阻断 |
| 端到端 | Playwright（chromium） | 核心闭环、浮窗 Agent、强制改密、错误态 | CI 可选（`-m e2e`） |
| 并发/性能 | `scripts/perf_smoke.py`（locust 风格 httpx 并发） | 管理接口 P95、任务提交耗时 | 夜间任务，结果归档 |
| 升级回归 | `agents/aragon-gateway/test/conformance.test.ts` + `pytest tests/test_engine_upgrade.py` | 导出清单/事件/工具/切换/回滚 | 升级流水线内阻断 |

### 15.2 必须完成的真实联调（需求硬性验收）

1. **文本能力**：至少 1 家（Anthropic 或 OpenAI 兼容端点）真实 `chat` 调用，同步 + 流式各 1 次，记录真实响应、用量与耗时。
2. **语音能力**：至少 1 家 TTS 或 ASR（如 OpenAI `audio/speech` 或 `audio/transcriptions`），产出真实音频文件并可在 UI 预览/下载。
3. **异步生成能力**：至少 1 家（视频或图像异步任务），覆盖提交 → 轮询 → 成功/失败，并把 `upstream_task_id` 落库。
4. **跨提供商**：以上覆盖 ≥ 2 个提供商、≥ 2 种 `kind`，并能展示**同一能力配置 2 条 route** 时的路由决策与一次真实故障切换（v2：这是 R01 修复后的直接验收项）。
5. **外部 Agent 全闭环**：用 `@skillrouter/cli` 在干净目录安装 SKILL（生成 `SKILL.md` + `invoke.mjs` + 配置）→ `verify` 三项全 ✓ → 用 `node ./invoke.mjs capabilities.list` 动态发现能力 → 用 `node ./invoke.mjs invoke ...` 发起一次真实调用 → 拿到结果并能在 `/calls` 看到记录（v2：命令必须是文档里写的那几条，不得临时手写 curl 冒充）。
6. **MCP 路径**：用 MCP 客户端（`initialize` → `tools/list` → `tools/call`）完成同一能力的一次调用，断言结果与 HTTP 一致（`structuredContent` 相同），且工具名为 `capability__<key 归一化>`。
7. 缺少第三方凭证的场景**必须标记「待验证」**并给出复现步骤（登记在 `docs/runbook/verification-checklist.md`），禁止用 mock 结果冒充验收。

### 15.3 异常与升级验收矩阵

| 场景 | 期望行为 | 对应测试 |
|---|---|---|
| 无效凭证 | 测试返回 `UPSTREAM_AUTH` + 明确 hint；能力不可发布；概览待办出现 | `test_credentials_verify.py`, `test_capabilities_validation.py` |
| 权限不足（令牌作用域外） | `403 TOKEN_SCOPE_DENIED`，记录 `call_logs(status=failed)`，不泄漏能力是否存在（统一 `404/403` 文案） | `test_platform_tokens.py` |
| 令牌撤销 | 撤销后下一次调用 `401 TOKEN_REVOKED`；撤销即时生效（无缓存窗口） | `test_platform_tokens.py` |
| 重复提交 | 同 `Idempotency-Key` 返回同一 `request_id`，上游只被调用 1 次（假上游计数断言） | `test_execution_sync_async_stream.py` |
| 幂等键跨令牌隔离（v2） | 令牌 A 的 `idem_key` 被令牌 B 使用时**不得**命中 A 的请求，返回 `409 IDEMPOTENCY_KEY_REUSED` 或建立独立记录 | `test_platform_tokens.py` |
| 异步提交不触达上游（v2） | `POST .../invoke?mode=async` 在假上游「零请求」的前提下返回 202 + `task_id`，且耗时 ≤1s | `test_execution_sync_async_stream.py` |
| 多 route 故障切换（v2） | 主 route 429 且未产生输出 → 切到备 route，`route_decisions.dropped` 记录原因；已产生首个 delta 后不切换 | `test_routing.py` |
| SKILL 停用即时生效（v2） | `POST /skills/{id}/disable` 后 `/api/v1/skills` 立即不再返回，执行计划返回 `409 SKILL_DISABLED`，版本行与历史记录不变 | `test_skills_import_export.py` |
| 上游超时 | `UPSTREAM_TIMEOUT`；非幂等请求进入 `status_unknown` 而非直接重投 | 同上 |
| 上游限流 | 按 `errors[]` 映射 `UPSTREAM_RATE_LIMIT`，按 backoff 重试至 `max_attempts`，超限失败并建议动作 | 同上 |
| 任务取消 | 上游支持 → `canceled`；不支持 → `409 CANCEL_UNSUPPORTED`，UI 明示「仅停止本地等待」 | `test_tasks_recovery.py` |
| 服务重启 | worker 重启后租约过期任务被重新领取；`running` 任务按 `resume_policy=check` 先查询再决定 | `test_tasks_recovery.py` |
| 备份恢复 | 恢复后计数一致、能力可调用、凭证仍可解密 | `scripts/restore.py` 自检 + 手工验收 |
| 引擎升级失败 | 自动回滚到 `previous`，`engine_upgrade_runs.result=failed`，概览高优待办 | `test_engine_upgrade.py` |
| 升级期间调用 | 已发布、非 `requires_agent_reasoning` 的能力调用成功率 100%（升级作业与执行路径隔离） | `test_engine_upgrade.py::test_calls_during_upgrade` |
| 会话过期 | 过期后管理接口 `401 SESSION_EXPIRED`，前端跳登录并 toast 说明 | `test_auth_flow.py` |
| 网络中断（前端） | SSE 自动重连 + `Last-Event-ID` 补事件，不重复执行；页面显示「连接已恢复」 | `e2e/agent-panel.spec.ts` |

### 15.4 性能验收

条件：4 vCPU / 8 GB / SSD，10 个并发客户端（httpx 并发 + 1 个 SSE 长连），数据集：200 能力、20 提供商、50 万 `call_logs`。

| 指标 | 目标 | 测量方式 |
|---|---|---|
| 管理接口 P95 | ≤ 500 ms（列表/详情/概览） | `perf_smoke.py` 5 分钟稳态 |
| 异步任务提交（平台侧） | ≤ 1 s（校验+持久化+返回 `task_id`） | 同上，排除上游耗时 |
| 同步调用平台开销 | ≤ 150 ms（不含上游） | 假上游 10ms 场景 |
| SSE 首字节 | ≤ 300 ms | `curl -N` 计时 |
| 任务领取延迟 | ≤ 500 ms（提交到 worker 开始执行） | 队列时间戳差 |
| 数据库写等待 | `DB_BUSY` 率 < 0.1% | 日志统计 |

不达标时的既定处置：`call_logs` 归档（>90 天转 `call_logs_archive`）、列表查询强制索引、概览聚合改为物化快照表（每 30s 刷新）。

### 15.5 端到端验收清单（Definition of Done）

- [ ] `docker compose up -d` 后 5 分钟内可完成首次登录与改密。
- [ ] 重启服务后默认密码不复活、`admin.initialized` 保持 1。
- [ ] 创建提供商 → 凭证（掩码展示）→ 能力（结构化编辑）→ 测试（真实响应）→ 发布，全链路可点通。
- [ ] 同一能力配置 2 条 route（不同提供商），能选择「主力」并在主 route 限流时完成一次真实故障切换，`/calls` 详情能看到决策与排除原因（v2）。
- [ ] 概览六项指标（可用能力数、接入状态、近期成功率、失败任务、调用耗时、待办）均可跳转。
- [ ] 浮窗 Agent：Enter 发送 / Shift+Enter 换行 / 排队状态 / 流式 / 工具进度 / 停止报告 / 重试 / 切页面保留 / 拖拽上传预览。
- [ ] `/agent` 接入流程：七步状态可见、每步证据可展开、人为让某步失败后能从该步继续并最终发布（v2：R10 的验收）。
- [ ] SKILL 导入（Git、URL、文件各一次）+ 校验报告 + 发布 + 导出 zip（`secret_scan` 通过）。
- [ ] SKILL「已发布」与「已启用」独立可操作：停用后外部不可见、启用后恢复，不产生新版本（v2：R17 的验收）。
- [ ] 外部 Agent 安装脚本可用（本地 tarball 路径真实存在），`verify` 逐项显示 ✓，且安装目录里确实存在 `SKILL.md`/`invoke.mjs`/`skillrouter.config.json`（v2 起为硬性检查，`SKILL.md` 的 `allowed-tools` 只含目标环境真实存在的工具）。
- [ ] `/api/v1` 六个接口真实可用，OpenAPI 与实现一致（契约测试）；`POST /api/v1/files` 上传后能用 `{"$file":...}` 发起一次带文件的调用（v2）。
- [ ] MCP `initialize/tools/list/tools/call` 与 HTTP 结果一致。
- [ ] 调用记录可按能力/状态/时间筛选，详情含路由决策与耗时分解，正文默认脱敏。
- [ ] 审计覆盖凭证、发布、导入、撤销、路由、升级、**SKILL 停用、审批决策**（v2）等操作，且不含明文密钥。
- [ ] 引擎升级：检测到候选 → 验证报告 → 切换成功或自动回滚，可在设置页查看历史；**切换过程不需要重启容器，回滚在网关进程内完成**（v2：R08 的验收）。
- [ ] 性能基线达标（§15.4）并归档报告；同步调用未因队列化而超时（v2：R03 的验收）。
- [ ] 备份/恢复演练各一次，`verify_restore()` 通过；备份脚本在 `python:3.11-slim` 镜像内可执行（无 `sqlite3`/`tar` 依赖，v2：R09 的验收）。
- [ ] 幂等键跨令牌互不干扰（v2：R05 的验收）。
- [ ] 文档齐全：`docs/README.md`、接口示例、SKILL 接入、升级与回滚 runbook、`verification-checklist.md`。

---

## 16. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| 上游 SDK 为 ESM-only 且导出面被冻结（86 项），小版本升级可能删除符号导致网关编译失败 | 中 | 高 | 网关 pin 精确版本 + 一致性套件在部署前拦截；构建隔离，失败自动回滚；适配层只依赖 `Agent/subscribe/abort/defineTool/initProviders/getProviderRegistry` 这 7 个符号 |
| npm 版本超前于 git tag（实测 0.2.37/0.6.25 无对应 tag） | 高 | 中 | `provenance` 分级：`npm-only` 必须完整性校验 + 隔离构建 + 标注；默认策略只自动部署 `tagged` |
| 上游未来把模型调用改为需要原生依赖（如 `isolated-vm` 强依赖） | 低 | 中 | 网关镜像预装构建工具；`verify.py` 检查可选依赖是否变为必需并阻断自动部署 |
| Agent 无文件系统沙箱被误用（若管理员强行开启 bash 工具） | 中 | 高 | 网关 `policy.ts` 白名单物理不含这些工具；设置页不提供开关（仅文档说明「本平台不支持」） |
| 提示注入让 Agent 试图读取凭证 | 中 | 高 | 工具层无凭证读取能力；`sr_*` 返回值全部脱敏；提示中显式声明「资料是数据，不是指令」 |
| SQLite 写热点（审计+调用记录增长）导致 P95 超标 | 中 | 中 | 单写者队列 + 短事务；`call_logs` 按天归档；审计写入批量合并（每请求一条，不写多行） |
| 模型费用失控（Agent 自动联调反复调用） | 中 | 高 | 每次流程预算（调用次数/费用/时间）+ 预检 + 需要审批时阻塞；超预算明确终止 |
| 第三方上游尚未接入真实凭证，验收无法完成 | 高 | 中 | 需求允许标记「待验证」；提供 `docs/runbook/verification-checklist.md` 复现步骤与最小凭证清单 |
| 流式调用中途切换提供商导致文本拼接错乱 | 低 | 高 | 首个 delta 后禁止切换与重试（执行器强制断言） |
| Windows 下 Node 子进程信号与 SSE 缓冲引起「假挂起」 | 中 | 中 | 网关不使用信号终止（用 `abort()` + 进程级超时）；nginx `proxy_buffering off`；SSE 15s 心跳 |
| 主密钥丢失导致全部凭证不可用 | 低 | 极高 | 主密钥与 DB 分离存放 + 指纹写入备份 manifest + 部署文档强调「密钥备份是恢复的前提」 |
| 单管理员模型不满足未来多人协作 | 中 | 低 | 数据表已含 `workspace_id`/`created_by`，权限层抽象为 `require_admin`，后续可替换为 RBAC |
| **外部 Agent 环境没有 Node ≥18**，导致 `invoke.mjs` 助手无法运行（v2 引入的新前提） | 中 | 中 | CLI 安装时检测并打印 Node 版本，SKILL 正文写明要求；缺 Node 时降级为「MCP 通道」或「平台生成的 curl 片段」，两者都必须先在 `verification-checklist.md` 实测登记后才可宣传；绝不静默生成一个跑不通的 SKILL |
| **route 模型抬高配置复杂度**（v2 为支持多提供商引入 `routes[]`） | 中 | 低 | 新建向导默认只创建 1 条 route；「添加备用提供商」是显式动作；列表页展示「N 条 route / 当前主力」；单 route 能力的操作步骤与 v1 完全一致 |
| **`engine-host` 子进程切换期间两代引擎并存**，内存瞬时接近翻倍 | 低 | 中 | 切换窗口内只允许存在 1 个新子进程；排空超时 60s 后强制 `abort` 并标记该轮 `turn_stopped`；切换期间新轮次排队而非并发启动；镜像内存下限按 1.5× 单实例预留 |

---

## 17. 里程碑与交付物

### 17.1 实施顺序（每步可独立验证）

| 里程碑 | 内容 | 验收方式 |
|---|---|---|
| M0 骨架 | 仓库结构、CI、settings、DB/WAL、Alembic `0001`、`/healthz` | `make dev` 起服务，`GET /healthz` 200 |
| M1 认证与安全 | 初始化、强制改密、会话、CSRF、限流、加密、主密钥 | `test_auth_flow.py` + 手工登录改密 |
| M2 资产 CRUD | 提供商、凭证、掩码、影响面、验证 | `test_crypto_credentials.py` + UI 走通 |
| M3 能力与执行 | 能力定义（标准能力 + `routes[]`）、校验、测试、发布、同步执行、记录 | 假上游集成测试 + 一次真实 LLM 调用 |
| M4 异步与文件 | 任务队列、租约、恢复、取消、文件与产物 | `test_tasks_recovery.py` + 一次真实异步生成 |
| M5 路由与多提供商 | 兼容性、优先级、健康、故障切换、决策记录（route 级） | `test_routing.py` + 两提供商真实对比 + 一次真实故障切换 |
| M6 Agent 网关 | 网关、`engine-host`、平台工具、事件映射、浮窗面板、流程状态机 | `test_agent_adapter.py` + 手工接入一个 API |
| M7 SKILL 与分发 | SKILL CRUD/导入/导出/启停/执行计划、npm CLI（含 `invoke.mjs`）、MCP | `test_skills_import_export.py`、`test_mcp.py`、干净目录安装后按文档命令真实调用 |
| M8 对外接口与观测 | `/api/v1` 全端点、OpenAPI、令牌与配额、审计、概览 | 契约测试 + 外部 Agent 闭环 |
| M9 引擎演进 | 检测/构建/验证/切换/回滚 + 设置页 | `test_engine_upgrade.py` + 一次真实升级演练 |
| M10 性能与交付 | 性能基线、备份恢复演练、部署文档、验收清单 | §15.4 报告 + §15.5 全部打勾 |

### 17.2 最终交付物

1. 可运行源码（`apps/api`、`apps/web`、`agents/aragon-gateway`、`packages/skillrouter-cli`）。
2. 部署配置（`deploy/docker-compose.yml`、三个 Dockerfile、`nginx.conf`、`.env.example`）。
3. 数据库迁移（Alembic revisions + `db/migrations/0001_init.sql` 审阅快照）。
4. 初始化与运维文档（`docs/runbook/init.md`、`backup-restore.md`、`engine-upgrade.md`、`troubleshooting.md`）。
5. 接口文档（`docs/api/openapi.json` 由 CI 生成 + `examples.md` 真实可复制示例）。
6. SKILL 接入说明（`docs/skills/connect.md`，含已验证 Agent/版本/接入方式矩阵）。
7. 可安装的 npm 包（`packages/skillrouter-cli` 源码 + `npm pack` 产物 `.tgz` + 本地安装命令）。
8. 测试报告（单元/契约/集成/e2e/性能/升级回归，含时间与环境）。
9. AragonAgent 集成与升级说明（`docs/plans/skill-router/aragonagent-upstream.md` + 设置页可视化）。

---

## 附录 A：错误码表（节选，实现时全量落地于 `errors.py`）

| 错误码 | HTTP | 含义 | 用户下一步 |
|---|---|---|---|
| `AUTH_INVALID_CREDENTIALS` | 401 | 用户名或密码错误 | 检查输入；注意大小写 |
| `NOT_INITIALIZED` | 409 | 平台尚未完成初始化 | 运行 `scripts/bootstrap.py` 完成初始化（v2） |
| `LOGIN_THROTTLED` | 429 | 登录尝试过多 | 等待 `retry_after` 秒 |
| `PASSWORD_CHANGE_REQUIRED` | 403 | 必须先修改默认密码 | 前往改密页 |
| `SESSION_EXPIRED` | 401 | 会话过期/被吊销 | 重新登录 |
| `CSRF_INVALID` | 403 | CSRF 校验失败 | 刷新页面后重试 |
| `VALIDATION_FAILED` | 400 | 通用字段校验失败 | 按 `details.fields` 修正（v2） |
| `DB_BUSY` | 503 | 数据库写等待超时（写队列 5s） | 稍后重试；若持续发生需降低并发（v2） |
| `INVALID_STATE_TRANSITION` | 409 | 状态机非法迁移（任务/流程/版本） | 刷新获取最新状态后重试（v2） |
| `PROVIDER_IN_USE` | 409 | 提供商仍有关联凭证/能力 | 先查看影响面，或 `?force=true` 软删（v2） |
| `CREDENTIAL_IN_USE` | 409 | 凭证仍被引用 | 先查看影响面并切换引用 |
| `CAPABILITY_IN_USE` | 409 | 能力被 SKILL/令牌/在途任务引用 | 查看影响面；改用停用而非删除（v2） |
| `PARAM_MAP_UNRESOLVED` | 400 | 参数映射引用了未声明的来源 | 检查 `param_map` 的 `input.*` / `route.*` 引用（v2） |
| `INPUT_SCHEMA_INVALID` | 400 | 输入不符合 Schema（v1 附录误写为 `CAPABILITY_SCHEMA_INVALID`，已统一） | 按 `details.fields` 修正 |
| `UNSUPPORTED_MODE` | 400 | 请求的模式不被该能力支持 | 查看能力详情中的 `modes` |
| `UNSUPPORTED_PARAM` | 400 | 未声明的参数/不被支持的 MIME | 移入 `options.extensions` 或删除 |
| `PROVIDER_NOT_AVAILABLE` | 409 | `preferred_provider` 不可用或无可用 route | 查看错误体中的可用 provider 列表（v2） |
| `SKILL_DEPENDENCY_MISSING` | 409 | SKILL 依赖的能力版本缺失或已废弃 | 补齐依赖或发布新版本（v2） |
| `SKILL_DISABLED` | 409 | SKILL 已被停用 | 由管理员在 SKILL 页启用（v2） |
| `NOT_FOUND` | 404 | 对象不存在或无权查看 | 检查标识；无权与不存在使用同一文案（v2） |
| `UPSTREAM_AUTH` | 502 | 上游拒绝鉴权 | 到凭证页检测/替换 |
| `UPSTREAM_RATE_LIMIT` | 429 | 上游限流 | 降低并发或稍后重试 |
| `UPSTREAM_QUOTA` | 402 | 上游余额/配额不足 | 到提供商控制台充值 |
| `UPSTREAM_TIMEOUT` | 504 | 上游超时 | 查看任务状态，勿重复提交 |
| `UPSTREAM_UNAVAILABLE` | 503 | 上游不可用 | 检查状态页或切换提供商 |
| `TASK_STATUS_UNKNOWN` | 409 | 无法判定上游是否受理 | 调用对账接口查询 |
| `CANCEL_UNSUPPORTED` | 409 | 上游不支持取消 | 只能停止本地等待 |
| `IDEMPOTENCY_KEY_REUSED` | 409 | 同一幂等键不同请求体 | 使用新的幂等键 |
| `TOKEN_SCOPE_DENIED` | 403 | 令牌无该能力权限 | 在管理端扩大作用域 |
| `QUOTA_EXCEEDED` | 429 | 平台配额用尽 | 调整配额或等待窗口重置 |
| `BUDGET_EXCEEDED` | 402 | 平台预算用尽 | 调整月预算 |
| `MAINTENANCE` | 503 | 维护模式（备份/恢复） | 稍后重试 |
| `IDEMPOTENCY_IN_PROGRESS` | 409 | 同一幂等键的首个请求仍在执行 | 等待首个请求完成，或换用新的 `Idempotency-Key`（§A.3.5） |
| `LEASE_LOST` | 409 | 任务队列租约已丢失、处理权已转移 | 无需人工干预：任务会被重新领取（§A.3.6-6/7） |
| `MIGRATION_NOT_ADOPTED` | —（启动期） | `alembic_version` 缺失/落后于 head 或 `user_version` 不符 | 生产环境启动即失败；运行 `python scripts/adopt_alembic.py --dry-run` 后 `alembic upgrade head`（§A.3.8） |
| `UPGRADE_IN_PROGRESS` | 409 | 有升级正在执行 | 等待当前升级结束 |
| `ENGINE_UNAVAILABLE` | 503 | 网关不可用 | 查看 `/settings/engine`，检查网关容器 |

## 附录 B：上游关键契约速查（实现时对照）

```text
aragon info --json  →  {cli, core, node, schemaVersion, home, configPath, sessionsDir,
                        provider, model, hasApiKey, tools[], outputFormats[], inputFormats[],
                        permissionModes[], features[]}
aragon doctor --json →  {ok, checks:[{name, verdict:pass|warn|fail, detail, remedy}]}  exit 0/1
aragon exec          →  NDJSON (--output-format stream-json) 事件:
                        system(init) / user / text_delta / thinking / assistant /
                        tool_call / tool_result / todo / team / fast_review / retry /
                        error / result；schemaVersion=1；未识别事件必须忽略
stdin (--input-format stream-json): {"type":"user","text":"…"} | {"type":"interrupt"} | {"type":"end"}
exit codes: 0 成功 / 1 运行错误 / 2 配置或用法错误 / 3 预算中止 / 130|143|129 信号
SDK 侧: new AragonAgent({systemPrompt, model, tools, providerRegistry, getApiKey, ...})
        agent.prompt() / subscribe(listener) / abort() / steer() / followUp()
        replaceMessages() / state / waitForIdle()
```

## 附录 C：外部 Agent 兼容矩阵（本阶段将验证并在此登记实测结果）

| Agent | 已验证版本 | 接入方式 | 目录 | 状态 |
|---|---|---|---|---|
| AragonAgent CLI | 待实测（目标 `@aragon-agent/cli` 0.6.22） | 写 `SKILL.md` 到 `.aragon/skills/` 或 `ARAGON_SKILLS_PATH` | `./.aragon/skills/skill-router/` | 待验证 |
| Claude Code | 待实测 | 写 `SKILL.md` 到 `.claude/skills/` | `./.claude/skills/skill-router/` | 待验证 |
| 任意 MCP 客户端 | MCP `2025-06-18` | `POST /api/v1/mcp` + Bearer 令牌 | — | 待验证（契约测试覆盖） |
| 通用 HTTP 客户端 | `curl`/任意语言 | `/api/v1` OpenAPI | — | 已设计，待实测 |

> 每一项在真实跑通后必须填写版本号与命令输出（`docs/skills/connect.md` 保存日志片段），未跑通不允许标为「已验证」。

---

## 18. 评审结论（Review Verdict）

### 18.1 结论

**有条件通过（Approved with Conditions）。**

v1.0 的架构方向、技术选型、安全模型与验收矩阵都成立，可以对它开工。但它有 **3 个 P0 问题会让核心需求在实现阶段直接撞墙**（多提供商路由的数据模型不成立、导出 SKILL 声明了目标环境不存在的工具、同步调用被错误地放进队列），以及 15 个 P1 问题会造成实现歧义、契约不一致或验收无法核对。这些问题已在 v2 正文全部修正（见 §0 评审记录逐项对应）。

**因此本结论的生效条件是：实现必须以 v2 为准，不得回退到 v1 的行为。** 特别是下面 3 条，任何一条被"按 v1 做"都会导致需求无法验收，而不是风格差异：

1. 能力必须是「标准能力 + `routes[]`」结构（§7.6/§10.1 `capability_routes`），否则「同一标准能力多个兼容提供商 + 优先级 + 健康检查 + 受限故障切换」无法实现。
2. 导出的 `SKILL.md` 的 `allowed-tools` 只能包含目标 Agent 环境真实存在的工具，平台调用一律经 `invoke.mjs`（§7.9/§9.8），否则安装后的 SKILL 在 AragonAgent / Claude Code 里会整份失配。
3. `mode=sync` / `mode=stream` 在 API 进程内执行，队列只承载 `async` 与长任务（§7.8），否则 §15.4 的 200ms/500ms 基线不可能达标。

### 18.2 放行条件（Conditions，全部为可验证项）

| 编号 | 条件 | 验证方式 | 状态 |
|---|---|---|---|
| C1 | 全部 P0/P1 修正落地到代码，不得只改文档 | 代码评审抽查 §0 表中的 18 项修复位置 | 待实现验证 |
| C2 | 能力模型支持多 route，并完成一次真实故障切换 | §15.2-4、§15.5 对应条目 | 待实现验证 |
| C3 | 干净环境安装 SKILL 后，**只用文档里写的命令**完成发现 + 调用（无手写 curl） | §15.2-5 | 待实现验证 |
| C4 | 异步提交在"上游零请求"前提下 ≤1s 返回 `task_id` | §15.3「异步提交不触达上游」 | 待实现验证 |
| C5 | 幂等键跨令牌互不干扰 | §15.3「幂等键跨令牌隔离」 | 待实现验证 |
| C6 | 引擎升级切换与回滚**不需要重启容器 / 不需要 Docker socket**，且升级期间已发布能力调用成功率 100% | §12.4-4、§15.3「升级期间调用」 | 待实现验证 |
| C7 | 备份/恢复脚本在 `python:3.11-slim` 镜像内、以及 Windows 主机上均可执行 | `scripts/backup.py` + `scripts/restore.py` 演练 | 待实现验证 |
| C8 | 附录 A 错误码与实现完全一致，前端分支不出现未登记 code | 契约测试 + `errors.py` 枚举比对 | 待实现验证 |

### 18.3 明确保留的已知限制（不作为放行阻塞，但必须在 UI/文档中如实呈现）

1. **MCP 客户端通路尚未实测**（附录 C 全部「待验证」）：本阶段只承诺「实现并做契约测试」，不承诺「已在真实 MCP 客户端验证」。UI 不得把 MCP 标为「已验证」。
2. **真实第三方凭证可能缺失**：§15.2 的真实验收项若因无凭证无法执行，必须在 `docs/runbook/verification-checklist.md` 登记为「待验证」并给出复现步骤，禁止用 mock 冒充。
3. **Agent 引擎无沙箱**：平台的对策是「不注册任何文件/命令工具」（D4），不是隔离。该事实必须在设置页与文档中如实说明，不得宣称"已沙箱化"。
4. **`npm-only` 上游版本默认不自动部署**：只允许 `tagged` 自动部署，`npm-only` 需人工确认（§12.4）。
5. **单管理员、单机 SQLite**：并发上限约 50 task/s，属于本阶段明确的范围（N1/N3），不得据此宣称"生产级多租户"。

### 18.4 评审签字

| 项 | 内容 |
|---|---|
| 评审范围 | `docs/plans/skill-router/spec.md` 全 17 节 + 3 个附录，以及 `aragonagent-upstream.md` 的事实一致性 |
| 发现 | P0 × 3、P1 × 15、P2 × 8（共 26 项） |
| 处置结果 | P0 3/3 已修、P1 15/15 已修、P2 7 项已修 + 1 项保留（P05，MCP 变更通知，需实测后补） |
| 未解决 P0/P1 | **0** |
| 结论 | **有条件通过**（条件见 §18.2，共 8 条，全部可在实现阶段逐项验证） |

---

## 实施过程发现的方案缺陷（Issues Found During Implementation）

> 本节由实现节点追加（§18 评审结论之后）。只记录**实现时发现的、正文存在偏差或缺失**的项；
> 已按「不静默偏离」原则在代码中采用修正后的做法，并在此登记原因与验证方式。

### I1（P1，已修正）`request.url_template` 的占位符语法未定义

- **问题**：§7.6 示例同时出现两种写法 —— `url_template: "{base_url}/v1/chat/completions"`（无 `$`）与
  `body_template: {"model": "${model}"}`（有 `$`），正文从未说明求值器必须同时支持两者。按 v2 字面实现会出现
  「URL 里 `{base_url}` 未被替换 → 不是合法 http(s) → 保存后测试必然 400」的死路。
- **修正**：`executors/mapping.py::fill_template` 同时支持 `{key}` 与 `${key}`；整串为单个占位符时保留原生类型
  （数字/对象不被转成字符串），并保留 `base_url` / `model` / `model_hint` 的隐含别名。
- **验证**：`tests/test_execution_sync_async_stream.py`、`scripts/e2e_live.py`（真实 echo 上游）。

### I2（P1，已修正）`modes` 含 `async` 的校验被写成「每条 route 都要支持」

- **问题**：§7.6 校验规则 7 的原文是「**至少一条 enabled route** 同时提供 `async.submit.task_id_path`
  与 `async.query.url_template`」，但按「逐 route 报 error」实现会使得「主 route 支持异步 + 备用 route 只做同步」
  这一完全合法的配置无法发布（多 route 是 v2 的核心能力）。
- **修正**：逐 route 缺异步配置降级为 `warn`（该 route 在 `mode=async` 时被路由器过滤）；
  能力级检查保留为 `error`：`modes` 含 `async` 但**没有任何** enabled route 支持 → `ASYNC_QUERY_MISSING`。
- **验证**：`tests/test_capabilities_validation.py::test_modes_async_requires_query_config`、
  `scripts/e2e_live.py`（2 条 route、仅主 route 支持异步时发布成功）。

### I3（P0，已修正）SSRF 防护被错误地施加到「管理员配置的上游地址」

- **问题**：§11.1 把「API 测试 URL」也列入 SSRF 防护范围，若照字面对 `credentials_verify` 复用
  `fetch_guard.validate_url()`，则 `base_url = http://127.0.0.1:11434`（本机 Ollama）或内网网关**永远无法验证**，
  与需求「支持任意 OpenAI 兼容端点」直接冲突；同时让「凭证有效性检测」变成不可用功能。
- **修正**：新增 `security/netpolicy.py::validate_admin_target()` —— 对**管理员显式配置**的地址只拒绝非
  http(s) 协议与云元数据地址（`169.254.169.254` 等），**允许私网**；而抓取外部网页、导入 SKILL 等
  「不可信来源」继续走 `fetch_guard.validate_url()` 的完整校验（DNS 解析后逐 IP 拒绝私网、逐跳重定向校验）。
  两类目标在代码上是两条路径，不是同一个开关。
- **验证**：`tests/test_ssrf_guard.py`（外部抓取仍拒绝私网/元数据）、
  `tests/test_credentials_verify.py`（本机上游可探测）、`scripts/e2e_live.py`。

### I4（P1，已修正）部署环境的系统代理会劫持「本机/内网上游」调用

- **问题**：正文未提及出站代理。实测环境（Windows）注册表存在系统代理 `127.0.0.1:7897`，
  httpx 默认 `trust_env=True` 会把 `http://127.0.0.1:<port>` 的上游请求也发给代理，表现为**上游 502**、
  测试面板显示「上游不可用」，与真实原因（本地代理）完全不符，排查成本极高。
- **修正**：`security/netpolicy.py::client_kwargs()` 在 loopback / 私网 / link-local / 单标签主机名
  目标上强制 `trust_env=False`（等价 NO_PROXY），外部目标仍尊重环境代理（企业网络需要）；
  `fetch_guard` 的外部抓取一律 `trust_env=False`，避免代理绕过 SSRF 校验。
- **验证**：`scripts/e2e_live.py` 的 58 项真实联调（本机 echo 上游，真实 HTTP）。

### I5（P1，已修正）Alembic 迁移在 SQLite 上会静默回滚

- **问题**：§7.2 要求「`alembic upgrade head`，`PRAGMA user_version` 与 `alembic_version` 双向校验，
  不一致即启动失败」，但正文未给出实现约束。在 `env.py` 里用
  `connection.exec_driver_sql("PRAGMA foreign_keys=ON")` 之后，SQLAlchemy 2.0 会隐式开启事务，
  导致 alembic 自身的 `alembic_version` 写入与迁移内的种子 DML **全部回滚**（实测：
  建表成功（DDL 非事务）但 `alembic_version` 为空、`user_version=0`，`alembic stamp head` 亦无效）。
- **修正**：PRAGMA 改为通过 Engine `connect` 事件设置（与 `db/engine.py` 一致），不在连接上手写 PRAGMA；
  `0001_init` 只做 schema + 触发器 + 种子 + `user_version`。`services/bootstrap.py::assert_migrated()`
  同时校验 `PRAGMA user_version` 与 `alembic_version == head`。
- **验证**：`apps/api/migrations/*` 实际执行 `alembic upgrade head` + `db/migrations/0001_init.sql` 快照导出
  （49 表 / 54 索引 / 2 触发器 / `user_version=1` / `alembic_version=0002_engine_upgrade`）。

### I6（P2，已修正）`SKILLROUTER_DB_PATH` 的相对路径基准未定义

- **问题**：§7.1 默认 `SKILLROUTER_DATA_DIR=var` 且 `SKILLROUTER_DB_PATH=var/skillrouter.db`。
  若把 `db_path` 解析为「相对 data_dir」，默认组合会得到 `var/var/skillrouter.db`（§7.1 的字面配置即失效）。
- **修正**：相对路径统一按**当前工作目录**解析（`Settings.db_file`），默认组合落在 `./var/skillrouter.db`；
  绝对路径原样使用。同时 `db_path`/`data_dir` 等字符串字段做 `strip()`（Windows `set VAR=value &&` 会带尾随空格）。
- **验证**：`scripts/bootstrap.py`、`scripts/e2e_live.py`（自定义数据目录的隔离实例）。

### I7（P2，已修正）登录限流窗口的维度

- **问题**：§7.3 写「固定窗口限流（5 次/5 分钟/IP+用户名）」，未说明是「IP 与用户名各自计数」还是
  「组合计数」。按「各自计数」实现时，攻击者用一个 IP 试错会把该 IP 上所有管理操作锁死，
  也让测试/多实例场景互相污染（实测：一个用例的失败会锁掉后续用例的登录）。
- **修正**：固定窗口按 **(username, ip) 组合**统计（与需求「IP+用户名」的表述一致），
  超限返回 `429 LOGIN_THROTTLED` + `details.retry_after`。
- **验证**：`tests/test_auth_flow.py::test_login_throttled_after_failures`。

### I8（P1，已补充）§9.5 未定义网关 `sr_*` 工具的**实现在哪一侧**

- **问题**：§12.3 把 12 个 `sr_*` 工具定义在网关（Node）侧，§9.5 只列了 `/internal/capabilities`、
  `/internal/credentials/materialize`、`/internal/audit`，没有定义工具**业务逻辑**的落点。
  若工具逻辑写进 Node，就会复制一份「能力校验/发布/测试」的业务规则，形成正文 D2 明确禁止的长期分叉。
- **修正**：网关只做「JSON Schema 参数校验 + 超时 + 审计」；工具的**唯一实现**在 Python
  `api/internal.py::POST /internal/tools/{tool_name}`（复用 `services/*`，与人工操作同一条代码路径）。
  未注册的工具名一律 404 并提示白名单（保证「上游新增工具默认不注册」）。
- **验证**：`tests/test_internal_tools.py`（含 Agent 提草案 → 结构校验 → 真实测试的端到端用例）、
  `agents/aragon-gateway/src/platform-client.ts`（转发契约）。

### I9（P2，记录在案）浮窗 Agent 的 SSE 断线与「turn 收尾」仍需真实 Agent 运行时验证

- **现状**：`agent_turn` handler 已把消息投递给网关，网关事件经
  `worker/handlers/agent_turn.py::ingest_stream()` 回灌 `agent_events` 并广播到
  `/api/admin/agent/sessions/{id}/stream`。但本机没有可用模型凭证，**真实推理链路仍为「待验证」**
  （已在 `docs/runbook/verification-checklist.md` 登记，未标为已验证）。
- **影响**：不影响已发布普通能力（§7.12：它们不经过网关，e2e 已验证升级/停机期间照常调用）。

### I10（P2，记录在案）`output_schema.additionalProperties=false` 对生成类能力偏严

- **问题**：§7.6 规则 4 要求 `input_schema` 与 `output_schema` 的 `additionalProperties` 都必须为 `false`。
  对输入是正确约束（「不支持的参数必须显式报错」），但对**上游返回**的输出 Schema 过严：
  图像/视频类供应商常返回未声明的元数据字段。
- **处置**：实现按正文执行（两者都必须是 `false`），但把「输出可能包含未声明字段」写进
  `docs/api/examples.md` 与能力编辑器的提示文案，建议用 `output_files` / 显式字段承接。
  若后续要放宽，只需改 `capabilities.validate_definition()` 的一处判定并更新本文。

### I11（P2，记录在案）`sse_events` 与 `agent_events` 的主键策略

- **问题**：§10.1 要求 `sse_events` 只承载非 Agent 通道（P04），但其 `id` 为自增 INTEGER，
  跨进程（api + worker 同时写）取「MAX(id)+1」有竞争。
- **处置**：`sse/broker.py` 优先用 `MAX(id)+1` 分配并在 `db_write()` 写锁内落库，失败时回退为进程内自增；
  事件表只用于 24h 回放，重复 id 不会造成业务错误（最坏丢失一次回放，前端可整段刷新）。
  真正需要严格顺序的 Agent 通道走 `agent_events`（`(turn_id, seq)` 唯一索引，由单一写入者分配）。

### I12（P1，已修正）共享密钥的空白字符与「本机代理」会让 Agent 接入直接失败

- **问题**：§7.1 只写了「`SKILLROUTER_AGENT_GATEWAY_SECRET`（32 字节随机）」，未涉及两件实测会踩到的事：
  1. 部署脚本里 `set VAR=value && next` 会把**尾随空格**带进变量，空格进入 `X-Internal-Token` 头后
     httpx 直接抛 `LocalProtocolError: Illegal header value`，而平台侧把它包装成「网关不可达」，
     提示完全指向错误方向（实测复现：`check_gateway.py` 首跑失败）。
  2. 与 I4 同源：适配层访问 `127.0.0.1:8500` 时同样会被系统代理接管。
- **修正**：`Settings` 对所有字符串字段做 `strip()`（`field_validator("*", mode="before")`）；
  `agent/client.py` 使用 `client_kwargs()`（本地目标 `trust_env=False`）并单独捕获
  `httpx.LocalProtocolError` / `InvalidURL`，错误提示明确写「检查两侧共享密钥是否完全一致（不含空格/换行）」。
- **验证**：`scripts/check_gateway.py` 12/12（真实网关进程 + 真实 SDK 0.2.34）。

---

_本节以上 12 项均已按「修正后的做法」落地，其中 I1/I3/I4/I5/I8/I12 属会直接导致功能不可用的缺陷。_

---

### D2. 迭代 2 实施过程发现（2026-10-19）

以下偏差不是「设计写错了就绕过去」，而是实施时**发现契约与源码/工具链事实不符**，按 §A 首段的规矩
先在此登记，再按「备注」列的做法实现。逐条都在 `docs/runbook/verification-report.md` 的「迭代 2」章节有对应证据。

| # | 发现的方案缺陷（证据） | 实施的处置 | 影响面 |
|---|---|---|---|
| I2-1 | **`ALL_EVENT_NAMES` 在网关侧不存在。** §A.3.7-2 要求「候选事件名集合 ⊇ 网关导出的 `ALL_EVENT_NAMES`（`agents/aragon-gateway/src/engine.ts:1358`）」，但该文件只有 `platformEventTypes()` 返回 **13 个平台侧事件名**；用平台名与候选产物比较会让「事件映射契约」检查**恒 failed**（与 R2-01 同类缺陷）。 | 在 `engine.ts` 新增 `ALL_EVENT_NAMES` = 适配层真正映射的上游 `AgentEvent` 判别式集合（由源码派生，不写死数量）；校验变成「候选 ⊆ 网关映射集合」，平台事件名不进比较。 | 升级验证三态、`scripts/verify_artifact.mjs` |
| I2-2 | **`tasks.released_at` 列不存在，也不在 §A.6.1 的 6 列清单里。** §A.3.4 说「用任务行上的 `released_at` 幂等标记」释放异步并发，但 §A.6.1 的迁移只允许 6 列 + 2 索引。 | 幂等标记放 `tasks.result["quota_released"]`（JSON 列，无需迁移）；`services/execution.py::release_task_quota(task_row)` 是唯一入口，重复调用返回 0。 | F-05 / T-46 |
| I2-3 | **`services.files.save_bytes()` 的字段名与 `describe()` 不一致。** §A.3.10-3 要求「补 `signed_url` 与 `expires_at`」，而 `describe()` 已有的键叫 `signed_url_expires_at`（秒）。 | `save_bytes()` 同时输出 `signed_url` / `expires_at`（与 §A 一致，供 `/api/v1/files` 上传响应）**和** `signed_url_expires_at`（保持 `describe()` 既有契约不破）。 | F-15 / T-31 / T-48 |
| I2-4 | **`apps/web/lib/hooks/useTableState.ts` 在第 1 轮并不存在。** §A.4.3 把它标成 `M`（修改），实际是新建。 | 按新建实现（薄封装 `components/common/DataTable` 的 `useTableState`，加 `sortWhitelist`/`dateKeys`），并在此更正动作类型。 | F-16 / F-17 / F-18 |
| I2-5 | **`migrations/env.py` 用占位 URL 且 `alembic.ini` 写死 `driver://`。** §A.3.8 假定 `alembic upgrade head` 能直接按 `SKILLROUTER_DB_PATH` 跑，实际必须先改 `env.py` 让调用方可覆盖 `sqlalchemy.url`。 | `env.py` 在调用方已设置 `sqlalchemy.url` 时尊重它，否则回落 `settings.db_url`；`fileConfig(..., disable_existing_loggers=False)` 避免把应用 logger 关掉。这是 §A.4 表外的最小改动，用于让迁移单轨真的可执行。 | F-11 / T-23 / T-24 |
| I2-6 | **`bootstrap.py` 的相对路径语义有坑（非本轮引入，但被本轮放大）：`db_path`/`data_dir` 都按**当前工作目录**解析。从仓库根 `bootstrap`、再从 `apps/api` 起 `uvicorn`，会打开两个不同的库（启动期 500 `no such table: system_state`）。 | 不改默认行为（会破坏既有部署），但在 `docs/runbook/init.md` 与 verification-report 里写明「`SKILLROUTER_DATA_DIR`/`SKILLROUTER_DB_PATH` 一律用**绝对路径**，四个进程共用同一组值」。 | 运维文档、`check_web.py` 实测 |
| I2-7 | **附录 A 与 §10.1 的 §A.10 同步项只落地了一部分。** 评审节点补了 §9.2 的 `cancel_scope` 与 `rate_counters`，但 `idempotency_keys.state`、`tasks.cancel_requested_at`、`credentials.meta_sealed`、`call_logs.needs_reconcile`、`engine_upgrade_runs.verify_status/blocked_reasons`、`engine_candidates` 去掉 `skipped`、`usage_records.source` 枚举、附录 A 三个新码仍缺。 | 本轮直接补齐（本文件已改）；`python scripts/check_file_plan.py` 同时对 §8 与 §A.4 校验，覆盖率 310/310。 | §A.10 / DoD |

---

## V2. 评审结论（Review Verdict，迭代 2：v3.0 → v4.0）

### V2.1 结论

**有条件通过（Approved with Conditions）。**

§A 的方向是对的：它把「纸面完整」拉回「可验证正确」，12 个 P0 全部指向真实会致错的语义，验收标准也从「测试数量」
改为「每条 P0 一条会红的断言」。但作为一份**改已交付代码**的契约，它在「能不能落地」这一维上有系统性偏差——
10 个 P0 里有 8 个属于「文档描述的机制在现有代码/镜像里并不存在，或已经存在另一套」。这些问题已逐条在 §A 正文修正
（§R2.1/§R2.2 给出证据与落地位置），修正后的契约可以开工。

**生效条件：实现必须以 v4.0 的 §A 为准，且 §A.10 的正文同步项要在同一提交内完成。**

三条最容易「按老文档做错」的地方：

1. **升级流水线的执行位置**（§A.3.7-0）：不在 api 容器里跑 npm/node/git，改为网关容器 + 两个内部端点；
   否则验证恒为 `blocked`，`policy=auto` 永不生效。
2. **迁移必须幂等**（§A.6.1）：`0003` 用条件加列；否则存量 `create_all` 库升级即 `duplicate column name`。
3. **「测试通过」的判定口径**（§A.3.1-2/3）：`call_logs.status` 落库是 `success`（不是 `succeeded`），
   `test_runs[].ok` 是布尔（不是 1）。任一写反，F-01/F-02 会原样复发，而且测试仍然「全绿」。

### V2.2 放行条件（Conditions，全部为可验证项）

| 编号 | 条件 | 验证方式 | 状态 |
|---|---|---|---|
| C1 | §R2.1 的 10 项 P0 修正落地到代码，不得只改文档 | 代码评审逐项对照 §R2.1「处置」列 | 待实现验证 |
| C2 | 迁移可在「空库」与「create_all 存量库」上各跑两遍 `upgrade`，且 `downgrade` 能回到 base | T-23、T-24、T-42 | 待实现验证 |
| C3 | 升级验证的真实性：网关不可达 → `blocked`；`policy=auto` 只在 `verify_status='passed'` 时自动切换 | T-16、T-18、T-41、T-47 | 待实现验证 |
| C4 | 「测试通过 → 发布按钮可用 → 刷新后仍可用」；列表成功率与「上次测试」有真实值 | T-01、T-02、T-33 + 手测 1/2 | 待实现验证 |
| C5 | 凭证投影无明文；敏感 meta 密封；回调仍验签；`probe_path` 不被白名单误丢 | T-03、T-04 | 待实现验证 |
| C6 | 流式/异步计费生效且不误伤：`quota_counters` 递增、超预算 402、并发额度不泄漏 | T-07、T-08、T-45、T-46 | 待实现验证 |
| C7 | 取消与租约：`queued→canceling` 合法、无孤儿/悬挂任务、越权 release 被拒 | T-12…T-15、T-44 | 待实现验证 |
| C8 | §A.10 的正文同步项全部落地；`docs/api/openapi.json` 重新导出无漂移 | 评审 + CI 的 `api` 作业 | 待实现验证 |

### V2.3 明确保留的已知限制（不作为放行阻塞，但必须在 UI/文档中如实呈现）

1. **预算在终态记账、不做预占**：单次调用可能超过剩余预算（超额上限＝本次成本），设置页与 `402` 的 hint 必须写明。
2. **历史测试不可回看完整响应**：`capability_test_runs` 不存 `output`/`dropped`（R2-30），UI 不得暗示可回看。
3. **真实第三方凭证、真实 Docker、目标硬件性能、真实 MCP 客户端**仍待验证（P1/P2/P3/P5/P6/P8/P11），
   不得因为本轮修了代码就改 `verification-checklist.md` 的状态。
4. **升级工具链依赖网关容器**：网关长期不可用期间无法自动升级，只能按 runbook 手动构建（R12）。

### V2.4 评审签字

| 项 | 内容 |
|---|---|
| 评审范围 | `spec.md` §A（v3.0）+ §1–§18 既有契约 + 仓库现状（`apps/api`、`apps/web`、`agents/aragon-gateway`、`deploy/`、`scripts/`、`docs/`） |
| 发现 | **P0 × 10、P1 × 17、P2 × 8**（共 35 项，见 §R2） |
| 处置结果 | P0 10/10 已修；P1 17/17 已修；P2 3 项顺手修 + 5 项记录在案 |
| 未解决 P0/P1 | **0** |
| 结论 | **有条件通过**（条件见 §V2.2，共 8 条，全部可在实现阶段逐项验证） |

---

_文档结束。实现过程中任何与本文不一致的行为都视为缺陷；如需变更设计，先更新本文并记录变更原因。_
