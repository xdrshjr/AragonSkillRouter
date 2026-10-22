/**
 * Types mirroring `docs/CONTRACT.md` §5 (admin API payloads) and §4 (status
 * vocabulary). Nothing here is invented: every field name comes from a backend
 * serialiser or contract snippet.
 */

/* ------------------------------------------------------------------ shared --- */

export interface ListEnvelope<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export type Severity = 'error' | 'warning' | 'info';

/* --------------------------------------------------------------- vocabulary --- */

export type CapabilityType = 'text' | 'image' | 'video' | 'speech' | 'transcription' | 'generic';
export type CapabilityStatus = 'draft' | 'testing' | 'published' | 'disabled';
export type VersionStatus = 'draft' | 'testing' | 'published' | 'deprecated';
export type TestStatus = 'passed' | 'failed' | 'blocked';
export type ProviderCategory = 'llm' | 'image' | 'video' | 'speech' | 'transcription' | 'generic';
export type ProviderEnvironment = 'production' | 'staging' | 'development';
export type ProviderStatus = 'active' | 'disabled';
export type CredentialStatus = 'active' | 'disabled' | 'expired';
export type AuthType = 'api_key' | 'bearer' | 'basic' | 'custom_header';
export type VerificationStatus = 'valid' | 'invalid' | 'unreachable' | 'rate_limited' | 'unknown';
export type RouteHealth = 'unknown' | 'healthy' | 'degraded' | 'unhealthy';
export type RouteStrategy = 'priority' | 'fixed' | 'health_first';
export type InvocationStatus =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'uncertain'
  | 'rejected';
export type InvocationMode = 'sync' | 'async' | 'stream';

export const CAPABILITY_TYPES: CapabilityType[] = [
  'text',
  'image',
  'video',
  'speech',
  'transcription',
  'generic',
];
export const PROVIDER_CATEGORIES: ProviderCategory[] = [
  'llm',
  'image',
  'video',
  'speech',
  'transcription',
  'generic',
];
export const PROVIDER_ENVIRONMENTS: ProviderEnvironment[] = ['production', 'staging', 'development'];
export const CAPABILITY_STATUSES: CapabilityStatus[] = ['draft', 'testing', 'published', 'disabled'];
export const CREDENTIAL_STATUSES: CredentialStatus[] = ['active', 'disabled', 'expired'];
export const AUTH_TYPES: AuthType[] = ['api_key', 'bearer', 'basic', 'custom_header'];
export const ROUTE_STRATEGIES: RouteStrategy[] = ['priority', 'fixed', 'health_first'];
export const INVOCATION_MODES: InvocationMode[] = ['sync', 'async', 'stream'];

/** Secret field names per auth type (§3.1). */
export const SECRET_FIELDS: Record<AuthType, string[]> = {
  api_key: ['api_key'],
  bearer: ['token'],
  basic: ['username', 'password'],
  custom_header: ['value'],
};

/* ----------------------------------------------------------------- session --- */

export interface SessionUser {
  id: string;
  username: string;
  role: string;
  must_change_password: boolean;
  created_at: string;
  password_changed_at: string | null;
}

export interface OnboardingSteps {
  password_changed?: boolean;
  agent_model_configured?: boolean;
  first_capability_published?: boolean;
  first_agent_token_created?: boolean;
}

export interface SessionPayload {
  user: SessionUser;
  csrf_token: string;
  must_change_password: boolean;
  onboarding: OnboardingSteps;
  initialized: boolean;
}

export interface LoginResponse {
  user: SessionUser;
  csrf_token: string;
  must_change_password: boolean;
}

export interface ChangePasswordResponse {
  ok: boolean;
  csrf_token: string;
  must_change_password: boolean;
}

export interface BootstrapStatus {
  initialized: boolean;
  admin_username: string | null;
  must_change_password: boolean;
  created_at: string | null;
  steps: OnboardingSteps;
  agent_configured: boolean;
  published_capabilities: number;
  active_agent_tokens: number;
  version: string;
}

