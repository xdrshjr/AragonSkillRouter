/**
 * Skill Router 对外统一接口（`/api/v1`，Bearer 令牌）的类型契约 —— 手写基线版本。
 *
 * 依据 docs/plans/skill-router/spec.md §9.1 / §9.2.1 / §9.3 / §9.4 / §9.7。
 * 当后端产出 `docs/api/openapi.json` 后，`npm run gen` 会用 openapi-typescript 覆盖本文件；
 * 生成器或文档缺失时脚本**优雅降级**（打印原因、退出码 0），本文件继续可用。
 *
 * 约定：
 * - 非 2xx 一律返回 `ApiErrorResponse`（§9.1），`request_id` 必带。
 * - 列表响应用 `Paginated<T>`（管理端）或 `ItemsResponse<T>`（对外目录）。
 * - 令牌形如 `srk_<keyid>_<secret>`，只在创建时返回一次；任何响应都不包含上游地址与凭证。
 */

/** 能力支持的调用模式（复数集合；§9.3）。 */
export type CapabilityMode = 'sync' | 'async' | 'stream';

/** 令牌允许的模式集合由令牌自身限定（§10.1 `platform_tokens.allowed_modes`）。 */
export type TokenMode = CapabilityMode;

/** 异步任务状态（§9.3）。 */
export type TaskStatus =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'canceling'
  | 'canceled'
  | 'status_unknown';

/** 取消作用域：上游真的取消了，还是只停止本地等待（§9.2.1 / §15.3）。 */
export type CancelScope = 'remote' | 'local_only';

/** 同步调用的终态。 */
export type InvocationStatus = 'succeeded' | 'failed' | 'canceled' | 'status_unknown';

/** 错误码。与 `apps/api/skillrouter_api/errors.py::Code` 保持同名（附录 A）。 */
export type ApiErrorCode =
  // 通用
  | 'VALIDATION_FAILED'
  | 'NOT_FOUND'
  | 'CONFLICT'
  | 'INTERNAL'
  | 'MAINTENANCE'
  | 'DB_BUSY'
  // 参数 / 能力
  | 'INPUT_SCHEMA_INVALID'
  | 'UNSUPPORTED_MODE'
  | 'UNSUPPORTED_PARAM'
  | 'PROVIDER_NOT_AVAILABLE'
  | 'CAPABILITY_NOT_CALLABLE'
  | 'IDEMPOTENCY_KEY_REUSED'
  // 令牌 / 配额
  | 'TOKEN_INVALID'
  | 'TOKEN_REVOKED'
  | 'TOKEN_EXPIRED'
  | 'TOKEN_SCOPE_DENIED'
  | 'QUOTA_EXCEEDED'
  | 'BUDGET_EXCEEDED'
  // SKILL
  | 'SKILL_DEPENDENCY_MISSING'
  | 'SKILL_DISABLED'
  // 文件
  | 'FILE_TOO_LARGE'
  | 'FILE_TYPE_UNSUPPORTED'
  // 任务
  | 'CANCEL_UNSUPPORTED'
  | 'ASYNC_QUERY_MISSING'
  // 上游
  | 'UPSTREAM_AUTH'
  | 'UPSTREAM_RATE_LIMIT'
  | 'UPSTREAM_QUOTA'
  | 'UPSTREAM_TIMEOUT'
  | 'UPSTREAM_UNAVAILABLE'
  | 'UPSTREAM_INVALID_RESPONSE'
  | 'UPSTREAM_BAD_REQUEST'
  | 'UPSTREAM_ERROR'
  // 兜底（客户端自造：连通性失败，服务端不会返回）
  | 'NETWORK_ERROR'
  | 'NETWORK_TIMEOUT';

/** §9.1 错误体。`request_id` 与响应头 `X-Request-Id` 同值。 */
export interface ApiError {
  code: ApiErrorCode | (string & {});
  message: string;
  hint?: string;
  retryable?: boolean;
  details?: Record<string, unknown>;
  request_id?: string;
  /** 上游 HTTP 状态（失败体补充字段，§9.3） */
  upstream_status?: number;
  /** 失败体补充字段：涉及的提供商（§9.3） */
  provider?: string;
}

/** 所有非 2xx 响应体。 */
export interface ApiErrorResponse {
  error: ApiError;
}

/** 管理端分页（§9.1）。 */
export interface Paginated<T> {
  items: T[];
  page: number;
  page_size: number;
  total: number;
}

/** 对外目录（`/api/v1/capabilities`、`/api/v1/skills`）既可能直接是数组，也可能包一层 `items`。 */
export interface ItemsResponse<T> {
  items: T[];
}

// ---------------------------------------------------------------------------
// GET /api/v1/health（无鉴权，连通性检查用）
// ---------------------------------------------------------------------------

export interface EngineHealth {
  version: string;
  status: 'available' | 'unavailable' | 'degraded' | (string & {});
}

