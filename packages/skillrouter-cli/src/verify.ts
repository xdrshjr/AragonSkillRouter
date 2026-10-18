import {
  ApiFailure,
  DEFAULT_TIMEOUT_MS,
  EXIT,
  apiRequest,
  asRecord,
  asString,
  pickItems,
  type ExitCode,
} from './http.js';

/** 失败归因：决定最终退出码，并在人类输出里给出可操作的提示。 */
export type VerifyFailureKind = 'none' | 'connectivity' | 'auth' | 'http' | 'probe-input';

export interface VerifyStep {
  /** 端点或检查名，例如 `GET /api/v1/health` */
  name: string;
  ok: boolean;
  detail: string;
  requestId: string | undefined;
  failure: VerifyFailureKind;
  hint: string | undefined;
}

export interface VerifyReport {
  ok: boolean;
  steps: VerifyStep[];
  exitCode: ExitCode;
}

export interface VerifyOptions {
  baseUrl: string;
  token: string;
  /** `--probe-capability <key>`：额外做一次真实最小调用 */
  probeCapability?: string;
  timeoutMs?: number;
}

function classify(error: ApiFailure): VerifyFailureKind {
  if (error.connectivity) return 'connectivity';
  if (error.status === 401 || error.status === 403) return 'auth';
  return 'http';
}

function failureStep(name: string, error: ApiFailure): VerifyStep {
  return {
    name,
    ok: false,
    detail: error.describe(),
    requestId: error.requestId,
    failure: classify(error),
    hint: error.hint,
  };
}

function okStep(name: string, detail: string, requestId: string | undefined): VerifyStep {
  return { name, ok: true, detail, requestId, failure: 'none', hint: undefined };
}

async function checkHealth(options: VerifyOptions): Promise<VerifyStep> {
  const name = 'GET /api/v1/health';
  try {
    const response = await apiRequest({
      baseUrl: options.baseUrl,
      path: '/api/v1/health',
      timeoutMs: options.timeoutMs ?? 10_000,
    });
    const record = asRecord(response.data) ?? {};
    const engine = asRecord(record.engine);
    const parts = [
      `status=${asString(record.status) ?? 'unknown'}`,
      `version=${asString(record.version) ?? 'unknown'}`,
    ];
    const engineVersion = engine ? asString(engine.version) : undefined;
    if (engineVersion) parts.push(`engine=${engineVersion}`);
    return okStep(name, parts.join(' '), response.requestId);
  } catch (error: unknown) {
    if (error instanceof ApiFailure) return failureStep(name, error);
    throw error;
  }
}

async function checkWhoami(options: VerifyOptions): Promise<VerifyStep> {
  const name = 'GET /api/v1/whoami';
  try {
    const response = await apiRequest({
      baseUrl: options.baseUrl,
      path: '/api/v1/whoami',
      token: options.token,
      timeoutMs: options.timeoutMs ?? DEFAULT_TIMEOUT_MS,
    });
    const record = asRecord(response.data) ?? {};
    const agent = asRecord(record.agent);
    const scopes = Array.isArray(record.scopes) ? record.scopes.length : 0;
    const modes = Array.isArray(record.allowed_modes) ? record.allowed_modes.join('/') : 'n/a';
    const parts = [
      `token_id=${asString(record.token_id) ?? 'unknown'}`,
      `agent=${agent ? (asString(agent.name) ?? 'unknown') : 'unknown'}`,
      `prefix=${asString(record.prefix) ?? 'n/a'}`,
      `scopes=${scopes}`,
      `modes=${modes}`,
    ];
    return okStep(name, parts.join(' '), response.requestId);
  } catch (error: unknown) {
    if (error instanceof ApiFailure) return failureStep(name, error);
    throw error;
  }
}

