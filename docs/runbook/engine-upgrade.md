# 引擎升级与回滚（runbook）

> 依据：spec §12.4（升级流水线）、§7.12 第 6 条（`engine-host` 热切换）、§7.13、§15.3「引擎升级失败/升级期间调用」。
>
> **一句话原则**：切换引擎**不重启容器、不需要 Docker socket、不使用信号**。
> API 进程只调网关的 `POST /internal/admin/reload`，由网关自己起新 `engine-host` 子进程、
> 排空旧子进程。API 进程从来没有容器权限，也不应该有（§4.3 信任域）。

## 0. 前提

| 项 | 要求 | 检查 |
|---|---|---|
| 网关引擎模式 | `AGENT_GATEWAY_ENGINE_MODE=host`（compose 已设） | `GET /healthz` 的 `engine.detail` 不含 `degradedNote` |
| 当前 pin | `system_state.engine_pin` / `SKILLROUTER_ENGINE_PIN` 与实际一致 | 设置页「引擎」或 `GET /api/admin/engine/status` |
| 出站策略 | `auto` 升级需要 `SKILLROUTER_EGRESS_POLICY=allow`；`deny` 时只能手工上传 tarball | `.env` |
| 备份 | 切换前**自动**跑 `backup_job.quick_backup(label="pre-upgrade")`（**只含 DB + manifest**，不带 `aragon-home` / `engine-runtime`）；失败即 abort，不装 home、不写指针 | `engine_upgrade_runs.backup_file_id`（失败时为 `null`，`stage="backup"`） |
| 工具链 | 隔离构建与产物验证跑在**网关容器**（`node:22-slim` + 本轮新增的 `git`）；api/worker 镜像没有 node/git | 见 §0.1；`SKILLROUTER_ENGINE_TOOLCHAIN=gateway`（默认） |
| 廉价模型 | 冒烟调用需要可用模型；没有则该项 `blocked`（不阻断部署，但 UI 必须明示「未做真实推理验证」） | 设置页 Agent 配置 |

### 0.1 工具链归属：构建在网关容器，不在 api（§A.3.7-0）

| 项 | 事实 | 后果（照旧实现会怎样） |
|---|---|---|
| 构建/验证在哪跑 | **网关容器**（`node:22-slim` + `git`），由 api 侧通过 `POST /internal/admin/build`、`POST /internal/admin/verify-artifact` 编排 | api 镜像只有 `ca-certificates`，自己 spawn `git clone`/`npm ci`/`npm run build` 必然失败 |
| Python 侧角色 | 只做**编排**：状态机、审计、并发限制、结果落库（`upgrade/toolchain.py`） | — |
| `SKILLROUTER_ENGINE_TOOLCHAIN=gateway`（生产默认） | 走内部端点，工具链不可用 → 构建阶段即 `blocked`（`detail` 说明缺哪个命令），**不产生候选行**，也不写 `failed` | 把「环境缺 git」误记成「候选有问题」 |
| `SKILLROUTER_ENGINE_TOOLCHAIN=local` | 仅**开发机**可用（`SKILLROUTER_ENV=development` 时生效），保留原 `subprocess` 路径，要求本机有 `git`/`npm` | 生产环境填 `local` 会被忽略并记 `log.warning("toolchain_local_ignored")`；生产镜像里没有 node，硬走 local 只会全 `blocked` |

```bash
# 自检：网关容器里到底有没有构建所需的两个命令
docker compose -f deploy/docker-compose.yml exec gateway sh -c 'node -v; npm -v; git --version'
# 期望三行都有版本号；缺 git → 说明镜像不是本轮版本（deploy/Dockerfile.gateway 已加 git）
```

> 两个变量必须在 api/worker/gateway **三侧同值**（`SKILLROUTER_ENGINE_HOME_ROOT` ↔ `AGENT_ENGINE_HOME_ROOT`、
> `AGENT_ENGINE_RUNTIME_DIR`）：只改一侧时「引擎装了但没生效」会以最难排查的形式复发（§A.3.7-2）。

若当前是 `inline` 模式，`POST /internal/admin/reload` 会**诚实返回** `{ok:false, stage:"rejected"}`
（`import()` 缓存导致不可能热替换）。此时升级只能「停容器 → 改 pin → 起容器」，
而这属于已知限制，不是回滚能力。

