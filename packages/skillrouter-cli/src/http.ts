import { createHash } from 'node:crypto';
import { execFile } from 'node:child_process';
import { promises as fs } from 'node:fs';
import path from 'node:path';

import { candidateSkillDirs, type AgentTarget, type Env } from './detect.js';

/**
 * 进程退出码（§9.8）。所有模块共用这一张表，禁止各处硬编码数字。
 */
export const EXIT = {
  OK: 0,
  FAILURE: 1,
  USAGE: 2,
  CONNECTIVITY: 3,
  PERMISSION: 4,
  NO_AGENT: 5,
} as const;

export type ExitCode = (typeof EXIT)[keyof typeof EXIT];

export interface Logger {
  info(message: string): void;
  success(message: string): void;
  fail(message: string): void;
  warn(message: string): void;
}

export interface ApiErrorPayload {
  code: string;
  message: string;
  hint?: string;
  retryable?: boolean;
  details?: Record<string, unknown>;
  request_id?: string;
  upstream_status?: number;
  provider?: string;
}

export interface ApiFailureInit {
  code: string;
  message: string;
  status?: number;
  requestId?: string;
  hint?: string;
  retryable?: boolean;
  details?: Record<string, unknown>;
  connectivity?: boolean;
  exitCode?: ExitCode;
}

/** 统一的失败对象：HTTP 失败、连通性失败、配置/凭据失败都用它。 */
export class ApiFailure extends Error {
  readonly code: string;
  readonly status: number;
  readonly requestId: string | undefined;
  readonly hint: string | undefined;
  readonly retryable: boolean;
  readonly details: Record<string, unknown> | undefined;
  /** true 表示没拿到 HTTP 响应（DNS/连接/超时），对应退出码 3 */
  readonly connectivity: boolean;
  readonly exitCode: ExitCode;

  constructor(init: ApiFailureInit) {
    super(init.message);
    this.name = 'ApiFailure';
    this.code = init.code;
    this.status = init.status ?? 0;
    this.requestId = init.requestId;
    this.hint = init.hint;
    this.retryable = init.retryable ?? false;
    this.details = init.details;
    this.connectivity = init.connectivity ?? false;
    this.exitCode =
      init.exitCode ??
      (init.connectivity === true
        ? EXIT.CONNECTIVITY
        : init.status === 401 || init.status === 403
          ? EXIT.FAILURE
          : EXIT.FAILURE);
  }

  /** 供人类输出用的一行摘要（绝不含令牌）。 */
  describe(): string {
    const parts = [`${this.code}`, this.message];
    if (this.status > 0) {
      parts.push(`HTTP ${this.status}`);
    }
    if (this.requestId) {
      parts.push(`request_id=${this.requestId}`);
    }
    if (this.hint) {
      parts.push(`hint: ${this.hint}`);
    }
    return parts.join(' · ');
  }

