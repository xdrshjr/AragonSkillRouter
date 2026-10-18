# 把 Skill Router 接进外部 Agent

> 目标：在**外部 Agent 所在的那台机器**上，用一条命令装好连接包，让 Agent 能
> 「发现能力 → 调用能力 → 拿结果」，而**永远接触不到上游真实密钥**。
>
> 本文的命令都是**可执行的确切命令**，不是示意。安装命令请优先从管理端
> 「访问 / 令牌」页面复制（平台下发的 `install_command` 与 npm 包版本一致），
> 不要手写 curl 冒充这套流程（§15.2 第 5 项是硬性验收：必须用文档里的这几条命令）。

## 0. 前置条件

| 项 | 要求 | 检查命令 |
|---|---|---|
| Node.js | **≥ 18**（`invoke.mjs` 只用内置 `fetch`，零依赖） | `node -v` |
| 平台访问 | 已发布 SKILL + 已创建外部 Agent 令牌 `srk_<keyid>_<secret>`；网络可达 | `curl -sS <base-url>/api/v1/health` |
| 目标 Agent | AragonAgent / Claude Code / 任意支持「执行命令」的运行时；或 `--dir` 指定目录 | 见 §5 矩阵 |
| 权限 | 目标目录与 `~/.skillrouter/` 可写 | — |

获取令牌：管理端 → 访问（`/access`）→ 新建 Agent → 「签发令牌」。**令牌只显示一次**，
且页面同时给出该 Agent 的 `install_command` 原文。

## 1. 安装

```bash
# 推荐：令牌从 stdin 读入（不进 shell 历史、不进 argv、不进 ps）
npx @skillrouter/cli install --base-url https://router.example.com --token-stdin

# 平台未发布 npm 包时（用平台下发的 tarball，路径真实存在，不是占位符）
npx ./skillrouter-cli-0.1.0.tgz install --base-url https://router.example.com --token-stdin

# 不推荐：令牌直接写在命令行（会留在 shell 历史里，CLI 会警告）
npx @skillrouter/cli install --base-url https://router.example.com --token srk_k1_xxx
```

```bat
:: Windows cmd.exe：先进入 tarball 所在目录；--token-stdin 后用 < 从文件喂入
cd C:\Users\me\Downloads
npx ./skillrouter-cli-0.1.0.tgz install --base-url https://router.example.com --token-stdin < token.txt
del token.txt
```

```powershell
# PowerShell：同样走 stdin，避免令牌进历史
Get-Content .\token.txt | npx @skillrouter/cli install --base-url https://router.example.com --token-stdin
Remove-Item .\token.txt
```

常用参数：

| 参数 | 说明 |
|---|---|
| `--base-url <url>` | 平台地址（必填）。`/api/v1` 由 CLI 自己拼，不要自己加 |
| `--token-stdin` | 从 stdin 读令牌（**推荐**） |
| `--token <srk_...>` | 直接给令牌（会留痕，CLI 会警告） |
| `--agent aragon\|claude\|generic` | 覆盖探测出的 Agent 标签（影响 `SKILL.md` 的 metadata） |
| `--dir <path>` | 显式指定安装目录（跳过探测；探测不到 Agent 时用它） |
| `--skill <name>` | 顺手拉取平台已发布 SKILL 的正文作为补充说明 |
| `--transport http\|mcp` | 目前**只支持 `http`**；`mcp` 因「未实测」直接拒绝（退出码 2） |
| `--no-verify` | 完全不联网（不查 health、不拉目录、不做安装后验证） |
| `--force` | 覆盖已存在的安装 |
| `--json` | 机器可读输出（`ok` / `exit_code` / `messages`） |

安装结束时 CLI 会打印**四个产物的绝对路径**和「为什么选这个目录」，例如
`检测到 .claude/skills，已写入此处`。请确认这四行都在：

| 产物 | 作用 | 里面有什么 |
|---|---|---|
| `SKILL.md` | Agent 的入口说明 | frontmatter：`name/description/version/license/allowed-tools: [bash]/activation: auto/metadata.skillrouter` |
| `invoke.mjs` | 唯一的调用通道（零依赖，Node ≥ 18） | `capabilities.list/get`、`invoke`、`task.status/cancel`、`skills.list/read`、`health` |
| `skillrouter.config.json` | 平台地址与安装信息 | `base_url`、`transport`、`token_ref`、`installed_at`、`cli_version`、`server_version`、能力快照。**不含令牌** |
| `README.md` | 给人和给 Agent 的使用说明 | 安装/验证/更新/卸载 + 已验证 Agent 矩阵 |

