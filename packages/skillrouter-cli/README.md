# `@skillrouter/cli`

把一个**已发布**的 Skill Router SKILL 安装到外部 Agent（AragonAgent / Claude Code / 通用目录），并生成
随包调用的零依赖助手 `invoke.mjs`。

设计约束（来自 §7.9 / §9.8）：

- 生成的 `SKILL.md` **只**声明目标环境真实存在的工具：`allowed-tools: [bash]`。不声明任何 `sr_*`、
  不声明未实测的 MCP 工具 —— 上游事实是「无法解析的工具名会让整份声明失效」。
- 平台调用一律经同目录的 `invoke.mjs`（Node ≥ 18，仅用内置 `fetch`，零运行时依赖），Agent 不手写 HTTP。
- 令牌**只**出现在 `~/.skillrouter/credentials.json`（POSIX `0600`；Windows 用 `icacls` 限制为当前用户）。
  `SKILL.md`、`invoke.mjs`、`skillrouter.config.json`、`README.md` 里没有任何密钥。

## 安装

```bash
# 推荐：令牌走 stdin，不进入 shell 历史
npx @skillrouter/cli install --base-url https://router.example.com --token-stdin

# 或（不推荐，令牌会留在历史里）
npx @skillrouter/cli install --base-url https://router.example.com --token srk_xxx_yyy
```

两者都缺且当前是 TTY 时，会交互式隐藏输入令牌。

常用参数：

| 参数 | 说明 |
|---|---|
| `--base-url <url>` | 平台地址（必填）；`/api/v1` 由 CLI 自己拼 |
| `--token-stdin` | 从标准输入读令牌（推荐） |
| `--token <srk_...>` | 直接给令牌（会留痕，CLI 会警告） |
| `--agent aragon\|claude\|generic` | 覆盖探测出的 Agent 标签 |
| `--transport http\|mcp` | 目前只支持 `http`；`mcp` 会因为「未实测」直接拒绝（退出码 2） |
| `--dir <path>` | 显式指定安装目录（跳过探测） |
| `--skill <name>` | 拉取平台上已发布 SKILL 的正文作为补充说明 |
| `--no-verify` | 完全不联网：不查 `/health`、不拉能力目录、不做安装后验证 |
| `--force` | 覆盖已存在的安装 |

### 写盘产物（恰好四个文件）

在选中目录里只创建：

1. `SKILL.md` —— frontmatter `name/description/version/license/allowed-tools: [bash]/activation: auto/metadata.skillrouter`，正文是 `invoke.mjs` 用法、错误契约与边界说明。
2. `invoke.mjs` —— 调用助手（内容优先用平台 `bundle` 下发的版本，保证与平台契约同步；取不到时用包内模板）。
3. `skillrouter.config.json` —— `base_url`、`transport`、`token_ref`、`installed_at`、`cli_version`、`server_version`、能力快照。**不含令牌。**
4. `README.md` —— 安装/验证/更新/卸载说明 + 已验证 Agent 矩阵。

安装结束会打印这四个文件的**绝对路径**，以及「为什么选这个目录」。

### 目标目录探测顺序

1. `--dir <path>`
2. `./.aragon/skills`（存在 `./.aragon` 时）
3. `./.claude/skills`（存在 `./.claude` 时）
4. `$ARAGON_HOME/skills`（`ARAGON_HOME` 已设置时）
5. `~/.aragon-agent/skills`（存在 `~/.aragon-agent` 时）
6. `./skillrouter-skill`（通用兜底，会提示「未探测到 Agent」）

每一步的选择与理由都会打印，例如「检测到 `.claude/skills`，已写入此处」。

## 其他命令

```bash
skillrouter verify  [--base-url <url>] [--token <token>] [--dir <path>] [--probe-capability <key>]
skillrouter list    [--dir <path>]
skillrouter update  [--dir <path>]          # 重新拉 SKILL 正文 + invoke.mjs + 能力快照，不重装
skillrouter uninstall [--dir <path>] [--keep-config]
skillrouter doctor                          # Node 版本 / 平台 / 凭据后端 / 目录候选 / 已有安装
```

- `verify`：`GET /api/v1/health` → `GET /api/v1/whoami` → `GET /api/v1/capabilities`，逐项 `✓/✗`；失败行带
  `code`、`hint` 与 `request_id`。加 `--probe-capability <key>` 再做**一次真实最小调用**。
- `update`：复用已存储令牌，不重新输入；不动 `installed_at`；只刷新三个产物 + 配置里的能力快照。
- `uninstall --keep-config`：删掉 `SKILL.md` / `invoke.mjs` / `README.md`，保留 `skillrouter.config.json` 与凭据。

所有命令都支持 `--json`（机器可读输出，同时给出 `ok` / `exit_code` / `messages`）。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 一般失败（如目标已存在且未加 `--force`、验证未通过、SKILL 契约自检失败） |
| 2 | 参数错误（未知命令/参数、缺 `--base-url`、令牌格式不对、`--transport mcp` 未实测） |
| 3 | 连通性失败（DNS/连接/超时） |
| 4 | 无写入权限（目标目录或凭据目录不可写） |
| 5 | 目标 Agent 未探测到 / 找不到已安装目录（用 `--dir` 指定） |

