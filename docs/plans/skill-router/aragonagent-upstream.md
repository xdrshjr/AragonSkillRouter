# AragonAgent 上游接入记录（版本 / 能力边界 / 运行方式）

| 项目 | 内容 |
|---|---|
| 文档 | `docs/plans/skill-router/aragonagent-upstream.md` |
| 采证时间 | 2026-10-18 |
| 采证机器 | Windows 10.0.26200 / Python 3.11.7 / Node v22.18.0 / npm 10.9.3 / git 可用 |
| 上游仓库 | `https://github.com/xdrshjr/AragonAgent.git` |
| 许可证 | MIT（`LICENSE`：`MIT License / Copyright (c) 2026 AragonAgent contributors`） |
| 采证方式 | `git ls-remote`（远端引用）、`git clone --depth 1`（只读研究副本，已删除）、`npm view`（registry dist-tags）、源码与文档阅读 |

> 本文是**事实记录**，供设计文档 §12 与后续实现、升级流水线对照。凡是本文件没有写的内容，都视为「未验证」，不得在代码或界面中当作已知事实使用。

---

## 1. 版本与引用（实测输出）

### 1.1 远端引用（`git ls-remote --heads --tags`）

```text
0a119a64890717b0e3017edbaaf00944babf19a8  refs/heads/main
f7a2847b62fc0a7c1425b3ca9fa6543c2f10c644  refs/heads/unrestricted-mode
bd6fa48e8f46bea0049da2d4f7e8e1c2339d4c47  refs/tags/cli-v0.4.1
fb66dd8cbc5bf89092bf0a0bd92e519d36105d1d  refs/tags/cli-v0.4.1^{}
59daf7a1d87a2b8cc2aaf044af7225a5cf50cf69  refs/tags/cli-v0.4.2
6ed5f0b228d8cca8348d326a9876a358e474f438  refs/tags/cli-v0.4.2^{}
3a69f72b080dc929b0bb04a7422d3af3be902492  refs/tags/cli-v0.6.13
3b5f54b16c2f06d528ba8643fc648f3477f991a1  refs/tags/cli-v0.6.13^{}
3a6988269b09404ffa8ff062c9149e8ab9e5b35a  refs/tags/cli-v0.6.22
0a119a64890717b0e3017edbaaf00944babf19a8  refs/tags/cli-v0.6.22^{}
1ce8f9005b0c20858e1dab014456302b1effaf60  refs/tags/cli-v0.6.7
ef752b2a20ffadab2f6f3bbb6d580fc22c5c1d6e  refs/tags/core-v0.1.1
fb66dd8cbc5bf89092bf0a0bd92e519d36105d1d  refs/tags/core-v0.1.1^{}
2d17a91f0ca642cdfa5d0ac4d09f6927bf839c8a  refs/tags/core-v0.1.2
6ed5f0b228d8cca8348d326a9876a358e474f438  refs/tags/core-v0.1.2^{}
1ce8f9005b0c20858e1dab014456302b1effaf60  refs/tags/core-v0.2.19
dcb06b0516920fcb82024c0df2a2277656a2433a  refs/tags/core-v0.2.25
3b5f54b16c2f06d528ba8643fc648f3477f991a1  refs/tags/core-v0.2.25^{}
b37ca3a85c00a4e008de79fc3ca586cc0a66720a  refs/tags/core-v0.2.34
0a119a64890717b0e3017edbaaf00944babf19a8  refs/tags/core-v0.2.34^{}
```

结论：

- `main` 的 HEAD 提交 `0a119a64890717b0e3017edbaaf00944babf19a8` **同时**是 `cli-v0.6.22^{}` 与 `core-v0.2.34^{}` 指向的提交（`^{}` 为 tag 解引用后的提交对象）。
- 另有未打 tag 的分支 `unrestricted-mode`（`f7a2847`）。本平台**不使用**该分支。
- tag 命名有两套，分别对应两个 npm 包：`cli-v*`、`core-v*`。检测器必须同时解析两套（见设计文档 §12.4）。

### 1.2 npm registry（`npm view <pkg> dist-tags --json`）

```text
@aragon-agent/core dist-tags → { "latest": "0.2.37" }
@aragon-agent/cli  dist-tags → { "latest": "0.6.25" }
```

