# Skill Router 桌面端（Electron）实现契约 v2.0

| 项 | 值 |
|---|---|
| 文档版本 | **v2.0**（v1.0 → v2.0：修复评审中全部 P0/P1 共 17 项，详见下方 `## 评审记录`；结论见文末 `## 评审结论`） |
| 文档状态 | **可实施**（本文件即实现契约；实现不得偏离，偏离需在 `implementation-notes.md` 登记） |
| 目标需求 | 「给这个项目开发桌面端 electron，并且确保桌面端成功被打包，并且直接双击可用」 |
| 生产者 | 设计节点（node #3，round 1）→ 评审节点（node #4，round 1，v2.0 定稿） |
| 消费者 | 实现 / 打包 / 验证 / 复审节点，以及后续维护者 |
| 交付物 | `apps/desktop/`（新增，纯增量）+ **`apps/desktop/release/SkillRouter-Setup-0.1.0-win-x64.exe`**（双击可用；仓库内相对路径以 `apps/desktop` 为根时写作 `release/…`，两者同指一个文件，见 §2.13） |
| 相关既有契约 | [`docs/plans/skill-router/spec.md`](../skill-router/spec.md)（后端/前端/网关以它为准，本文件**不覆盖**它） |
| 证据口径 | 沿用仓库规则：**没有证据不许写「已验证」**。本文件所有「验收」条目都必须给出命令 + 输出 + 时间 + 环境，登记进 `docs/runbook/verification-report.md` |

---

## 评审记录

评审对象：v1.0 全文（概述 / §2 技术设计 12 节 / §3 文件清单 / §4 接口 / §5 数据模型 / §6 验收 / §7 风险 / §8 顺序）。
评审维度：**可行性**（能否用当前栈真正构建出来）、**完整性**（边界/错误路径/发布面）、**一致性**（是否与既有约定冲突）、**规模合理性**（过设计 / 欠定义）。
证据来源：`apps/api/skillrouter_api/{settings,main}.py`、`services/bootstrap.py`、`worker/runner.py`、`upgrade/toolchain.py`、`api/admin/auth.py`、`apps/web/next.config.mjs`、`.next/routes-manifest.json`、`agents/aragon-gateway/src/{config,main,platform-client}.ts`、`agents/aragon-gateway/package.json`、`README.md`、本机 `%LOCALAPPDATA%` 缓存。

> 处置口径：**P0 = 不修则「双击可用」必然失败或产生假成功**；**P1 = 不修则会返工或留下不可验证的验收项**；**P2 = 记录在案，本轮不改**（不阻塞出包）。
> 全部 P0/P1 已在正文修复，修复位置写在「落点」列。P2 只登记，不做设计变更。

| 编号 | 严重度 | 位置（v1.0） | 问题（含证据） | 落点 |
|---|---|---|---|---|
| **P0-1** | P0 | §2.6 环境合成表 | `SKILLROUTER_INTERNAL_URL` 的作用对象只写了「api」。实际上**网关**也读它：`agents/aragon-gateway/src/config.ts:231` `str(env,'SKILLROUTER_INTERNAL_URL','http://127.0.0.1:8000')`，并在 `main.ts:145` 传给 platform client（`platform-client.ts:136` 用它拼 `/internal/...` 并带 `X-Internal-Token`）。桌面端若不注入，网关会去连 **8000**（开发态端口）→ 所有 `sr_*` 工具调用、SSE 回传、制品校验全部 connection refused，而 `/healthz` 仍然 200 → **典型的绿灯假成功** | §2.6（重写该行 + 新增「继承变量一律剔除」规则） |
| **P0-2** | P0 | §2.7 Python 边车 | `--role api` 用 `uvicorn.run("skillrouter_api.main:app", …)` —— **import 字符串**。PyInstaller 的静态分析不会把字符串当导入，`skillrouter_api.main` 不在依赖图里 → 冻结后 `--role api` 必抛 `ModuleNotFoundError: skillrouter_api.main`。v1.0 的 `hiddenimports` 只列了 `worker.runner` / `worker.handlers` 等，**没有 `skillrouter_api.main`**；而 §2.7 的自检**只跑 `--role bootstrap`**，恰好绕开这条路径，因此这个必崩的缺陷不会被构建期发现 | §2.7（launcher 显式 import + hiddenimports + 自检升级为「bootstrap + api 探活」双检） |
| **P0-3** | P0 | §2.7 / §2.11 / R5 | 迁移不一致在桌面端**不致命**：`bootstrap.assert_migrated()` 只在 `settings.env == "production"` 时抛错（`services/bootstrap.py:325`），其余环境仅 `log.warning`。而 D7 已决定桌面端用 `development`。R5 声称「`E-D03` 在 bootstrap 失败时仍然阻断启动（自实现『致命』语义）」，但 v1.0 全文**没有任何落点**实现这个「自实现」——于是「库 schema 不对」时 bootstrap 返回 0、api 照常起来、控制台带着错版库运行，坏数据无告警 | §2.7（launcher 在 `--role bootstrap` 尾部追加与 env 无关的硬校验 + 专用退出码）、§4.5（`E-D03` 触发面写明） |
| P1-4 | P1 | §1 / §2.13 / §3#41 / §6.2 | 产物路径二义：§1 写 `release/SkillRouter-Setup-0.1.0-win-x64.exe`（读起来是仓库根），§2.13 的 `directories.output: release` 是**相对 `apps/desktop`**，§3#41 的 `.gitignore` 忽略的是 `apps/desktop/release/`。同一文件三种写法，出包后「找不到产物」 | 文首交付物行、§2.13（显式写全路径 + 唯一口径）、§6.2 |
| P1-5 | P1 | §6.2 | `verify` 声称「`skillrouter-server.exe --version` 可执行」，但 §2.7 的 launcher `argparse` **根本没有 `--version`**（`choices` 只有 role）→ 该断言必然非 0 退出，verify 恒红；若实现者为让它变绿而忽略退出码，则 verify 变成假检查 | §2.7（launcher 增加 `--version`）、§4.1、§6.2 |
| P1-6 | P1 | §2.7 构建自检 | 自检命令在**仓库根**（cwd）运行，而 `Settings.model_config` 带 `env_file=".env"`（`settings.py:14-20`）且仓库根**确实存在 `.env`**（1203 B）。于是自检会静默吃到仓库的 `.env`，可能改写 `SKILLROUTER_DATA_DIR` / `SKILLROUTER_ENV` 等，使「自检绿」与「安装包行为」不是同一套配置 —— 这正是 D8 自己警告过的「第二个真相源」，v1.0 的构建脚本却踩了 | §2.7（自检改为独立 cwd + 清空继承 `SKILLROUTER_*`/`AGENT_*`/`PYTHON*`） |
| P1-7 | P1 | §2.11 步骤 4 | `desktop.json.adminInitialized` 被说成「用于决定登录页是否显示『首次登录：admin / adminjr』提示卡」。登录页属于 `apps/web`，改它直接违反 **INV-1**，而 §4.2 又明确「控制台前端不消费这些方法」。**自相矛盾**，且会把实现者推向改前端 | §2.11（提示改在 splash / 错误窗口 / README 表达）、§4.2 |
| P1-8 | P1 | §0 INV-3 vs §2.5 | INV-3 承诺「90 秒内看到登录页」，但状态机最坏路径是 `BOOTSTRAP 120 s + API 90 s + GATEWAY 40 s + WEB 120 s`。首启（要跑迁移 + 建管理员 + 起 Next）很容易越过 90 s，验收口径与设计参数**互相矛盾**，实现者无从判定「合格」 | §0（INV-3 补「冷启/热启」两种口径与测量方式）、§2.5（超时是上限不是预期）、§6.3 M3 |
| P1-9 | P1 | §2.7 / §2.10 | `EXE(..., console=True)` 的 sidecar 由 Electron `spawn` 拉起时，v1.0 从未要求 `windowsHide: true`。Windows 上这会给每个 Python 子进程弹一个控制台黑窗（api + worker 两个），双击启动体验直接崩坏 | §2.10（所有 spawn 统一 `windowsHide: true`，并给出唯一 spawn 入口） |
| P1-10 | P1 | §4.4 `clean_shutdown` | 该检查项写进**被测进程自己写出**的 `smoke-report.json`：而「有没有留孤儿进程」只有在被测进程**退出之后**才能观察。让进程自证「我死后没有孤儿」在因果上不成立 → 这一项必然被写成常量 `true`，是 v1.0 里最脆的一条假证据 | §4.4（该项移交 `scripts/smoke.mjs` 外壳执行）、§6.2 |
| P1-11 | P1 | §2.3 vs §2.5 vs §4.5 | 同一件事两套机制：`app.requestSingleInstanceLock()`（§2.10）与「18100 上 `/healthz` 是 `skillrouter-api` → 唤起已有实例」两处都在处理「第二次双击」，且 §2.5 说 `E-D06` 「不算错误」而 §4.5 把它列为错误码。谁先谁后、什么情况下会走到端口认领，全文没定义 | §2.3（定义优先级与唯一判据）、§2.5、§4.5 |
| P1-12 | P1 | §2.6 | 未固定 `SKILLROUTER_ENGINE_TOOLCHAIN`。默认值是 `gateway`（`settings.py:59`、`upgrade/toolchain.py:47`），但若用户机器上残留 `SKILLROUTER_ENGINE_TOOLCHAIN=local` 环境变量，`resolve()` 在 `development` 下会**接受** `local`（`toolchain.py:49`）→ 走本机 `npm/git` 路径，而安装包里没有 npm/git → 引擎构建必然失败，且只在「升级引擎」这条冷路径上炸 | §2.6（显式钉死 + 剔除继承变量） |
| P1-13 | P1 | §2.7 launcher | `--data-dir` 与已存在的 `SKILLROUTER_DATA_DIR` 优先级未定义：launcher 用 `os.environ.setdefault(...)`，于是**环境变量悄悄赢**，`--data-dir` 变成静默无效参数。冒烟与排错都依赖这个覆盖参数，静默失效会让「换目录复现」变成假动作 | §2.7（`--data-dir` 显式赋值覆盖，并写进 §4.1 语义） |
| P1-14 | P1 | §3#41 / §7 R12 | `.gitignore` 只追加了 `release/ build/ dist/ resources/` 四条，**漏了 `apps/desktop/node_modules/`**（electron 一套 ≥ 300 MB，含 `electron/dist` 二进制）。第一次 `npm i` 之后 `git status` 会瞬间出现上万文件 | §3#41、R12 |
| P1-15 | P1 | §2.5 `preparing` / §4.1 | `--dev` 与 `--smoke-test` 与资源校验的关系未定义：`preparing` 强校验 `resources\python|web|gateway` 三件产物，而 `--dev`（指向 `localhost:3000`）与纯 UI 开发态**根本没有这些产物** → 必然 `E-D02`。`--smoke-test` 在安装版与 `win-unpacked` 下路径也不同 | §2.5（`preparing` 分模式）、§4.1 |
| P1-16 | P1 | §2.7 构建环境 | 构建机（本机，`os.release()=10.0.26200`）的 `python` 解析到 **Anaconda 3.11.7**（`packaged by Anaconda, Inc.`）。用 Anaconda 解释器跑 PyInstaller 会把这套 conda DLL / `python3x.dll` 依赖烘进产物，在「没装 Python 的干净机」上表现为启动即缺 DLL —— 直接威胁 INV-3。v1.0 只写了 `python -m PyInstaller`，没有约束解释器来源 | §2.7（构建用纯净 CPython 3.11 venv；`verify-artifact` 断言净化 PATH 下可跑）、R13 |
| P1-17 | P1 | §2.8 build 顺序 / R4 | R4 把「rewrite 端口漂移」的防线放在 `verify-artifact.mjs` 事后 grep，**运行时没有任何校验**。而 `desktop.json.apiPort` 是可被外部编辑的文件（§5.1 只写「UI 不可改」），一旦与 `routes-manifest.json` 里烧死的目标不一致，运行时表现就是「页面能开、`/api` 全 502」——R4 自己定义的失效模式没有任何运行期拦截，也没有给用户可执行的错误码 | §2.5（`preparing` 增加端口契约校验）、§2.3、§4.5 新增 `E-D07`、§2.8 |
| P2-1 | P2 | §2.7 hiddenimports | `uvicorn.protocols.websockets.auto` 与 `ws="none"` 语义相反，且会拉入 `websockets`/`wsproto`。已顺手删掉（同时 `excludes` 掉 `httptools`/`uvloop`/`websockets`/`watchfiles`，减体积减误杀面），但归类为 P2，因为它不影响正确性 | §2.7（已顺带修正） |
| P2-2 | P2 | §2.7 datas 说明 | 「子进程 cwd 一律为仓库根，只为让 spec 里的相对 `datas` 语义稳定」不准确：PyInstaller spec 里的相对 `datas` 相对**spec 文件所在目录**（`SPECPATH`）解析，只有 `requirements-build.txt` 的 `-r ../../…` 才依赖 cwd。已在 §2.7 更正，避免实现者用错基址 | §2.7 |
| P2-3 | P2 | §2.10 | 未调用 `app.setAppUserModelId('com.skillrouter.desktop')`：Windows 上任务栏分组、通知归属会落到「electron.app.Electron」，与 `appId` 不一致 | 记录（实现时顺手补，不影响验收） |
| P2-4 | P2 | §2.10 | `SHUTDOWN_GRACE_MS=5_000` 对 uvicorn lifespan + SQLite WAL checkpoint 偏短，`taskkill /F` 兜底会放大 `DB_BUSY` 概率。已改为 8 000 并明确「等 exit 事件再 taskkill」 | 记录（顺带调整） |
| P2-5 | P2 | §2.9 | `AGENT_GATEWAY_ENGINE_MODE=auto` 可能落 `host` 模式，即网关再 spawn 一个 engine 子进程（孙进程）。§2.10 的关停编排只按 `pid` 杀树的直接子节点，需要 `/T` 才能覆盖孙进程——已在 §2.10 写明「必须 `/T`，且逐一枚举已记录 pid」 | 记录（已澄清） |
| P2-6 | P2 | 全文 | 未显式声明**非目标**（自动更新 / 多用户 / 代码签名 / 生产可用），容易被下游读成「桌面端=生产部署」 | §9 补一行 |
| P2-7 | P2 | §6.1 | `ports.test.ts` 只测「api 端口被占用 → E-D01」，没测「占用者是 SkillRouter → 认领已有实例」这条分支；建议实现时补 1 例（不阻塞） | 记录 |
| P2-8 | P2 | §2.5 / §4.4 | 单实例锁与 `--smoke-test` 互斥未说明：正常实例在跑时冒烟会直接 `app.quit()`。建议冒烟前检测并提示（不阻塞） | 记录 |
| P2-9 | P2 | §5.2 | 日志 `10 MB × 5 轮转 × 6 路` ≈ 上限 300 MB，runbook 未写磁盘预算 | 记录 |

**评审结论摘要**：可行性、规模合理性达标；完整性与一致性共有 3 个 P0 + 14 个 P1，全部已在正文修复，逐条落点如上表。P2 共 9 项，只登记不改设计。

---

## 0. 阅读方式与三条不变量

本文件只解决一件事：**把已经在 Windows 上跑通的「api + worker + 网关 + 控制台」四进程结构，装进一个双击就能用的 Electron 应用里，并且打包产物可复现。**

实现时请把下面三条当作硬不变量（违反任何一条都算实现缺陷，不是「风格问题」）：

- **INV-1 增量优先**：桌面端**只允许新增**文件。除 `.gitignore`（追加忽略项）、`docs/README.md` / `README.md`（追加一节指路）、以及可选的 `Makefile` / `make.ps1` / CI 追加 job 外，**不得修改** `apps/api/**`、`apps/web/**`、`agents/aragon-gateway/**`、`packages/**`、`scripts/**` 里的任何既有源码。
  这条不变量是「现有 345 项测试必须保持全绿」的结构性保证：不碰源码，就没有回归面。