## 1. 流水线（状态机）

```
detected → verify_only? ─┬─► built ─► verified ─► deployed ─► healthy
                         └─► verified（只验证，不部署）
    任一步失败 ─► failed →（自动）rollback_if_deployed → previous_active
```

每步状态记在 `engine_upgrade_runs.steps`（`pending|running|passed|failed|skipped`），
同一时刻最多一个 `running`（唯一部分索引），期间新的升级请求返回 `409 UPGRADE_IN_PROGRESS`。

### 1.1 检测（`upgrade/detect.py`）

- 通道 A（git，权威）：`GET https://api.github.com/repos/xdrshjr/AragonAgent/releases`，
  稳定通道取 `core-v*` tag；失败回退 `git ls-remote --tags`。记录 `version`、`commit_sha`（tag 解引用）、`published_at`。
- 通道 B（npm，可部署工件）：`GET https://registry.npmjs.org/@aragon-agent/core` 的
  `dist-tags.latest` 与 `dist.integrity`（sha512）；`@aragon-agent/cli` 同样处理。
- provenance 分级：

| provenance | 判定 | 处理 |
|---|---|---|
| `tagged` | git tag 与 npm 版本 + commit 一致 | 默认策略下可自动部署（最高可信） |
| `npm-only` | 只有 npm，无对应 tag | 候选可用，但**必须**完整性校验 + 隔离构建，UI 标注「上游无 tag」 |
| `branch-commit` | `ENGINE_CHANNEL=branch:<name>` | 锁定 commit，UI 标注分支名 |

调度：每 `SKILLROUTER_ENGINE_CHECK_INTERVAL_HOURS`（默认 24h）或手动 `POST /api/admin/engine/check`。

### 1.2 隔离构建（`upgrade/build.py`）

目录 `var/engine-builds/<version>-<provenance>/`。**两阶段安装（v2 修正，别退回旧写法）**：

```bash
# ① 构建阶段：必须含 devDependencies（typescript 在里面），--ignore-scripts 仍禁止任意 postinstall
npm ci --ignore-scripts
npm run build -w packages/core

# ② 裁剪阶段：在独立目录生成仅运行时依赖，避免运行目录里带编译器
npm ci --omit=dev --ignore-scripts
```

产物：`dist/` + 裁剪后的 `node_modules/` + `build-manifest.json`
`{version, commit, integrity, node, deps_lock_sha256, pruned_lock_sha256, built_at, adapter_version}`。
构建**在网关容器内**的子进程执行（api/worker 镜像没有 node/npm/git，见 §0.1），
超时 15 分钟；stdout/stderr 脱敏后写 `engine_upgrade_runs.steps[built].detail`（保留末尾 200 行）。

### 1.3 验证（`upgrade/verify.py`，artifact-first 三态）

唯一输入是**候选产物目录** `engine-builds/<version>-<provenance>/runtime`，
不再读 `agents/aragon-gateway` 源码（`api` 镜像里那条相对路径根本不存在）。
检查项固定 7 个、顺序固定，每项 `passed | failed | blocked` 且 `detail` 必填（`skipped` 已从类型里删除）：

| 检查项 | 通过条件 | 不通过的后果 |
|---|---|---|
| 构建产物存在 | `<artifact>/packages/core/dist/index.js` 存在且非空 | `failed` |
| 构建清单一致 | `build-manifest.json` 的 `version/commit/integrity` 与候选一致 | `failed` |
| 导出清单兼容 | 冻结符号（`AragonAgent/Agent/defineTool/initProviders/getProviderRegistry`）都在；**删除 → failed**；新增 → `needs_adapter_review`（记录，不阻断） | `failed` |
| 事件映射一致 | `schemaVersion` 未变、`AgentEvent` 成员无删除 | `failed` |
| 工具清单 | 上游新增宿主工具不得自动进入白名单 | `failed` |
| 状态兼容 | `sessions/*.json` 可读；结构变化 → `needs_migration` | `failed` / 阻断自动部署 |
| 冒烟调用 | 廉价模型 `prompt("reply with OK")` 收到 assistant + 完成事件，< 60s | 无模型 / 网关不可达 → `blocked`（UI 必须明示「未做真实推理验证」） |