**已确认的关键偏差**：registry 上的 `latest`（0.2.37 / 0.6.25）**高于** git 上可见的最新 tag（0.2.34 / 0.6.22）。

含义与处理：

1. 只信 git → 会漏掉已发布的 npm 版本；只信 npm → 拿不到可追溯的 commit 与可复现的构建来源。
2. 因此设计文档规定双通道检测 + `provenance` 分级（`tagged` / `npm-only` / `branch-commit`），且默认只**自动部署** `tagged`；`npm-only` 必须通过完整性校验与隔离构建，并在界面标注「上游无对应 tag」。
3. 本仓库的接入 pin 采用**可追溯的一侧**：`@aragon-agent/core@0.2.34`（= `main@0a119a6` = `core-v0.2.34`）。

### 1.3 本次采证用的只读副本

```text
git clone --depth 1 https://github.com/xdrshjr/AragonAgent.git .tmp/aragonagent
git -C .tmp/aragonagent rev-parse HEAD
→ 0a119a64890717b0e3017edbaaf00944babf19a8
```

该副本仅用于阅读，**已在本轮结束前删除**（不进入交付物，也不作为依赖来源）。

---

## 2. 仓库结构与运行方式

```text
AragonAgent/
  packages/
    core/     @aragon-agent/core  0.2.34   ESM-only 引擎（Node ≥18）
    cli/      @aragon-agent/cli   0.6.22   TUI + `aragon exec` 机器契约
  desktop/    Electron + Next.js 桌面端（本平台不使用）
  docs/plans/ 上游自己的设计文档（多个 spec.md，可作为 API 稳定性参考）
  scripts/, logo/, LICENSE, README.md, CLAUDE.md, package.json (npm workspaces)
```

| 项 | 实测值 |
|---|---|
| 包管理器 | npm workspaces（`workspaces: ["packages/*", "desktop"]`） |
| 构建 | `npm install && npm run build`（各包 `tsc -p tsconfig.json` → `dist`） |
| 测试 | `npm test`（`vitest run`，各包独立） |
| 模块格式 | `"type": "module"`；`exports` 只有 `types` + `import` 条件，**无 `require`** → 不能被 CJS `require()` 引入 |
| 引擎入口 | `@aragon-agent/core` → `./dist/index.js` |
| CLI 入口 | `bin.aragon = ./dist/launcher.js`（`aragon` 命令） |
| 引擎运行时目录 | `~/.aragon-agent/`（`config.json`、`logs/`、`sessions/`、`skills/`），`ARAGON_HOME` 可重定位 |
| 引擎依赖 | `optionalDependencies: ajv ^8.18.0, isolated-vm ^6.1.2`（缺 `ajv` 走内置校验回退；缺 `isolated-vm` 仅 `sandbox/*` 子路径不可用，根 barrel 不导出沙箱） |
| CLI 依赖 | `@aragon-agent/core ^0.2.34`、`commander`、`chalk`、`cli-highlight`、`dotenv` 等 |
| 许可证 | MIT |

---

## 3. 引擎 SDK（`@aragon-agent/core`）可依赖面

公开导出由 `packages/core/src/__tests__/public-api.test.ts` 冻结（实测 `EXPECTED.length === 86`；`API.md` 标题写 85，比代码旧一项）。平台**只允许**依赖下列少数符号，其它一律视为内部实现：

```ts
import {
  AragonAgent,            // 与 Agent 是同一个类（export { Agent as AragonAgent }）
  defineTool,             // 定义工具（含 JSON Schema 参数校验）
  textResult, errorResult,// 工具结果构造
  initProviders, getProviderRegistry,
  type AgentTool, type ModelRef, type AgentEvent,
} from '@aragon-agent/core';
```