export interface HealthResponse {
  status: 'ok' | 'degraded' | (string & {});
  /** 平台版本（写进 `skillrouter.config.json` 的 `server_version`） */
  version: string;
  engine: EngineHealth;
  /** ISO8601 UTC */
  time: string;
}

// ---------------------------------------------------------------------------
// GET /api/v1/whoami（令牌主体 / 作用域 / 配额余量）
// ---------------------------------------------------------------------------

export interface TokenLimits {
  rpm: number | null;
  concurrency: number | null;
  daily_calls: number | null;
  budget_usd: number | null;
}

export interface WhoamiResponse {
  token_id: string;
  agent: { id: string; name: string };
  /** 令牌前缀（`srk_<keyid>`，可公开；完整令牌不会出现在任何响应里） */
  prefix: string;
  /** 能力键、`tag:<tag>` 或 `*`（§7.3） */
  scopes: string[];
  allowed_modes: TokenMode[];
  limits: TokenLimits;
  expires_at?: string | null;
}

// ---------------------------------------------------------------------------
// JSON Schema（能力输入/输出的形状，不引入第三方类型）
// ---------------------------------------------------------------------------

export interface JsonSchema {
  type?: string | string[];
  title?: string;
  description?: string;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  items?: JsonSchema | JsonSchema[];
  oneOf?: JsonSchema[];
  anyOf?: JsonSchema[];
  allOf?: JsonSchema[];
  enum?: unknown[];
  const?: unknown;
  default?: unknown;
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  pattern?: string;
  format?: string;
  additionalProperties?: boolean | JsonSchema;
}

/** 已上传文件的输入引用（§9.3）。 */
export interface FileRef {
  $file: string;
  mime?: string;
}

/** 能力声明的文件输入/输出 MIME 白名单（§9.3）。 */
export interface FileIo {
  input: string[];
  output: string[];
}

/** 能力限制（`limits`，具体字段随能力实现而定）。 */
export interface CapabilityLimits {
  max_input_bytes?: number;
  max_output_bytes?: number;
  max_duration_ms?: number;
  max_concurrency?: number;
  rpm?: number;
}

/** 路由摘要：只暴露提供商名/优先级/可用性，不暴露上游 URL 与凭证（§9.2.1）。 */
export interface RouteSummary {
  provider: string;
  priority: number;
  available: boolean;
  route_key?: string;
}

/** 目录里的能力条目（`GET /api/v1/capabilities`）。 */
export interface CapabilitySummary {
  key: string;
  name: string;
  kind?: string;
  description?: string;
  modes: CapabilityMode[];
  version?: string;
  tags?: string[];
  requires_agent_reasoning?: boolean;
}

/** 能力详情（`GET /api/v1/capabilities/{key}`）。 */
export interface CapabilityDetail extends CapabilitySummary {
  input_schema: JsonSchema;
  output_schema?: JsonSchema;
  examples?: Array<{ title?: string; input: Record<string, unknown> }>;
  limits?: CapabilityLimits;
  file_io?: FileIo;
  routes_summary?: RouteSummary[];
}

/** 能力目录响应：`GET /api/v1/capabilities`（支持 `ETag` / `If-None-Match` / `?refresh=1`）。 */
export type CapabilitiesResponse = ItemsResponse<CapabilitySummary> | CapabilitySummary[];

// ---------------------------------------------------------------------------
// POST /api/v1/capabilities/{key}/invoke（§9.3）
// ---------------------------------------------------------------------------

export interface InvokeOptions {
  /** 必须 ∈ 能力 `modes`；省略取 `modes[0]` */
  mode?: CapabilityMode;
  timeout_ms?: number;
  stream?: boolean;
  preferred_provider?: string;
  max_cost_usd?: number;
  idempotency_key?: string;
  callback_url?: string;
  /** 提供商特有参数的唯一合法入口 */
  extensions?: Record<string, Record<string, unknown>>;
}

export interface InvokeRequest {
  /** 固定能力版本（semver）；省略用当前发布版本 */
  capability_version?: string;
  input: Record<string, unknown>;
  options?: InvokeOptions;
}

export interface TokenUsage {
  unit: 'tokens' | 'images' | 'seconds' | 'chars' | 'bytes';
  input?: number;
  output?: number;
  total?: number;
  cost_usd?: number;
  source?: 'provider' | 'estimated';
}

export interface InvokeTimings {
  queue_ms?: number;
  upstream_ms?: number;
  total_ms?: number;
}

export interface OutputFile {
  file_id: string;
  mime: string;
  size: number;
}

/** 同步调用响应（200）。 */
export interface InvokeResponse {
  request_id: string;
  status: InvocationStatus;
  capability: { key: string; version: string };
  provider: { id: string; name: string };
  output: Record<string, unknown>;
  output_files?: OutputFile[];
  usage?: TokenUsage;
  timings?: InvokeTimings;
  error?: ApiError | null;
}

/** 异步调用响应（202）。 */
export interface AsyncInvokeResponse {
  request_id: string;
  task_id: string;
  status: 'queued' | 'running';
}