async function checkCapabilities(options: VerifyOptions): Promise<VerifyStep> {
  const name = 'GET /api/v1/capabilities';
  try {
    const response = await apiRequest({
      baseUrl: options.baseUrl,
      path: '/api/v1/capabilities',
      token: options.token,
      timeoutMs: options.timeoutMs ?? DEFAULT_TIMEOUT_MS,
    });
    const items = pickItems(response.data);
    return okStep(name, `${items.length} 个可用能力`, response.requestId);
  } catch (error: unknown) {
    if (error instanceof ApiFailure) return failureStep(name, error);
    throw error;
  }
}

const PROBE_INPUT_ERRORS = new Set(['INPUT_SCHEMA_INVALID', 'UNSUPPORTED_PARAM', 'UNSUPPORTED_MODE']);

export interface MinimalInput {
  input: Record<string, unknown>;
  /** 已按 schema 填好的必填字段 */
  filled: string[];
  /** 无法合成的必填字段（对象/数组/文件引用），交给平台判定 */
  skipped: string[];
}

function placeholderFor(property: Record<string, unknown>): unknown {
  const enumValues = Array.isArray(property.enum) ? property.enum : [];
  if (enumValues.length > 0) return enumValues[0];
  if (property.const !== undefined) return property.const;
  const type = asString(property.type);
  switch (type) {
    case 'string':
      return property.default ?? 'probe';
    case 'integer':
    case 'number':
      return typeof property.minimum === 'number' ? property.minimum : property.default ?? 1;
    case 'boolean':
      return property.default ?? false;
    default: {
      const variants = Array.isArray(property.oneOf) ? property.oneOf : Array.isArray(property.anyOf) ? property.anyOf : [];
      for (const variant of variants) {
        const record = asRecord(variant);
        if (!record) continue;
        if (record.type === 'string' || record.type === 'number' || record.type === 'integer') {
          return placeholderFor(record);
        }
      }
      return undefined;
    }
  }
}

/**
 * 从能力的 `input_schema` 合成一次「最小真实调用」的入参：
 * 只填 JSON Schema `required` 里的标量字段（最多 8 个），对象/数组/`$file` 引用不猜测。
 */
export function buildMinimalInput(schema: unknown): MinimalInput {
  const record = asRecord(schema);
  if (!record) return { input: {}, filled: [], skipped: [] };
  const requiredRaw = Array.isArray(record.required) ? record.required : [];
  const properties = asRecord(record.properties) ?? {};
  const input: Record<string, unknown> = {};
  const filled: string[] = [];
  const skipped: string[] = [];
  for (const name of requiredRaw.slice(0, 8)) {
    if (typeof name !== 'string') continue;
    const property = asRecord(properties[name]);
    if (!property) {
      skipped.push(name);
      continue;
    }
    const type = asString(property.type);
    if (type === 'object' || type === 'array') {
      skipped.push(name);
      continue;
    }
    const value = placeholderFor(property);
    if (value === undefined) {
      skipped.push(name);
      continue;
    }
    input[name] = value;
    filled.push(name);
  }
  return { input, filled, skipped };
}

interface CapabilityDetail {
  inputSchema: unknown;
  modes: string[];
}

async function fetchCapabilityDetail(options: VerifyOptions, capability: string): Promise<CapabilityDetail> {
  try {
    const response = await apiRequest({
      baseUrl: options.baseUrl,
      path: `/api/v1/capabilities/${encodeURIComponent(capability)}`,
      token: options.token,
      timeoutMs: options.timeoutMs ?? DEFAULT_TIMEOUT_MS,
    });
    const record = asRecord(response.data) ?? {};
    const modes = Array.isArray(record.modes)
      ? record.modes.filter((mode): mode is string => typeof mode === 'string')
      : [];
    return { inputSchema: record.input_schema, modes };
  } catch {
    // 详情拿不到就退回空入参 + 平台默认模式，让真实调用自己说话
    return { inputSchema: undefined, modes: [] };
  }
}