构造签名（`AgentConfig`，逐字来自源码）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `systemPrompt` | `string` | 是 | 系统提示 |
| `model` | `ModelRef { providerId, modelId, baseUrl? }` | 是 | 模型引用 |
| `tools` | `AgentTool[]` | 是 | **最终工具集**：不在数组里的工具，模型看不到也规划不到 |
| `providerRegistry` | `ProviderRegistry` | 是 | `initProviders()` / `getProviderRegistry()` 获取 |
| `getApiKey` | `(providerId: string) => string \| undefined` | 是 | 密钥解析钩子 —— 平台凭证注入的**唯一**位置 |
| `thinkingLevel` | `'off'\|'minimal'\|'low'\|'medium'\|'high'\|'xhigh'` | 否 | 默认 `'off'` |
| `maxTokens` | `number` | 否 | 输出上限，经 `resolveOutputTokens` 收敛 |
| `contextManager` | `ContextManager` | 否 | `shouldCompact(probe)` / `compact(ctx)`，无 setter |
| `sandbox` | `CodeActSandbox` | 否 | 本平台不启用 |
| `timeouts` | `{llmCallTimeout?, idleTimeout?, toolTimeout?, codeTimeout?, compactionHardTimeout?, toolTimeoutOverrides?}` | 否 | 默认 300s / 60s / 120s / 30s |

**不存在**的选项（不要在实现里写）：`maxTurns`、`signal`/`abortSignal`、`onEvent`/`callbacks`、`onToolUse`、权限钩子。取消只有 `agent.abort()`。

公开方法（实测）：`prompt(string | UserMessage)`、`continue()`、`abort()`、`waitForIdle()`、`subscribe(listener)`、`steer(message, id?)`、`followUp(message)`、`setModel`、`setSystemPrompt`、`setThinkingLevel`、`setMaxTokens`、`setTools`、`clearMessages`、`appendMessage`、`replaceMessages`、`clearAllQueues`、`pauseIdleWatchdog`、`resumeIdleWatchdog`、只读 getter `state`。

事件：`subscribe((event: AgentEvent) => void): () => void`（同步监听器，返回退订函数；**无** async iterator）。事件家族包含 `agent_start` / `agent_end` / `turn_start` / `steering_accepted` / 文本与思考增量 / 工具调用与结果 / todo / usage 等（完整字段以 `packages/core/src/types.ts` 为准，升级时由一致性套件断言「成员集合不得删减」）。

### 3.1 对平台的直接结论

1. **工具即权限边界**：`tools` 数组就是模型可见的全部工具。平台只放 `sr_*` 白名单工具，上游内置的 `bash`/`write_file`/`edit_file`/`skill_install` 等一律不注册 → 引擎升级不会自动扩大权限（需求 §「Agent 权限边界」）。
2. **密钥注入点唯一**：`getApiKey(providerId)` 在请求发出瞬间被调用，返回值不进消息历史 → 平台在此处从凭证代理取短时明文（需求 §「凭证安全」）。
3. **无持久化**：SDK 不保存会话 → 平台自建 `agent_sessions`/`agent_turns` 并用 `replaceMessages()` 重放（需求 §「会话历史」「切换页面保留工作状态」）。
4. **无预算参数**：`--max-turns`/`--max-duration` 是 CLI 特性，SDK 侧需要平台在 `subscribe()` 里自己计数并 `abort()`（设计文档 §12.3 的预算预检）。

---

## 4. CLI 机器契约（`@aragon-agent/cli` 0.6.22，仅用于一致性校验）

| 命令 | 用途 | 平台是否作为业务通道 |
|---|---|---|
| `aragon info --json` | 版本/工具/格式/特性自省 | 是（升级验证的一项检查） |
| `aragon doctor --json` | 本机是否可运行（`{ok, checks[]}`，pass/warn/fail，exit 0/1） | 是（环境自检） |
| `aragon exec` | NDJSON 事件流、会话、权限、预算 | 否（只做契约冒烟） |
| `aragon sessions *` | `exec` 会话管理 | 否 |
| `aragon`（TUI） | 交互终端界面 | 否 |

`aragon info --json` 字段（源自 `packages/cli/src/diagnostics/info.ts`）：
`cli, core, node, schemaVersion(=1), home, configPath, sessionsDir, provider, model, hasApiKey, tools[], outputFormats[], inputFormats[], permissionModes[], features[]`。

`aragon exec` 关键 flag：`--output-format text|json|stream-json`、`--input-format text|stream-json`、`--partial-messages`、`--include-thinking`、`--session-id`、`--resume`、`-c/--continue`、`--no-save-session`、`--max-turns`、`--max-duration`、`--permission-mode auto|plan|strict`、`--allow-tool`/`--deny-tool`、`--append-system-prompt[-file]`，以及根级 `--provider/--model/--base-url/--api-key/--cwd/--tool-timeout/--idle-timeout/--no-skills/...`。