  toPayload(): ApiErrorPayload {
    const payload: ApiErrorPayload = { code: this.code, message: this.message };
    if (this.hint !== undefined) payload.hint = this.hint;
    if (this.retryable) payload.retryable = this.retryable;
    if (this.details !== undefined) payload.details = this.details;
    if (this.requestId !== undefined) payload.request_id = this.requestId;
    return payload;
  }
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function asString(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined;
}

export function asNumber(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

export function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

export function asRecord(value: unknown): Record<string, unknown> | undefined {
  return isRecord(value) ? value : undefined;
}

export function pickString(source: Record<string, unknown>, key: string): string | undefined {
  return asString(source[key]);
}

/** 从 `{items:[...]}` 或裸数组里取出条目（两种形状后端都可能返回）。 */
export function pickItems(data: unknown): unknown[] {
  if (Array.isArray(data)) return data;
  const record = asRecord(data);
  if (record) {
    const items = record.items;
    if (Array.isArray(items)) return items;
    const capabilities = record.capabilities;
    if (Array.isArray(capabilities)) return capabilities;
  }
  return [];
}

export const CONFIG_FILENAME = 'skillrouter.config.json';
export const CREDENTIALS_FILENAME = 'credentials.json';
export const DEFAULT_TIMEOUT_MS = 15_000;

/** 去掉尾部斜杠并校验 http(s) 协议。 */
export function normalizeBaseUrl(raw: string): string {
  const trimmed = raw.trim();
  if (trimmed.length === 0) {
    throw new ApiFailure({
      code: 'USAGE_BASE_URL_MISSING',
      message: '缺少 --base-url，例如 --base-url https://router.example.com',
      exitCode: EXIT.USAGE,
    });
  }
  let url: URL;
  try {
    url = new URL(trimmed);
  } catch {
    throw new ApiFailure({
      code: 'USAGE_BASE_URL_INVALID',
      message: `--base-url 不是合法 URL: ${trimmed}`,
      hint: '示例: --base-url https://router.example.com',
      exitCode: EXIT.USAGE,
    });
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new ApiFailure({
      code: 'USAGE_BASE_URL_INVALID',
      message: `--base-url 只支持 http/https，实际是 ${url.protocol}`,
      exitCode: EXIT.USAGE,
    });
  }
  const pathname = url.pathname.replace(/\/+$/, '');
  return `${url.protocol}//${url.host}${pathname}${url.search}`;
}

export function joinUrl(baseUrl: string, suffix: string): string {
  const base = baseUrl.replace(/\/+$/, '');
  const tail = suffix.startsWith('/') ? suffix : `/${suffix}`;
  return `${base}${tail}`;
}

/** 令牌格式 `srk_<keyid>_<secret>`（与 apps/api 的 security/tokens.py 一致）。 */
export const TOKEN_PATTERN = /^srk_[0-9A-Za-z]+_\S+$/;

export function isValidTokenFormat(token: string): boolean {
  return TOKEN_PATTERN.test(token.trim());
}

/** 只显示可公开的前缀（与平台 `prefix = token[:12]` 对齐），绝不输出完整令牌。 */
export function maskToken(token: string): string {
  const trimmed = token.trim();
  if (trimmed.length <= 12) {
    return '****';
  }
  return `${trimmed.slice(0, 12)}…${trimmed.slice(-2)}`;
}

export interface ApiRequestOptions {
  baseUrl: string;
  path: string;
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  token?: string;
  body?: unknown;
  timeoutMs?: number;
  headers?: Record<string, string>;
  idempotencyKey?: string;
}

export interface ApiResponse {
  status: number;
  requestId: string | undefined;
  data: unknown;
  rawText: string;
}

function parseBody(text: string): unknown {
  if (text.length === 0) return undefined;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

function extractError(data: unknown, status: number, requestId: string | undefined): ApiFailure {
  const envelope = asRecord(data);
  const errorBody = envelope ? asRecord(envelope.error) : undefined;
  if (errorBody) {
    return new ApiFailure({
      code: asString(errorBody.code) ?? `HTTP_${status}`,
      message: asString(errorBody.message) ?? `请求失败（HTTP ${status}）`,
      status,
      requestId: asString(errorBody.request_id) ?? requestId,
      hint: asString(errorBody.hint),
      retryable: errorBody.retryable === true,
      details: asRecord(errorBody.details),
    });
  }
  return new ApiFailure({
    code: `HTTP_${status}`,
    message:
      typeof data === 'string' && data.length > 0 && data.length < 400
        ? data
        : `请求失败（HTTP ${status}）`,
    status,
    requestId,
  });
}

/**
 * 极简 fetch 包装：超时、`Bearer` 注入、`{error:{code,message,hint,request_id}}` 解析。
 * 无响应（DNS/连接/超时）统一抛 connectivity=true 的 ApiFailure → 退出码 3。
 */
export async function apiRequest(options: ApiRequestOptions): Promise<ApiResponse> {
  const url = joinUrl(options.baseUrl, options.path);
  const headers: Record<string, string> = {
    accept: 'application/json',
    'user-agent': 'skillrouter-cli',
    ...options.headers,
  };
  if (options.token) {
    headers.authorization = `Bearer ${options.token}`;
  }
  if (options.body !== undefined) {
    headers['content-type'] = 'application/json';
  }
  if (options.idempotencyKey) {
    headers['idempotency-key'] = options.idempotencyKey;
  }

  const init: RequestInit = {
    method: options.method ?? 'GET',
    headers,
    signal: AbortSignal.timeout(options.timeoutMs ?? DEFAULT_TIMEOUT_MS),
  };
  if (options.body !== undefined) {
    init.body = JSON.stringify(options.body);
  }

  let response: Response;
  try {
    response = await fetch(url, init);
  } catch (unknownError: unknown) {
    const name = unknownError instanceof Error ? unknownError.name : '';
    const isTimeout = name === 'TimeoutError' || name === 'AbortError';
    throw new ApiFailure({
      code: isTimeout ? 'NETWORK_TIMEOUT' : 'NETWORK_ERROR',
      message: isTimeout
        ? `请求超时（${options.timeoutMs ?? DEFAULT_TIMEOUT_MS} ms）：${url}`
        : `无法连接 ${url}：${unknownError instanceof Error ? unknownError.message : String(unknownError)}`,
      hint: '检查 --base-url 是否可达、是否需要代理或 VPN',
      retryable: true,
      connectivity: true,
    });
  }

  const requestId = response.headers.get('x-request-id') ?? undefined;
  let rawText = '';
  try {
    rawText = await response.text();
  } catch {
    rawText = '';
  }
  const data = parseBody(rawText);

  if (!response.ok) {
    throw extractError(data, response.status, requestId);
  }
  return { status: response.status, requestId, data, rawText };
}

export type Transport = 'http' | 'mcp';

export interface CapabilitySnapshot {
  fetched_at: string;
  count: number;
  keys: string[];
}

/** `skillrouter.config.json`（**不含令牌**，只存 `token_ref`）。 */
export interface SkillRouterConfig {
  base_url: string;
  transport: Transport;
  token_ref: string;
  installed_at: string;
  updated_at?: string;
  cli_version: string;
  server_version: string | null;
  skill: string;
  agent: AgentTarget;
  capabilities_snapshot?: CapabilitySnapshot;
}

export function configPath(dir: string): string {
  return path.join(dir, CONFIG_FILENAME);
}

export async function readConfig(dir: string): Promise<SkillRouterConfig | undefined> {
  let raw: string;
  try {
    raw = await fs.readFile(configPath(dir), 'utf8');
  } catch {
    return undefined;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw) as unknown;
  } catch {
    throw new ApiFailure({
      code: 'CONFIG_CORRUPT',
      message: `${configPath(dir)} 不是合法 JSON`,
      hint: '重新运行 skillrouter install --force 覆盖安装',
      exitCode: EXIT.FAILURE,
    });
  }
  const record = asRecord(parsed);
  if (!record) {
    throw new ApiFailure({
      code: 'CONFIG_CORRUPT',
      message: `${configPath(dir)} 顶层不是对象`,
      exitCode: EXIT.FAILURE,
    });
  }
  const baseUrl = asString(record.base_url);
  const tokenRef = asString(record.token_ref);
  if (!baseUrl || !tokenRef) {
    throw new ApiFailure({
      code: 'CONFIG_INCOMPLETE',
      message: `${configPath(dir)} 缺少 base_url 或 token_ref`,
      hint: '重新运行 skillrouter install --force 覆盖安装',
      exitCode: EXIT.FAILURE,
    });
  }
  const transport = asString(record.transport) === 'mcp' ? 'mcp' : 'http';
  const agentRaw = asString(record.agent);
  const agent: AgentTarget =
    agentRaw === 'aragon' || agentRaw === 'claude' || agentRaw === 'generic' ? agentRaw : 'generic';
  const config: SkillRouterConfig = {
    base_url: baseUrl,
    transport,
    token_ref: tokenRef,
    installed_at: asString(record.installed_at) ?? new Date(0).toISOString(),
    cli_version: asString(record.cli_version) ?? '0.0.0',
    server_version: asString(record.server_version) ?? null,
    skill: asString(record.skill) ?? 'skillrouter',
    agent,
  };
  const updatedAt = asString(record.updated_at);
  if (updatedAt !== undefined) config.updated_at = updatedAt;
  const snapshot = asRecord(record.capabilities_snapshot);
  if (snapshot) {
    config.capabilities_snapshot = {
      fetched_at: asString(snapshot.fetched_at) ?? new Date(0).toISOString(),
      count: asNumber(snapshot.count) ?? 0,
      keys: asArray(snapshot.keys).filter((item): item is string => typeof item === 'string'),
    };
  }
  return config;
}

export async function writeConfig(dir: string, config: SkillRouterConfig): Promise<string> {
  const target = configPath(dir);
  const ordered = {
    base_url: config.base_url,
    transport: config.transport,
    token_ref: config.token_ref,
    installed_at: config.installed_at,
    ...(config.updated_at === undefined ? {} : { updated_at: config.updated_at }),
    cli_version: config.cli_version,
    server_version: config.server_version,
    skill: config.skill,
    agent: config.agent,
    ...(config.capabilities_snapshot === undefined
      ? {}
      : { capabilities_snapshot: config.capabilities_snapshot }),
  };
  await fs.writeFile(target, `${JSON.stringify(ordered, null, 2)}\n`, 'utf8');
  return target;
}

export interface InstallationLocation {
  dir: string;
  config: SkillRouterConfig;
}

/**
 * 找到已安装的目录：`--dir` 优先，否则按探测顺序逐个检查 `skillrouter.config.json`。
 * 找不到时返回 undefined（调用方用退出码 5 提示用户传 `--dir`）。
 */
export async function locateInstallation(
  env: Env,
  explicitDir?: string,
): Promise<InstallationLocation | undefined> {
  if (explicitDir && explicitDir.trim().length > 0) {
    const dir = path.resolve(env.cwd, explicitDir.trim());
    const config = await readConfig(dir);
    return config ? { dir, config } : undefined;
  }
  for (const candidate of candidateSkillDirs(env)) {
    const config = await readConfig(candidate);
    if (config) {
      return { dir: candidate, config };
    }
  }
  return undefined;
}

export interface CredentialEntry {
  token: string;
  base_url: string;
  token_ref: string;
  label?: string;
  created_at: string;
  cli_version: string;
}

export interface CredentialsFile {
  version: number;
  tokens: Record<string, CredentialEntry>;
}

/** `~/.skillrouter`（可用 `SKILLROUTER_HOME` 覆盖，便于测试与多环境隔离）。 */
export function credentialsDir(env: Env): string {
  const override = env.env.SKILLROUTER_HOME;
  if (override && override.trim().length > 0) {
    return override.trim();
  }
  return path.join(env.home, '.skillrouter');
}

export function credentialsPath(env: Env): string {
  return path.join(credentialsDir(env), CREDENTIALS_FILENAME);
}

function parseCredentialEntry(value: unknown): CredentialEntry | undefined {
  const record = asRecord(value);
  if (!record) return undefined;
  const token = asString(record.token);
  const baseUrl = asString(record.base_url);
  const tokenRef = asString(record.token_ref);
  if (!token || !baseUrl || !tokenRef) return undefined;
  const entry: CredentialEntry = {
    token,
    base_url: baseUrl,
    token_ref: tokenRef,
    created_at: asString(record.created_at) ?? new Date(0).toISOString(),
    cli_version: asString(record.cli_version) ?? '0.0.0',
  };
  const label = asString(record.label);
  if (label !== undefined) entry.label = label;
  return entry;
}

export async function readCredentials(env: Env): Promise<CredentialsFile> {
  const file = credentialsPath(env);
  let raw: string;
  try {
    raw = await fs.readFile(file, 'utf8');
  } catch {
    return { version: 1, tokens: {} };
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw) as unknown;
  } catch {
    throw new ApiFailure({
      code: 'CREDENTIALS_CORRUPT',
      message: `${file} 不是合法 JSON`,
      hint: '删除该文件后重新运行 skillrouter install --token-stdin',
      exitCode: EXIT.FAILURE,
    });
  }
  const record = asRecord(parsed);
  const tokensRaw = record ? asRecord(record.tokens) : undefined;
  const tokens: Record<string, CredentialEntry> = {};
  if (tokensRaw) {
    for (const [key, value] of Object.entries(tokensRaw)) {
      const entry = parseCredentialEntry(value);
      if (entry) tokens[key] = entry;
    }
  }
  return { version: asNumber(record?.version ?? undefined) ?? 1, tokens };
}

async function restrictPermissions(file: string, env: Env): Promise<void> {
  // POSIX：0600 就够了。
  if (env.platform !== 'win32') {
    try {
      await fs.chmod(file, 0o600);
    } catch {
      // 文件系统不支持 chmod（FAT/exFAT）：无法加固，但不阻塞安装
    }
    return;
  }

  // Windows：chmod 只切换只读位，不会收紧 ACL，因此**总是**再尝试 icacls。
  try {
    await fs.chmod(file, 0o600);
  } catch {
    // 忽略：Windows 上语义有限
  }
  const owner = env.env.USERNAME ?? env.env.USER;
  if (!owner) return;
  await new Promise<void>((resolve) => {
    execFile(
      'icacls',
      [file, '/inheritance:r', '/grant:r', `${owner}:F`],
      { windowsHide: true },
      () => {
        // 尽力而为：失败只影响加固强度，不影响可用性（§9.8 明确允许忽略）
        resolve();
      },
    );
  });
}

/** 写 `~/.skillrouter/credentials.json`（0600；Windows 失败时退回 icacls，忽略失败）。 */
export async function writeCredentials(env: Env, file: CredentialsFile): Promise<string> {
  const dir = credentialsDir(env);
  const target = credentialsPath(env);
  try {
    await fs.mkdir(dir, { recursive: true, mode: 0o700 });
    const body = `${JSON.stringify(file, null, 2)}\n`;
    await fs.writeFile(target, body, { encoding: 'utf8', mode: 0o600 });
  } catch (error: unknown) {
    const code = error instanceof Error && 'code' in error ? String(error.code) : '';
    throw new ApiFailure({
      code: code === 'EACCES' || code === 'EPERM' ? 'CREDENTIALS_NOT_WRITABLE' : 'CREDENTIALS_WRITE_FAILED',
      message: `无法写入凭据文件 ${target}: ${error instanceof Error ? error.message : String(error)}`,
      hint: '检查该路径的写权限，或设置 SKILLROUTER_HOME 指向可写目录',
      exitCode: code === 'EACCES' || code === 'EPERM' ? EXIT.PERMISSION : EXIT.FAILURE,
    });
  }
  await restrictPermissions(target, env);
  return target;
}

export async function saveToken(env: Env, entry: CredentialEntry): Promise<string> {
  const file = await readCredentials(env);
  file.tokens[entry.token_ref] = entry;
  return writeCredentials(env, file);
}

export async function resolveStoredToken(env: Env, tokenRef: string): Promise<string | undefined> {
  const file = await readCredentials(env);
  return file.tokens[tokenRef]?.token;
}

export async function deleteStoredToken(env: Env, tokenRef: string): Promise<boolean> {
  const file = await readCredentials(env);
  if (!(tokenRef in file.tokens)) {
    return false;
  }
  delete file.tokens[tokenRef];
  await writeCredentials(env, file);
  return true;
}

/** 稳定、可读的 `token_ref`（同一 base_url 反复安装会复用同一条凭据）。 */
export function tokenRefFor(baseUrl: string): string {
  const slug = baseUrl
    .replace(/^https?:\/\//, '')
    .replace(/[^0-9A-Za-z.]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .toLowerCase()
    .slice(0, 48);
  const digest = createHash('sha256').update(baseUrl).digest('hex').slice(0, 8);
  return `local_${slug}_${digest}`;
}