`verify_status = failed if 任一 failed else (blocked if 任一 blocked else passed)`；
`needs_adapter_review` = 被判 `blocked` 的检查项名列表。
报告落 `engine_candidates.verification_report`（每项 `{check, status, detail}`）。

### 1.4 切换（`upgrade/switch.py`）

1. **先备份**：`backup_job.quick_backup(label="pre-upgrade")`（DB + manifest，**纯标准库**）。
   失败 → `stage="backup"`、`backup_file_id=null`、**不写指针、不装 home**，直接 abort
   （「升级后才发现没有回滚点」是 §A.3.7-3 明确要拦住的场景）；
   需要连引擎目录一起留档时，手工另跑：
   `python scripts/backup.py --label pre-upgrade --include var/aragon-home --include var/engine-runtime`；
2. `install_home.install(artifact_dir, version)` →
   `AGENT_ENGINE_HOME_ROOT/<version>/node_modules/@aragon-agent/core`（幂等）；
3. 写 `engine_deployments`（新版本 `pending`），更新指针文件 `var/engine-runtime/current.json`
   （**指针文件是唯一实现**，不依赖 symlink/junction —— Windows 上创建符号链接需要额外权限）；
4. `POST http://<gateway>/internal/admin/reload {"version": "<v>", "engine_home": "<home>"}`，网关内部四步：
   ① 用目标 pin 起新 `engine-host` 子进程 →
   ② 等它自报 `ready`（含 `engine.version` 与工具清单摘要）→
   ③ `abort()` + `waitForIdle()` 排空旧子进程（60s 上限，超时强制 abort 并标 `turn_stopped`）→
   ④ 关闭旧子进程，更新 `/healthz` 报告的版本；
5. 健康检查：`GET /healthz` 连续 **3 次**通过（间隔 2s）且 `engine.version === 期望版本`，
   再跑一次最小 `prompt` 断言完整事件流（无模型则 `blocked` 并明示）；
6. 成功：新版本 `active`、旧版本 `previous`，更新 `system_state.engine_pin` / `engine_previous_pin`
   与 `engine_upgrade_runs.backup_file_id`，`result=success`，审计 `engine.switched`；
7. 失败：立即 `rollback.py` —— 再调 `reload {"version": <previous>}` → 健康检查 →
   新版本 `failed`、旧版本恢复 `active` → `result=failed` → 审计 `engine.rolled_back` + 概览高优待办。

**`result` 与 `verify_status` 是两件事**（§A.6.2 I5，代码里也是分开写的）：

| 字段 | 取值 | 谁写 | 含义 |
|---|---|---|---|
| `verify_status` | `passed / blocked / failed` | 验证阶段 | **候选产物**的性质：这个版本本身能不能被验证通过 |
| `result` | `success / failed / aborted` | 切换阶段 | **本次操作**的结果：这次升级做成了没有 |

所以 `verify_status=blocked` + `result=success` 是合法组合（管理员确认风险后手动切换，见 §1.6）。

**切换失败的语义**：`{ok:false, stage, detail}` 且 **pin 不变**、旧子进程继续服务。
回滚是同一进程内完成的，不需要重启容器。

### 1.5 升级期间的调用

已发布、且 `requires_agent_reasoning=false` 的能力**完全不经过网关**（执行在 API 进程内），
因此升级期间成功率应为 100%（§15.3 的 `test_calls_during_upgrade`）。
只有 `requires_agent_reasoning=true` 的能力与浮窗 Agent 会受切换影响（活跃轮次被收尾并标 `turn_stopped`）。

### 1.6 `blocked` 是什么意思，怎么消掉（F-26）

`blocked` 不是「候选有问题」，而是「**这次我们没能验证它**」——环境缺工具链、网关不可达、
没有可用模型跑冒烟、上游无 tag 导致完整性无法确认……。所以它是**中性结论**，与 `failed` 严格区分：

| verify_status | 含义 | 自动部署（`policy=auto`） | 手工部署 |
|---|---|---|---|
| `passed` | 所有检查项都跑到并通过 | 允许 | 允许 |
| `blocked` | 至少一项**没能验证**（`blocked_reasons` 列出原因） | **禁止**（进「待升级」待确认） | 需 `acknowledge_blocked=true` 二次确认 |
| `failed` | 某一项**跑到且不通过**（导出被删、事件字段被改…） | 禁止 | 禁止（`failed` 不给绕过） |