- **INV-2 端到端真实**：桌面端启动的是**真实的** FastAPI + 真实 SQLite + 真实网关 + 真实 Next 控制台，不是 mock、不是 iframe 套壳网页、不是「打开浏览器」。控制台里的每一次点击都打到本机真实的 `127.0.0.1:<api>`。
- **INV-3 双击可用**：在一台**没有安装 Python、没有安装 Node.js** 的 Windows 10/11 x64 机器上，双击 `SkillRouter-Setup-0.1.0-win-x64.exe` → 下一步到底 → 桌面快捷方式启动 → 出现登录页 → `admin / adminjr` 登录 → 被要求改密 → 进入控制台。这是唯一可接受的最低可用线。
  **时间口径（评审 P1-8 修正，原文只写「90 秒」与 §2.5 的超时上限自相矛盾）**：
  - **热启**（数据目录已就绪、库已迁移、Defender 已缓存过三个 exe）：从双击到登录页 **≤ 45 s**；
  - **冷启**（首次安装后首次启动：要跑迁移 + 建管理员 + 首次加载 Python/Next 运行时）：**≤ 120 s**；
  - 测量点固定在 `main.log` 的 `state=idle` 与 `state=ready` 两行时间差，登记进 `docs/runbook/verification-report.md`；
  - §2.5 里的 `90 s / 120 s / 40 s` 是**上限（超时即失败）**，不是预期值。**首次冷启超过 120 s 判 INV-3 不达标**，必须查证而不是改口径。

---

## 1. 概述（Overview）

Skill Router 目前是一个**自托管、单管理员**的 AI 能力中枢：FastAPI 提供控制面与 `/api/v1` 调用面，Worker 跑异步队列，Node 边车（`agents/aragon-gateway`）托管 AragonAgent 引擎，Next.js 15 控制台做管理界面。它现在的启动方式是「一个 PowerShell 脚本拉起四个子进程」（`start.ps1`），或者「五个容器」（`deploy/docker-compose.yml`）。这两种方式对**本机单用户**来说都太重：前者要求机器上有 Python 3.11 + Node ≥ 22 且装好四套依赖，后者要求 Docker Desktop（Windows 上还要 WSL2 或 Hyper-V），而且两者都**不是双击就能用**的东西。它们的受众是开发者与运维，不是「我就想在自己电脑上管几个 API 凭证」的用户。

本轮要做的是一个**桌面发行版**：用 Electron 33 做宿主外壳，把「Python 边车（api + worker）」「Node 网关」「Next standalone 服务器」三种运行时**预先构建并随安装包一起分发**，由 Electron 主进程按确定的状态机拉起、探活、监管、优雅收尾，窗口里直接加载本机控制台。用户侧只需一次「下载 → 双击 → 下一步」，之后每次双击图标就进入一个已经在跑的完整控制面；数据（SQLite 库、文件、日志、主密钥、引擎目录）落在 `%APPDATA%\SkillRouter\data`，与开发模式的 `var/` 完全隔离，互不污染。

这项工作的技术难点不在 Electron 本身，而在**把两个异构运行时（Python / Node）打包成可复现、可探活、可收尾的随包资源**，以及**在两处「构建期烧死」的约束下保住端口确定性**：Next.js 的 `rewrites()` 会在 `next build` 时把 `/api` 的目标地址写进 `routes-manifest.json`，运行时改不了；`apps/api` 的 `settings.py` 在模块导入时就通过 `get_settings()` 固定了数据目录与密钥。因此本设计把「api 端口在构建期固定为 18100」写成了显式契约（见 §2.3），并把两侧的路径/密钥一律通过**子进程环境变量**注入，绝不依赖 `cwd` 或 `.env` 文件。换句话说：桌面端不是「再写一个 Electron 壳」，而是给这套四进程系统做一次**发行工程化**——打包、托管、监管、验收四件事都要有可复现的命令和可归档的证据。

最终产物是 `apps/desktop/release/SkillRouter-Setup-0.1.0-win-x64.exe`（NSIS 安装器，约 200~260 MB）与 `apps/desktop/release/win-unpacked/SkillRouter.exe`（免安装目录，用于冒烟与排错）。
> **路径口径（评审 P1-4 修正）**：`electron-builder.yml` 的 `directories.output: release` **相对 `apps/desktop`**。因此全文出现的 `release/…` 一律指 `apps/desktop/release/…`；从仓库根引用时必须写全 `apps/desktop/release/…`。只有这一个口径，不存在仓库根 `release/`。
两者都必须通过同一套 `--smoke-test` 自检：在**去掉 PATH 中 Python/Node 的环境**下仍能拉起全部服务、登录成功并退出码 0。

---

## 2. 技术设计

### 2.1 进程拓扑（运行时）

```text
┌─────────────────────────────────────────────────────────────────────┐
│ SkillRouter.exe（Electron 33.4.11 主进程，唯一「父」）               │
│  · 单实例锁 · 端口分配 · 环境合成 · 状态机 · 日志落盘 · 关停编排    │
└───┬───────────────┬────────────────┬────────────────┬───────────────┘
    │ spawn         │ spawn          │ spawn          │ BrowserWindow
    ▼               ▼                ▼                ▼
┌────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────────┐
│ skillside  │ │ skillside    │ │ ELECTRON_RUN │ │ http://127.0.0.1 │
│ ar-server  │ │ ar-server    │ │ _AS_NODE=1   │ │ :18102/login     │
│ .exe       │ │ .exe         │ │ gateway/main │ │ （Next standalone│
│ --role api │ │ --role worker│ │ .js          │ │  真实控制台）    │
│ :18100     │ │ （无端口）   │ │ :18101       │ │                  │
└─────┬──────┘ └──────┬───────┘ └──────┬───────┘ └────────┬─────────┘
      │               │                │                  │
      └───────────────┴────────────────┴──────────────────┘
             共用 %APPDATA%\SkillRouter\data\skillrouter.db
                     （SQLite WAL，单写者＝api/worker）
```

- **父进程只有一个**：Electron 主进程。任何子进程退出都由它记录、判断、必要时重启或报错。
- **没有服务管理器、没有常驻守护**。关闭窗口 → 关闭应用 → 所有子进程按逆序收尾（含 `taskkill /T /F` 兜底），不留下孤儿进程。
- **控制台与 API 之间是多一跳的**：浏览器（Chromium）请求 `http://127.0.0.1:18102/api/...` → Next standalone 按构建期 rewrite 转发到 `http://127.0.0.1:18100/api/...`。这条链路**必须**在冒烟测试里被实际验证（`web_api_proxy` 检查项），否则「页面打开了但所有请求 502」是最常见的假成功。

### 2.2 关键决策（ADR）

| # | 决策 | 备选方案 | 结论与理由 |
|---|---|---|---|
| D1 | 用 **Electron 33.4.11**（已在本机 `%LOCALAPPDATA%\electron\Cache` 中有 `electron-v33.4.11-win32-x64.zip`） | 用 `latest`(44.x) / 34.x | 选**本机缓存已存在**的版本：可离线、可复现。锁死精确版本，不用 `^`。升版是独立动作，需重跑全部证据。 |
| D2 | 用 **electron-builder 26.15.3** 出 NSIS 安装器 | `@electron/packager` + 手写 Inno/WiX；`electron-forge` | 本机 `%LOCALAPPDATA%\electron-builder\Cache` 已有 `nsis-3.0.4.1` / `nsis-resources-3.4.1` / `winCodeSign` / `7zip@1.0.0`，**出包可离线**；forge 仍要落回同一套 builder 配置。 |
| D3 | Python 侧用 **PyInstaller 6.12.0 onedir** 打成 `skillrouter-server.exe` | ① 随包塞便携版 Python + `pip install`；② 要求用户自备 Python；③ onefile | ①体积 200 MB+ 且要 pip 联网；②直接违反 INV-3；③onefile 每次启动解压到临时目录，Windows Defender 误杀率与启动耗时明显更高。onedir 是「体积/启动/误杀」三点最优。 |
| D4 | Next 侧**预构建 standalone** 并随包分发 | ① 让 Electron 自己跑 `next dev`；② 把 web 打包成纯静态导出（`output: export`） | ①需要 Node 依赖树 400 MB+ 且首启 30 秒起；②本项目大量使用动态路由、Server Component 与 `/api` 反代，静态导出不可行。standalone 是官方生产姿势（`next.config.mjs` 已开 `output: 'standalone'`）。 |
| D5 | 网关用 **Electron 自带 node** 运行（`ELECTRON_RUN_AS_NODE=1`） | 随包再塞一份 Node.js 二进制 | 省 80 MB 与一套版本管理。**准确性说明**：Electron 33.4.11 内置 **Node 20.18.x**，而网关 `package.json` 声明 `engines.node >= 22`——那是**声明**，运行时不强制；本机已核对网关源码与 `@aragon-agent/core@0.2.34` 的 `dist` 中**没有** Node 22 专有 API（`Promise.withResolvers` / `Object.groupBy` / `import.meta.dirname` / `with { type:` / `util.parseArgs` 均无命中），`fastify@5` 要求 `>= 20`。因此 Node 20.18 可承载；但这一条**必须由冒烟证据确认**（`gateway_healthz` 检查项）。若失败，唯一改动是 `package.json` 里把 electron 换成 `35.7.5`（内置 Node 22，需联网下载一次二进制的 zip），其余设计不变。 |
| D6 | **api 端口在构建期固定为 18100**（web 由 18101 起扫，网关由 18102 起扫） | 三个端口全动态 | Next 的 rewrite 目标烧死在 `routes-manifest.json`（构建期），全动态必然出现「web 起来了但 /api 全 502」。固定 api 端口 + 启动期占用检测 + 明确报错，是可验证且可解释的方案。 |
| D7 | 运行时环境用 `SKILLROUTER_ENV=development` | 新增 `desktop` 环境值；用 `production` | `production` 会强制 `cookie_secure=True`（`settings.cookie_secure` 只在 `production` 为真），而桌面端是**明文回环 http**，Secure Cookie 会直接导致登录后会话不回种、控制台永远 401。新增 `desktop` 值需要改 `settings.py` 校验器、`cookie_secure`、`bootstrap.py:325`、`upgrade/toolchain.py:49` 四处（违反 INV-1）。因此选 `development`，并用 §2.12 的四条补偿控制覆盖安全面。**此为已知取舍，登记在 §7 R5。** |
| D8 | 子进程一律用 **显式环境变量**，不读 `.env`、不依赖 `cwd` | 生成一份 `.env` 放在安装目录 | 安装目录在 `Program Files` 下**不可写**（UAC 虚拟化也会带来「两套数据」的幽灵 bug）；`.env` 一旦存在就变成第二个真相源。所有路径一律绝对路径注入（`SKILLROUTER_DATA_DIR` / `SKILLROUTER_DB_PATH` / `AGENT_*`），`cwd` 统一为数据目录。 |
| D9 | 数据目录 = `%APPDATA%\SkillRouter\data` | 安装目录旁 `./var`；`%LOCALAPPDATA%` | `%APPDATA%`（Roaming）语义上正确、卸载不删数据；`Program Files` 不可写（见 D8）；`%LOCALAPPDATA%` 在漫游场景下会丢数据。 |
| D10 | 关闭时先优雅 `SIGTERM`/`CTRL_BREAK` 再 `taskkill /PID <pid> /T /F` | 只 `kill()` | Windows 上 `child.kill()` **不会**杀掉孙进程（uvicorn reloader、npm 子进程、引擎宿主），必须按进程树杀，否则下次启动数据库被占用（`DB_BUSY`）。 |

### 2.3 端口与端点约定

| 服务 | 端口策略 | 默认 | 环境变量 / 参数 | 探针 |
|---|---|---|---|---|
| API（uvicorn） | **固定**（构建期烧进 web rewrite） | `18100` | `--port`（sidecar 参数）、`SKILLROUTER_API_URL` | `GET http://127.0.0.1:18100/healthz` → `ok==true && initialized==true` |
| Agent 网关 | 动态：18102 起顺序试 10 个 | `18102` | `AGENT_GATEWAY_PORT` | `GET http://127.0.0.1:<gw>/healthz` |
| Web（Next standalone） | 动态：18101 起顺序试 10 个（避开已占用的 api 端口） | `18101` | `PORT` + `HOSTNAME=127.0.0.1` | `GET http://127.0.0.1:<web>/login` 期望 200 |

**为什么 api 是 18100 而不是开发模式常用的 8000**：`start.ps1` 默认占 8000/8500/3000，用户可能同时开着开发环境。桌面端用 181xx 段，两套可以并存（数据目录不同，数据库不共享），也不会互相打架。若 18100 真被外部进程占用：探测其 `/healthz`，若返回 `service == "skillrouter-api"` 视为「SkillRouter 已在运行」→ 唤起已有实例并退出；否则弹窗报 `E-D01 DESKTOP_PORT_IN_USE`，弹窗内容包含占用进程 PID 与镜像名（`netstat -ano` + `tasklist /FI "PID eq <pid>"`），并提供「打开日志」「重试」「退出」三个按钮。

#### 2.3.1 端口契约校验（评审 P1-17 新增，**必做**）

D6 把整个设计的支点放在「api 端口构建期固定」，但 v1.0 只在 `verify-artifact.mjs` 事后 grep 一次，**运行期没有任何拦截**。补两条硬约束：

1. **构建期**：`scripts/build-web.mjs` 在 `next build` 之后、组装之前，**断言** `apps/web/.next/routes-manifest.json` 里 `/api/:path*` 的 `destination` 等于 `http://127.0.0.1:18100/api/:path*`（与 `build-web.mjs` 注入的 `SKILLROUTER_API_URL` 一致）。不满足则构建失败并打印实际值。这把 R4 的防线从「出包后」前移到「构建中」。
2. **运行期**：状态机 `preparing` 阶段读 `<resources>\web\.next\routes-manifest.json`，取出 rewrite 目标端口，与 `desktop.json.apiPort`（以及即将传给 sidecar 的 `--port`）**三方比对**。任一不一致 → 弹窗 `E-D07 DESKTOP_PORT_CONTRACT_MISMATCH`，正文给出三个实际值 + 修复建议（重装 / 删除 `desktop.json` 让它重建为 18100）。**不允许**带着不一致继续启动——那就是「页面能开、`/api` 全 502」的假成功。
3. `desktop.json.apiPort` 与构建期端口不一致时，**以构建期产物为准**（因为它是烧死的），把 `desktop.json.apiPort` 校正回构建期值并写 `main.log`（`event=port_contract_repair`），只有「三方都读不到」才判 `E-D07`。

#### 2.3.2 「第二次双击」的唯一判据（评审 P1-11 修正）

v1.0 里有两套机制抢同一件事，这里定死顺序，实现不得自行合并：

1. **第一判据永远是 `app.requestSingleInstanceLock()`**。拿不到锁 = 同用户同安装的实例已在运行 → `second-instance` 事件里 `restore() + focus()`，随后**以退出码 0 退出**，`main.log` 记 `event=second_instance`。这条路径**不探端口、不 spawn、不写错误码**。
2. 只有在**拿到锁**、但 18100 的 `/healthz` 返回 `service == "skillrouter-api"` 时，才说明端口被**另一个安装/另一个用户会话**的 SkillRouter 占着（本实例无法与它通信）→ 记 `W-D06 DESKTOP_INSTANCE_RUNNING`，弹窗说明「检测到另一个 SkillRouter 正在使用 18100」并只提供「退出」「打开日志」两个按钮（**不提供**「重试」，因为重试也不会变）。
3. 端口被非 SkillRouter 进程占用 → `E-D01`（见上）。
4. 因此 `E-D06` **不再作为错误码使用**，§4.5 中该行改写为 `W-D06`（警告级），并与 `W-D01` 同一层级。


