# Skill Router 桌面端（Electron）

> 目标：**双击可用**。在一台**没有装 Python、没有装 Node.js** 的 Windows 10/11 x64 机器上，
> 双击 `SkillRouter-Setup-0.1.0-win-x64.exe` → 下一步到底 → 桌面快捷方式 → 登录页 →
> `admin` / `adminjr` 登录 → 强制改密 → 控制台。
>
> 设计契约：[`docs/plans/desktop-electron/spec.md`](../../docs/plans/desktop-electron/spec.md)（v2.0）。
> 运维手册：[`docs/runbook/desktop-app.md`](../../docs/runbook/desktop-app.md)（数据目录、备份、错误码处置）。

桌面端**不是** mock、不是 iframe 套壳、不是「打开浏览器」：它启动的是真实的 FastAPI + 真实 SQLite +
真实 Node 网关 + 真实 Next.js standalone 控制台，四个进程由 Electron 主进程按确定状态机托管。

---

## 1. 产物

| 产物 | 路径（相对仓库根） |
|---|---|
| NSIS 安装器（双击这个） | `apps/desktop/release/SkillRouter-Setup-0.1.0-win-x64.exe` |
| 免安装目录（冒烟/排错用） | `apps/desktop/release/win-unpacked/SkillRouter.exe` |
| 哈希清单 | `apps/desktop/release/SKILLROUTER-SHA256SUMS.txt` |
| 冒烟报告 | `apps/desktop/release/smoke-report.json` |

> 全文只有这一个路径口径：`electron-builder.yml` 的 `directories.output: release` **相对 `apps/desktop`**。

## 2. 一条命令出包

```bat
cd apps\desktop
npm install --include=dev
npm run dist:win
```

`dist:win` = `npm run build` + `electron-builder --win nsis dir --x64 --publish never`。
`build`（`scripts/build-all.mjs`）的顺序固定，任一步失败即停：

1. `build:gateway` → `build/gateway/`（tsc + 仅生产依赖）
2. `build:web` → `build/web/`（`SKILLROUTER_API_URL=http://127.0.0.1:18100` 构建 standalone，并**断言端口契约**）
3. `build:sidecar` → `build/python/skillrouter-server/`（PyInstaller onedir + **双检**）
4. `build:main` → `dist/main.js` + `dist/preload.js`（esbuild）
5. 汇总到 `resources/{python,web,gateway}`
6. `electron-builder` 出 NSIS + `win-unpacked`

### 2.1 Python 边车必须用纯净 CPython 3.11 构建（P1-16）

本机 `where python` 是 Anaconda（`3.11.7 | packaged by Anaconda, Inc.`）。用它跑 PyInstaller 会把
conda DLL 依赖烘进产物，在「没装过 Python 的干净机」上表现为**启动即缺 DLL**。因此：

```bat
uv venv --seed --python 3.11 apps\desktop\build\.venv
set PYTHON_BIN=apps\desktop\build\.venv\Scripts\python.exe
```

`scripts/build-sidecar.mjs` 会先校验解释器来源：命中 `conda|anaconda|miniconda|windowsapps` 或不是
3.11 就直接失败（`PYTHON_BIN` 可覆盖）。

### 2.2 边车双检（P0-2 / P0-3）

`build-sidecar.mjs` 在打完包后**必须**跑两条，任一条红即不出包：

```bat
:: 1) bootstrap（覆盖迁移目录双布局 + alembic hidden import + 迁移硬校验）
build\python\skillrouter-server\skillrouter-server.exe --role bootstrap --data-dir build\.smoke-data --json

:: 2) api 真起一次并探活（覆盖 skillrouter_api.main 在依赖图内 + uvicorn 协议栈）
build\python\skillrouter-server\skillrouter-server.exe --role api --port 18099 --data-dir build\.smoke-data
```

两条都在**净化环境**下执行：`cwd=apps/desktop/build`（不是仓库根，避免读到仓库根的 `.env`），
并剔除继承的 `SKILLROUTER_*` / `AGENT_*` / `ENGINE_*` / `PYTHONHOME` / `PYTHONPATH`。

## 3. 出包后校验

```bat
cd apps\desktop
npm run verify
npm run smoke
```

- `verify`：资源三件套 + `app.asar` + 迁移目录双布局 + **两个 exe 的 `--version`** +
  `routes-manifest.json` 的 `/api` 目标必须是 `127.0.0.1:18100` + 写 `release/SKILLROUTER-SHA256SUMS.txt`。
- `smoke`：用**净化 PATH**（`C:\Windows\system32;C:\Windows`）启动 `win-unpacked\SkillRouter.exe --smoke-test`，
  走完 `preparing → ready` 并执行 10 项检查（含反代 `/api/health` 与经 Next 登录闭环），
  **退出后**由外壳补 `shellChecks`（`clean_shutdown` 孤儿进程 + `artifact_hash`）。

## 4. 开发态

```bat
:: 只用 start.ps1 起后端（8000/3000/8500），Electron 只做壳
cd apps\desktop
npm run build:main
npx electron . --dev
```

`--dev` **跳过**产物校验与端口契约（安装包外没有 `resources\`），窗口直接指向 `http://localhost:3000`。

其他参数（`--version` / `--smoke-test` / `--smoke-out <path>` / `--data-dir <path>` / `--verbose`）
见 spec §4.1。

## 5. 端口与数据

| 服务 | 端口 | 说明 |
|---|---|---|
| api（uvicorn） | **18100（固定）** | 构建期烧进 web 的 rewrite，运行时不允许漂移 |
| web（Next standalone） | 18101 起扫 | 动态安全（web 端口不参与构建期契约） |
| 网关（Node sidecar） | 18102 起扫 | `AGENT_GATEWAY_PORT` |

数据目录：`%APPDATA%\SkillRouter\data`（日志在 `%APPDATA%\SkillRouter\logs\desktop`）。
开发态用的是 `var\`，两者**互不污染**。

## 6. 测试

```bat
cd apps\desktop
npx vitest run       :: 42 项：env 13 / config 5 / ports 13 / startup 11
npx tsc --noEmit
```

## 7. 已知差距（按仓库证据口径如实登记）

| 项 | 状态 | 原因 |
|---|---|---|
| 代码签名 / 公证 | **未做** | SmartScreen 会提示「更多信息 → 仍要运行」；签名是独立任务（R1） |
| 自动更新 | **不做** | 非目标（spec §9 P2-6） |
| 内置 Agent 真实模型推理 | **待验证** | 需要真实模型凭证；网关起不来只降级为 `W-D01` |
| macOS / Linux 打包 | **不做** | 非目标 |
| 桌面端「生产可用」 | **不声称** | 桌面端 = 单机单用户；生产用 `deploy/docker-compose.yml`（R5） |

## 8. 排错入口

1. `%APPDATA%\SkillRouter\logs\desktop\main.log` —— `state=` / `error=` / `pid=` / `exit=` / `restart=` 五个键可直接 grep。
2. 菜单「复制诊断信息」—— 一键拿到版本、端口、路径、`SKILLROUTER_INTERNAL_URL`。
3. 错误码处置表：[`docs/runbook/desktop-app.md`](../../docs/runbook/desktop-app.md) §错误码。
4. 冒烟报告 `release/smoke-report.json` —— 哪一项红就是哪一环断。