async function probeCapability(options: VerifyOptions, capability: string): Promise<VerifyStep> {
  const name = `POST /api/v1/capabilities/${capability}/invoke`;
  const path = `/api/v1/capabilities/${encodeURIComponent(capability)}/invoke`;
  const detail = await fetchCapabilityDetail(options, capability);
  const minimal = buildMinimalInput(detail.inputSchema);
  const preferredMode = detail.modes.includes('sync') ? 'sync' : detail.modes[0];

  const attempt = async (body: unknown): Promise<VerifyStep> => {
    const response = await apiRequest({
      baseUrl: options.baseUrl,
      path,
      method: 'POST',
      token: options.token,
      body,
      timeoutMs: options.timeoutMs ?? 60_000,
    });
    const record = asRecord(response.data) ?? {};
    const status = asString(record.status) ?? `http_${response.status}`;
    const taskId = asString(record.task_id);
    const provider = asRecord(record.provider);
    const parts = [`status=${status}`];
    if (taskId) parts.push(`task_id=${taskId}`);
    if (provider) parts.push(`provider=${asString(provider.name) ?? asString(provider.id) ?? 'unknown'}`);
    if (minimal.filled.length > 0) parts.push(`input=${minimal.filled.join(',')}`);
    return okStep(name, parts.join(' '), response.requestId);
  };

  const withMode = (mode: string | undefined, input: Record<string, unknown>): unknown =>
    mode === undefined
      ? { input }
      : { input, options: { mode, timeout_ms: 20_000 } };

  try {
    try {
      return await attempt(withMode(preferredMode ?? 'sync', minimal.input));
    } catch (error: unknown) {
      if (error instanceof ApiFailure && error.code === 'UNSUPPORTED_MODE') {
        // 该能力不支持首选模式：让平台按 modes[0] 自行决定
        return await attempt({ input: minimal.input });
      }
      throw error;
    }
  } catch (error: unknown) {
    if (error instanceof ApiFailure) {
      if (PROBE_INPUT_ERRORS.has(error.code)) {
        const skippedNote =
          minimal.skipped.length > 0
            ? `该能力的必填字段 ${minimal.skipped.join(', ')} 是对象/数组/文件引用，CLI 不替你猜；`
            : '';
        return {
          name,
          ok: false,
          detail: `${error.code} · ${error.message}`,
          requestId: error.requestId,
          failure: 'probe-input',
          hint:
            '平台已连通且令牌被接受，但这次最小调用被输入校验拒绝。' +
            skippedNote +
            "用 node ./invoke.mjs capabilities.get <key> 看 input_schema，再用 node ./invoke.mjs invoke --input '<json>' 实测",
        };
      }
      return failureStep(name, error);
    }
    throw error;
  }
}

/** `/health` → `/whoami` → `/capabilities` → 可选真实最小调用（§9.8）。 */
export async function runVerify(options: VerifyOptions): Promise<VerifyReport> {
  const steps: VerifyStep[] = [];
  steps.push(await checkHealth(options));
  steps.push(await checkWhoami(options));
  steps.push(await checkCapabilities(options));
  if (options.probeCapability !== undefined && options.probeCapability.length > 0) {
    steps.push(await probeCapability(options, options.probeCapability));
  }

  const failed = steps.filter((step) => !step.ok);
  const ok = failed.length === 0;
  let exitCode: ExitCode = EXIT.OK;
  if (!ok) {
    exitCode = failed.some((step) => step.failure === 'connectivity') ? EXIT.CONNECTIVITY : EXIT.FAILURE;
  }
  return { ok, steps, exitCode };
}

/** 统一的 ✓/✗ 行格式。 */
export function describeStep(step: VerifyStep): string {
  const symbol = step.ok ? '✓' : '✗';
  const base = `${symbol} ${step.name} — ${step.detail}`;
  return step.hint ? `${base}\nhint: ${step.hint}` : base;
}

export function formatVerifyReport(report: VerifyReport): string[] {
  return report.steps.map(describeStep);
}
