/**
 * 与后端模型 / spec §9 对齐的共享类型。
 * 字段名严格使用 API 的 snake_case；禁止 any。
 */

/* ── 基础 ─────────────────────────────────────────────── */

export type Json = string | number | boolean | null | Json[] | { [key: string]: Json };
export type JsonObject = { [key: string]: Json };

export interface Paginated<T> {
  items: T[];
  page: number;
  page_size: number;
  total: number;
}

export type SortDir = 'asc' | 'desc';
export type PageSize = 20 | 50 | 100;

export interface ApiErrorShape {
  code: string;
  message: string;
  hint?: string | null;
  retryable?: boolean;
  details?: JsonObject;
  request_id?: string | null;
  upstream_status?: number | null;
  provider?: { id: string; name: string } | null;
}

export interface IdName {
  id: string;
  name: string;
}

/* ── 认证 ─────────────────────────────────────────────── */

export interface AdminSummary {
  id: string;
  username: string;
  must_change_password: boolean;
}

export interface BootstrapInfo {
  initialized: boolean;
  must_change_password: boolean;
  version?: string | null;
}

export interface LoginResult {
  admin: AdminSummary;
  csrf_token: string;
}

export type EngineState =
  | 'available'
  | 'degraded'
  | 'unavailable'
  | 'needs_adapter'
  | 'permission_limited'
  | 'unsupported';

export type EnginePolicy = 'auto' | 'notify' | 'manual';

export interface EngineSummary {
  version: string | null;
  status: EngineState;
  candidate?: string | null;
  policy?: EnginePolicy | null;
}

export interface MeResponse {
  admin: AdminSummary;
  session: { expires_at: string | null; scope?: string | null };
  engine: EngineSummary;
  env: string;
  version?: string;
}

/* ── 提供商与凭证 ─────────────────────────────────────── */

export type ProviderStatus = 'active' | 'disabled';
export type CredentialStatus = 'active' | 'disabled' | 'expired';

export interface Provider {
  id: string;
  slug: string;
  name: string;
  category: string;
  base_url: string | null;
  docs_url: string | null;
  tags: string[];
  environment: string;
  notes: string | null;
  status: ProviderStatus;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
  credential_count?: number;
  active_credential_count?: number;
  capability_count?: number;
}

export interface Credential {
  id: string;
  provider_id: string;
  name: string;
  kind: string;
  auth_type: string;
  header_name: string | null;
  secret_mask: string;
  secret_fingerprint: string;
  meta: JsonObject;
  status: CredentialStatus;
  expires_at: string | null;
  last_verified_at: string | null;
  last_verify_ok: boolean | null;
  rotation_of: string | null;
  created_at: string;
  updated_at: string;
}

export interface CredentialVerification {
  id: string;
  credential_id: string;
  ok: boolean;
  status_code: number | null;
  latency_ms: number | null;
  error_code: string | null;
  detail_redacted: string | null;
  at: string;
}

export interface ImpactRef {
  id: string;
  name: string;
  detail?: string | null;
}

export interface CredentialImpact {
  capabilities: ImpactRef[];
  skills: ImpactRef[];
  tokens: ImpactRef[];
  tasks: ImpactRef[];
}

export interface ProviderDetail extends Provider {
  credentials: Credential[];
  capabilities: CapabilitySummary[];
}

export interface CapabilitySummary {
  id: string;
  key: string;
  name: string;
  status: CapabilityStatus;
}

/* ── 能力 ─────────────────────────────────────────────── */

export type CallMode = 'sync' | 'async' | 'stream';
export type CapabilityStatus = 'draft' | 'published' | 'deprecated' | 'failing';
export type CapabilityKind =
  | 'text.generate'
  | 'image.generate'
  | 'video.generate'
  | 'audio.speech'
  | 'audio.transcribe'
  | 'generic.http';

export interface FileIo {
  input?: string[];
  output?: string[];
}

export interface FailoverConfig {
  enabled: boolean;
  max_switches: number;
}

export type ParamTransform =
  | 'passthrough'
  | 'int'
  | 'float'
  | 'string'
  | 'bool'
  | 'json'
  | 'join'
  | 'base64';

export interface ParamMapEntry {
  from: string;
  transform?: ParamTransform | null;
  default?: Json;
  max?: number | null;
  [key: string]: Json | ParamTransform | undefined | null;
}