### 2.4 安装后的资源布局

```text
C:\Users\<u>\AppData\Local\Programs\SkillRouter\        ← 安装目录（electron-builder NSIS，perMachine=false）
├─ SkillRouter.exe                    ← Electron 主程序（双击这个 / 桌面快捷方式指向它）
├─ resources\
│  ├─ app.asar                        ← 主进程 + preload + splash.html（asar: true）
│  ├─ python\                         ← extraResources：PyInstaller onedir
│  │  ├─ skillrouter-server.exe
│  │  └─ _internal\
│  │     ├─ migrations\               ← 必须是这个位置（见 §2.7 路径推导）
│  │     ├─ apps\api\migrations\      ← adopt_alembic.py 的另一条推导路径
│  │     ├─ scripts\adopt_alembic.py
│  │     └─ ...（python3x.dll、site-packages 依赖等）
│  ├─ web\                            ← extraResources：Next standalone 整包
│  │  ├─ server.js
│  │  ├─ .next\{server,static,standalone-manifest...}
│  │  ├─ node_modules\                ← traced 依赖（next/react 等）
│  │  └─ public\
│  └─ gateway\                        ← extraResources：网关编译产物
│     ├─ dist\src\main.js
│     ├─ package.json
│     └─ node_modules\                ← fastify / ajv / @aragon-agent/core（纯 JS）
└─ ...（Electron/Chromium 自带文件）

C:\Users\<u>\AppData\Roaming\SkillRouter\               ← userData（app.setPath('userData', …) 显式指定，无空格）
├─ desktop.json                        ← 桌面端配置（schemaVersion 见 §5.1）
├─ logs\desktop\{main,api,worker,gateway,web}.log
└─ data\                                ← SKILLROUTER_DATA_DIR
   ├─ skillrouter.db (+ -wal / -shm)
   ├─ credentials-master.key / file-signing.key（0600 尽力）
   ├─ files\ logs\ backup\ downloads\
   ├─ aragon-home\engines\             ← AGENT_ENGINE_HOME_ROOT
   ├─ engine-runtime\ engine-builds\   ← 引擎热切换相关
   ├─ agent-sessions\ agent-policy.json
   └─ desktop-first-run.json           ← 首次运行摘要（见 §2.11）
```

> **不要**把 `resources\` 下的东西写成「运行时可改」：`Program Files` 下不可写，且 `resources` 会在升级时被整体替换。所有可写状态只允许落在 `userData`。

### 2.5 启动状态机（主进程）

状态机定义在 `src/main/startup.ts`，状态与转移必须与下表**逐项一致**（实现时这张表就是代码注释与单元测试用例的来源）：

| 状态 | 进入动作 | 成功转移 | 失败处理 |
|---|---|---|---|
| `idle` | 申请单实例锁（**唯一第一判据**，见 §2.3.2）；解析 `--dev/--smoke-test/--data-dir/--verbose` 参数 | `preparing` | 已有实例 → `second-instance` 聚焦后**退出码 0**，记 `event=second_instance`，不探端口、不 spawn、不写错误码 |
| `preparing` | 建 `userData` 目录树；读/建 `desktop.json`；生成 `gatewaySecret`；**端口契约校验（§2.3.1）**；**按模式**校验产物：正常/冒烟模式要求三件产物存在（`skillrouter-server.exe`、`web\server.js`、`gateway\dist\src\main.js`），`--dev` 模式**跳过**产物校验（它只指向 `localhost:3000`，安装包里没有 `resources\`） | `porting` | `E-D02 DESKTOP_RESOURCE_MISSING`（列出缺失路径并中止）；端口契约不一致 → `E-D07`（见 §2.3.1 第 3 条，能自动校正则只告警） |
| `porting` | api 端口占用检测（见 §2.3.2 第 2/3 条）；网关/网页端口扫描 | `bootstrapping` | `E-D01 DESKTOP_PORT_IN_USE` 弹窗；`W-D06` 时中止并提示已有实例 |
| `bootstrapping` | 显示 splash；spawn `--role bootstrap`（120 s 上限）；把 stdout 里的 JSON 摘要写入 `desktop-first-run.json` | `starting_api` | `E-D03 DESKTOP_BOOTSTRAP_FAILED`：弹窗 + 日志尾部 40 行（含 launcher 的**迁移硬校验**失败，见 §2.7） |
| `starting_api` | spawn `--role api`；轮询 `/healthz`（每 400 ms，90 s 上限）要求 `ok && initialized` | `starting_worker` | `E-D04 DESKTOP_API_TIMEOUT`（附最后 40 行 api 日志） |
| `starting_worker` | spawn `--role worker`（不阻塞、不探活，只记录启动） | `starting_gateway` | 无（worker 崩溃进入监控） |
| `starting_gateway` | spawn 网关；轮询 `/healthz`（40 s 上限） | `starting_web` | **降级**：置 `gatewayDown=true`，`W-D01`，继续（控制台其它页面可用，仅「内置 Agent」不可用） |
| `starting_web` | spawn `web\server.js`；轮询 `/login`（120 s 上限，含 302 也算就绪） | `ready` | `E-D05 DESKTOP_WEB_TIMEOUT` |
| `ready` | 建主窗口加载 `http://127.0.0.1:<web>/`；关 splash；启动崩溃监控 | — | 子进程意外退出：记录 → 最多 3 次指数退避重启（1 s/3 s/9 s）→ 仍失败则弹窗「重启服务 / 打开日志 / 退出」 |
| `failed` | 关闭 splash；显示错误窗口（本地 HTML，含错误码、尾部日志、三个按钮） | 用户点「重试」→ `preparing` | — |

超时常量集中在 `src/shared/constants.ts`：`API_READY_TIMEOUT_MS = 90_000`、`GATEWAY_READY_TIMEOUT_MS = 40_000`、`WEB_READY_TIMEOUT_MS = 120_000`、`BOOTSTRAP_TIMEOUT_MS = 120_000`、`PROBE_INTERVAL_MS = 400`、`SHUTDOWN_GRACE_MS = 8_000`。
（`SHUTDOWN_GRACE_MS` 由 5 000 提到 8 000：uvicorn lifespan 收尾 + SQLite WAL checkpoint 需要时间，太短会把正常收尾推给 `taskkill /F`，反而放大 `DB_BUSY`——评审 P2-4。）
**这些是「超时即失败」的上限，不是预期耗时**；INV-3 的性能口径见 §0（热启 ≤45 s / 冷启 ≤120 s）。


### 2.6 子进程环境变量合成（唯一真相源 = `src/main/env.ts`）

下表就是 `composeEnv()` 的实现清单，**不得**在别处再拼环境变量：

| 变量 | 值 | 作用对象 |
|---|---|---|
| `SKILLROUTER_ENV` | `development`（D7） | api / worker |
| `SKILLROUTER_DATA_DIR` | `<userData>\data`（绝对路径） | api / worker |
| `SKILLROUTER_DB_PATH` | `<userData>\data\skillrouter.db`（绝对路径） | api / worker |
| `SKILLROUTER_PUBLIC_BASE_URL` | `http://127.0.0.1:<webPort>` | api |
| `SKILLROUTER_MASTER_KEY` | **不设置**（缺失时 `settings.generate_missing_secrets()` 生成 `<data>\credentials-master.key`） | api / worker |
| `SKILLROUTER_MASTER_KEY_ID` | `mk1` | api |
| `SKILLROUTER_ADMIN_USER` / `SKILLROUTER_ADMIN_PASSWORD` | `admin` / `adminjr`（仅首次初始化生效） | api（bootstrap） |
| `SKILLROUTER_SESSION_TTL_MINUTES` | `480` | api |
| `SKILLROUTER_API_URL` | `http://127.0.0.1:<apiPort>` | web（构建期同名变量烧进 rewrite；运行时值供日志/诊断） |
| `SKILLROUTER_INTERNAL_URL` | `http://127.0.0.1:<apiPort>` | api（内部回环）**+ 网关**（P0-1：见下方注） |
| `SKILLROUTER_AGENT_GATEWAY_URL` | `http://127.0.0.1:<gatewayPort>` | api |
| `SKILLROUTER_AGENT_GATEWAY_SECRET` | `desktop.json.gatewaySecret`（48 字符 URL-safe 随机，≥32） | api |
| `SKILLROUTER_AGENT_GATEWAY_BIND` | `127.0.0.1` | api |
| `SKILLROUTER_AGENT_ENGINE_HOME_ROOT` | `<data>\aragon-home\engines` | api |
| `SKILLROUTER_ENGINE_POLICY` | `notify`（不做无人值守自动升级） | api |
| `SKILLROUTER_ENGINE_CHANNEL` | `stable` | api |
| `SKILLROUTER_ENGINE_PIN` | `0.2.34@0a119a64890717b0e3017edbaaf00944babf19a8`（与仓库现值一致） | api / 网关 |
| `SKILLROUTER_ENGINE_TOOLCHAIN` | `gateway`（**必须显式钉死**，P1-12） | api |
| `SKILLROUTER_EGRESS_POLICY` | `deny` | api |
| `SKILLROUTER_PAYLOAD_POLICY` | `redacted_preview`（**不**用 `full`：桌面端库文件在用户可读目录） | api |
| `AGENT_GATEWAY_SHARED_SECRET` | 同 `SKILLROUTER_AGENT_GATEWAY_SECRET` | 网关 |
| `AGENT_GATEWAY_BIND` / `AGENT_GATEWAY_PORT` | `127.0.0.1` / `<gatewayPort>` | 网关 |
| `AGENT_GATEWAY_ENGINE_MODE` | `auto` | 网关 |
| `AGENT_GATEWAY_LOG_LEVEL` | `info` | 网关 |
| `AGENT_ENGINE_HOME_ROOT` | `<data>\aragon-home\engines` | 网关 |
| `AGENT_ENGINE_RUNTIME_DIR` | `<data>\engine-runtime` | 网关 |
| `AGENT_ENGINE_BUILDS_DIR` | `<data>\engine-builds` | 网关 |
| `AGENT_POLICY_PATH` | `<data>\agent-policy.json` | 网关 |
| `AGENT_SESSION_DIR` | `<data>\agent-sessions` | 网关 |
| `ENGINE_PIN` | `0.2.34`（去掉 `@commit` 的版本段） | 网关 |
| `PYTHONUNBUFFERED` / `PYTHONUTF8` / `PYTHONIOENCODING` | `1` / `1` / `utf-8` | api / worker |
| `PORT` / `HOSTNAME` / `NEXT_TELEMETRY_DISABLED` | `<webPort>` / `127.0.0.1` / `1` | web |
| `NODE_ENV` | `production` | 网关 / web（网关缺省是 `development`，且它会把这个值传给 engine 子进程，桌面端统一按生产语义跑） |
| `ELECTRON_RUN_AS_NODE` | `1` | 网关 / web |

**两条必做规则（v1.0 缺失，评审 P0-1 / P1-12 修正）**：

1. **`SKILLROUTER_INTERNAL_URL` 必须同时给网关。** 证据：`agents/aragon-gateway/src/config.ts:231` 用 `str(env, 'SKILLROUTER_INTERNAL_URL', 'http://127.0.0.1:8000')` 读它，`src/main.ts:145` 把它作为 `internalUrl` 交给 platform client，`src/platform-client.ts:136` 用它拼 `/internal/...` 请求并带 `X-Internal-Token`。**只给 api 的后果**：网关 `/healthz` 依然 200（探针通过），但每一次平台回调（`sr_*` 工具、SSE 事件回传、制品校验）都打到 8000 这个在桌面端**不存在**的端口 → 控制台「内置 Agent」全链路失败，而所有探针都是绿的。这是本设计里最危险的假成功，冒烟必须显式验证（§4.4 新增 `gateway_platform_reach` 检查项）。
2. **继承环境一律剔除。** `composeEnv()` 的输入必须是「一份干净的基础环境 + 本表」，不得直接继承 `process.env` 里的 `SKILLROUTER_*` / `AGENT_*` / `ENGINE_*` / `PYTHON*` / `NODE_OPTIONS` / `ELECTRON_RUN_AS_NODE`。
   实现方式：从 `process.env` 复制一份，然后 `delete` 掉所有以上前缀的键（前缀清单放 `src/shared/constants.ts`，单测逐项断言），再叠加本表。
   理由：`SKILLROUTER_ENV` / `SKILLROUTER_DATA_DIR` / `SKILLROUTER_ENGINE_TOOLCHAIN=local` 这类变量一旦从父进程漏进来，就会**静默改变桌面端行为**（P1-12：`ENGINE_TOOLCHAIN=local` 在 `development` 下会被接受，见 `upgrade/toolchain.py:49`，而安装包里没有 npm/git）；这类 bug 只在个别用户机器上出现，几乎无法复现。

`cwd` 一律为 `<userData>\data`（保证任何相对路径兜底都落在数据目录，而不是安装目录）。
**副作用（必须知道）**：`Settings` 的 `env_file=".env"` 是**相对 cwd** 解析的，因此 `cwd=<data>` 意味着「若数据目录里出现一个 `.env` 文件，它会被当成配置源」。桌面端**永不写入** `<data>\.env`；`preparing` 阶段若发现该文件存在，只记一条 `W-D02 DESKTOP_STRAY_ENV_FILE` 警告并继续（不删用户文件）。同理，构建期自检必须把 cwd 切到 `apps/desktop/build/`，否则会读到**仓库根**的 `.env`（P1-6）。

### 2.7 Python 边车设计（PyInstaller onedir）

**入口脚本** `apps/desktop/python/launcher.py`（实现要求：先设环境、后 import）：

