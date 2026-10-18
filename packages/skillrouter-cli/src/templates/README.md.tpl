# {{SKILL_TITLE}} — Skill Router 接入包

本目录由 `@skillrouter/cli` `{{CLI_VERSION}}` 于 `{{INSTALLED_AT}}` 生成，请勿手工编辑生成文件。

| 文件 | 作用 | 是否可手改 |
|---|---|---|
| `SKILL.md` | 给 Agent 读的技能声明（`allowed-tools: [bash]`，平台调用一律经 `invoke.mjs`） | 不建议（`skillrouter update` 会覆盖） |
| `invoke.mjs` | 零依赖 Node ESM 调用助手，唯一合法的平台访问通道 | 不建议（`skillrouter update` 会覆盖） |
| `skillrouter.config.json` | `base_url` / `transport` / `token_ref` / 时间戳 / 版本号，**不含令牌** | 只读 |
| `README.md` | 本文件 | 可 |

## 安装信息

- 平台地址：`{{BASE_URL}}`
- 通道：`{{TRANSPORT}}`
- 目标 Agent：`{{AGENT}}`
- 安装目录：`{{DIR}}`
- 凭据引用：`token_ref = {{TOKEN_REF}}`（令牌本身在 `~/.skillrouter/credentials.json`，0600）
- 平台版本：`{{SERVER_VERSION}}`

## 常用命令（都在本目录执行）

```bash
node ./invoke.mjs health                       # 连通性检查（不需要令牌）
node ./invoke.mjs capabilities.list            # 动态发现能力（加 --refresh 绕过缓存）
node ./invoke.mjs capabilities.get <key>       # 查看 input_schema / modes / limits
node ./invoke.mjs invoke --capability <key> --mode sync --input '{"prompt":"hi"}'
node ./invoke.mjs task.status <task_id>
node ./invoke.mjs task.cancel <task_id>
node ./invoke.mjs skills.list
node ./invoke.mjs skills.read <name>
```

`invoke.mjs` 成功时打印 **一行 JSON**（`--mode stream` 时每行一个 SSE 事件 JSON），失败时打印
`{"ok":false,"error":{"code","message","hint","request_id","http_status"}}` 并以非 0 退出。

## 维护

```bash
skillrouter verify  --dir .                        # /health → /whoami → /capabilities（逐项 ✓/✗）
skillrouter verify  --dir . --probe-capability <key>  # 额外做一次真实最小调用
skillrouter update  --dir .                        # 重新拉取 SKILL 正文 + 能力目录快照（不重装）
skillrouter uninstall --dir . --keep-config        # 卸载但保留配置与凭据
skillrouter doctor                                 # 探测本机 Agent 环境 / Node 版本 / 凭据后端
```

退出码：`0` 成功、`1` 一般失败、`2` 参数错误、`3` 连通性失败、`4` 无写入权限、`5` 目标 Agent 未探测到（用 `--dir` 指定）。

## 已验证 Agent 矩阵

| Agent | 版本 | 状态 | 说明 |
|---|---|---|---|
| AragonAgent（`./.aragon/skills`） | — | 待验证 | 走 `bash` + `invoke.mjs`；`allowed-tools` 只声明 `bash` |
| Claude Code（`./.claude/skills`） | — | 待验证 | 同上 |
| 通用目录（`./skillrouter-skill`） | — | 待验证 | 无 Agent 探测结果时的兜底位置 |

> 「待验证」= 尚未在真实 Agent 环境里跑通「发现 → 调用」闭环（§15.2-5）。不要把它当成已验证。

## 令牌与安全

- 令牌只保存在 `~/.skillrouter/credentials.json`（POSIX 0600；Windows 用 `icacls` 限制为当前用户，失败时降级但不报错）。
- `skillrouter.config.json` 与 `SKILL.md` 中**没有**任何密钥。
- 不要把令牌写进命令行（会进 shell 历史）；安装时用 `--token-stdin`。
- 需要更换令牌：`skillrouter install --dir . --token-stdin --force`。