export interface RouteRequest {
  method: string;
  url_template: string;
  headers?: Record<string, string> | null;
  query?: string[] | null;
  body_template?: JsonObject | null;
  encoding?: 'json' | 'form' | 'multipart';
}

export interface RouteLimits {
  max_input_bytes?: number | null;
  max_output_bytes?: number | null;
  max_duration_ms?: number | null;
  allowed_mime?: string[] | null;
  max_file_mb?: number | null;
}

export interface RouteErrorMatch {
  status?: number | null;
  json?: string | null;
  equals?: Json;
}

export interface RouteErrorMap {
  match: RouteErrorMatch;
  code: string;
  retryable: boolean;
  hint?: string | null;
  backoff_ms?: number | null;
}

export interface RetryPolicy {
  max_attempts: number;
  base_ms: number;
  max_ms: number;
}

export interface CapabilityRoute {
  id: string;
  capability_id: string;
  provider_id: string;
  credential_id: string;
  route_key: string;
  priority: number;
  enabled: boolean;
  model_hint: string | null;
  request: RouteRequest;
  param_map: Record<string, ParamMapEntry>;
  response_map: JsonObject;
  stream: JsonObject | null;
  async_spec: JsonObject | null;
  limits: RouteLimits;
  errors: RouteErrorMap[];
  timeout_ms: number;
  retry: RetryPolicy;
  health: JsonObject;
  created_at: string;
  updated_at: string;
  provider_name?: string;
  credential_name?: string;
}

export interface Capability {
  id: string;
  key: string;
  name: string;
  kind: CapabilityKind | string;
  modes: CallMode[];
  file_io: FileIo;
  compatibility_group: string | null;
  enabled: boolean;
  /** 详情接口返回的可编辑定义（含 failover / routes / schema）；列表接口不返回。 */
  definition?: JsonObject;
  draft_revision: number;
  published_version_id: string | null;
  latest_version: string | null;
  status: CapabilityStatus;
  requires_agent_reasoning: boolean;
  tags: string[];
  created_at: string;
  updated_at: string;
  routes?: CapabilityRoute[];
  success_rate_24h?: number | null;
  last_test_ok?: boolean | null;
  last_test_at?: string | null;
  has_unpublished_changes?: boolean;
  issues?: CapabilityIssue[];
}

export interface CapabilityIssue {
  path: string;
  code: string;
  message: string;
  hint?: string | null;
  severity?: 'error' | 'warn';
}

export interface CapabilityValidation {
  ok: boolean;
  issues: CapabilityIssue[];
}

export interface CapabilityTestResult {
  test_run_id: string;
  ok: boolean;
  status: string;
  http_status: number | null;
  latency_ms: number | null;
  response_preview: string | null;
  error: ApiErrorShape | null;
  route: {
    id: string;
    provider_id?: string | null;
    provider_name?: string | null;
    credential_id?: string | null;
  } | null;
  at?: string | null;
}

export interface CapabilityTestRun {
  id: string;
  capability_id: string;
  capability_version_id: string | null;
  route_id: string | null;
  version: string | null;
  mode: CallMode;
  ok: boolean;
  http_status: number | null;
  latency_ms: number | null;
  request_preview_redacted: string | null;
  response_preview_redacted: string | null;
  error_code: string | null;
  error_detail_redacted: string | null;
  at: string;
}

export interface CapabilityVersion {
  id: string;
  capability_id: string;
  version: string;
  content_hash: string;
  definition: JsonObject;
  status: string;
  published_at: string | null;
  published_by: string | null;
  changelog: string | null;
  test_run_ids: string[];
  created_at: string;
}

export type DiffKind = 'added' | 'removed' | 'changed';

export interface DiffEntry {
  path: string;
  before: Json;
  after: Json;
  kind: DiffKind;
}

export interface CapabilityDiff {
  from: string;
  to: string;
  entries: DiffEntry[];
}

export interface CapabilityImpact {
  skills: ImpactRef[];
  tokens: ImpactRef[];
  tasks: ImpactRef[];
  credentials: ImpactRef[];
}

/* ── SKILL ────────────────────────────────────────────── */

export type SkillStatus = 'draft' | 'published' | 'deprecated';

export interface Skill {
  id: string;
  name: string;
  title: string;
  description: string;
  category: string | null;
  tags: string[];
  status: SkillStatus;
  enabled: boolean;
  published_version_id: string | null;
  latest_version: string | null;
  source_type: 'manual' | 'git' | 'url' | 'file';
  source_ref: string | null;
  source_commit: string | null;
  source_url: string | null;
  dependency_count?: number;
  created_at: string;
  updated_at: string;
}