**令牌存在哪**：优先 `~/.skillrouter/credentials.json`（POSIX `chmod 0600`；Windows 用
`icacls` 收紧为仅当前用户），`SKILLROUTER_TOKEN` 环境变量可临时覆盖。
`skillrouter.config.json` 里只有 `token_ref`。

## 2. 验证

```bash
# 三项只读检查：/api/v1/health → /api/v1/whoami → /api/v1/capabilities
skillrouter verify

# 额外做一次真实最小调用（会产生极小费用，需显式开启）
skillrouter verify --probe-capability text.generate.gpt4o-mini
```

输出逐项 `✓ / ✗`，失败行带 `code`、`hint`、`request_id`。判定标准：

| 检查项 | 通过条件 | 失败常见原因 |
|---|---|---|
| health | `200` 且 `status` 存在 | 网络/证书/反代（`3` 连接失败） |
| whoami | `200` 且返回令牌主体 | `401 TOKEN_REVOKED` / `403 TOKEN_SCOPE_DENIED` |
| capabilities | `200` 且 `items` 可解析 | 平台未发布任何能力（目录为空≠失败，但请确认） |
| `--probe-capability` | 一次真实调用返回 2xx | `400 INPUT_SCHEMA_INVALID`：CLI 会说明「平台已连通、令牌可用，但最小入参被拒」，此时按报错里的字段名手工补一次 |

`--verify` 的每一项都要能对着平台日志用 `X-Request-Id` 对上（§7.14）。

## 3. 日常使用（Agent 侧）

Agent 不写 HTTP，只执行一条命令：

```bash
# 发现能力（平台新增能力会即时出现，无需重装；ETag 缓存自动失效）
node ./invoke.mjs capabilities.list
node ./invoke.mjs capabilities.list --refresh

# 看某个能力的输入 Schema 与示例
node ./invoke.mjs capabilities.get text.generate.gpt4o-mini

# 调用（sync / async / stream）
node ./invoke.mjs invoke --capability text.generate.gpt4o-mini --input '{"prompt":"你好"}'
node ./invoke.mjs invoke --capability image.generate.kling --mode async \
  --input '{"prompt":"雨中的柴犬","duration":5}'
node ./invoke.mjs invoke --capability text.generate.gpt4o-mini --mode stream \
  --input '{"prompt":"数到五"}'

# 异步任务
node ./invoke.mjs task.status tsk_01J9Z9AB
node ./invoke.mjs task.cancel tsk_01J9Z9AB

# SKILL 与自检
node ./invoke.mjs skills.list
node ./invoke.mjs skills.read video-short-clip
node ./invoke.mjs health          # 不需要令牌
```

约定：

- 成功时打印**一行 JSON**（`--mode stream` 时每行一个 SSE `data:` 事件），便于 Agent 解析。
- 失败时打印 `{"ok":false,"error":{"code","message","hint","request_id","http_status"}}`，
  并以非 0 退出（`2` 参数错误、`3` 网络失败、`1` 其他）。把 `request_id` 报给管理员即可定位。
- 令牌**永远不在 argv 里**：助手自己从 `~/.skillrouter/credentials.json`（或
  `SKILLROUTER_TOKEN`）读取。

## 4. 更新与卸载

```bash
# 重新拉取 SKILL 正文 + invoke.mjs + 能力快照（不重装、不动 installed_at、不用重新输入令牌）
skillrouter update

# 卸载：默认连配置一起删；--keep-config 保留 skillrouter.config.json 与凭据
skillrouter uninstall
skillrouter uninstall --keep-config

# 体检：Node 版本 / 平台连通性 / 凭据后端 / 目录候选 / 已有安装
skillrouter doctor

# 列出已安装位置
skillrouter list
```

升级平台侧 SKILL 版本时**不需要**重装连接包：`capabilities.list` 每次都会拿最新目录，
破坏性变更只通过主版本号体现并在能力详情里给迁移说明。

## 5. 目标目录探测顺序与已验证矩阵

探测顺序（每一步都会打印选择理由）：