`stream-json` 事件（`schemaVersion: 1`）：
`system(init)` / `user` / `text_delta` / `thinking` / `assistant` / `tool_call` / `tool_result` / `todo` / `team` / `fast_review` / `retry` / `error` / `result`。
契约明确：**忽略未知事件类型与未知字段**；新增不升版，改名/删除才升版。

stdin（`--input-format stream-json`，源码 `packages/cli/src/exec/stdin-stream.ts`）：
`{"type":"user","text":"..."}` / `{"type":"interrupt"}` / `{"type":"end"}`；EOF 等价于 `end`；坏行只产生一条非致命 `error` 事件并被跳过。

退出码：`0` 成功 / `1` 运行错误 / `2` 配置或用法错误 / `3` 预算中止 / `130|143|129` 信号。

---

## 5. SKILL 格式（与平台导出对齐的依据）

来源：`packages/cli/README.md`（Skills 章节）与 `packages/core/src/skills/*`。

发现顺序（后者按名覆盖前者）：`bundled`（包内 `skills/`）→ `user`（`aragon skills path`）→ `project`（`<cwd>/.aragon/skills/`）→ `project`（`<cwd>/.claude/skills/`，只读互操作）→ `env`（`ARAGON_SKILLS_PATH`，`:`/`;` 分隔）。

frontmatter（实测文档给出的键）：

```yaml
---
name: pdf-forms               # 必填，kebab-case，≤64 字符
description: …                # 唯一定位依据：必须说明「做什么」与「何时用」
version: 1.2.0
license: MIT
author: …
homepage: …
keywords: [pdf, forms]
allowed-tools: [read_file, bash]   # 每轮工具上限（只影响 5 个写类工具）
activation: auto                    # auto | always | manual
---
```

补充事实：

- `allowed-tools` 作用域是**一轮**（加载技能那一刻起，到下一条消息为止）；只读工具（`read_file/list_dir/glob/grep/skill/skill_find`）恒可用。
- Claude Code 的工具名会被映射（`Read`→`read_file`、`Bash`→`bash`、`MultiEdit`→`edit_file`）；无法解析的名字会让整份声明失效（不是变窄）。
- `activation: always` 的正文会注入每个会话的 system prompt（总量上限 12000 字节），且**不施加工具上限**。
- 安装需要人工批准；`--skills-yes` 或 `skills.requireApproval=false` 可绕过；安装期**不执行任何脚本**（无 postinstall）。
- 上游自述：**没有沙箱**。`allowed-tools` 是影响面控制，不是安全边界。

平台导出 SKILL 时必须满足以上 frontmatter 约定，并在 `metadata.skillrouter` 里携带平台地址与能力依赖（设计文档 §7.9）。

---

## 6. 环境变量（与平台集成相关的一小部分）

- API Key：`ANTHROPIC_API_KEY`、`OPENAI_API_KEY`、`GOOGLE_API_KEY` / `GEMINI_API_KEY`。
- 重定位：`ARAGON_HOME`（在模块加载时读取，**不从 `.env` 读取**）。
- 模型与行为：`ARAGON_PROVIDER`、`ARAGON_MODEL`、`ARAGON_BASE_URL`、`ARAGON_THINKING`、`ARAGON_MAX_TOKENS`、`ARAGON_SKILLS_PATH`、`ARAGON_SKILLS_DISABLED`、`ARAGON_TEAM`、`ARAGON_FAST*`、`ARAGON_COMPACTION*`、`ARAGON_UPDATE` / `ARAGON_UPDATE_REGISTRY`、`ARAGON_LOG_LEVEL` / `ARAGON_LOG_DIR`。
- 未发现 `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` 的显式引用 → 走 Node `fetch` 的默认行为，平台部署需要代理时由容器环境统一注入。

平台侧对应做法：网关进程的 `ARAGON_HOME` 指向 `var/aragon-home/engines/<version>/`（按版本隔离，便于回滚）；模型密钥不经环境变量传递，而是通过 `getApiKey` 钩子从凭证代理取。

---

## 7. 上游自述的边界（必须如实转述给平台用户）