/* ------------------------------------------------------------------ overview -- */

export interface Counts {
  capabilities: { total: number; published: number; draft: number; testing: number; disabled: number };
  providers: { total: number; active: number };
  credentials: { total: number; active: number; expiring_soon: number; invalid: number; expired: number };
  skills: { total: number; published: number; draft: number };
  agents: { total: number; active: number };
  tasks: { queued: number; running: number; failed: number; uncertain: number };
}

export interface EngineHealth {
  status: string;
  version: string | null;
  adapter: string | null;
  channel: string;
  release_id?: string;
  activated_at?: string;
}

export interface HealthSummary {
  success_rate_24h: number | null;
  calls_24h: number;
  failed_calls_24h?: number;
  avg_latency_ms_24h: number | null;
  failed_tasks_24h: number;
  engine: EngineHealth;
  generated_at?: string;
}

export interface Problem {
  id: string;
  severity: Severity;
  title: string;
  detail: string;
  href: string;
  action: string;
}

export interface Onboarding {
  steps: OnboardingSteps;
  agent_configured: boolean;
  initialized: boolean;
  admin_username: string | null;
  must_change_password: boolean;
  published_capabilities: number;
  active_agent_tokens: number;
}

export interface OverviewPayload {
  counts: Counts;
  health: HealthSummary;
  problems: Problem[];
  problems_total: number;
  recent_invocations: Invocation[];
  onboarding: Onboarding;
  generated_at: string;
}

/* ---------------------------------------------------------------- providers --- */

export interface ProviderCounts {
  credentials: number;
  capabilities: number;
  routes?: number;
}

export interface Provider {
  id: string;
  name: string;
  slug: string;
  category: ProviderCategory;
  base_url: string | null;
  docs_url: string | null;
  tags: string[];
  environment: ProviderEnvironment;
  notes: string | null;
  status: ProviderStatus;
  counts: ProviderCounts;
  created_at: string;
  updated_at: string;
}

export interface ProviderCapabilityRef {
  id: string;
  slug: string;
  name: string;
  type: CapabilityType;
  status: CapabilityStatus;
  callable: boolean;
}

export interface ProviderRouteRef {
  id: string;
  capability_id: string;
  capability_slug: string;
  capability_version_id: string;
  enabled: boolean;
  health_status: RouteHealth;
  last_health_at: string | null;
}

export interface ProviderDetail extends Provider {
  credentials: Credential[];
  capabilities: ProviderCapabilityRef[];
  routes: ProviderRouteRef[];
}

export interface ProviderInput {
  name: string;
  category: ProviderCategory;
  base_url?: string | null;
  docs_url?: string | null;
  tags: string[];
  environment: ProviderEnvironment;
  notes?: string | null;
  status: ProviderStatus;
}

export interface ProviderFilters {
  search?: string;
  category?: ProviderCategory | '';
  environment?: ProviderEnvironment | '';
  status?: ProviderStatus | '';
  limit?: number;
  offset?: number;
}

/* -------------------------------------------------------------- credentials --- */

export interface CredentialConfig {
  header_name?: string | null;
  prefix?: string | null;
  value_template?: string | null;
  test_url?: string | null;
  username?: string | null;
}

export interface Credential {
  id: string;
  provider_id: string;
  provider_name: string | null;
  name: string;
  auth_type: AuthType;
  /** Masked hint only — the secret itself is never returned by the API. */
  hint: string;
  field_names: string[];
  config: CredentialConfig;
  status: CredentialStatus;
  expires_at: string | null;
  last_verified_at: string | null;
  last_verify_status: VerificationStatus | null;
  last_verify_message: string | null;
  last_rotated_at: string | null;
  key_id: string | null;
  created_at: string;
  updated_at: string;
  usage?: { capabilities?: number } | null;
  expiring_soon?: boolean;
}

