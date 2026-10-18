# `@skillrouter/contracts`

Skill Router 对外接口 **`/api/v1`** 的 TypeScript 类型契约（依据 `docs/plans/skill-router/spec.md` §9.1 / §9.2.1 / §9.3 / §9.4）。

- **纯类型包**：只分发 `src/api.d.ts`，没有运行时代码，不引入任何运行时依赖。
- `src/api.d.ts` 当前是**手写基线**：即使后端还没产出 `docs/api/openapi.json`，前端的调用代码也能先按真实字段写。
- 后端产出 OpenAPI 后，`scripts/gen.ts` 会用 `openapi-typescript` 覆盖本文件；生成器或文档缺失时脚本**优雅降级**
  （打印原因、退出码 0），不会把 CI 卡死。

## 使用

```ts
import type {
  HealthResponse,
  WhoamiResponse,
  CapabilityDetail,
  InvokeRequest,
  InvokeResult,
  TaskResponse,
  CancelTaskResponse,
  SkillDetailResponse,
  ApiErrorResponse,
} from '@skillrouter/contracts';

const health: HealthResponse = await fetch(`${base}/api/v1/health`).then((r) => r.json());

const req: InvokeRequest = {
  input: { prompt: '雨中的柴犬' },
  options: { mode: 'async', idempotency_key: 'agent-a-94f2' },
};

const result: InvokeResult = await post('/api/v1/capabilities/text.generate.gpt4o-mini/invoke', req);
// 判别：异步返回 {task_id}，同步返回 {output,...}
const taskId = 'task_id' in result ? result.task_id : undefined;
```

错误一律用 `ApiErrorResponse` 收口（`error.code` / `error.hint` / `error.request_id`），
`request_id` 与响应头 `X-Request-Id` 同值。

## 覆盖的端点与类型

| 端点 | 类型 |
|---|---|
| `GET /api/v1/health` | `HealthResponse` |
| `GET /api/v1/whoami` | `WhoamiResponse`（`scopes` / `allowed_modes` / `limits`） |
| `GET /api/v1/capabilities` | `CapabilitiesResponse`（`CapabilitySummary[]` 或 `{items:[]}`） |
| `GET /api/v1/capabilities/{key}` | `CapabilityDetail`（`input_schema` / `modes` / `limits` / `file_io` / `routes_summary`） |
| `POST /api/v1/capabilities/{key}/invoke` | `InvokeRequest` → `InvokeResult`（`InvokeResponse` \| `AsyncInvokeResponse`） |
| `GET /api/v1/tasks/{task_id}` | `TaskResponse` |
| `POST /api/v1/tasks/{task_id}/cancel` | `CancelTaskResponse`（`cancel_scope: remote \| local_only`） |
| `POST /api/v1/files` | `FileUploadResponse` |
| `GET /api/v1/files/{file_id}` | `FileDownloadMeta`（响应头约定） |
| `GET /api/v1/skills` | `SkillsResponse` |
| `GET /api/v1/skills/{name}` | `SkillDetailResponse`（`skill_md` / `required_capabilities`） |
| SSE（`call.*` / `task.*`） | `CallSseEvent` / `TaskSseEvent` |

另外导出：

- `ApiErrorCode` —— 与 `apps/api/skillrouter_api/errors.py::Code` 同名（附录 A）；客户端自造的
  `NETWORK_ERROR` / `NETWORK_TIMEOUT` 也在其中，方便统一分支。
- `Paginated<T>` / `ItemsResponse<T>` —— 管理端分页 vs 对外目录的两种列表形状。
- `ApiV1Path` + `ApiV1Responses` —— 路径清单与其成功响应类型映射，便于写泛型客户端和契约测试。
- `JsonSchema` / `FileRef`（`{"$file": "fil_..."}`）/ `FileIo` / `CapabilityLimits` / `RouteSummary`。

## 生成（后端 OpenAPI 就绪后）

```bash
npm --prefix packages/contracts install --include=dev   # 本机 NODE_ENV=production 时必须带 --include=dev
npm --prefix packages/contracts run gen                # docs/api/openapi.json → src/api.d.ts
npm --prefix packages/contracts run gen:check          # CI：与生成结果有 diff 则退出 1
node --experimental-strip-types packages/contracts/scripts/gen.ts --dry-run          # 只看结果，不落盘
node --experimental-strip-types packages/contracts/scripts/gen.ts --input <path>     # 换一个 OpenAPI 文件
```

行为约定：

| 情况 | 行为 | 退出码 |
|---|---|---|
| `docs/api/openapi.json` 不存在 | 打印「跳过」+ 提示，保持手写基线 | 0 |
| `openapi-typescript` 未安装 | 打印「跳过」+ 安装提示，保持手写基线 | 0 |
| 生成成功且内容有变化 | 写入 `src/api.d.ts`（带来源横幅） | 0 |
| 生成成功且内容无变化 | 打印「无变化」 | 0 |
| `--check` 且有差异 | 打印差异原因（手写基线会额外提示） | 1 |
| OpenAPI 文档 JSON 损坏 / 缺 `paths` | 报错退出 | 1 |

> `openapi-typescript` v7 的默认导出返回 AST，脚本会自动用同模块的 `astToString` 转成文本；
> 若某个版本两者都不兼容，脚本报「无法把生成结果序列化为文本」并以 1 退出（不会写出半个文件）。

## 开发

```bash
npm --prefix packages/contracts run typecheck   # tsc -p tsconfig.json（strict, noEmit）
```

本机已验证：

- `tsc -p tsconfig.json` 通过（`src/api.d.ts` + `scripts/gen.ts`，strict、无 `any`）。
- 一个只做 `import type` 的消费者文件对 `HealthResponse` / `InvokeRequest` / `InvokeResult` /
  `TaskResponse` / `CapabilityDetail` / `SkillDetailResponse` / `ApiErrorResponse` 全部类型检查通过
  （含 `InvokeResult` 的联合类型收敛）。
- 降级路径：`docs/api/openapi.json` 不存在 → 打印跳过、退出码 0。
- 生成路径：用一份样例 OpenAPI 跑 `--dry-run` 成功产出 `paths` 类型；`--check` 在存在差异时退出码 1。

## 注意

- 本包只分发 `.d.ts`（`exports` 的 `types`/`default` 都指向它）。**不要在运行时 `import` 它的值**；
  需要运行时校验的，用后端 `/api/openapi.json` 或自己写 zod schema。
- 类型的唯一事实来源是 spec §9 与后端实现。发现不一致时，以 spec/后端为准并让本包跟着改（或在 OpenAPI 就绪后改为生成）。