## `invoke.mjs` 子命令

```bash
node ./invoke.mjs capabilities.list [--refresh]
node ./invoke.mjs capabilities.get <key>
node ./invoke.mjs invoke --capability <key> [--version <v>] [--mode sync|async|stream] --input '<json>'
node ./invoke.mjs task.status <task_id>
node ./invoke.mjs task.cancel <task_id>
node ./invoke.mjs skills.list
node ./invoke.mjs skills.read <name>
node ./invoke.mjs health            # 不需要令牌
node ./invoke.mjs --help
```

- 成功：打印**一行 JSON**（`--mode stream` 时每行一个 SSE `data:` 事件）。
- 失败：打印 `{"ok":false,"error":{"code","message","hint","request_id","http_status"}}`，非 0 退出
  （`2` 参数错误、`3` 网络失败、`1` 其他）。
- 令牌从 `~/.skillrouter/credentials.json`（`SKILLROUTER_HOME` 可覆盖）或 `SKILLROUTER_TOKEN` 环境变量解析，
  **永不来自 argv**。

## 已验证 Agent 矩阵

> 只有真的在对应环境里跑通「安装 → 发现 → 调用」闭环才会标「已验证」。

| Agent | 目录 | 状态 | 说明 |
|---|---|---|---|
| AragonAgent | `./.aragon/skills` | **待验证** | 走 `bash` + `invoke.mjs`；frontmatter 只声明 `bash` |
| Claude Code | `./.claude/skills` | **待验证** | 同上 |
| 通用目录 | `./skillrouter-skill` | **待验证** | 未探测到 Agent 时的兜底位置 |
| MCP 客户端通道 | — | **待验证（不生成）** | 平台 MCP 通路未实测，`--transport mcp` 目前直接拒绝 |

### 本机已实测的部分（不是 Agent 端到端）

- `npm run build`（`tsc -p .`，ES2022 / NodeNext / strict，无 `any`）通过。
- `npm test`（vitest：`test/detect.test.ts`、`install.test.ts`、`verify.test.ts`，37 条）通过。
- `src/templates/invoke.mjs.tpl` 通过 `node --check`；在临时目录里按平台契约起假服务后，
  `invoke.mjs` 的 `capabilities.list` / `capabilities.get` / `invoke`（sync + async + **stream**）/ `task.status` /
  `task.cancel` / `skills.*` / `health` 都拿到预期的单行 JSON：
  - `--mode stream` 逐行输出 4 个 SSE 事件（`call.delta` ×2、`call.usage`、`call.done`），`: keep-alive` 注释被忽略；
  - 平台把 stream 降级成普通 JSON 时，退化为打印那一行 JSON；
  - 网络不可达 / 参数错误 / 平台错误体都只打印一行 JSON 并以非 0 退出。
- `install` → `verify`（含 `--probe-capability`）→ `list` → `update` → `uninstall --keep-config` → `uninstall`
  全链路跑通；退出码 1/2/3/5 与 `--json` 输出、`--force` 守卫均已实测。
- Windows 上凭据文件的 ACL 已用 `icacls` 收紧为「仅当前用户」（`icacls` 输出确认无继承项）。
- 假服务端只验证了**协议形状**，不是真实第三方上游；真实平台/真实能力的联调仍属「待验证」。

## 已知限制

- `--probe-capability <key>` 会**产生一次真实调用**（可能有极小费用）。它先 `GET /api/v1/capabilities/<key>`
  读 `input_schema`，按 JSON Schema 的 `required` 合成最小入参（字符串→`"probe"`、数字→`minimum`、布尔→`false`、
  `enum`→首个值；对象/数组/`{"$file":...}` 不猜），再按能力 `modes` 选模式（有 `sync` 就用 `sync`）。
  若平台仍以 `INPUT_SCHEMA_INVALID` 拒绝，CLI 会明确说明「平台已连通、令牌可用，但这次最小调用被输入校验拒绝，
  跳过的字段是 X」，退出码 1 —— 此时用 `node ./invoke.mjs capabilities.get <key>` 看 schema 后手工 `invoke` 实测。
- 没有引入 `keytar`（避免原生编译依赖）：Windows 用 `icacls`（`/inheritance:r` + 仅当前用户），POSIX 用 `chmod 0600`。
  若系统里恰好能 `import('keytar')`，`doctor` 会报告存在 OS 凭据库，但本版本仍以文件为准。
- Windows 上 `fs.stat().mode` 仍会显示 `666`（Node 的 chmod 只切换只读位）；真实收紧靠 `icacls`，`icacls <file>` 可复核。
- 本机 `NODE_ENV=production` 时 `npm install` 会跳过 devDependencies，需要
  `npm install --include=dev` 才能跑 `tsc`/`vitest`。

## 开发

```bash
npm install --include=dev
npm run build      # tsc -p tsconfig.json → dist/
npm test           # vitest run
npm run typecheck  # tsc --noEmit
```

包内分发 `dist/` 与 `src/templates/`（`files` 字段），模板在运行时从 `dist/templates` → `templates/` →
`src/templates/` 依次探测，因此源码树与打包产物都能工作。