export interface CredentialInput {
  provider_id: string;
  name: string;
  auth_type: AuthType;
  /** Write-only. Only the fields for the chosen auth type are sent. */
  secret?: Record<string, string>;
  config?: CredentialConfig | null;
  expires_at?: string | null;
  status?: CredentialStatus;
}

export type CredentialUpdateInput = Omit<CredentialInput, 'provider_id' | 'auth_type' | 'status'> & {
  status?: CredentialStatus;
};

export interface CredentialImpact {
  capabilities: Array<{ id: string; slug: string; name: string; status: CapabilityStatus }>;
  routes: number;
  active_tasks: number;
  blocking: boolean;
}

export interface CredentialVerifyResult {
  status: VerificationStatus;
  message: string;
  http_status: number | null;
  latency_ms: number | null;
  checked_at: string;
  credential_id: string;
  truthful: boolean;
}

export interface CredentialDeleteResult {
  deleted: boolean;
  impact: CredentialImpact;
}

/* ------------------------------------------------------------------ invoke --- */

export interface InvocationError {
  code: string;
  message: string;
  action?: string | null;
}

export interface Invocation {
  id: string;
  request_id: string;
  caller_type: string;
  agent: { id: string | null; name: string | null } | null;
  surface: string;
  capability: { slug: string; name: string } | null;
  version: number | null;
  provider: { id: string | null; name: string | null } | null;
  mode: InvocationMode | null;
  status: InvocationStatus;
  http_status: number | null;
  latency_ms: number | null;
  retries: number;
  task_id: string | null;
  usage: Record<string, unknown>;
  cost: { estimated_usd: number | null; actual_usd: number | null; currency: string } | null;
  error: InvocationError | null;
  created_at: string;
  finished_at: string | null;
}

/* -------------------------------------------------------------- capability --- */

export interface CapabilityIssue {
  severity: 'error' | 'warning';
  field: string;
  message: string;
}

export interface CapabilityCounts {
  versions?: number;
  routes?: number;
  tests?: number;
  skills?: number;
  [key: string]: number | undefined;
}

export interface CapabilityTestRow {
  id: string;
  capability_id: string;
  version_id: string | null;
  version: number | null;
  kind: string;
  status: TestStatus;
  http_status: number | null;
  latency_ms: number | null;
  error: unknown;
  actor: string | null;
  created_at: string;
  request?: unknown;
  response?: unknown;
}

export interface CallableChecks {
  published: boolean;
  credential_required: boolean;
  credential_active: boolean;
  provider_active: boolean;
  route_usable: boolean;
  route_detail: string;
  route_id: string | null;
  modes_checked: string[];
}

export interface VerificationSummary {
  saved: boolean;
  saved_version: number | null;
  saved_version_status: VersionStatus | null;
  last_test: CapabilityTestRow | null;
  last_test_status: TestStatus | null;
  test_passed: boolean;
  published_version: number | null;
  published_version_id: string | null;
  published_at: string | null;
  callable: boolean;
  callable_reason: string;
  checks: CallableChecks;
}

export interface Capability {
  id: string;
  slug: string;
  name: string;
  type: CapabilityType;
  description: string;
  provider_id: string | null;
  provider_name: string | null;
  credential_id: string | null;
  credential_name: string | null;
  status: CapabilityStatus;
  needs_live_agent: boolean;
  needs_live_agent_note?: string | null;
  current_version_id: string | null;
  current_version: number | null;
  current_version_status: VersionStatus | null;
  saved: boolean;
  last_test_status: TestStatus | null;
  last_test: CapabilityTestRow | null;
  published_version: number | null;
  published_version_id: string | null;
  callable: boolean;
  verification: VerificationSummary;
  counts: CapabilityCounts;
  created_at: string;
  updated_at: string;
}

export interface CapabilityDetail extends Capability {
  versions: CapabilityVersion[];
  routes: Route[];
  issues: CapabilityIssue[];
  tests?: CapabilityTestRow[];
}