/** 依赖项（`skills.draft_definition.dependencies`，spec §7.9）。 */
export interface SkillDependency {
  capability_key: string;
  version_range?: string | null;
}

export type SkillStepKind = 'capability' | 'agent_reasoning' | 'transform';

/** 执行计划步骤（`SkillExecutionPlan.steps[]`，spec §7.9）。 */
export interface SkillPlanStep {
  id: string;
  type: SkillStepKind;
  capability_key?: string | null;
  version?: string | null;
  inputs?: JsonObject;
  output_as?: string | null;
  on_error?: 'stop' | 'continue' | 'fallback';
  fallback?: JsonObject | null;
}

export interface SkillVersion {
  id: string;
  skill_id: string;
  version: string;
  content_hash: string;
  skill_md: string;
  frontmatter: JsonObject;
  execution_plan: JsonObject;
  dependencies: SkillDependency[];
  permissions: JsonObject;
  examples: JsonObject[];
  status: string;
  published_at: string | null;
  created_at: string;
}

export interface SkillDetail extends Skill {
  definition: JsonObject;
  dependencies: SkillDependency[];
  skill_md: string;
  published_skill_md?: string | null;
  versions: {
    id: string;
    version: string;
    status: string;
    published_at: string | null;
    is_current: boolean;
  }[];
  install_command: string;
  missing_dependencies: { capability_key: string; version_range: string | null; reason: string }[];
  installable: boolean;
}

export interface SkillImportPreview {
  preview_token: string;
  kind: 'git' | 'url' | 'file';
  source_url: string | null;
  resolved_commit: string | null;
  files: { path: string; size: number; mime?: string | null; executable?: boolean }[];
  warnings: string[];
  requires_sandbox_review: boolean;
  skill_md_preview?: string | null;
}

export interface SkillPreview {
  skill_md: string;
  version: string | null;
}

/* ── 接入与令牌 ───────────────────────────────────────── */

export interface ExternalAgent {
  id: string;
  name: string;
  description: string | null;
  contact: string | null;
  status: 'active' | 'disabled';
  last_seen_at: string | null;
  created_at: string;
  token_count?: number;
}

export interface PlatformToken {
  id: string;
  agent_id: string;
  name: string;
  prefix: string;
  scopes: string[];
  allowed_modes: CallMode[];
  expires_at: string | null;
  rpm: number | null;
  concurrency: number | null;
  daily_calls: number | null;
  budget_usd: number | null;
  revoked_at: string | null;
  last_used_at: string | null;
  created_at: string;
}

export interface CreatedToken extends PlatformToken {
  /** 仅创建时返回一次，之后永不回显 */
  token: string;
  install_command: string;
}

/* ── 调用记录 / 任务 ──────────────────────────────────── */

export type CallStatus = 'success' | 'failed' | 'status_unknown' | 'canceled';

export interface CallLog {
  id: string;
  request_id: string;
  caller_type: string;
  caller_id: string | null;
  capability_key: string;
  capability_version_id: string | null;
  provider_id: string | null;
  credential_id: string | null;
  mode: CallMode;
  status: CallStatus;
  http_status: number | null;
  error_code: string | null;
  latency_ms: number | null;
  queue_ms: number | null;
  upstream_ms: number | null;
  retries: number;
  payload_policy: string;
  request_preview: string | null;
  response_preview: string | null;
  task_id: string | null;
  created_at: string;
}

export interface RouteDecisionRecord {
  id: string;
  call_id: string | null;
  candidates: JsonObject[];
  dropped: JsonObject[];
  chosen_route_id: string | null;
  chosen_capability_version_id: string | null;
  chosen_provider_id: string | null;
  reason: string | null;
  failover_from: string | null;
  created_at: string;
}

export interface CallDetail extends CallLog {
  route_decision: RouteDecisionRecord | null;
  retry_chain: JsonObject[];
  error_mapping: JsonObject | null;
  timings: { queue_ms: number | null; upstream_ms: number | null; total_ms: number | null };
}

export type TaskStatus =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'canceling'
  | 'canceled'
  | 'status_unknown';

export interface OutputFile {
  file_id: string;
  mime: string;
  size: number;
}