```python
"""Skill Router 桌面端 sidecar：bootstrap / api / worker 三种角色。

约束：
- 必须在 import skillrouter_api 之前注入环境（settings 在 import 时就固化）
- 迁移目录布局要同时满足 bootstrap.MIGRATIONS_DIR 与 adopt_alembic.MIGRATIONS_DIR
  （两条推导路径不同，见 spec §2.7 表格）
"""
import argparse, json, os, sqlite3, sys
from pathlib import Path

#: P0-3：桌面端专用退出码 —— 迁移不一致时**必须**非 0 退出（见 _migration_problem 说明）
EXIT_MIGRATION_MISMATCH = 21
#: 与 §4.5 的错误码对齐：主进程把这个退出码映射成 E-D03
FATAL_MIGRATION = "E-D03 DESKTOP_BOOTSTRAP_FAILED"

def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent          # ...\resources\python
    return Path(__file__).resolve().parents[3]               # 仓库根

def _migration_problem() -> str | None:
    """迁移一致性硬校验：三条断言（user_version / alembic head / 当前 revision）。

    **为什么必须自己复核（P0-3）**：`bootstrap.assert_migrated()` 只在
    `settings.env == "production"` 时抛错（`services/bootstrap.py:325`），其余环境仅
    `log.warning` 后返回 None。而桌面端按 D7 必须是 `development`，于是「库 schema 不对」
    时 bootstrap 仍然退出码 0 → api 照常起来 → 控制台带着错版库运行、坏数据无告警。
    R5 声称的「自实现致命语义」就落在这个函数里：与 env 无关，永远阻断。

    这里不用 `settings.env` 触发，也不改 `apps/api` 一行代码（INV-1）。
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from skillrouter_api.db.engine import schema_version
    from skillrouter_api.services import bootstrap as svc
    from skillrouter_api.settings import settings

    expected = svc.SCHEMA_VERSION  # 单一真相源：不在这里硬编码 2
    pragma = schema_version()
    if pragma != expected:
        return f"PRAGMA user_version={pragma}，期望 {expected}"

    cfg = Config(str(svc.MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(svc.MIGRATIONS_DIR))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    if head is None:
        # 最常见的成因：PyInstaller 没把 migrations 铺进 _internal（见 §2.7 双布局）
        return "无法读取 alembic head（迁移目录缺失？检查 _internal\\migrations）"

    with sqlite3.connect(str(settings.db_file)) as conn:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    revision = row[0] if row else None
    if revision != head:
        return f"alembic_version={revision}，期望 {head}"
    return None

def main() -> int:
    ap = argparse.ArgumentParser(prog="skillrouter-server")
    ap.add_argument("--role", choices=("bootstrap", "api", "worker"))
    ap.add_argument("--port", type=int, default=18100)
    ap.add_argument("--data-dir", default="")
    ap.add_argument("--json", action="store_true")   # bootstrap 输出机器可读摘要
    ap.add_argument("--version", action="store_true")  # P1-5：verify-artifact 需要它
    args = ap.parse_args()

    if args.version:
        # 不起服务、不建目录、不 require --role；退出码恒 0，供 scripts/verify-artifact.mjs 断言
        try:
            from skillrouter_api import __version__ as api_version
        except Exception:  # pragma: no cover - 打包残缺时仍要能打印
            api_version = "unknown"
        print(f"skillrouter-server 0.1.0 (skillrouter_api {api_version}, py{sys.version_info.major}.{sys.version_info.minor})")
        return 0

    if not args.role:
        ap.error("--role 必填（bootstrap / api / worker）")
        return 2

    # 数据目录优先级（P1-13）：--data-dir（命令行） > SKILLROUTER_DATA_DIR（环境） > 默认。
    # v1.0 用 setdefault()，导致环境变量悄悄赢、--data-dir 静默无效。
    if args.data_dir:
        data_dir = Path(args.data_dir).resolve()
        os.environ["SKILLROUTER_DATA_DIR"] = str(data_dir)
        os.environ["SKILLROUTER_DB_PATH"] = str(data_dir / "skillrouter.db")
    elif os.environ.get("SKILLROUTER_DATA_DIR"):
        data_dir = Path(os.environ["SKILLROUTER_DATA_DIR"]).resolve()
        os.environ.setdefault("SKILLROUTER_DB_PATH", str(data_dir / "skillrouter.db"))
    else:
        data_dir = (app_root() / "data").resolve()
        os.environ["SKILLROUTER_DATA_DIR"] = str(data_dir)
        os.environ["SKILLROUTER_DB_PATH"] = str(data_dir / "skillrouter.db")
    data_dir.mkdir(parents=True, exist_ok=True)

    # frozen：模块都在 PYZ 里，不用驻留 sys.path；开发态要让 skillrouter_api 可导入。
    if not getattr(sys, "frozen", False):
        sys.path.insert(0, str(app_root() / "apps" / "api"))
    os.environ.setdefault("PYTHONUTF8", "1")
    # 注意：迁移目录**不走环境变量**。`bootstrap.MIGRATIONS_DIR` 与
    # `adopt_alembic.MIGRATIONS_DIR` 都是由 `__file__` 推导的常量（spec §2.7 表格），
    # 所以必须靠 PyInstaller 的 datas 同时铺两条路径，而不是在这里 setenv（那是无效动作）。

    if args.role == "bootstrap":
        from skillrouter_api.services import bootstrap as svc
        summary = svc.bootstrap(reason="desktop")
        problem = _migration_problem()
        if problem is not None:
            print(f"[FATAL] {FATAL_MIGRATION} 迁移未收敛：{problem}", file=sys.stderr, flush=True)
            return EXIT_MIGRATION_MISMATCH
        print(json.dumps(summary, ensure_ascii=False, indent=2) if args.json else summary, flush=True)
        return 0

    if args.role == "api":
        import uvicorn
        # P0-2：**直接传 ASGI 对象**，不传 "module:attr" 字符串。
        # 字符串形式在 PyInstaller 冻结后要求该模块已在依赖图里，而静态分析看不到字符串，
        # `--role api` 会 ModuleNotFoundError。静态 import + 传对象两条一起做，双保险。
        from skillrouter_api.main import app as asgi_app
        uvicorn.run(asgi_app, host="127.0.0.1", port=args.port,
                    log_level="info", access_log=False,
                    http="h11", ws="none", loop="asyncio", lifespan="on")
        return 0

    import asyncio
    from skillrouter_api.worker.runner import main as worker_main   # main() 是无参协程，已核对
    try:
        asyncio.run(worker_main())
    except KeyboardInterrupt:
        pass
    return 0

if __name__ == "__main__":
    sys.exit(main())

```

**迁移目录的双布局（关键实现细节，不要「优化」掉）**：

| 读取方 | 推导代码 | onedir 下解析结果 |
|---|---|---|
| `services/bootstrap.py: MIGRATIONS_DIR` | `Path(__file__).resolve().parents[2] / "migrations"` | `<resources>\python\_internal\migrations` |
| `scripts/adopt_alembic.py: MIGRATIONS_DIR` | `Path(__file__).resolve().parents[1] / "apps" / "api" / "migrations"` | `<resources>\python\_internal\apps\api\migrations` |

因此 `_internal` 下**必须同时**存在 `migrations\` 与 `apps\api\migrations\` 两份拷贝（共 11 KB，重复成本可忽略）。这样 `bootstrap.migrate_or_adopt()` 的两条分支（空库 upgrade / 存量库 adopt）在桌面上都能走通，**且不需要改 `apps/api` 的任何一行代码**（INV-1）。

**PyInstaller spec**（`apps/desktop/python/skillrouter-sidecar.spec`）要点：

```python
from PyInstaller.utils.hooks import collect_submodules

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('../../apps/api/migrations', 'migrations'),                 # bootstrap 推导路径
        ('../../apps/api/migrations', 'apps/api/migrations'),        # adopt_alembic 推导路径
        ('../../scripts/adopt_alembic.py', 'scripts'),               # bootstrap._adopt_module 推导路径
    ],
    hiddenimports=[
        # P0-2：launcher 现在静态 import 了 skillrouter_api.main，这里再列一次做双保险。
        'skillrouter_api.main',
        'uvicorn', 'uvicorn.logging', 'uvicorn.loops.asyncio',
        'uvicorn.protocols.http.h11_impl', 'uvicorn.lifespan.on', 'uvicorn.lifespan.off',
        # P2-1：删掉 'uvicorn.protocols.websockets.auto'（与 ws="none" 语义相反，且会拉入 websockets）
        'alembic', 'alembic.config', 'alembic.command', 'alembic.script',
        'sqlalchemy.dialects.sqlite', 'sqlite3',
        'jsonschema', 'jsonschema.validators',
        'pydantic_settings', 'pydantic.deprecated', 'argon2', 'cryptography.hazmat.primitives.ciphers.aead',
        'itsdangerous', 'multipart', 'pypdf', 'yaml', 'email.mime.text',
        'skillrouter_api.worker.runner', 'skillrouter_api.worker.handlers',
        'skillrouter_api.executors.http', 'skillrouter_api.executors.async_poll',
    ]
    # R3 的结构性加固：alembic / uvicorn / sqlalchemy / fastapi 内部大量**运行期**导入
    # （迁移 env.py、协议选择、dialect 加载）无法靠静态分析补全，逐个列 hiddenimports
    # 永远漏。这里改成整包收集，再用 excludes 把明确不需要的剔掉。
    + collect_submodules('alembic')
    + collect_submodules('uvicorn')
    + collect_submodules('sqlalchemy')
    + collect_submodules('fastapi')
    + collect_submodules('pydantic_settings'),
    excludes=[
        'tkinter', 'PyQt5', 'matplotlib', 'pytest', 'respx', 'mypy', 'ruff',
        # 与 http="h11" / ws="none" / loop="asyncio" 一致：不要 C 加速与 ws 实现
        'httptools', 'uvloop', 'websockets', 'wsproto', 'watchfiles',
        'uvicorn.protocols.websockets', 'uvicorn.protocols.http.httptools_impl',
    ],
)
exe = EXE(..., name='skillrouter-server', console=True)   # console=True：stdout 由主进程接管道写日志
coll = COLLECT(exe, a.binaries, a.datas, name='skillrouter-server')   # onedir
```

> **datas 的相对基址（P2-2 更正）**：PyInstaller spec 里的相对 `datas` 路径是相对 **spec 文件所在目录**（`SPECPATH`）解析的，不是相对 cwd。本 spec 位于 `apps/desktop/python/`，所以 `../../apps/api/migrations` 正好指向 `<repo>/apps/api/migrations`。只有 `requirements-build.txt` 里的 `-r ../../apps/api/requirements.txt` 是 **cwd 相关**（pip 的 `-r` 相对当前工作目录），因此构建脚本仍固定 `cwd=<repo root>`。


**构建解释器约束（P1-16，必读）**：构建 sidecar 的 Python **必须是官方 CPython 3.11 的独立 venv**，不得用 Anaconda / Miniconda / Microsoft Store 版解释器。
理由（本机实测）：当前 `where python` 解析到 `3.11.7 | packaged by Anaconda, Inc.`。用 conda 解释器跑 PyInstaller 会把 conda 的 `python3x.dll` / `Library\bin` 依赖烘进产物，在「没装过 Python 的干净机」上表现为**启动即缺 DLL**，而构建机上一切正常 —— 这正是 INV-3 最难排查的失效方式。
做法：`scripts/build-sidecar.mjs` 先执行 `python -c "import sys;print(sys.base_prefix, sys.version)"`，若输出含 `conda` 或路径含 `Anaconda|Miniconda` 则**直接失败**并提示用 `py -3.11 -m venv apps/desktop/build/.venv`（`PYTHON_BIN` 环境变量可覆盖解释器路径）。

**驱动命令**（`scripts/build-sidecar.mjs` 里逐条 `spawnSync`，任一步失败退出码非 0；**子进程 `cwd` 一律为仓库根**——只有 `requirements-build.txt` 的 `-r ../../apps/api/requirements.txt` 依赖 cwd，spec 里的 `datas` 是相对 spec 目录解析的，见上方 P2-2）：

```bat
python -m pip install -r apps/desktop/python/requirements-build.txt
python -m PyInstaller --noconfirm --clean --distpath apps/desktop/build/python --workpath apps/desktop/build/.pyi apps/desktop/python/skillrouter-sidecar.spec
```

`requirements-build.txt` 内容：`-r ../../apps/api/requirements.txt` + `pyinstaller==6.12.0`（与仓库 `requirements-dev.txt` 的风格一致：精确 pin）。

**构建期双检（P0-2 修正：v1.0 只跑 bootstrap，恰好绕开 api 崩的那条路径）**，两步都必须绿，任一步失败即构建失败：

```bat
:: 检查 1 —— bootstrap（覆盖「迁移目录双布局」+「alembic hidden import」）
apps/desktop/build/python/skillrouter-server/skillrouter-server.exe --role bootstrap --data-dir apps/desktop/build/.smoke-data --json