1. `--dir <path>`
2. `./.aragon/skills`（存在 `./.aragon` 时）
3. `./.claude/skills`（存在 `./.claude` 时）
4. `$ARAGON_HOME/skills`（`ARAGON_HOME` 已设置时）
5. `~/.aragon-agent/skills`（存在 `~/.aragon-agent` 时）
6. `./skillrouter-skill`（通用兜底，会提示「未探测到 Agent」）

**已验证 Agent 矩阵**（与 spec 附录 C 保持一致；未跑通的一律保持「待验证」）：

| Agent | 目标目录 | 接入方式 | 状态 |
|---|---|---|---|
| AragonAgent CLI（目标 `@aragon-agent/cli` 0.6.22） | `./.aragon/skills/skill-router/` | `SKILL.md` 只声明 `allowed-tools: [bash]`，调用走 `invoke.mjs` | **待验证** |
| Claude Code | `./.claude/skills/skill-router/` | 同上 | **待验证** |
| 任意 MCP 客户端（协议 `2025-06-18`） | — | `POST /api/v1/mcp` + Bearer 令牌 | **待验证**（仅契约测试覆盖；CLI 的 `--transport mcp` 当前直接拒绝） |
| 通用 HTTP 客户端（curl / 任意语言） | — | `/api/v1` OpenAPI | 已设计，**待实测** |

> 只有真的在对应 Agent 环境里跑通「安装 → 发现 → 调用」闭环，才能把状态改成「已验证」，
> 并把版本号、命令输出、`request_id` 记到
> [../runbook/verification-checklist.md](../runbook/verification-checklist.md) 的 §A5。

## 6. 退出码

| 码 | 含义 | 处置 |
|---|---|---|
| 0 | 成功 | — |
| 1 | 一般失败（目标已存在且未加 `--force`、验证未通过、SKILL 契约自检失败） | 看输出里的 `hint` |
| 2 | 参数错误（未知命令/参数、缺 `--base-url`、令牌格式不对、`--transport mcp`） | `skillrouter <cmd> --help` |
| 3 | 连通性失败（DNS/连接/超时） | `curl -sS <base-url>/api/v1/health` |
| 4 | 无写入权限 | 换 `--dir` 或修目录权限 |
| 5 | 未探测到 Agent / 找不到已安装目录 | 用 `--dir <path>` 指定 |

## 7. 故障排查

| 现象 | 可能原因 | 处置 |
|---|---|---|
| `verify` 三项全 ✗ 且退出码 3 | 域名/证书/反代不通，或平台未启动 | `curl -sS <base-url>/api/v1/health`；查 nginx 与 api 日志 |
| `401 TOKEN_REVOKED` | 令牌被撤销 | 管理端重新签发，重跑 `install`（或改凭据文件） |
| `403 TOKEN_SCOPE_DENIED` | 令牌作用域不含该能力 | 管理端扩大作用域后重试（无需重装） |
| `409 NOT_INITIALIZED` | 平台还没初始化 | 服务器上跑 `python scripts/bootstrap.py` |
| `403 PASSWORD_CHANGE_REQUIRED`（管理端） | 仍用默认密码 | 先完成强制改密 |
| `invoke` 报 `400 INPUT_SCHEMA_INVALID` | 入参不符合能力 `input_schema` | `node ./invoke.mjs capabilities.get <key>` 看 schema；未声明字段放 `options.extensions` |
| `400 UNSUPPORTED_MODE` | 能力不支持 `--mode` | 看 `capabilities.get` 返回的 `modes` |
| `409 PROVIDER_NOT_AVAILABLE` | 指定 provider 不可用 | 去掉该选项或看错误体里的可用列表 |
| 流式无输出、一次性刷出全部内容 | 反代开了缓冲 | nginx 需 `proxy_buffering off` + `X-Accel-Buffering: no`（`deploy/nginx.conf` 已配） |
| Windows 上凭据文件权限看着是 `666` | Node 的 chmod 只切换只读位 | 实际靠 `icacls`；用 `icacls <file>` 复核 |
| `npm install` 后没有 `tsc`/`vitest` | 本机 `NODE_ENV=production` 跳过 devDependencies | 加 `--include=dev` |
| 安装到错误目录 | 探测命中了别的 Agent 目录 | 用 `--dir` 显式指定；`skillrouter doctor` 看候选 |

排错时给管理员的信息请包含：`code`、`request_id`、时间、命令原文、
以及 `skillrouter doctor --json` 的输出。