| 边界 | 上游原文要点 | 平台对策 |
|---|---|---|
| 无文件系统沙箱 | 工具路径不受工作目录约束，`bash` 可触达调用用户可达的一切 | 网关注册工具集**不含**文件/命令工具；不提供开启入口 |
| Windows 信号差异 | 仅 Ctrl-C 会产生最终 `result`，`taskkill` 直接结束进程 | 网关用 `agent.abort()` + 进程级超时；父进程同时监听 exit |
| CLI 会话与 TUI 共享目录 | `sessions prune` 只动 `exec` 创建的会话（除非 `--all`） | 平台不复用上游会话存储，自建会话表 |
| 早期阶段、Windows 优先 | 「Expect fewer polished edges than a funded product」 | 升级流水线的验证套件承担稳定性兜底 |

---

## 8. 平台侧的接入决策（结论）

| 决策 | 选择 | 依据 |
|---|---|---|
| 集成形态 | **SDK 内嵌**（Node 边车 `agents/aragon-gateway`） | 需要注册平台工具与接收细粒度事件；CLI 的工具集固定，无法注入平台工具 |
| 版本 pin | `@aragon-agent/core@0.2.34`（精确）；`@aragon-agent/cli@0.6.22`（devDependency，仅契约校验） | 与 `main@0a119a6` 的 tag 一致，来源可追溯 |
| 升级策略 | 双通道检测 + 隔离构建 + 一致性套件 + 原子切换 + 自动回滚 | npm 与 git 版本已确认存在偏差（§1.2） |
| 权限模型 | 工具白名单 + 凭证代理短时令牌 | 引擎无沙箱、升级不可扩大权限 |
| 能力状态呈现 | `available / needs_adapter / permission_limited / unsupported` 四态持久化 | 需求要求「不得仅凭版本升级宣称新能力可用」 |

---

## 9. 升级验证清单（每次升级必须逐项留痕）

- [ ] `git ls-remote --tags` 与 npm `dist-tags` 均已采集，`provenance` 已判定。
- [ ] 目标版本可复现构建（`npm ci --ignore-scripts` + `npm run build`），`build-manifest.json` 记录了 commit、integrity、lock 哈希。
- [ ] 导出符号集合对比：无删除/改名；新增项已登记为「待适配」。
- [ ] `AgentEvent` 成员集合无删除；`schemaVersion` 未变（若变则禁止自动部署）。
- [ ] `HOST_TOOL_NAMES` 新增工具**没有**被自动加入平台白名单（`policy.test.ts` 断言）。
- [ ] 网关 `tsc --noEmit` + `vitest run` 全绿。
- [ ] 一次真实最小 `prompt` 成功（无可用模型时标记 `skipped` 并在界面明示）。
- [ ] `aragon info --json` 的 `schemaVersion === 1`、`features` 含 `exec,sessions,permissions,budgets`。
- [ ] 切换后 `/healthz` 连续 3 次通过且 `engine.version` 匹配；真实一次 Agent 轮次可用。
- [ ] 失败路径演练：人为破坏构建 → 断言自动回滚、`result=failed`、概览出现高优待办、已发布能力调用不受影响。
- [ ] 回滚后 `var/aragon-home/engines/<old>/` 可读、会话重放正常。

---

## 10. 未验证项（明确标注，禁止当作已知事实）

| 项 | 状态 | 说明 |
|---|---|---|
| `@aragon-agent/core@0.2.37`（npm `latest`）的实际内容与行为 | 未验证 | registry 有版本、git 无对应 tag；需在隔离环境构建后补测 |
| `@aragon-agent/cli@0.6.25` 的 `exec` 契约细节 | 未验证 | `schemaVersion` 是否仍为 1 需实测 |
| 真实模型推理（无可用 API Key 的机器上） | 未验证 | 本机无第三方凭证；设计文档 §15.2 已列为「待验证」 |
| `isolated-vm` 沙箱（CodeAct） | 未启用 | 可选依赖，本阶段不安装 |
| 团队子代理（`team`）与快速评审（`fast_review`）事件 | 未适配 | 事件可读，平台暂不展示，状态标 `needs_adapter` |
| 桌面端（`desktop/`）能力 | 不使用 | 与平台无关 |

---

_本文件的任何新增事实都必须附上采证命令与输出，否则不得写入。_