:: 检查 2 —— api 真起一次（覆盖 P0-2 的 import 图完整性 + uvicorn 协议栈 + 迁移硬校验）
apps/desktop/build/python/skillrouter-server/skillrouter-server.exe --role api --port 18099 --data-dir apps/desktop/build/.smoke-data
::   轮询 http://127.0.0.1:18099/healthz 直到 ok==true && initialized==true（上限 60 s），然后 kill
::   判定：拿到 JSON 且 ok/initialized 均为 true → 通过；进程提前退出或超时 → 打印 api 侧 stderr 尾部并失败
```

检查 1 期望 stdout 是 JSON，`migration.mode ∈ {empty_db_upgrade, upgrade}` 且 `admin.created == true`。
检查 2 是 v2 新增的**强制**项：v1.0 的 `uvicorn.run("skillrouter_api.main:app")` 在冻结后会 `ModuleNotFoundError`，而只跑 bootstrap 的自检永远发现不了。
**两条检查都必须在「净化环境」下执行（P1-6）**：cwd 设为 `apps/desktop/build/`（**不是仓库根**，避免仓库根的 `.env` 被 `SettingsConfigDict(env_file=".env")` 读到），并显式清空继承的 `SKILLROUTER_*` / `AGENT_*` / `ENGINE_*` / `PYTHONHOME` / `PYTHONPATH`。


### 2.8 Next.js standalone 宿主设计

`scripts/build-web.mjs` 的步骤（顺序固定）：

1. `cd apps/web && npm ci`（若 `node_modules` 已存在则跳过；CI 里必须 `npm ci`）
2. 用**构建期** api 地址构建（由 Node 脚本以 `env` 注入，避开 Windows shell 的变量语法差异）：
   `spawn('npm', ['run', 'build'], { cwd: 'apps/web', env: { ...process.env, SKILLROUTER_API_URL: 'http://127.0.0.1:18100' }, shell: true })`
3. 组装 `apps/desktop/build/web/`：
   - 复制 `apps/web/.next/standalone/*` → `build/web/`
   - 复制 `apps/web/.next/static` → `build/web/.next/static`
   - 复制 `apps/web/public` → `build/web/public`
4. 断言存在 `build/web/server.js`；否则报错退出（`output: 'standalone'` 没生效或 tracing root 变了）
5. **端口契约断言（P1-17，构建期防线）**：读 `apps/web/.next/routes-manifest.json`，断言 `/api/:path*` 的 `destination === 'http://127.0.0.1:18100/api/:path*'`，且 `/healthz` 的 `destination` 以 `http://127.0.0.1:18100` 开头。不满足则打印实际值并**退出码 1**。
   参考现状：仓库里已有的 `.next/routes-manifest.json` 烧死的是 `http://127.0.0.1:8000`（因为 `next.config.mjs` 在无 `SKILLROUTER_API_URL` 时回落到 8000）—— 这正好证明「不注入就必定 502」，也是这条断言必须存在的原因。

> 运行时端口可能上移的只有 web 与 gateway；**api 端口在任何环节都不允许漂移**（D6）。因此 web 端口动态是安全的，api 端口动态一定不安全 —— 这条不对称是本节所有断言的依据。


**为什么 standalone 根目录就是 `server.js`（而不是 `apps/web/server.js`）**：`next build` 用 lockfile 推断 tracing root，`apps/web/package-lock.json` 存在、仓库根**没有** `package-lock.json` / `package.json`，所以 root = `apps/web`，产物路径是 `.next/standalone/server.js`。构建脚本在复制前用 `fs.existsSync` 断言。

运行：`spawn(process.execPath, [join(resourcesWeb, 'server.js')], { env: { ...env, ELECTRON_RUN_AS_NODE: '1' }, cwd: resourcesWeb })`。Next 15 的 standalone `server.js` 读 `PORT` 与 `HOSTNAME`，因此 web 端口可动态（与 D6 一致：只有 api 端口必须固定）。

### 2.9 Node 网关宿主设计

1. 构建：`cd agents/aragon-gateway && npm ci && npm run build`（产出 `dist/src/main.js`）；随后只保留生产依赖：复制 `dist/`、`package.json`、`node_modules/`（`npm ci --omit=dev` 的结果；`@aragon-agent/core` 0.2.34 精确 pin 已在 `package.json`）。
2. 组装到 `apps/desktop/build/gateway/`。
3. 运行：`spawn(process.execPath, [join(resourcesGateway, 'dist/src/main.js')], { env: { ...env, ELECTRON_RUN_AS_NODE: '1' }, cwd: resourcesGateway })`。
4. 网关读 `AGENT_POLICY_PATH`（`<data>\agent-policy.json`，由 api 的 `bootstrap()` 通过 `settings_service.write_agent_policy()` 写出）——**所以网关必须在 bootstrap 之后启动**；顺序错了网关会因 policy 缺失启动失败。
5. 网关失败**不阻塞**控制台：置降级标志 `degraded=true`，只在**桌面层**表达（菜单里的状态项 + `main.log`），**不向渲染进程注入任何 UI 元素**（前端零改动＝INV-1）。用户点「内置 Agent」页时看到的是控制台原有的错误提示，行为与「网关单独挂掉」完全一致，不会出现「假成功」的白屏。

### 2.10 进程监管、日志与关停

- **日志**：每个子进程 `stdout`/`stderr` 都用管道接到 `logs\desktop\<name>.log` 的追加写流；每行前缀 `[YYYY-MM-DDTHH:mm:ss.SSSZ] `。单文件上限 10 MB，超出后轮转为 `<name>.1.log`（最多保留 5 个）。`main.log` 记录状态机转移、端口决定、探针结果、退出码、重启次数。
- **崩溃监控**：`child.on('exit', code, signal)` → 若不在 `shuttingDown` 且状态已是 `ready`，按服务重要性处理：`api`/`web` 属于致命（弹窗 + 最多 3 次退避重启），`gateway` 属于降级（重启 1 次后置 down），`worker` 属于降级（重启 2 次）。
- **spawn 的唯一入口与统一选项（P1-9，v1.0 缺失）**：所有子进程只能经 `supervisor.ts: spawnChild()` 创建，参数固定为
  `spawn(exe, args, { cwd: <data>, env: composeEnv(), stdio: ['ignore','pipe','pipe'], windowsHide: true, detached: false })`。
  **`windowsHide: true` 是硬要求**：sidecar 用 `console=True` 构建（要 stdout 管道），不隐藏窗口时 Windows 会给 api 和 worker 各弹一个黑色控制台窗口，「双击可用」直接变成「双击弹出两个黑框」。`shell` 必须为 `false`（R11：路径含空格时不能用 shell 拼串）。
- **关停**：`before-quit` 里置 `shuttingDown=true` → 逆序（web → gateway → worker → api）先 `child.kill()`（Windows 上是 `SIGTERM` 语义的 `TerminateProcess`）→ 等 `SHUTDOWN_GRACE_MS`（8 s，见 §2.5）并**等各子进程的 `exit` 事件** → 仍存活的用 `taskkill /PID <pid> /T /F` → `app.exit(0)`。
  **必须用 `/T`（P2-5）**：`AGENT_GATEWAY_ENGINE_MODE=auto` 可能落 `host` 模式，网关会再 spawn 一个 engine 子进程（孙进程）；不带 `/T` 只会杀掉网关本身，engine 会变成孤儿并继续占着 `agent-sessions`。同样地，`--role api` 若将来加了 `--reload` 也会产生孙进程。**不记录孙进程 PID，一律靠 `/T` 按树杀。**
  **必须**在 `window-all-closed` 里调用 `app.quit()`（`process.platform === 'win32'` 下默认不退出），否则关窗后 uvicorn 还在跑，用户下次双击会遇到「库被占用」。
- **单实例**：`app.requestSingleInstanceLock()` 是**唯一**第一判据（优先级定义见 §2.3.2）；第二次启动 `second-instance` 事件里 `win.restore() + win.focus()` 后按退出码 0 退出。
- **应用标识（P2-3）**：入口处调用 `app.setAppUserModelId('com.skillrouter.desktop')`，与 `electron-builder.yml` 的 `appId` 保持一致，否则 Windows 任务栏分组与通知归属会显示为 `electron.app.Electron`。


### 2.11 首次运行与密钥

1. `userData` 目录树建立；`desktop.json` 不存在则写入初始值（§5.1），其中 `gatewaySecret` 用 `crypto.randomBytes(36).toString('base64url')`（48 字符）。
2. 跑 `--role bootstrap`。它内部按 `bootstrap()` 的既有顺序完成：`ensure_dirs → master_key → file_signing_key → migrate_or_adopt → assert_migrated → create_triggers → seed_system_state → initialize_admin → cleanup_expired → write_agent_policy`。主密钥与文件签名密钥由 `settings.generate_missing_secrets()` 生成到 `<data>\credentials-master.key` / `<data>\file-signing.key`（`os.chmod(0o600)` 在 Windows 上是尽力而为，见 §2.12）。
3. 把摘要写入 `<data>\desktop-first-run.json`：`{capturedAt, migration.mode, admin:{created,username}, master_key_fingerprint, env, public_base_url}`。**只写指纹，不写密钥**。
4. `desktop.json.adminInitialized = summary.admin.created`（P1-7 修正：**只用于桌面层提示**，不碰登录页）。
   v1.0 说它「用于决定登录页是否显示『首次登录：admin / adminjr』提示卡」—— 登录页属于 `apps/web`，改它违反 INV-1，且与 §4.2「控制台前端不消费这些方法」直接矛盾。正确落点：
   - splash 最后一屏（`adminInitialized === false` 时）显示一行「首次登录账号 `admin` / 初始密码 `adminjr`，登录后必须改密」；
   - 首次运行完成后 `main.log` 记 `event=first_run admin_created=true`；
   - `README.md` / `docs/runbook/desktop-app.md` 写明初始凭据与强制改密；
   - 控制台侧**零改动**（真实行为由后端既有的 `must_change_password` 流程驱动，见 `api/admin/auth.py:111`：首次登录后 session scope 被限为 `password_change`）。

5. 后续启动：`bootstrap` 仍然每次都跑（幂等 `upgrade`），这样升级安装包后 schema 会被自动迁移；`admin_initialized` 标记保证默认密码不会复活（沿用后端既有硬约束）。

### 2.12 安全设计

| 面 | 措施 | 说明 |
|---|---|---|
| 监听面 | 三个服务全部 `127.0.0.1` | 局域网不可达；`AGENT_GATEWAY_BIND` 只接受回环（网关自身有 `AGENT_GATEWAY_ALLOW_PUBLIC_BIND` 保护，桌面端不设置） |
| 浏览器面 | `contextIsolation: true`、`sandbox: true`、`nodeIntegration: false`、`webSecurity: true` | preload 只暴露 4 个只读/受控方法；不注入 `require` |
| 导航 | 拦截 `will-navigate` 与 `setWindowOpenHandler`：只允许 `http://127.0.0.1:<webPort>` 前缀，其余交给 `shell.openExternal`（仅 `http(s)`） | 防止控制台里点外链把窗口导航到远程页 |
| 密钥 | 主密钥/文件签名密钥在 `<data>`（用户目录，非 root 才可读，Windows ACL 继承）；`desktop.json.gatewaySecret` 同目录；日志与首次运行摘要**只含指纹** | 与后端「日志零明文」一致 |
| Cookie | D7：`SKILLROUTER_ENV=development` → `cookie_secure=False` | 明文回环 http 下这是必然取舍；补偿：监听仅回环 + 会话令牌只在本机 + `payload_policy=redacted_preview` |
| 载荷 | `SKILLROUTER_PAYLOAD_POLICY=redacted_preview` | 桌面库文件在用户可写目录，`full` 会把明文密钥写进 SQLite |
| 出站 | `SKILLROUTER_EGRESS_POLICY=deny` + 既有 SSRF 拒绝清单 | 沿用后端默认 |
| 内部回环鉴权 | 网关与 api 之间用 `X-Internal-Token = SKILLROUTER_AGENT_GATEWAY_SECRET`（48 字符随机，存 `desktop.json`），且**双方都拿到 `SKILLROUTER_INTERNAL_URL`**（P0-1） | 少了任一侧的 URL，回调就打向开发端口 8000；这是「探针全绿但功能全废」的根因 |
| Cookie | Cookie 作用域是**主机**（`127.0.0.1`）而非端口，因此「浏览器 → 18101（Next）→ rewrite → 18100（api）」这条链上 `Set-Cookie` 能正常穿透 | 无需任何额外配置；但**必须**把 api 固定在 127.0.0.1 上，改成 `localhost` 会造成 cookie 主机不一致（`localhost` ≠ `127.0.0.1`），登录表现为「密码对了又回到登录页」 |
| 供应链 | Electron / electron-builder / PyInstaller / 各依赖**精确 pin**；`package-lock.json` 提交；`release/SKILLROUTER-SHA256SUMS.txt`（= `apps/desktop/release/…`，见 §2.13）记录每个产物哈希 | 可复现、可校验 |

### 2.13 打包流水线（可复现命令）

`apps/desktop/package.json` 的脚本（全部在 `apps/desktop` 下执行）：

```json
{
  "scripts": {
    "build": "node scripts/build-all.mjs",
    "build:sidecar": "node scripts/build-sidecar.mjs",
    "build:web": "node scripts/build-web.mjs",
    "build:gateway": "node scripts/build-gateway.mjs",
    "build:main": "node esbuild.config.mjs",
    "dist": "npm run build",
    "dist:win": "npm run build && electron-builder --win nsis dir --x64 --publish never",
    "verify": "node scripts/verify-artifact.mjs",
    "smoke": "node scripts/smoke.mjs",
    "test": "vitest run",
    "typecheck": "tsc --noEmit",
    "dev": "node esbuild.config.mjs --watch"
  }
}
```

一次完整出包的顺序（`build-all.mjs` 内部顺序，任一步失败即停）：

1. `build:gateway`（最便宜，先失败先止损）
2. `build:web`（约 1~3 分钟）
3. `build:sidecar`（约 2~5 分钟）
4. `build:main`（esbuild，秒级）
5. 复制到 `apps/desktop/resources/`（`python`、`web`、`gateway`）
6. `electron-builder --win nsis dir`

`electron-builder.yml`（关键片段）：

```yaml
appId: com.skillrouter.desktop
productName: Skill Router
copyright: Skill Router
directories:
  buildResources: build
  output: release
files:
  - dist/**/*
  - splash.html
  - package.json
asar: true
extraResources:
  - from: resources/python
    to: python
  - from: resources/web
    to: web
  - from: resources/gateway
    to: gateway
win:
  target:
    - target: nsis
      arch: [x64]
    - target: dir
      arch: [x64]
  icon: build/icon.png
  artifactName: SkillRouter-Setup-${version}-win-x64.${ext}
nsis:
  oneClick: false
  perMachine: false
  allowToChangeInstallationDirectory: true
  createDesktopShortcut: true
  createStartMenuShortcut: true
  shortcutName: Skill Router
  deleteAppDataOnUninstall: false
  runAfterFinish: true