export interface Task {
  id: string;
  request_id: string;
  capability_key: string;
  capability_version_id: string | null;
  mode: CallMode;
  status: TaskStatus;
  progress_percent: number;
  progress_stage: string | null;
  upstream_task_id: string | null;
  attempts: number;
  max_attempts: number;
  failure_class: string | null;
  error_code: string | null;
  error_detail_redacted: string | null;
  cancel_capability: 'none' | 'local_only' | 'remote';
  result: JsonObject;
  output_files: OutputFile[];
  resume_policy: string;
  route_id: string | null;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
}

export interface CancelResult {
  cancel_scope: 'remote' | 'local_only';
  status: TaskStatus;
}

/* ── Agent ────────────────────────────────────────────── */

export interface AgentAttachment {
  file_id: string;
  name?: string;
  mime?: string;
  size?: number;
}

export type TurnStatus = 'queued' | 'running' | 'stopping' | 'stopped' | 'succeeded' | 'failed';

export interface StopReport {
  stopped_steps: string[];
  external_pending: { kind: string; ref: string; note?: string | null }[];
  note?: string | null;
}

export interface AgentTurn {
  id: string;
  session_id: string;
  ordinal: number;
  user_text: string;
  attachments: AgentAttachment[];
  status: TurnStatus;
  queue_position: number | null;
  stop_report: StopReport | null;
  usage: JsonObject;
  error_code: string | null;
  error_detail_redacted: string | null;
  retry_of: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
}

