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
| 备份 | 切换前自动备份（DB + `var/aragon-home` + 网关 lock） | `engine_upgrade_runs.backup_file_id` |
| 廉价模型 | 冒烟调用需要可用模型；没有则该项 `skipped`（不阻断，但 UI 必须明示「未做真实推理验证」） | 设置页 Agent 配置 |

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
构建在子进程执行，超时 15 分钟；stdout/stderr 脱敏后写 `engine_upgrade_runs.steps[built].detail`（保留末尾 200 行）。

### 1.3 验证（`upgrade/verify.py` + `agents/aragon-gateway/test/conformance.test.ts`）

| 检查项 | 通过条件 | 失败后果 |
|---|---|---|
| 导出清单兼容 | 与冻结的 86 个导出对比：新增 → `needs_adapter_review`（记录，不阻断）；**删除/改名 → failed** | 阻断部署 |
| 事件字段兼容 | `schemaVersion` 未变、`AgentEvent` 成员无删除 | 阻断部署 |
| 工具清单 | 上游新增宿主工具不得自动进入白名单（`policy.test.ts` 断言） | 阻断部署 |
| 适配层编译 | 网关 `tsc --noEmit` + `vitest run` 全绿 | 阻断部署 |
| 冒烟调用 | 廉价模型 `prompt("reply with OK")` 收到 assistant + 完成事件，< 60s | 无模型时 `skipped`，UI 明示 |
| CLI 契约（可选） | `aragon info --json` 的 `schemaVersion===1`、`features` 含 `exec,sessions,permissions,budgets` | 记录 |
| 数据兼容 | `sessions/*.json` 可读；结构变化 → `needs_migration` | 阻断自动部署 |

报告落 `engine_candidates.verification_report`（每项 `{check, status, detail}`）。

### 1.4 切换（`upgrade/switch.py`）

1. 备份：DB + `var/aragon-home` + 网关 `package-lock.json`（用 §7.4 的纯 Python 备份）；
2. 写 `engine_deployments`（新版本 `pending`），更新指针文件 `var/engine-runtime/current.json`
   （**指针文件是唯一实现**，不依赖 symlink/junction —— Windows 上创建符号链接需要额外权限）；
3. `POST http://<gateway>/internal/admin/reload {"target_version": "<v>"}`，网关内部四步：
   ① 用目标 pin 起新 `engine-host` 子进程 →
   ② 等它自报 `ready`（含 `engine.version` 与工具清单摘要）→
   ③ `abort()` + `waitForIdle()` 排空旧子进程（60s 上限，超时强制 abort 并标 `turn_stopped`）→
   ④ 关闭旧子进程，更新 `/healthz` 报告的版本；
4. 健康检查：`GET /healthz` 连续 **3 次**通过（间隔 2s）且 `engine.version === 期望版本`，
   再跑一次最小 `prompt` 断言完整事件流（无模型则 `skipped` 并明示）；
5. 成功：新版本 `active`、旧版本 `previous`，更新 `system_state.engine_pin` / `engine_previous_pin`，
   `result=success`，审计 `engine.switched`；
6. 失败：立即 `rollback.py` —— 再调 `reload {"target_version": <previous>}` → 健康检查 →
   新版本 `failed`、旧版本恢复 `active` → `result=failed` → 审计 `engine.rolled_back` + 概览高优待办。

**切换失败的语义**：`{ok:false, stage, detail}` 且 **pin 不变**、旧子进程继续服务。
回滚是同一进程内完成的，不需要重启容器。

### 1.5 升级期间的调用

已发布、且 `requires_agent_reasoning=false` 的能力**完全不经过网关**（执行在 API 进程内），
因此升级期间成功率应为 100%（§15.3 的 `test_calls_during_upgrade`）。
只有 `requires_agent_reasoning=true` 的能力与浮窗 Agent 会受切换影响（活跃轮次被收尾并标 `turn_stopped`）。

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
# 在 api 容器内（同一内部网络）
docker compose -f deploy/docker-compose.yml exec api python - <<'PY'
import json, os, urllib.request
payload = json.dumps({"target_version": "0.2.34"}).encode()
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
| `stage:"spawn"` 失败 | 目标 pin 未安装到 `var/aragon-home/engines/<version>/` | 先跑隔离构建；确认目录存在且属主为 uid 1000 |
| `stage:"ready"` 超时 | 子进程启动失败（缺依赖、ESM 导出变化） | 看 `engine-host` 的 stderr（`var/logs`）；回滚已自动完成 |
| 健康检查 3 次内版本不匹配 | 指针文件与子进程 pin 不一致 | 检查 `var/engine-runtime/current.json`；重跑 reload |
| `409 UPGRADE_IN_PROGRESS` | 已有 running 升级 | 等结束；崩溃残留需人工改 `result=failed` 后再试（不会自动重投） |
| 冒烟项 `skipped` | 未配置可用模型 | 该项不算失败，但**必须**在验收记录里写明「未做真实推理验证」 |
| npm 版本比 tag 新（常见的 `npm-only`） | 上游发布节奏 | 走隔离构建 + 完整性校验，UI 标注；默认策略不自动部署 `npm-only` |

更多网关侧契约见 [`../../agents/aragon-gateway/README.md`](../../agents/aragon-gateway/README.md)。

## 5. 当前状态（诚实记录）

| 项 | 状态 |
|---|---|
| 网关 `engine-host` + `/internal/admin/reload` 四步实现 | 已实现（网关 owner 报告：host/inline 两种模式均可启动；`reload` 行为见其 README） |
| 平台侧 `upgrade/`（detect/build/verify/switch/rollback） | 按 §8.2 清单推进中，**待验证** |
| 真实升级演练（部署 + 回滚各一次） | **待验证** —— 未做过一次真实候选版本升级，验收时必须在 [verification-checklist.md](verification-checklist.md) 登记命令与输出 |
| `auto` 策略下的无人值守升级 | **未启用**，建议先长期用 `notify`，等演练记录齐了再评估 |