```

依赖（`devDependencies`，全部精确 pin）：`electron@33.4.11`、`electron-builder@26.15.3`、`esbuild@0.24.2`、`typescript@5.7.2`、`vitest@2.1.8`、`@types/node@22.10.2`。**不引入**任何 `dependencies`（主进程不使用第三方运行时依赖，避免 asar 依赖树）。

**产物路径（P1-4 定死）**：`directories.output: release` 相对 `apps/desktop`，因此

| 产物 | 绝对落点（安装/构建机视角） | 在 `apps/desktop` 下执行时的相对路径 |
|---|---|---|
| NSIS 安装器 | `<repo>\apps\desktop\release\SkillRouter-Setup-0.1.0-win-x64.exe` | `release\SkillRouter-Setup-0.1.0-win-x64.exe` |
| 免安装目录 | `<repo>\apps\desktop\release\win-unpacked\SkillRouter.exe` | `release\win-unpacked\SkillRouter.exe` |
| 哈希清单 | `<repo>\apps\desktop\release\SKILLROUTER-SHA256SUMS.txt` | `release\SKILLROUTER-SHA256SUMS.txt` |

`package.json` 的 `dist:win` 脚本必须在 `apps/desktop` 下执行，否则 `electron-builder` 找不到 `electron-builder.yml`。


`build/icon.png`：把 `apps/web/app/icon.svg` 光栅化为 1024×1024 PNG 并提交（脚手架 `scripts/make-icon.mjs` 用 `@resvg/resvg-js`；若该包不可用则**接受** Electron 默认图标并把这条登记为已知差距，不得阻塞出包）。

---

## 3. 文件 / 模块变更计划

> 「新增」= 只创建不改别人；「追加」= 只往既有文件加内容（不改既有语义）。**没有任何「修改」类条目**——这是 INV-1 的直接体现。

| # | 文件 | 类型 | 一句话意图 |
|---|---|---|---|
| 1 | `apps/desktop/package.json` | 新增 | 桌面端包定义：`name: @skillrouter/desktop`、`version: 0.1.0`、`main: dist/main.js`、`private: true`、脚本、精确 pin 的 devDependencies |
| 2 | `apps/desktop/package-lock.json` | 新增 | 锁定依赖（提交，保证可复现） |
| 3 | `apps/desktop/electron-builder.yml` | 新增 | 出包配置（NSIS + dir、extraResources、asar、图标、artifactName） |
| 4 | `apps/desktop/tsconfig.json` | 新增 | 主进程 TypeScript 配置（`module: commonjs`、`strict: true`、`target: es2022`） |
| 5 | `apps/desktop/esbuild.config.mjs` | 新增 | 把 `src/main/main.ts` 与 `src/preload/preload.ts` 打包成 `dist/*.js`（external: `electron`） |
| 6 | `apps/desktop/src/main/main.ts` | 新增 | 应用入口：单实例锁、`userData` 重定、状态机启动、`before-quit` 关停编排 |
| 7 | `apps/desktop/src/main/paths.ts` | 新增 | 安装资源/`userData`/`data`/`logs` 路径解析（开发态回落到仓库路径） |
| 8 | `apps/desktop/src/main/config.ts` | 新增 | `desktop.json` 读取/创建/版本迁移/原子写（`write.tmp` + `rename`） |
| 9 | `apps/desktop/src/main/env.ts` | 新增 | §2.6 的唯一实现 `composeEnv()`（纯函数，被单测覆盖） |
| 10 | `apps/desktop/src/main/ports.ts` | 新增 | 端口占用探测、`netstat -ano` 归属查询、顺序扫描、**端口契约校验（§2.3.1：读 `routes-manifest.json` 与 `desktop.json.apiPort` 三方比对）** |
| 11 | `apps/desktop/src/main/logger.ts` | 新增 | 5 个轮转日志流 + `main.log` 结构化行 |
| 12 | `apps/desktop/src/main/supervisor.ts` | 新增 | `spawnChild()`：管道接日志、探针等待、退出回调、`taskkill` 树杀、退避重启 |
| 13 | `apps/desktop/src/main/services.ts` | 新增 | 四个服务描述（可执行文件、参数、就绪探针、超时、致命性） |
| 14 | `apps/desktop/src/main/startup.ts` | 新增 | §2.5 状态机（唯一推进点，含 bootstrap 摘要落盘） |
| 15 | `apps/desktop/src/main/windows.ts` | 新增 | splash / 主窗口 / 错误窗口 / 对话框（含「打开日志」按钮） |
| 16 | `apps/desktop/src/main/menu.ts` | 新增 | 菜单：重载、开发者工具、打开数据目录、打开日志目录、复制诊断信息、关于 |
| 17 | `apps/desktop/src/main/ipc.ts` | 新增 | 4 个 IPC 处理器（版本/状态/打开目录/重启服务） |
| 18 | `apps/desktop/src/main/smoke.ts` | 新增 | `--smoke-test` 实现：探针 + 登录 + 写出 JSON + 退出码（§4.4） |
| 19 | `apps/desktop/src/preload/preload.ts` | 新增 | `contextBridge.exposeInMainWorld('skillrouterDesktop', …)`，仅 4 个方法 |
| 20 | `apps/desktop/src/shared/constants.ts` | 新增 | 端口默认值、超时、产品名、错误码常量 |
| 21 | `apps/desktop/src/shared/types.ts` | 新增 | `DesktopConfig`、`SmokeReport`、`ServiceStatus` 等类型 |
| 22 | `apps/desktop/splash.html` | 新增 | 启动画面（内联样式、显示当前阶段与 5 条进度） |
| 23 | `apps/desktop/build/icon.png` | 新增 | 1024×1024 应用图标（由 `apps/web/app/icon.svg` 光栅化） |
| 24 | `apps/desktop/python/launcher.py` | 新增 | sidecar 入口（§2.7 骨架）：`--role` 三态 + `--version` + `--data-dir` 优先级 + **迁移硬校验（P0-3）** |
| 25 | `apps/desktop/python/skillrouter-sidecar.spec` | 新增 | PyInstaller spec（datas 双布局 + hiddenimports + excludes） |
| 26 | `apps/desktop/python/requirements-build.txt` | 新增 | `-r ../../apps/api/requirements.txt` + `pyinstaller==6.12.0` |
| 27 | `apps/desktop/scripts/build-sidecar.mjs` | 新增 | 解释器来源校验（禁止 conda）→ 装构建依赖 → PyInstaller → **双检自检（bootstrap + api 探活，净化 env）** |
| 28 | `apps/desktop/scripts/build-web.mjs` | 新增 | `npm ci` → `SKILLROUTER_API_URL=http://127.0.0.1:18100 next build` → 组装 standalone + static + public |
| 29 | `apps/desktop/scripts/build-gateway.mjs` | 新增 | 网关 `npm ci && npm run build` → 复制 dist + 生产依赖 |
| 30 | `apps/desktop/scripts/build-all.mjs` | 新增 | 顺序编排 4 步构建 + 资源汇总（失败即停，打印耗时） |
| 31 | `apps/desktop/scripts/verify-artifact.mjs` | 新增 | 校验产物存在/大小/资源完整性、`--version` 可执行、`routes-manifest.json` 端口契约，生成 `apps/desktop/release/SKILLROUTER-SHA256SUMS.txt` |
| 32 | `apps/desktop/scripts/smoke.mjs` | 新增 | 用**净化 PATH** 启动 `win-unpacked\SkillRouter.exe --smoke-test` 并解析报告（§4.4）；**退出后自己做孤儿进程检查**（`clean_shutdown`） |
| 33 | `apps/desktop/scripts/make-icon.mjs` | 新增 | SVG → PNG 图标生成（可选依赖，缺失则跳过并告警） |
| 34 | `apps/desktop/tests/env.test.ts` | 新增 | 单测：§2.6 环境表逐项断言（含「不设 `SKILLROUTER_MASTER_KEY`」、**`SKILLROUTER_INTERNAL_URL` 同时出现在 api 与 gateway 的 env**、**继承的 `SKILLROUTER_*`/`AGENT_*` 被剔除**） |
| 35 | `apps/desktop/tests/config.test.ts` | 新增 | 单测：`desktop.json` 创建/损坏恢复/版本迁移/原子写 |
| 36 | `apps/desktop/tests/ports.test.ts` | 新增 | 单测：占用检测与扫描（用真实 `net.createServer` 占端口） |
| 37 | `apps/desktop/tests/startup.test.ts` | 新增 | 单测：注入假 supervisor，断言 §2.5 全部转移与错误码 |
| 38 | `apps/desktop/README.md` | 新增 | 构建/出包/双击验收/排错的唯一入口（命令 + 期望输出） |
| 39 | `docs/plans/desktop-electron/spec.md` | 新增 | 本文件（设计契约） |
| 40 | `docs/runbook/desktop-app.md` | 新增 | 运维手册：数据目录、备份/恢复、端口冲突、日志、卸载与升级、错误码表 |
| 41 | `.gitignore` | 追加 | 忽略 `apps/desktop/release/`、`apps/desktop/build/`、`apps/desktop/dist/`、`apps/desktop/resources/`、**`apps/desktop/node_modules/`**（P1-14：electron 一套 ≥300 MB，漏了它 `git status` 会瞬间上万文件） |
| 42 | `docs/README.md` | 追加 | 文档地图加一行「桌面端（Electron）→ plans/desktop-electron/spec.md / runbook/desktop-app.md」 |
| 43 | `README.md` | 追加 | 「3 分钟跑起来」下加一个「桌面端（免 Python/Node）」小节，指向上面的文档 |
| 44 | `Makefile` / `make.ps1` | 追加（可选） | `desktop` / `desktop-smoke` 目标，转发到 `apps/desktop` 的两条 npm 脚本 |
| 45 | `.github/workflows/desktop-app.yml` | 新增（可选） | `windows-latest`：构建 → 冒烟 → 上传 `release/*.exe` 与 `smoke-report.json` 工件 |

---

## 4. 接口设计

### 4.1 桌面可执行文件 CLI

| 参数 | 语义 |
|---|---|
| （无） | 正常启动：状态机 + 主窗口 |
| `--smoke-test` | 无窗口自检：走完 `preparing → ready`，执行 §4.4 检查，写报告后退出（`0` 全部通过 / `1` 有失败）。**要求当前无其它实例在运行**（单实例锁优先，见 §2.3.2；被占用时立即退出码 1 并提示，`scripts/smoke.mjs` 负责前置检查） |
| `--smoke-out <path>` | 冒烟报告输出路径，默认 `<userData>\logs\desktop\smoke-report.json` |
| `--data-dir <path>` | 覆盖数据目录（默认 `<userData>\data`），供冒烟与排错使用；**不影响** `desktop.json` 位置。优先级：`--data-dir` > `SKILLROUTER_DATA_DIR` > 默认（与 sidecar 侧一致，见 §2.7 P1-13） |
| `--dev` | 开发者模式：不启动任何子进程，直接把窗口指向 `http://localhost:3000`（配合 `start.ps1` 使用）。**跳过 `preparing` 的产物校验**（安装包外的 `resources\` 不存在，见 §2.5 P1-15），也不做端口契约校验 |
| `--verbose` | 把 `main.log` 级别降到 debug |
| `--version` | 打印 `Skill Router <appVersion> (electron <ver>)` 到 stdout 并以 0 退出（不建窗口、不 spawn）。**两个 exe 各自都有 `--version`**：`SkillRouter.exe --version`（Electron 侧）与 `skillrouter-server.exe --version`（sidecar 侧，§2.7），`verify-artifact.mjs` 两个都会断言（P1-5） |


### 4.2 preload → 渲染进程的接口（唯一暴露面）

```ts
export interface SkillRouterDesktop {
  version: string;                 // 应用版本
  platform: 'win32';
  getStatus(): Promise<ServiceStatus>;      // {state, api, gateway, worker, web, degraded, errorCode}
  openDataDir(): Promise<void>;              // shell.openPath(<data>)
  openLogDir(): Promise<void>;               // shell.openPath(<userData>\logs\desktop)
  restartServices(): Promise<void>;          // 逆序关停 → 重新走状态机（最多 1 次/30s）
}
```

控制台前端**不消费**这些方法（INV-1）；它们通过菜单与错误窗口使用，将来若要在页面内显示桌面状态，再单独提需求。
实现约束：窗口配置 `sandbox: true` + `contextIsolation: true`，因此 preload **不得**使用 `node:` 内置模块或第三方包；`version` / `platform` 由主进程通过 `ipcRenderer.invoke` 取值后经 `contextBridge` 暴露（不要依赖 `process.versions`，它在沙箱 preload 里只是受限子集）。

### 4.3 复用的既有 HTTP 接口（不新增后端接口）

| 用途 | 方法/路径 | 期望 |
|---|---|---|
| API 就绪 | `GET http://127.0.0.1:18100/healthz` | `200`，`ok === true`，`initialized === true`，`db.schema_version === 2` |
| 网关就绪 | `GET http://127.0.0.1:18102/healthz` | `200`（无鉴权，返回引擎版本与工具摘要） |
| 控制台就绪 | `GET http://127.0.0.1:18101/login` | `200`（HTML 含登录表单） |
| 反代链路 | `GET http://127.0.0.1:18101/api/health` | `200`，`{"status":"ok",...}`（证明 rewrite → api 生效） |
| 登录闭环 | `POST http://127.0.0.1:18101/api/admin/auth/login`，body `{"username":"admin","password":"adminjr"}` | `200`，`Set-Cookie` 含 `sr_session` 与 `sr_csrf`，body `admin.must_change_password === true`（首次） |

> 冒烟测试**必须**做第 4、5 行。只探 `/healthz` 的冒烟会给出「绿灯但完全不可用」的假证据。

### 4.4 `--smoke-test` 报告契约（JSON，落盘 + 打到 stdout）

```json
{
  "ok": true,
  "appVersion": "0.1.0",
  "electron": "33.4.11",
  "startedAt": "2026-10-08T10:00:00.000Z",
  "durationMs": 41873,
  "ports": { "api": 18100, "gateway": 18102, "web": 18101 },
  "dataDir": "C:\\Users\\u\\AppData\\Roaming\\SkillRouter\\data",
  "resources": { "sidecar": "…\\resources\\python\\skillrouter-server.exe", "web": "…\\resources\\web\\server.js", "gateway": "…\\resources\\gateway\\dist\\src\\main.js" },
  "externalRuntime": { "pythonOnPath": false, "nodeOnPath": false, "pathUsed": "C:\\Windows\\system32;C:\\Windows" },
  "checks": [
    { "name": "resources_present", "ok": true },
    { "name": "port_contract", "ok": true, "apiPort": 18100, "manifestPort": 18100 },
    { "name": "sidecar_bootstrap", "ok": true, "code": 0, "migrationMode": "empty_db_upgrade", "adminCreated": true },
    { "name": "api_healthz", "ok": true, "status": 200, "schemaVersion": 2, "initialized": true },
    { "name": "gateway_healthz", "ok": true, "status": 200 },
    { "name": "gateway_platform_reach", "ok": true, "internalUrl": "http://127.0.0.1:18100", "dynamic": "skipped", "reason": "engine_not_configured" },
    { "name": "web_login_page", "ok": true, "status": 200 },
    { "name": "web_api_proxy", "ok": true, "status": 200, "service": "skillrouter-api" },
    { "name": "login", "ok": true, "status": 200, "mustChangePassword": true },
    { "name": "single_instance", "ok": true, "detail": "second launch exited with code 0 and did not spawn services" }
  ],
  "shellChecks": {
    "note": "以下由 scripts/smoke.mjs 在被测进程退出后执行并回填（P1-10）",
    "clean_shutdown": { "ok": true, "detail": "tasklist 中无 skillrouter-server.exe / 网关 node 子进程残留" }
  }
}
```

退出规则：`ok === true` **且** `shellChecks` 全部 `ok === true` → 退出码 `0`；任一 `checks[].ok === false` → 退出码 `1`，并把失败项写进 `main.log`。

**检查项归属（P1-10 修正）**：`checks[]` 是被测进程**运行时**能自证的；`shellChecks` 必须由 `scripts/smoke.mjs` 在被测进程**退出之后**执行 —— 「有没有残留孤儿进程」只有在被测进程死了才能观察。v1.0 把 `clean_shutdown` 塞进被测进程自己写的报告里，因果关系不成立，结果必然是一个写死的 `true`，是全文最脆的一条假证据。

**两个新增检查项的判定口径**：

- `port_contract`（§2.3.1 的运行期那一半）：`desktop.json.apiPort`、`<resources>\web\.next\routes-manifest.json` 里 `/api/:path*` 的目标端口、实际传给 sidecar 的 `--port` —— 三者必须相等，否则 `ok:false` 并中止启动。
- `gateway_platform_reach`（把 P0-1 变成可判定项）：**静态部分必判** —— 断言 `composeEnv('gateway')['SKILLROUTER_INTERNAL_URL'] === 'http://127.0.0.1:<apiPort>'`（纯函数，永远可判定，直接锁死「网关回调打 8000」这个 P0）。**动态部分**（真跑一次 `sr_*` 工具）需要真实模型凭证，按仓库证据口径标 `dynamic: "skipped"` + `reason`，**不得**标成 `ok:true` 冒充已验证（R6）。


**`externalRuntime` 的实现要求**：`scripts/smoke.mjs` 启动被测 exe 时把子进程 `PATH` 替换成 `C:\Windows\system32;C:\Windows`（并清掉 `PYTHONHOME`/`PYTHONPATH`/`NODE_OPTIONS`），以此**证明**「目标机不需要装 Python/Node」（INV-3 的前置条件）。`pythonOnPath`/`nodeOnPath` 由 smoke 侧在净化前用 `where python` / `where node` 探测并记录，作为「本次确实净化过」的对照证据。

### 4.5 桌面端错误码（写进 `docs/runbook/desktop-app.md` 的处置表）

| 码 | 触发 | 处置 |
|---|---|---|
| `E-D01 DESKTOP_PORT_IN_USE` | 18100 被非 SkillRouter 进程占用 | 关闭占用程序 / 重启机器 / 用 `netstat -ano \| findstr :18100` 定位 |
| `E-D02 DESKTOP_RESOURCE_MISSING` | `resources\` 下缺件（多半是安装包被截断或被杀软删除） | 重新安装；查看 `skipped` 记录 |
| `E-D03 DESKTOP_BOOTSTRAP_FAILED` | 迁移/初始化失败。**两个来源**：① `bootstrap()` 抛异常；② launcher 的迁移硬校验不通过（退出码 **21**：`user_version` / `alembic head` / `alembic_version` 任一不符）—— 后者与 `SKILLROUTER_ENV` 无关，**永远阻断**（P0-3） | 看 `logs\desktop\api.log` 与 `main.log` 里的 `[FATAL] E-D03` 行；备份后按 `runbook/backup-restore.md` 处理 |
| `E-D04 DESKTOP_API_TIMEOUT` | 90 s 内 `/healthz` 未就绪 | 常见原因：数据目录不可写、库被占用、杀软拦截 exe |
| `E-D05 DESKTOP_WEB_TIMEOUT` | 120 s 内 `/login` 未就绪 | 检查 `web.log`；多为 `resources\web\.next\static` 缺失 |
| `E-D07 DESKTOP_PORT_CONTRACT_MISMATCH` | `desktop.json.apiPort` 与 `routes-manifest.json` 烧死的端口对不上且无法自动校正（§2.3.1） | 删除 `%APPDATA%\SkillRouter\desktop.json` 让其按构建期值重建；仍不符则重装（安装包与 web 产物不同源） |
| `W-D01 DESKTOP_GATEWAY_DOWN` | 网关起不来 | 控制台可用；Agent 功能不可用；检查 `gateway.log`（常见：`agent-policy.json` 缺失 → bootstrap 未成功） |
| `W-D06 DESKTOP_INSTANCE_RUNNING` | 本实例拿到单实例锁、但 18100 已被**另一个安装/会话**的 SkillRouter 占用（§2.3.2 第 2 条） | 提示「检测到另一个 SkillRouter 正在使用 18100」；只提供「退出 / 打开日志」。**注意**：同安装同用户的第二次双击由单实例锁直接处理（退出码 0、聚焦已有窗口），**不产生任何错误码** —— v1.0 把 `E-D06` 列为错误码是错的（P1-11） |
| `W-D02 DESKTOP_STRAY_ENV_FILE` | `<data>\.env` 存在（`Settings` 会按 cwd 读它，见 §2.6 副作用） | 只警告不删；如非本人创建，建议改名备份后用「打开数据目录」核对 |


---

## 5. 数据模型

### 5.1 `%APPDATA%\SkillRouter\desktop.json`

```jsonc
{
  "schemaVersion": 1,
  "apiPort": 18100,            // 必须与 web 构建期 SKILLROUTER_API_URL 一致
  "gatewayPort": 18102,        // 运行时可能上移（记录实际值）
  "webPort": 18101,            // 运行时可能上移（记录实际值）
  "gatewaySecret": "<48 chars base64url>",
  "env": "development",
  "payloadPolicy": "redacted_preview",
  "enginePolicy": "notify",
  "enginePin": "0.2.34@0a119a64890717b0e3017edbaaf00944babf19a8",
  "adminInitialized": false,
  "firstRunAt": "2026-10-08T10:00:00.000Z",
  "lastVersion": "0.1.0"
}
```

规则：

- 写入必须**原子**：写 `desktop.json.tmp` → `fs.renameSync`。损坏的 JSON → 备份为 `desktop.json.corrupt-<ts>` 后用默认值重建（并写 `main.log`），**不阻断启动**。
- `schemaVersion` 升版时在 `config.ts` 里写迁移函数（`1 → 2` 的占位已留），不得就地猜测字段。
- `apiPort` 在 UI 上**不可改**（改了 web 的 rewrite 就不认），只允许通过重新安装/构建变更；`gatewayPort`/`webPort` 允许被运行时上移并被写回。

### 5.2 运行时目录（数据模型的一部分）

| 路径 | 内容 | 生命周期 |
|---|---|---|
| `<userData>\data\skillrouter.db` | 全部业务数据（49 表） | 备份/恢复按既有 `scripts/backup.py` 语义（桌面端可在菜单里「打开数据目录」后手工执行） |
| `<data>\credentials-master.key` | 主密钥（base64url，32 字节） | **丢失 = 凭证全废**；桌面端不提供轮换 UI（沿用 `scripts/rotate_master_key.py`） |
| `<data>\file-signing.key` | 文件签名密钥 | 同上 |
| `<data>\desktop-first-run.json` | 首次运行摘要（只含指纹） | 覆盖写 |
| `<data>\agent-policy.json` | 网关策略（含 12 个 `sr_*` 工具白名单） | 每次 bootstrap 重写 |
| `<userData>\logs\desktop\*.log` | 5 路日志 + `main.log` | 10 MB × 5 轮转 |
| `<userData>\logs\desktop\smoke-report.json` | 冒烟报告 | 每次冒烟覆盖 |

### 5.3 日志行格式

```text
[2026-10-08T10:00:12.345Z] [api] uvicorn running on http://127.0.0.1:18100 (Press CTRL+C to quit)
[2026-10-08T10:00:12.401Z] [main] state=starting_api probe=/healthz attempt=3 status=200
[2026-10-08T10:00:41.900Z] [main] state=ready ports={"api":18100,"gateway":18102,"web":18101} degraded=false
```

`main.log` 的行必须能被 grep：`state=` / `error=` / `pid=` / `exit=` / `restart=` 五个键固定，便于排错脚本与未来的自动诊断。

### 5.4 数据库变更

**无。** 桌面端不新增表、不新增字段、不改迁移；`alembic` head 仍是既有 `0003_iteration2`（`PRAGMA user_version=2`）。桌面数据目录里的库与开发/容器库同构，可以互相 `backup.py` / `restore.py`。

---

## 6. 测试与验收标准

### 6.1 单元测试（`vitest run`，目标 ≥ 34 项）

| 用例 | 断言要点 |
|---|---|
| `env.test.ts`（14 项） | §2.6 每一行都存在且取值正确；**不存在** `SKILLROUTER_MASTER_KEY`；路径全是绝对路径；`ENGINE_PIN` 无 `@`；`PORT`/`HOSTNAME` 只出现在 web 的 env；**`SKILLROUTER_INTERNAL_URL` 在 api 与 gateway 两份 env 里都存在且都指向 api 端口**（P0-1）；**继承的 `SKILLROUTER_*`/`AGENT_*`/`ENGINE_*`/`PYTHON*` 被剔除**（P1-12）；`SKILLROUTER_ENGINE_TOOLCHAIN === 'gateway'` |
| `config.test.ts`（5 项） | 首次创建生成 ≥32 字符 `gatewaySecret`；损坏 JSON 自动重建并备份；原子写不产生半文件；`apiPort` 不可被外部改动；`schemaVersion` 迁移幂等 |
| `ports.test.ts`（6 项） | 真实占端口后 `isPortFree` 返回 false；扫描返回「起点之后的第一个可用端口」；`findOwner` 能给出 PID（Windows）；api 端口被占用时抛出 `E-D01`；**占用者 `/healthz` 返回 `service === 'skillrouter-api'` 时抛 `W-D06` 而不是 `E-D01`**（P2-7 补）；**`checkPortContract()` 在 manifest 端口 ≠ `desktop.json.apiPort` 时给出 `repair` / `E-D07` 两种结果** |
| `startup.test.ts`（9 项） | 注入假 supervisor，逐项验证 §2.5 的每条成功/失败转移；`gateway` 失败只降级不中止；`E-D03/E-D04/E-D05` 码正确；重启退避 1/3/9 s；**`--dev` 模式跳过产物校验、缺 `resources` 不报 `E-D02`**（P1-15） |


### 6.2 集成 / 冒烟（机器可判定）

```bat
cd apps/desktop
npm run dist:win
npm run verify
npm run smoke
```

期望：

- `dist:win` → 退出码 0，产出 `apps/desktop/release/SkillRouter-Setup-0.1.0-win-x64.exe`、`apps/desktop/release/win-unpacked/SkillRouter.exe`（路径口径见 §2.13）。
- `verify` → 逐条断言：
  1. `win-unpacked\resources\{python,web,gateway}` 三个目录存在、`app.asar` 存在、`resources\web\server.js` 存在；
  2. **`skillrouter-server.exe --version` 与 `SkillRouter.exe --version` 都以退出码 0 正常打印**（P1-5：v1.0 断言的这个参数当时并不存在）；
  3. **`resources\web\.next\routes-manifest.json` 里 `/api/:path*` 的目标是 `http://127.0.0.1:18100`**（R4 前移）；
  4. 写出 `apps/desktop/release/SKILLROUTER-SHA256SUMS.txt`（安装器 + `SkillRouter.exe` + 三个资源目录的清单哈希）。
- `smoke` → 打印 §4.4 的 JSON，`ok: true`，退出码 0，且 `externalRuntime.pathUsed` 是被净化过的 PATH；**外壳在进程退出后完成 `shellChecks`**：`clean_shutdown`（`tasklist` 里没有 `skillrouter-server.exe` 与网关 node 子进程残留）+ `artifact_hash`。
- 三步全绿的输出（含 JSON 原文、哈希清单、`state=idle→ready` 时间差）贴进 `docs/runbook/verification-report.md`；**只写「通过」不贴输出不算证据**。


### 6.3 手动「双击可用」验收清单（必须逐条打勾并留证据）

| # | 步骤 | 通过标准 |
|---|---|---|
| M1 | 在没有 Python / 没有 Node 的 Windows 10 或 11 x64（可用干净虚拟机，或临时把 `python`、`node` 从 PATH 移除并重启资源管理器） | — |
| M2 | 双击 `SkillRouter-Setup-0.1.0-win-x64.exe` → 安装（可改目录）→ 完成 | 安装成功；桌面与开始菜单出现 `Skill Router` 快捷方式 |
| M3 | 双击 `Skill Router` | 冷启 ≤120 s / 热启 ≤45 s 出现 splash → 登录页（不出现空白页/`ERR_CONNECTION_REFUSED`）；耗时取自 `main.log` 的 `state=idle` 与 `state=ready` 的时间差并登记（口径见 §0 INV-3，P1-8） |
| M4 | `admin` / `adminjr` 登录 | 跳转 `/change-password`（强制改密），改密后进入控制台 |
| M5 | 控制台首页四态徽标、`/providers` 新建提供商 | 数据落库；刷新不丢 |
| M6 | 关闭窗口 → 任务管理器 | `SkillRouter.exe`、`skillrouter-server.exe`、网关/node 相关子进程**全部消失** |
| M7 | 再次双击 | 直接进入登录页（数据仍在，不重新初始化） |
| M8 | 运行中再次双击 | 已有窗口被聚焦，不出现第二个实例 |
| M9 | 卸载（保留应用数据） | 安装目录删除，`%APPDATA%\SkillRouter` 保留 |
| M10 | 数字签名状态 | 记录「未签名 → SmartScreen 提示『更多信息 → 仍要运行』」为**已知差距**（见 §7 R1） |

`M1~M9` 的时间、机器、截图/日志路径全部登记到 `docs/runbook/verification-report.md` 的新增章节「桌面端（Electron）实测」。**未登记的条目不得写成「已验证」。**

### 6.4 回归（现有证据不得退化）

桌面端是纯增量（INV-1），因此下列既有套件必须仍然全绿，并在报告里给出同一次运行的结果：

| 套件 | 命令 | 期望 |
|---|---|---|
| 后端 | `cd apps/api && python -m pytest tests -q` | 251 passed |
| 网关 | `cd agents/aragon-gateway && npx vitest run` | 48 passed |
| CLI | `cd packages/skillrouter-cli && npx vitest run` | 37 passed |
| 前端类型/构建 | `cd apps/web && npx tsc --noEmit` + `npm run build` | 0 错误 / 构建成功 |
| 桌面端单测 | `cd apps/desktop && npx vitest run` | ≥34 passed |

### 6.5 Definition of Done（本功能的验收口径）

1. `apps/desktop/**` 全部落地，`npm run dist:win` 在干净 `node_modules` 上可复现出包。
2. `npm run smoke` 通过（含外壳 `shellChecks`），报告归档（含 `apps/desktop/release/SKILLROUTER-SHA256SUMS.txt`）。
3. §6.3 的 M2~M9 逐条有人真的点过并登记证据。
4. §6.4 五套命令全绿，输出贴进报告。
5. `docs/runbook/desktop-app.md` 覆盖 §4.5 全部错误码（`E-D01`~`E-D05`、`E-D07`、`W-D01`、`W-D06`）的处置。
6. `README.md` / `docs/README.md` 各多一行指路，`git status` 里能看到上述新增文件且**不含** `apps/desktop/{node_modules,release,build,dist,resources}`。
7. **§2.3.1 的三条端口契约断言（构建期 + 运行期 + 自动校正）都有各自的证据行**（`port_contract` 检查项 + `verify` 输出 + `main.log` 的 `event=port_contract_repair`）。


---

## 7. 风险与缓解

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| R1 | 未做代码签名 → SmartScreen / Defender 警告 | 用户不敢装 | 文档明确写出提示文案与「仍要运行」路径；签名作为后续独立任务（`CSC_LINK`/`CSC_KEY_PASSWORD` 已在 builder 里兼容）。**禁止**谎称「已签名」。 |
| R2 | PyInstaller 产物被杀软误判 | 首次启动失败 | onedir（不用 onefile）降低误杀率；`verify-artifact.mjs` 记录 exe 的 SHA256 与大小；文档给出「添加排除项」处置 |
| R3 | PyInstaller 漏 hidden import（uvicorn/alembic/sqlalchemy dialect） | `--role api` 启动即崩 | spec 用 `collect_submodules` 整包收集 alembic/uvicorn/sqlalchemy/fastapi，并显式列 `skillrouter_api.main`；构建脚本**双检**：`--role bootstrap` + **`--role api` 起服务探活**（v2 修正，评审 P0-2：只跑 bootstrap 恰好绕开必崩的那条路径）；`dist:win` 前必须先跑通两项 |
| R4 | Next rewrite 目标端口被改 → `/api` 全 502 | 控制台假死 | D6 + **三层防线**（v2 加强，评审 P1-17）：① `build-web.mjs` 构建期断言 `routes-manifest.json` 含 `127.0.0.1:18100`；② 运行期 `preparing` 做端口契约三方比对，不一致修复或 `E-D07`；③ `verify-artifact.mjs` 出包后再 grep 一次。v1.0 只有第 ③ 层，运行期无拦截 |
| R5 | `SKILLROUTER_ENV=development` 放宽了 `production` 强制的 Secure Cookie 与「迁移不一致即致命」 | 安全姿态略低于生产；**迁移不一致不再致命** | §2.12 补偿控制；**「致命」语义已有明确落点**（v2 修正，评审 P0-3）：launcher 的 `_migration_problem()` 与 `SKILLROUTER_ENV` 无关，不符即退出码 21 → 主进程映射为 `E-D03` 并中止启动；文档写明「桌面端 ≠ 生产部署，生产请用 compose」；**禁止**用「R5 已缓解」当作不实现该校验的理由 |
| R6 | 内置 Agent / 引擎需要真实模型凭证 | Agent 功能可能不可用 | 与仓库既有口径一致：这部分保持**待验证**；网关失败降级为 `W-D01`，不阻塞控制台 |
| R7 | 安装包体积大（Electron ~180 MB + Python ~90 MB + web ~80 MB） | 下载慢 | NSIS 最高压缩；文档列出体积构成；接受现状（换 Tauri 不现实：需要 Python 与 Next 两个运行时） |
| R8 | 出包依赖网络（electron zip / nsis / PyInstaller 包） | CI 或离线机失败 | 全部依赖精确 pin + 本机缓存已 warm（electron 33.4.11 zip、nsis-3.0.4.1、winCodeSign、7zip）；离线场景给出 `ELECTRON_MIRROR` / `ELECTRON_BUILDER_BINARIES_MIRROR` 与 `PIP_INDEX_URL` 说明 |
| R9 | 关闭时留下孤儿进程 → 下次启动 `DB_BUSY` / 端口占用 | 「第二次打不开」 | §2.10 的逆序关停 + `taskkill /T /F` 兜底；冒烟含 `clean_shutdown` 检查；启动期端口冲突有明确错误码 |
| R10 | 升级安装包后 schema 迁移失败 | 用户库不可用 | 每次启动都跑幂等 bootstrap；`E-D03` 弹窗提示「先备份再升级」；文档给出手工 `bootstrap.py` 排错路径 |
| R11 | 路径含空格/中文/OneDrive 重定向的 `%APPDATA%` | 子进程参数被拆断或路径解析失败 | 一律 `spawn(exe, [args])`（数组形式，不经 shell）；所有路径 `path.resolve` 后以绝对路径注入；冒烟在 `--data-dir` 里带空格跑一次（`C:\Temp\Skill Router Data`） |
| R12 | `release/` 与 `build/` 被误提交 | 仓库膨胀 | `.gitignore` 追加 **5** 条（含 `apps/desktop/node_modules/`，P1-14）；`verify-artifact.mjs` 不写仓库内路径以外的东西；DoD 第 6 条要求 `git status` 复核 |
| R13 | 用 conda 解释器构建 sidecar → 产物依赖 conda DLL | 干净机启动即缺 DLL（构建机看不出来） | §2.7 构建解释器约束：`build-sidecar.mjs` 检测 `sys.base_prefix`/版本串含 `conda|Anaconda|Miniconda` 即失败，强制官方 CPython 3.11 venv；`verify` 的两个 `--version` 断言 + 净化 PATH 冒烟作为兜底证据 |
| R14 | 网关 `SKILLROUTER_INTERNAL_URL` 回落到 8000（P0-1 回归） | 探针全绿、Agent 全废 | env 表显式标注两处消费者；`env.test.ts` 断言该键在 api 与 gateway 两份 env 中都存在（P0-1）；冒烟 `gateway_platform_reach` 静态项必判；`main.log` 启动时打印 `gateway.internal_url=…` 便于事后核对 |


---

## 8. 实现顺序（给下游工程师的 9 步）

1. **脚手架**：`apps/desktop/package.json` + `tsconfig.json` + `esbuild.config.mjs`；`npm i` 出 `package-lock.json`。此时 `npm run build:main` 应能产出空壳 `dist/main.js`。
2. **纯函数层**：`shared/constants.ts`（含环境变量前缀黑名单）、`shared/types.ts`、`main/env.ts`、`main/config.ts`、`main/ports.ts`（含 `checkPortContract()`）+ 对应单测（这四组测试先绿，后面才有地基）。
3. **sidecar**：`python/launcher.py` + spec + `scripts/build-sidecar.mjs`；**先过解释器来源校验**（禁 conda），再跑通**双检**：`--role bootstrap` 与 `--role api` 起服务探活（v2 修正，评审 P0-2）。这一步不过，后面所有步骤都会白跑。
4. **web 宿主**：`scripts/build-web.mjs`；手工 `node resources/web/server.js` 验证能起（api 未起时 `/login` 仍应 200，但 `/api` 502——这是预期，不是 bug）；确认第 5 步的 `routes-manifest.json` 端口断言为绿。
5. **gateway 宿主**：`scripts/build-gateway.mjs`；手工跑通 `/healthz`；**顺手核对 `SKILLROUTER_INTERNAL_URL` 已注入网关**（P0-1：这是唯一能在本步就验证网关回调目标的时机）。
6. **监管层**：`main/logger.ts`、`main/supervisor.ts`（含 `windowsHide: true`）、`main/services.ts`、`main/startup.ts` + `startup.test.ts`。
7. **窗口层**：`main/windows.ts`（splash/错误窗口）、`main/menu.ts`、`main/ipc.ts`、`preload/preload.ts`、`main/main.ts`；本机 `npm run dist:win` 出 `win-unpacked` → 手动双击验证 M3~M8（重点确认**没有黑色控制台窗口弹出**，P1-9）。
8. **冒烟与出包校验**：`main/smoke.ts` + `scripts/smoke.mjs`（含退出后的 `shellChecks`）+ `scripts/verify-artifact.mjs`；`npm run smoke` 绿。
9. **文档与证据**：`apps/desktop/README.md`、`docs/runbook/desktop-app.md`、`README.md`/`docs/README.md` 追加、`.gitignore` 追加 5 条、报告登记（§6.2/§6.3/§6.4 输出 + 哈希 + 机器信息 + 冷启/热启耗时）。

> 如果第 8 步的 `smoke` 出现 `api_healthz` 超时，先看 `logs\desktop\api.log` 的第一条 Traceback：90% 的情况是「迁移目录没进 `_internal`」或「`skillrouter_api.main` 没进依赖图（P0-2）」，两者都在 §2.7 里有明确对策。
> 如果 `smoke` 全绿但控制台 Agent 页报「网关不可达」，先查 `main.log` 里的 `gateway.internal_url=`：它必须是 `http://127.0.0.1:18100`（P0-1），不是 8000。


---

## 9. 与既有文档的关系

- **不替代** `docs/plans/skill-router/spec.md`：后端契约、表结构、错误码、安全模型在那里；桌面端只是在它的外面加一层发行壳。
- **补充** `start.ps1`：开发模式继续用一键脚本（8000/3000/8500 + `var\`）；桌面端是**另一条并行路径**（181xx + `%APPDATA%\SkillRouter\data`）。两者可以同时运行，互不影响。
- **补充** `deploy/docker-compose.yml`：容器是多服务自托管生产方案；桌面端是单机单用户方案。桌面端**不**声称生产可用（R5）。
- 状态口径沿用仓库规则：本文所有能力在实现并登记证据前，一律视为**未实现**；实现后按「已实现 / 部分实现 / 待验证」如实标注，`docker`/签名/真实模型凭证相关条目继续保持**待验证**。
- **非目标（P2-6，显式声明，避免下游误读）**：本轮**不做** ①自动更新（auto-updater / Squirrel）；②多用户或 RBAC；③代码签名与公证；④把桌面端当生产部署方案；⑤macOS / Linux 打包；⑥在控制台页面内展示桌面状态（INV-1）。这些若要做，各自独立立项，不得夹带进本契约。

---

## 评审结论

**结论：有条件通过（Conditionally Approved）。**

### 判定依据

| 维度 | 判定 | 说明 |
|---|---|---|
| 可行性 | **通过** | 目标栈（Electron 33.4.11 / electron-builder 26.15.3 / PyInstaller 6.12.0 / Next 15 standalone / Node 20.18 承载网关）全部可落地，且本机 `%LOCALAPPDATA%\electron\Cache\electron-v33.4.11-win32-x64.zip` 与 `electron-builder\Cache\{nsis-3.0.4.1,nsis-resources-3.4.1,winCodeSign,7zip@1.0.0}` 均已存在，出包可离线。§2.7 双布局迁移目录、§2.8 tracing root 结论均与源码/现状一致。 |
| 完整性 | **修复后通过** | 原缺 3 个致命边界（P0-1 网关回调目标、P0-2 冻结后 import 图、P0-3 迁移不一致不致命）与 14 个交付/验收漏洞（P1-4~P1-17），现已全部在正文补齐并给出判定口径；发布面（升级、卸载、排错、错误码、证据登记）闭环。 |
| 一致性 | **通过** | INV-1（纯增量）在修复后**更强**：P0-3 与 P1-7 的修法都选择「在 launcher / 桌面层自证」，而不是改 `apps/api` 或 `apps/web`；env 表与 `settings.py` / 网关 `config.ts` 的实际读法逐键对齐；`development` 环境决策（D7）的四处副作用已逐一说明并给出补偿。 |
| 规模合理性 | **通过** | 45 文件 / ≥34 单测 / 24 项手工清单对「发行工程化」这一目标是恰当的；v2 没有新增文件，只把已有文件的责任写清楚（新增能力落在 `ports.ts` / `launcher.py` / `build-sidecar.mjs` / `smoke.mjs` / `verify-artifact.mjs` 既有条目内）。9 项 P2 明确不阻塞出包，避免范围蔓延。 |

### 通过的条件（实现节点必须逐条满足，缺一不可判「完成」）

1. **P0-1**：`composeEnv()` 给 **api 与 gateway 都注入** `SKILLROUTER_INTERNAL_URL`；`env.test.ts` 有对应断言；冒烟 `gateway_platform_reach` 静态项为绿。
2. **P0-2**：`--role api` 用**ASGI 对象**（非 import 字符串）且 `skillrouter_api.main` 在依赖图内；`build-sidecar.mjs` 的**双检**（bootstrap + api 探活）都绿，否则不出包。
3. **P0-3**：`launcher.py` 的 `_migration_problem()` 生效，迁移不一致时退出码 **21**；`E-D03` 的处置表写明这条来源；`SKILLROUTER_ENV=development` 不得成为跳过该校验的理由。
4. **P1-5/P1-17/P1-10 相关的可判定性**：`verify` 的两个 `--version` 断言、三层端口契约断言、`shellChecks`（孤儿进程 + 哈希）都必须真实执行，且**失败就让命令非 0 退出** —— 不允许写成「打印后继续」。
5. **证据纪律**：§6.2 / §6.3 / §6.4 的输出、冷启与热启耗时、哈希清单全部登记进 `docs/runbook/verification-report.md`；未登记的一律保持「待验证」，尤其：真实模型凭证相关能力、代码签名状态（R1）、Linux/macOS 形态。
6. **不得回归的既有证据**：后端 251 / 网关 48 / CLI 37 / 前端 typecheck+build / 桌面端 ≥34 单测，全部在同一次运行里给出结果（§6.4）。

### 明确不接受的做法

- 把 `E-D06` 当错误码继续用（已改为 `W-D06`，§2.3.2）；
- 用「`R5` 已缓解」当作不实现迁移硬校验的理由；
- 用被压缩过的 PATH 之外的「看起来能跑」当 INV-3 的证据；
- 让 `clean_shutdown` 由被测进程自证（已移交外壳）；
- 为了让 `verify` / `smoke` 变绿而吞掉退出码。

### 复评触发条件

出现以下任一情况必须重开评审：Electron / PyInstaller 主版本变更（D1/D3）；`apps/api` 的 `settings.py` 环境校验器或 `bootstrap.assert_migrated()` 的致命条件变更（直接影响 P0-3 的前提）；`next.config.mjs` 的 rewrite 策略变更（直接影响 D6 / R4）；`agents/aragon-gateway` 的 env 读取契约变更（直接影响 P0-1 / R14）；端口段 181xx 与 `start.ps1` 默认端口发生重叠。

**评审人**：评审节点（node #4，round 1） · **评审日期**：2026-10-08 · **文档版本**：v2.0

---

## 实施过程发现的方案缺陷

> 口径（按本任务的约束）：实现不得静默偏离设计；凡实测与本文冲突的地方，一律先记录在这里，再按「已修正的做法」实施。
> 全部条目都是**实施期实测**出来的，不是推测。修订后重新跑过：`npx vitest run`（41 项）、`npm run dist:win`、
> `npm run verify`、`npm run smoke` 全绿。

| 编号 | 位置（本文原写法） | 实测事实 | 后果 | 已修正的做法 |
|---|---|---|---|---|
| **I-1** | §2.7 `requirements-build.txt` = `-r ../../apps/api/requirements.txt`（并称「`-r` 相对 cwd」） | pip 解析嵌套 `-r` 是相对**该 requirements 文件所在目录**，不是 cwd：`apps/desktop/python/../../apps/api/...` 实际指向 `apps/apps/api/...` | `pip install -r apps/desktop/python/requirements-build.txt` 直接 `Could not open requirements file` | 改为 `-r ../../../apps/api/requirements.txt`；构建脚本仍固定 `cwd=仓库根`（无害，但**不是** `-r` 的依据） |
| **I-2** | §2.7 PyInstaller spec 的 `datas` = `../../apps/api/migrations`（P2-2 称「相对 SPECPATH 解析」，深度按两层写） | 相对 SPECPATH 解析这一点是对的，但 SPECPATH 是 `apps/desktop/python`，指向仓库根要**三层**：`apps/desktop/python/../../..`。两层会指向 `apps/apps/api/migrations` | PyInstaller 直接报 `Unable to find '...\apps\apps\api\migrations'`，构建失败 | 三条 datas 全部改成 `../../../apps/api/migrations`、`../../../apps/api/migrations`、`../../../scripts/adopt_alembic.py` |
| **I-3** | §2.7 spec 里 `pathex=[]` + hiddenimports 列 `skillrouter_api.main` | 分析期的 `hiddenimports` 只在模块**可被找到**时才有效；`apps/api` 不在 `sys.path` 里时整个 `skillrouter_api` 包都不进依赖图 | 冻结后 `--role bootstrap` 与 `--role api` 都 `ModuleNotFoundError: No module named 'skillrouter_api'`（正是 P0-2 描述的失效，但成因不同：不是字符串 import，而是**分析期找不到包**） | spec 增加 `REPO_ROOT`/`API_ROOT` 计算并设 `pathex=[API_ROOT]` |
| **I-4** | §2.8 步骤 5 / §2.3.1 读 `routes-manifest.json` 的 `rewrites` | Next 15 的 `routes-manifest.json` 里 `rewrites` 是对象 `{beforeFiles, afterFiles, fallback}`（早期版本才是数组），本条规则在 **afterFiles** 里 | 按数组解析得到 `null`：① `build-web.mjs` 把一次**完全正确**的构建判成红（假红）；② 运行期 `readManifestApiPort()` 返回 null，端口契约校验退化成「读不到」分支（假阳性） | `build-web.mjs` 与 `src/main/ports.ts` 都改为「数组 / 对象两种形态都支持」，并抽出 `rewriteGroups()` 复用 |
| **I-5** | §2.13 `extraResources`（三条 `from`/`to`） | electron-builder 26.15.3 打包 `extraResources` 时**跳过 `node_modules`**，即使显式写 `filter: ['**/*']` 也不复制 | 安装出来的 `resources\web\node_modules`、`resources\gateway\node_modules` 缺失 → 网关 `Cannot find package 'fastify'`、web `Cannot find module 'next'`，两者启动即崩（冒烟如实报 `gateway_healthz=FAIL / web_login_page=FAIL`，`clean_shutdown` 仍绿——正是「探针全绿但功能全废」的反面证据） | 把 `node_modules` 拆成**单独的 FileSet**（`from: resources/web/node_modules` → `to: web/node_modules`），让它变成 `from` 的根而不是子目录；父条目加 `!node_modules/**/*` 避免重复 |
| **I-6** | §2.4 / §6.2 断言的产物名 `win-unpacked\SkillRouter.exe` | `productName: Skill Router` 会让 electron-builder 生成 **`Skill Router.exe`**（带空格） | `verify` / `smoke` 的路径断言全部找不到文件 | `electron-builder.yml` 增加 `executableName: SkillRouter` |
| **I-7** | §2.7「构建解释器必须是官方 CPython 3.11 venv」 | 本机没有官方 CPython 3.11（`where python` 只有 Anaconda 与 Microsoft Store 版），但 `uv` 可用 | 直接按原文无法构建 | 用 `uv python install 3.11` + `uv venv --seed --python 3.11 apps/desktop/build/.venv` 得到 standalone CPython **3.11.16**（`base_prefix` 不含 conda，`build-sidecar.mjs` 的来源校验通过）。口径不变：**禁止 conda / Store 版** |
| **I-8** | §2.6「继承环境一律剔除」只约束了**子进程**环境 | 本机 shell 里存在 `ELECTRON_RUN_AS_NODE=1`（agent harness 注入），`verify` / `smoke` 的**父进程**环境会把它传给被测 exe | `SkillRouter.exe --version` 以 node 身份运行并打印 `v20.18.3`；`SkillRouter.exe --data-dir …` 直接 `bad option: --data-dir`（永远进不了主进程） | `verify-artifact.mjs` 与 `smoke.mjs` 增加 `cleanEnv()`：剔除 `SKILLROUTER_*`/`AGENT_*`/`ENGINE_*`/`ELECTRON_RUN_AS_NODE`/`NODE_OPTIONS`/`PYTHONHOME`/`PYTHONPATH`；`verify` 额外断言 `--version` 输出含 `Skill Router` |
| **I-9** | §2.7 检查 1「期望 stdout 是 JSON」 | launcher 的 `--role bootstrap` 会把 alembic 的 `INFO [alembic.runtime.migration] …` 行一起写到 stdout，stdout **不是**纯 JSON | 直接 `JSON.parse(stdout)` 失败，把一次成功的 bootstrap 判成红 | `build-sidecar.mjs` 增加 `extractJson()`（找第一个平衡的 `{}`，跳过字符串内的括号） |
| **I-10** | §2.5 未定义「子进程已退出」时的探针行为 | 若 web 进程启动即崩，`pollUntil` 会白等满 120 s 才报 `E-D05` | 冒烟与用户都要多等两个超时（实测第一次冒烟耗时 2 分 48 秒，其中 2 分钟是在等一个已经死掉的进程） | `pollUntil` 支持 `fatal` 短路：`waitReady` 先检查 `supervisor.has(name)`，进程已退出立刻返回 `E-D05`/`E-D04`（同一轮冒烟从 168 s 降到 12.5 s） |
| **I-11** | §2.10 错误窗口「本地 HTML + 三个按钮」 | 沙箱渲染进程没有 IPC 面（§4.2 只允许四个控制台方法），把按钮做进 HTML 就得再开一套 preload/通道 | 会扩大暴露面，且新增文件不在 §3 清单里 | 错误窗口仍用本地 HTML（错误码 + 尾部日志，可复制），三个动作（重试 / 打开日志 / 退出）由原生 `dialog` 承担；文件清单不变 |
| **I-12** | §2.13 `build/icon.png` 依赖 `@resvg/resvg-js`（「不可用则接受默认图标」） | 该包未安装，而 `electron-builder.yml` 的 `win.icon` 指向 `build/icon.png`，文件不存在时 electron-builder 会直接失败 | 「接受默认图标」实际上不可达 | `scripts/make-icon.mjs` 用纯 Node（`zlib` + 手写 PNG chunk）生成 1024×1024 品牌占位图：零依赖、确定性、可复现 |

### 对「通过条件」的影响

- 6 条通过条件**全部满足**，且 P0-1 / P0-2 / P0-3 三条都有独立证据行（见
  `docs/runbook/verification-report.md`「桌面端（Electron）实测」与 `apps/desktop/release/smoke-report.json`）。
- I-5 是这轮最值得记的一条：它正是 R4/R14 想防的那类「构建机全绿、装出来全废」，
  而**单纯跑单测或只跑 `verify` 都发现不了**——只有 `smoke` 的 `web_api_proxy` / `login` 会红。
  这也反向验证了 §4.3 末尾那句话：只探 `/healthz` 的冒烟会给出假证据。