查看与二次确认：

```bash
# 1) 看结论 + 阻断原因（blocked_reasons 是数组，每项一句话）
curl -sS -b cookies.txt https://router.example.com/api/admin/engine/status
# → {..., "pin_source":"pointer","resolved_path":"/data/aragon-home/engines/0.2.34/node_modules/@aragon-agent/core",
#        "verify_status":"blocked","blocked_reasons":["工具链不可用：网关返回缺少命令 git"], ...}

# 2) 确认风险后手动部署（不带 acknowledge_blocked 会 400 VALIDATION_FAILED）
curl -sS -b cookies.txt -X POST https://router.example.com/api/admin/engine/upgrade \
  -H "Content-Type: application/json" -H "X-CSRF-Token: <csrf>" \
  -d '{"version":"0.2.37","mode":"deploy","acknowledge_blocked":true}'
```

`blocked_reasons` 的常见取值与消法：

| `blocked_reasons`（示例） | 根因 | 消掉的方式 |
|---|---|---|
| `工具链不可用：缺少命令 git` / `网关不可达` | 网关镜像是旧版本，或网络/端口变更 | 用本轮镜像重建网关（`Dockerfile.gateway` 已加 `git`）；`docker compose exec gateway sh -c 'node -v; git --version'` 自检 |
| `冒烟调用未执行：未配置可用模型` | 设置页没配模型 | 配一个廉价模型后重跑 `mode=verify_only` |
| `完整性校验无法完成：上游无对应 tag`（`npm-only`） | 上游只发 npm 没打 tag | 接受风险并 `acknowledge_blocked=true`，或等上游发 tag |
| `出站策略为 deny` | `SKILLROUTER_EGRESS_POLICY=deny` | 手工上传 tarball 升级；或临时改 `allow`（会放开版本检测出站） |

> 纪律：`blocked` 的候选**不会**被计入 `passed`，`engine_candidates.verification_status` 里也已
> 删除 `skipped` 取值（历史行按 `blocked` 读取）。任何「把 blocked 当 passed」的展示都属于缺陷。

## 2. 手工演练（每次发布前至少一次）

### 2.1 只验证不部署（最安全的一次演练）

```bash
# 1) 立即检测候选
curl -sS -b cookies.txt -X POST https://router.example.com/api/admin/engine/check \
  -H "X-CSRF-Token: <csrf>"
curl -sS -b cookies.txt https://router.example.com/api/admin/engine/candidates

# 2) 只跑流水线到 verified（不部署）
curl -sS -b cookies.txt -X POST https://router.example.com/api/admin/engine/upgrade \
  -H "Content-Type: application/json" -H "X-CSRF-Token: <csrf>" \
  -d '{"version":"0.2.34","mode":"verify_only"}'

# 3) 看检查项明细
curl -sS -b cookies.txt https://router.example.com/api/admin/engine/runs/<run_id>
```

### 2.2 部署一次并回滚（把回滚也练一遍）

```bash
# 部署
curl -sS -b cookies.txt -X POST https://router.example.com/api/admin/engine/upgrade \
  -H "Content-Type: application/json" -H "X-CSRF-Token: <csrf>" \
  -d '{"version":"<候选版本>","mode":"deploy"}'

# 直接看网关侧真相（在 api 容器内执行，用内部令牌）
docker compose -f deploy/docker-compose.yml exec api python -c \
"import os,urllib.request,json;r=urllib.request.Request('http://gateway:8500/healthz');print(json.load(urllib.request.urlopen(r)))"

# 回滚
curl -sS -b cookies.txt -X POST https://router.example.com/api/admin/engine/rollback \
  -H "Content-Type: application/json" -H "X-CSRF-Token: <csrf>" \
  -d '{"to_version":"0.2.34"}'
```

### 2.3 直接验证网关的热切换（不经过平台 UI）

```bash
# 在 api 容器内（同一内部网络）；`version` 也可写成兼容别名 `target_version`
docker compose -f deploy/docker-compose.yml exec api python - <<'PY'
import json, os, urllib.request
payload = json.dumps({"version": "0.2.34"}).encode()
req = urllib.request.Request(
    "http://gateway:8500/internal/admin/reload", data=payload,
    headers={"Content-Type": "application/json",
             "X-Internal-Token": os.environ["SKILLROUTER_AGENT_GATEWAY_SECRET"]},
    method="POST")
print(json.load(urllib.request.urlopen(req, timeout=180)))
PY
```

