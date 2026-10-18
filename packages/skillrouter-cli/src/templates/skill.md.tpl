---
name: {{SKILL_NAME}}
description: {{DESCRIPTION}}
version: {{SKILL_VERSION}}
license: proprietary
allowed-tools: [bash]
activation: auto
metadata:
  skillrouter:
    base_url: {{BASE_URL}}
    helper: ./invoke.mjs
    config: ./skillrouter.config.json
    transport: {{TRANSPORT}}
    required_capabilities: [{{REQUIRED_CAPABILITIES}}]
---

# {{SKILL_TITLE}}

本 SKILL 让当前 Agent 通过 **Skill Router 平台** 使用一批标准化 AI 能力（文本、图像、语音、视频等）。
所有平台访问都必须经由同目录下的零依赖助手 `invoke.mjs`，**不要**自己拼 HTTP 请求、**不要**在命令里出现任何密钥。

## 前置条件

- Node.js ≥ 18（`node --version` 自检）。
- 本目录已由 `skillrouter` CLI 安装完成：`SKILL.md`、`invoke.mjs`、`skillrouter.config.json`、`README.md`。
- 平台令牌已保存在用户目录的凭据文件里（`~/.skillrouter/credentials.json`，0600）。
  令牌**不在** `skillrouter.config.json` 中，也不在命令行参数中，你无法也不需要读取它。

## 使用方式（一条稳定命令）

1. **动态发现能力**（平台新增能力会立即出现在结果里，无需重装本 SKILL）：

   ```
   node ./invoke.mjs capabilities.list
   ```

   需要绕过本地 ETag 缓存时加 `--refresh`：

   ```
   node ./invoke.mjs capabilities.list --refresh
   ```

2. **查看某个能力的输入/输出 Schema 与支持模式**：

   ```
   node ./invoke.mjs capabilities.get <capability-key>
   ```

   重点看返回里的 `input_schema`、`modes`、`limits`、`file_io`。`extensions` 是提供商特有参数的唯一合法入口。

3. **调用能力**：

   ```
   node ./invoke.mjs invoke --capability {{EXAMPLE_CAPABILITY}} --mode sync --input '{"prompt":"一只在雨中的柴犬"}'
   ```

   - `--mode` 必须取该能力 `modes` 里实际存在的值：`sync` / `async` / `stream`，省略时按平台默认值。
   - `--version <semver>` 可固定能力版本；不传则用平台当前发布版本。
   - `--mode async` 返回 `task_id`，用下一步轮询。
   - `--mode stream` 会逐行打印 SSE 的 `data:` JSON（每行一个事件对象）。

4. **异步任务**：

   ```
   node ./invoke.mjs task.status <task_id>
   node ./invoke.mjs task.cancel <task_id>
   ```

5. **平台上的 SKILL 目录**（若你要复用平台上已发布的 SKILL 说明）：

   ```
   node ./invoke.mjs skills.list
   node ./invoke.mjs skills.read <skill-name>
   ```

## 输出与错误契约

- `invoke.mjs` 每次都只打印 **一行 JSON**（`stream` 模式是每行一个事件 JSON），便于你直接解析。
- 失败时退出码非 0，并打印一行 JSON：

  ```json
  {"ok":false,"error":{"code":"UPSTREAM_RATE_LIMIT","message":"上游限流","hint":"稍后重试或降低并发","request_id":"9f3c1a7b2e4d4a10","http_status":429}}
  ```

- 遇到失败时：
  1. 读 `error.code` 判断类型（`TOKEN_INVALID`/`TOKEN_REVOKED`/`TOKEN_SCOPE_DENIED`/`QUOTA_EXCEEDED`/`BUDGET_EXCEEDED`/`UPSTREAM_*`/`NETWORK_*`）。
  2. 把 `error.request_id` 原样报给管理员，它是平台侧定位问题的唯一凭据。
  3. 不要为了让调用成功而改写令牌、绕过 `invoke.mjs` 或猜测上游接口。

## 边界与禁止事项

- 你只能通过 `./invoke.mjs` 访问平台。SKILL 的 `allowed-tools: [bash]` 只是影响面控制，**不是安全边界**；
  真正的边界在平台侧（令牌作用域 + 速率/配额 + 预算），越界会直接返回错误。
- 不要读取、打印、转述 `~/.skillrouter/credentials.json` 或任何形如 `srk_...` 的字符串。
- 不要修改 `skillrouter.config.json` 的 `base_url` / `token_ref`；需要换环境请让用户重新运行 `skillrouter install`。
- 平台可能随时新增/下线能力：调用前用 `capabilities.list` 现场确认，不要凭记忆写死能力键。
- 不可用模式不会被静默接受：平台会返回 `400 UNSUPPORTED_MODE` 并在 `details.supported` 里列出真实支持的模式。

## 维护

```
skillrouter verify  --dir .   # 检查 /health、/whoami、/capabilities
skillrouter update  --dir .   # 重新拉取 SKILL 正文与能力目录快照（不重装）
skillrouter uninstall --dir . # 清理（--keep-config 保留配置）
```

安装信息：CLI `{{CLI_VERSION}}`，安装时间 `{{INSTALLED_AT}}`，平台 `{{BASE_URL}}`。
{{SKILL_BODY_EXTRA}}