export interface CapabilityVersion {
  id: string;
  capability_id: string;
  version: number;
  status: VersionStatus;
  notes: string | null;
  checksum: string;
  created_by: string | null;
  created_at: string;
  published_at: string | null;
  editable: boolean;
  last_test_status: TestStatus | null;
  last_test_at: string | null;
  config?: CapabilityConfig;
  raw_config?: CapabilityConfig;
}

export interface CapabilityInput {
  name: string;
  slug?: string;
  type: CapabilityType;
  description: string;
  provider_id?: string | null;
  credential_id?: string | null;
}

export interface CapabilityFilters {
  search?: string;
  type?: CapabilityType | '';
  status?: CapabilityStatus | '';
  provider_id?: string;
  limit?: number;
  offset?: number;
}

/* --------------------------------------------------- capability config (§3.2) -- */

export interface QueryParam {
  name: string;
  value: string;
  when_mode?: InvocationMode | '';
}

export interface RequestConfig {
  method?: string;
  url?: string;
  headers?: Record<string, string>;
  query?: QueryParam[];
  encoding?: 'json' | 'form' | 'multipart' | 'none';
  form_fields?: Record<string, string>;
  multipart_file_field?: string;
  body_template?: unknown;
}

export interface ExtractPaths {
  result?: string;
  usage_in?: string;
  usage_out?: string;
  usage_total?: string;
  cost?: string;
}

export interface ArtifactConfig {
  kind?: 'image' | 'audio' | 'video' | 'file';
  path?: string;
  encoding?: 'base64' | 'url';
  mime?: string;
  mime_path?: string;
  filename?: string;
}

export interface ResponseConfig {
  kind?: 'json' | 'text';
  extract?: ExtractPaths;
  artifact?: ArtifactConfig;
}

export interface AsyncStatusConfig {
  url?: string;
  method?: string;
  extract?: { status?: string; progress?: string };
  map?: Record<string, string[]>;
  poll_interval_s?: number | null;
  result_extract?: Record<string, string>;
}

export interface AsyncConfig {
  enabled?: boolean;
  submit_extract?: Record<string, string>;
  status?: AsyncStatusConfig;
  cancel?: { url?: string; method?: string; extract?: Record<string, string> };
}

export interface StreamConfig {
  enabled?: boolean;
  protocol?: 'sse' | 'ndjson';
  delta_path?: string;
  done_marker?: string;
}

export interface ErrorRuleMatch {
  http_status?: number[];
  body_path?: string;
  equals?: string;
  contains?: string;
}

export interface ErrorRule {
  match?: ErrorRuleMatch;
  code?: string;
  message?: string;
  action?: string;
  retryable?: boolean;
}

export interface ErrorsConfig {
  rules?: ErrorRule[];
  default_code?: string;
}

export interface LimitsConfig {
  timeout_s?: number | null;
  max_output_bytes?: number | null;
  max_input_bytes?: number | null;
  file_input?: boolean;
  max_file_mb?: number | null;
  allowed_mime?: string[];
  max_duration_s?: number | null;
}

export interface ModesConfig {
  sync?: boolean;
  async?: boolean;
  stream?: boolean;
}

export interface RetryConfig {
  max_attempts?: number;
  backoff_ms?: number[];
  retry_on?: string[];
}

export interface PricingConfig {
  per_call?: number;
  input_per_1k?: number;
  output_per_1k?: number;
  per_second?: number;
  currency?: string;
}

export interface CapabilityExample {
  input: Record<string, unknown>;
  note?: string;
}

export interface JsonSchema {
  type?: string;
  properties?: Record<string, unknown>;
  required?: string[];
  additionalProperties?: boolean;
  [key: string]: unknown;
}