/** 调用入口的判别联合：`options.mode` 决定拿到哪一种。 */
export type InvokeResult = InvokeResponse | AsyncInvokeResponse;

// ---------------------------------------------------------------------------
// GET /api/v1/tasks/{task_id} / POST /api/v1/tasks/{task_id}/cancel
// ---------------------------------------------------------------------------

export interface TaskProgress {
  percent: number;
  stage: string;
}

export interface TaskResponse {
  task_id: string;
  status: TaskStatus;
  /** 异步任务的结果（成功时） */
  result?: InvokeResponse;
  error?: ApiError | null;
  progress?: TaskProgress;
  status_detail?: string;
  upstream_task_id?: string;
  created_at?: string;
  updated_at?: string;
}

export interface CancelTaskResponse {
  task_id: string;
  status: TaskStatus;
  /** 上游不支持取消时是 `local_only`，UI 必须如实说明（§15.3） */
  cancel_scope: CancelScope;
}

// ---------------------------------------------------------------------------
// 文件（§9.2.1 / §7.11）
// ---------------------------------------------------------------------------

export interface FileUploadResponse {
  file_id: string;
  mime: string;
  size: number;
  sha256: string;
  expires_at?: string | null;
}

/** `GET /api/v1/files/{file_id}` 成功时返回二进制体 + 这些响应头。 */
export interface FileDownloadMeta {
  'content-type': string;
  'content-length': number;
  'x-content-type-options': 'nosniff';
  'content-disposition'?: string;
}

// ---------------------------------------------------------------------------
// SKILL 目录（`/api/v1/skills`，只返回 `enabled=1 && status=published`）
// ---------------------------------------------------------------------------

export interface SkillSummary {
  name: string;
  description: string;
  version: string;
  required_capabilities: string[];
  title?: string;
  tags?: string[];
}

/** `GET /api/v1/skills/{name}`：frontmatter + 正文 + 示例。 */
export interface SkillDetailResponse extends SkillSummary {
  title: string;
  /** 完整 SKILL.md（frontmatter + 正文）。CLI 只采用其正文，frontmatter 由 CLI 自己生成。 */
  skill_md: string;
  examples?: Array<{ title?: string; input: Record<string, unknown> }>;
  /** 可选：平台下发的 `invoke.mjs` 源（有则用它覆盖 CLI 模板，保证契约同步） */
  invoke_mjs?: string;
}

export type SkillsResponse = ItemsResponse<SkillSummary> | SkillSummary[];

// ---------------------------------------------------------------------------
// SSE 事件（§9.4，`data:` 一行 JSON）
// ---------------------------------------------------------------------------

export interface SseEnvelope<TType extends string, TPayload> {
  id: number;
  type: TType;
  at: string;
  turn_id?: string;
  seq?: number;
  payload: TPayload;
}

export type CallSseEvent =
  | SseEnvelope<'call.delta', { chunk: string }>
  | SseEnvelope<'call.usage', TokenUsage>
  | SseEnvelope<'call.done', { request_id: string; status: InvocationStatus }>
  | SseEnvelope<'call.error', ApiError>;

export type TaskSseEvent =
  | SseEnvelope<'task.updated', { status: TaskStatus; progress?: TaskProgress }>
  | SseEnvelope<'task.log', { line: string }>
  | SseEnvelope<'task.finished', { status: TaskStatus; result?: InvokeResponse; error?: ApiError }>;

// ---------------------------------------------------------------------------
// 路由表（便于客户端与测试共用一份路径清单）
// ---------------------------------------------------------------------------

/** `/api/v1` 端点（§9.2.1）。`{param}` 为路径参数。 */
export type ApiV1Path =
  | '/api/v1/health'
  | '/api/v1/whoami'
  | '/api/v1/capabilities'
  | '/api/v1/capabilities/{key}'
  | '/api/v1/capabilities/{key}/invoke'
  | '/api/v1/tasks/{task_id}'
  | '/api/v1/tasks/{task_id}/cancel'
  | '/api/v1/files'
  | '/api/v1/files/{file_id}'
  | '/api/v1/skills'
  | '/api/v1/skills/{name}'
  | '/api/v1/skills/{name}/bundle'
  | '/api/v1/mcp';

/** 各端点成功响应的类型映射（便于写泛型客户端）。 */
export interface ApiV1Responses {
  '/api/v1/health': HealthResponse;
  '/api/v1/whoami': WhoamiResponse;
  '/api/v1/capabilities': CapabilitiesResponse;
  '/api/v1/capabilities/{key}': CapabilityDetail;
  '/api/v1/capabilities/{key}/invoke': InvokeResult;
  '/api/v1/tasks/{task_id}': TaskResponse;
  '/api/v1/tasks/{task_id}/cancel': CancelTaskResponse;
  '/api/v1/files': FileUploadResponse;
  '/api/v1/skills': SkillsResponse;
  '/api/v1/skills/{name}': SkillDetailResponse;
}