export interface AgentSession {
  id: string;
  title: string;
  model_profile: JsonObject;
  permission_profile: JsonObject;
  engine_version: string | null;
  status: 'active' | 'archived';
  turn_count: number;
  created_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface ToolCallSummary {
  id: string;
  turn_id: string;
  tool_name: string;
  args_summary?: JsonObject;
  result_summary: string | null;
  ok: boolean | null;
  duration_ms: number | null;
  at: string;
}

export interface AgentTodo {
  id: string;
  text: string;
  status: 'pending' | 'in_progress' | 'done';
}

export interface AgentSessionDetail extends AgentSession {
  turns: AgentTurn[];
  tool_calls: ToolCallSummary[];
  todos?: AgentTodo[];
}

export interface SendMessageResult {
  turn_id: string;
  queue_position: number | null;
}

export type FlowStep = 'parse' | 'draft' | 'validate' | 'test' | 'sample' | 'check' | 'publish';
export type StepStatus = 'pending' | 'running' | 'passed' | 'failed' | 'skipped';

export interface AgentFlowStep {
  id: string;
  flow_id: string;
  step: FlowStep;
  ordinal: number;
  status: StepStatus;
  detail: string | null;
  evidence: JsonObject;
  error_code: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface AgentFlow {
  id: string;
  title: string;
  capability_id: string | null;
  skill_id: string | null;
  credential_id: string | null;
  status: 'running' | 'waiting_approval' | 'failed' | 'published' | 'stopped';
  auto_publish: boolean;
  budget: JsonObject;
  spent: JsonObject;
  current_step: FlowStep;
  steps: AgentFlowStep[];
  created_at: string;
  updated_at: string;
  finished_at: string | null;
}

export interface AgentApproval {
  id: string;
  flow_id: string | null;
  turn_id: string | null;
  tool_call_id: string | null;
  action: string;
  target: string | null;
  impact: JsonObject;
  requested_scope: JsonObject;
  status: 'pending' | 'approved' | 'rejected' | 'expired';
  decided_by: string | null;
  decided_at: string | null;
  expires_at: string;
  note: string | null;
  created_at: string;
}

export interface AgentConfig {
  provider: string | null;
  model: string | null;
  base_url: string | null;
  temperature: number | null;
  max_tokens: number | null;
  credential_ref: string | null;
  allowed_providers: string[];
  max_test_calls_per_flow: number;
  max_cost_usd_per_flow: number;
  allowed_window: string;
}

export interface AgentConnectivityResult {
  gateway_ok: boolean;
  model_ok: boolean;
  detail: string | null;
  checked_at?: string | null;
}

/* ── 引擎 ─────────────────────────────────────────────── */

export interface EngineTool {
  name: string;
  summary?: string;
  permission?: string;
}

export type EngineCapabilityState =
  | 'available'
  | 'needs_adapter'
  | 'permission_limited'
  | 'unsupported';

export interface EngineCapabilityStatus {
  id?: string;
  capability_key: string;
  state: EngineCapabilityState;
  detail: string | null;
  since_version: string | null;
  updated_at?: string;
  ordinal?: number;
}

/** `/admin/engine/capabilities` 的形状：按状态分组。 */
export interface EngineCapabilityGroups {
  groups: Partial<Record<EngineCapabilityState, { key: string; detail: string | null; since_version: string | null }[]>>;
  note: string;
}

export interface EngineStatus {
  /** 适配层从网关 /healthz 拿到的实时引擎信息 */
  engine: {
    status: EngineState;
    version?: string | null;
    reachable?: boolean;
    active_turns?: number;
    error?: string;
  };
  /** 当前实际部署的 pin（`<version>@<commit>`） */
  engine_pin: string;
  engine_previous_pin?: string | null;
  engine_policy: EnginePolicy;
  engine_channel: string;
  egress_policy: 'allow' | 'deny';
  adapter_version: string;
  tools: (EngineTool | { name: string })[];
  capabilities: { key: string; state: EngineCapabilityState; detail: string | null; since_version: string | null; ordinal?: number }[];
  upgrade_in_progress: boolean;
  checks_allowed: boolean;
  last_checked_at: string | null;
}

export interface EngineCandidate {
  id: string;
  version: string;
  commit_sha: string | null;
  provenance: 'tagged' | 'npm-only' | 'linked' | string;
  published_at: string | null;
  channel: string;
  notes: string | null;
  detected_at: string;
  verification_status: 'pending' | 'passed' | 'failed' | 'skipped';
  verification_report: JsonObject;
}

export interface EngineRunStep {
  name: string;
  status: StepStatus;
  detail: string | null;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface EngineUpgradeRun {
  id: string;
  from_version: string;
  to_version: string;
  mode: 'verify_only' | 'deploy';
  policy: string;
  started_at: string;
  finished_at: string | null;
  result: 'passed' | 'failed' | 'rolled_back' | null;
  steps: EngineRunStep[];
  error: string | null;
  backup_file_id: string | null;
  rollback_of: string | null;
}

/* ── 概览 / 设置 / 审计 ───────────────────────────────── */

export interface AttentionItem {
  code: string;
  severity: 'info' | 'warning' | 'error';
  title: string;
  /** 前端可跳转路由，保证每项信息都能进详情 */
  target: string;
}

export interface Overview {
  capabilities: { published: number; draft: number; failing: number };
  providers: { total: number; with_active_credential: number };
  success_rate_24h: number;
  calls_24h: number;
  latency: { p50_ms: number; p95_ms: number };
  failed_tasks: number;
  queue_depth: number;
  engine: EngineSummary;
  attention: AttentionItem[];
  recent_calls?: CallLog[];
}

export type PayloadPolicy = 'none' | 'redacted_preview' | 'full';

export interface PlatformSettings {
  payload_policy: PayloadPolicy;
  retention_days: number;
  fetch_allowlist: string[];
  /** 月预算（USD）；GET 返回 default_budget_usd，PATCH 也用它（没有嵌套的 budget 对象）。 */
  default_budget_usd: number;
  engine_policy: EnginePolicy;
  env: string;
  install_mode?: string;
  install_command?: string;
  public_base_url?: string;
  max_upload_mb?: number;
  egress_policy?: 'allow' | 'deny';
  retention_days_max?: number;
  ssrf_deny?: string[];
  agent_model?: JsonObject;
  plan_mode_enabled?: boolean;
  allowed_providers?: string[];
}

export interface AuditLogEntry {
  id: string;
  at: string;
  actor_type: string;
  actor_id: string | null;
  action: string;
  object_type: string | null;
  object_id: string | null;
  summary: JsonObject;
  request_id: string | null;
  ip: string | null;
}

export interface BackupResult {
  file_id: string;
  sha256?: string;
  download_url?: string;
  created_at?: string | null;
}

export interface RotateMasterKeyResult {
  rotation_id: string;
  status: string;
  rows_total?: number;
  rows_done?: number;
  started_at?: string | null;
  finished_at?: string | null;
}

/* ── SSE ──────────────────────────────────────────────── */

export interface SseEnvelope {
  id?: number | string;
  type: string;
  at: string;
  turn_id?: string;
  session_id?: string;
  seq?: number;
  payload: JsonObject;
}

/* ── 表格查询参数 ─────────────────────────────────────── */

export interface TableQuery {
  page: number;
  page_size: PageSize;
  sort: string;
  q: string;
  [key: string]: string | number | boolean | undefined;
}