期望：`{"ok":true,"engine_version":"0.2.34","stage":"ready",...}`，并且随后连续 3 次
`/healthz` 都报同一版本。若返回 `{ok:false,...}`：pin 未变、旧子进程仍在服务，属预期安全失败。

## 3. 记录与审计

| 位置 | 内容 |
|---|---|
| `engine_candidates` | `version / commit_sha / provenance / source_url / integrity / published_at / verification_report` |
| `engine_upgrade_runs` | `steps[]`（每步状态与脱敏详情）、`result`、`backup_file_id`、耗时 |
| `engine_deployments` | `active / previous / failed`、切换时间、健康检查结果 |
| `system_state` | `engine_pin` / `engine_previous_pin` |
| `audit_logs` | `engine.upgrade_started / verified / switched / rolled_back` |
| 概览待办 | 失败时生成高优待办（`ENGINE_CANDIDATE` / 升级失败条目） |

## 4. 排错

| 现象 | 可能原因 | 处置 |
|---|---|---|
| `{ok:false, stage:"rejected"}` | 网关是 `inline` 模式 | 改 `AGENT_GATEWAY_ENGINE_MODE=host` 后重启网关（这是一次性动作，不是每次升级） |
| `verify_status=blocked`，`blocked_reasons` 说缺 `git`/网关不可达 | 工具链归属没落实（旧网关镜像 / 网络变更） | 见 §0.1 与 §1.6：重建网关镜像（含 `git`），再 `mode=verify_only` 重跑；确认风险后可 `acknowledge_blocked=true` 部署 |
| `verify_status=blocked`，说未做冒烟 | 没配可用模型 | 配廉价模型后重跑；验收记录里必须写明「本轮未做真实推理验证」 |
| `stage:"spawn"` 失败 | 目标 pin 未安装到 `AGENT_ENGINE_HOME_ROOT/<version>/`（容器内 `/data/aragon-home/engines/<version>/`） | 先跑隔离构建/`install_home`；确认目录存在且属主为 uid 1000 |
| `stage:"ready"` 超时 | 子进程启动失败（缺依赖、ESM 导出变化） | 看 `engine-host` 的 stderr（`/data/logs`）；回滚已自动完成 |
| 健康检查 3 次内版本不匹配 | 指针文件与子进程 pin 不一致 | 查 `/data/engine-runtime/current.json` 与 `GET /api/admin/engine/status` 的 `pin_source`（`pointer`/`env`）、`resolved_path`；重跑 reload |
| `409 UPGRADE_IN_PROGRESS` | 已有 running 升级 | 等结束；崩溃残留需人工改 `result=failed` 后再试（不会自动重投） |
| npm 版本比 tag 新（常见的 `npm-only`） | 上游发布节奏 | 走隔离构建 + 完整性校验，UI 标注；默认策略不自动部署 `npm-only` |

更多网关侧契约见 [`../../agents/aragon-gateway/README.md`](../../agents/aragon-gateway/README.md)。

## 5. 当前状态（诚实记录）

| 项 | 状态 |
|---|---|
| 网关 `engine-host` + `/internal/admin/reload` 四步实现 | 已实现（host/inline 两种模式均可启动；`reload` 见网关 README） |
| 平台侧 `upgrade/`（detect / toolchain / build / verify / install_home / switch / rollback / gc） | 已实现，**脚本化流水线可在本机闭环**（单元/契约级证据见 [verification-checklist.md](verification-checklist.md)） |
| 工具链归属（构建在网关容器，`SKILLROUTER_ENGINE_TOOLCHAIN=gateway`） | 已实现（`deploy/Dockerfile.gateway` 已装 `git`；`local` 仅 development 生效） |
| 真实候选版本升级（部署 + 回滚各一次） | **待验证（P11）** —— 尚未做过一次真实上游版本的升级+回滚；命令与输出必须登记在 [verification-checklist.md](verification-checklist.md) |
| `auto` 策略下的无人值守升级 | **未启用**，建议先长期用 `notify`，等演练记录齐了再评估 |