export interface CapabilityConfig {
  model?: string;
  request?: RequestConfig;
  auth?: { mode?: 'credential' | 'none' };
  input_schema?: JsonSchema;
  output_schema?: JsonSchema;
  response?: ResponseConfig;
  async?: AsyncConfig;
  stream?: StreamConfig;
  errors?: ErrorsConfig;
  limits?: LimitsConfig;
  modes?: ModesConfig;
  provider_params?: string[];
  retry?: RetryConfig;
  pricing?: PricingConfig;
  dedupe_window_s?: number | null;
  examples?: CapabilityExample[];
  [key: string]: unknown;
}

/* -------------------------------------------------------------------- tests --- */

export interface TestExecutionResult {
  request?: unknown;
  status?: string;
  response?: unknown;
  result?: unknown;
  error?: { code?: string; message?: string } | null;
  http_status?: number | null;
  latency_ms?: number | null;
  [key: string]: unknown;
}

export interface TestVersionResponse {
  capability_id: string;
  capability_slug: string;
  version_id: string;
  version: number;
  status: TestStatus;
  result: TestExecutionResult;
  evidence: CapabilityTestRow;
  saved: boolean;
  test_passed: boolean;
  published_version: number | null;
  callable: boolean;
  real_call_attempted: boolean;
  actions?: string[];
}

export interface ValidateResponse {
  capability_id: string;
  version_id: string;
  version: number;
  status: VersionStatus;
  checksum: string;
  issues: CapabilityIssue[];
  blocking: boolean;
  errors: number;
  warnings: number;
  actions: string[];
}

export interface DiffChange {
  path: string;
  kind: 'added' | 'removed' | 'changed';
  before: unknown;
  after: unknown;
  type_before?: string;
  type_after?: string;
}

export interface DiffResponse {
  capability_id: string;
  from: { version_id: string; version: number; status: VersionStatus };
  to: { version_id: string; version: number; status: VersionStatus };
  identical: boolean;
  summary: { added: number; removed: number; changed: number };
  changes: DiffChange[];
}

/* ------------------------------------------------------------------- routes --- */

export interface Route {
  id: string;
  capability_id: string;
  capability_version_id: string;
  version?: number | null;
  version_status?: VersionStatus | null;
  provider_id: string | null;
  provider_name: string | null;
  provider_status?: ProviderStatus | null;
  credential_id: string | null;
  credential_name: string | null;
  credential_status?: CredentialStatus | null;
  strategy: RouteStrategy;
  priority: number;
  weight: number;
  enabled: boolean;
  health_status: RouteHealth;
  last_health_at: string | null;
  conditions?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface RouteInput {
  capability_version_id: string;
  provider_id?: string | null;
  credential_id?: string | null;
  strategy: RouteStrategy;
  priority: number;
  weight: number;
  enabled: boolean;
}

export interface HealthCheckResult {
  status: RouteHealth;
  latency_ms: number | null;
  message: string;
  [key: string]: unknown;
}

export interface RoutePreview {
  reason: string;
  chosen_route: unknown;
  candidates: unknown[];
  rejected: unknown[];
  capability_id?: string;
  capability_slug?: string;
  version?: number;
  version_status?: VersionStatus;
  mode?: InvocationMode;
  network_call?: boolean;
}

/* ------------------------------------------------------------------ invokes --- */

export interface InvokeResult {
  request_id: string;
  invocation_id: string;
  status: InvocationStatus | 'preview';
  mode: InvocationMode;
  capability?: string;
  version?: number;
  task_id: string | null;
  result?: unknown;
  artifacts?: Array<{ id: string; url: string; content_type?: string; size?: number }>;
  usage?: Record<string, unknown>;
  cost?: Record<string, unknown>;
  provider?: { id: string | null; name: string | null };
  route_reason?: string;
  error?: InvocationError | null;
  http_status?: number | null;
  latency_ms?: number | null;
  attempts?: number;
  deduplicated?: boolean;
  schema_valid?: boolean;
  dry_run?: boolean;
  request?: unknown;
  message?: string;
  callable?: boolean;
  version_status?: VersionStatus;
}
