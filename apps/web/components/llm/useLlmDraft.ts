'use client';

/**
 * LLM 配置草稿状态机（spec §3.8 ②③、§4.3）。
 *
 * 两个消费者，一份真相源：
 * 1. `/settings/agent` 没有 react-hook-form，用 `useLlmDraft()` 持有草稿；
 * 2. `/providers/{id}` 的凭证抽屉已经有 RHF + zodResolver（`credentialSchema`），
 *    那里**不得**再开一份 useState —— 只用本文件导出的**纯函数** `buildProbeTarget(values)`
 *    把 RHF 的当前值翻译成探测请求体。
 *
 * INV-1：协议差异（默认地址、可选认证方式、路径是否继承 wire）一律来自
 * `GET /admin/llm/protocols` 的 `LlmProtocolSpec`，本文件不做任何 `if (protocol === 'xxx')`。
 * INV-3：`secret` 只活在这个 hook 的 state 里，不发日志、不进 queryKey、不落库。
 */

import * as React from 'react';
import type { LlmPaths, LlmProbeMode, LlmProbeTarget, LlmProtocolSpec } from '@/lib/types';

/* ── 类型 ─────────────────────────────────────────────── */

/** 草稿值（React 侧用 camelCase；提交时才翻译成 API 的 snake_case） */
export interface LlmDraftValues {
  /** 协议 key（来自 GET /llm/protocols 的 items[].key） */
  protocol: string;
  /** 线协议：只有 `spec.wire === null` 的协议（custom）才由用户选 */
  wire: string;
  baseUrl: string;
  authStyle: string;
  headerName: string;
  /** 已保存凭证 id；与 `secret` 二选一 */
  credentialId: string;
  /** 草稿密钥：只在内存里 */
  secret: string;
  model: string;
  /** 路径覆盖（仅后端声明「路径可覆盖」的协议生效） */
  paths: LlmPaths | null;
}

/** 空草稿：页面在 GET 回来之前用它占位 */
export const EMPTY_LLM_DRAFT_VALUES: LlmDraftValues = {
  protocol: '',
  wire: '',
  baseUrl: '',
  authStyle: '',
  headerName: '',
  credentialId: '',
  secret: '',
  model: '',
  paths: null,
};

/**
 * `buildProbeTarget` 接受的输入：两种命名都认。
 *
 * 抽屉里是 RHF 的 snake_case（`base_url` / `auth_style` …），
 * 设置页是 `useLlmDraft` 的 camelCase（`baseUrl` / `authStyle` …）。
 * 认两种写法比让两处各自拼一份请求体安全——后者迟早漂移成第二个真相源。
 */
export interface LlmProbeDraftInput {
  protocol?: string | null;
  wire?: string | null;
  baseUrl?: string | null;
  base_url?: string | null;
  authStyle?: string | null;
  auth_style?: string | null;
  headerName?: string | null;
  header_name?: string | null;
  credentialId?: string | null;
  credential_id?: string | null;
  secret?: string | null;
  model?: string | null;
  paths?: LlmPaths | null;
  pathsModels?: string | null;
  pathsChat?: string | null;
  /** RHF 表单里的写法（凭证抽屉直接传 `form.getValues()`） */
  paths_models?: string | null;
  paths_chat?: string | null;
  extraHeaders?: Record<string, string> | null;
  extra_headers?: Record<string, string> | null;
  queryParams?: Record<string, string> | null;
  query_params?: Record<string, string> | null;
}

export interface BuildProbeTargetOptions {
  /** 缺省不带 mode（由调用方 api 层补 `auth`） */
  mode?: LlmProbeMode;
  /** 覆盖输入里的密钥（例如抽屉里密钥在 RHF 的另一处） */
  secret?: string | null;
  /** 覆盖输入里的模型（`mode` 含 completion 时必填） */
  model?: string | null;
  /** 草稿保存时建议的凭证名（只回显，不落库） */
  credentialName?: string | null;
  /**
   * 协议目录（`GET /llm/protocols` 的 items）。
   * 用来判断「当前协议的线协议是否由用户选」——后端对非 custom 协议传 `wire` 会
   * `VALIDATION_FAILED`，所以必须由元数据决定发不发，而不是前端硬编码协议名。
   */
  protocols?: readonly LlmProtocolSpec[];
  /** 显式覆盖上面的推导（一般不用） */
  wireSelectable?: boolean;
}

/* ── 纯函数：请求体构造 ───────────────────────────────── */

const text = (value: string | null | undefined): string =>
  typeof value === 'string' ? value.trim() : '';

const first = (...values: (string | null | undefined)[]): string => {
  for (const value of values) {
    const trimmed = text(value);
    if (trimmed !== '') return trimmed;
  }
  return '';
};

function compactObject(
  value: Record<string, string> | null | undefined,
): Record<string, string> | undefined {
  if (!value) return undefined;
  const entries = Object.entries(value).filter(
    ([key, item]) => key.trim() !== '' && typeof item === 'string' && item.trim() !== '',
  );
  return entries.length > 0 ? Object.fromEntries(entries) : undefined;
}

/** 路径覆盖：空串即「不覆盖」，两个都没有就不发 `paths` */
function normalizePaths(input: LlmProbeDraftInput): LlmPaths | undefined {
  const fromObject = input.paths ?? undefined;
  const models = first(input.pathsModels, input.paths_models, fromObject?.models);
  const chat = first(input.pathsChat, input.paths_chat, fromObject?.chat);
  if (models === '' && chat === '') return undefined;
  const paths: LlmPaths = {};
  if (models !== '') paths.models = models;
  if (chat !== '') paths.chat = chat;
  return paths;
}

/**
 * 把草稿值翻译成 `POST /llm/test-connection` / `POST /llm/models` 的请求体。
 *
 * 硬规则（spec §3.3 / §5.2）：
 * - `credential_id` 与 `secret` **二选一**：给了凭证就不再发密钥（两个都给 → 后端 400）；
 * - `auth_style` / `header_name` 只在草稿密钥分支带（凭证分支的认证方式由凭证自身映射）；
 * - `wire` 只在「该协议由用户选线协议」时带（否则后端 400）；
 * - `paths` 只在有覆盖值时带，且后端对非 custom 协议忽略它。
 */
export function buildProbeTarget(
  input: LlmProbeDraftInput,
  options: BuildProbeTargetOptions = {},
): LlmProbeTarget {
  const protocol = first(input.protocol);
  const credentialId = first(input.credentialId, input.credential_id);
  const baseUrl = first(input.baseUrl, input.base_url);
  const model = first(options.model, input.model);
  // 密钥不做 trim：某些网关的 key 尾巴就是有空白，改了反而鉴权失败
  const secret = options.secret ?? input.secret ?? '';

  const target: LlmProbeTarget = { protocol };

  if (credentialId !== '') {
    target.credential_id = credentialId;
  } else {
    if (secret !== '') target.secret = secret;
    const authStyle = first(input.authStyle, input.auth_style);
    const headerName = first(input.headerName, input.header_name);
    if (authStyle !== '') target.auth_style = authStyle;
    if (headerName !== '') target.header_name = headerName;
  }

  if (baseUrl !== '') target.base_url = baseUrl;

  const spec = options.protocols?.find((item) => item.key === protocol);
  const wireSelectable = options.wireSelectable ?? spec?.wire === null;
  const wire = first(input.wire);
  if (wireSelectable && wire !== '') target.wire = wire;

  const paths = normalizePaths(input);
  if (paths) target.paths = paths;

  const extraHeaders = compactObject(input.extraHeaders ?? input.extra_headers);
  if (extraHeaders) target.extra_headers = extraHeaders;
  const queryParams = compactObject(input.queryParams ?? input.query_params);
  if (queryParams) target.query_params = queryParams;

  if (model !== '') target.model = model;
  if (options.mode) target.mode = options.mode;
  const credentialName = first(options.credentialName);
  if (credentialName !== '') target.credential_name = credentialName;

  return target;
}

/* ── hook ─────────────────────────────────────────────── */

export interface UseLlmDraftOptions {
  /** 服务端已保存的配置（GET 回来后用 `reset()` 对齐即可，这里只做首帧初值） */
  initial?: Partial<LlmDraftValues> | null;
  /** `GET /llm/protocols` 的 items；决定 canTest 与切协议后的默认值 */
  protocols?: readonly LlmProtocolSpec[];
}

/** `setProtocol` 的返回值：页面据此 toast「地址已切换为新协议默认值」 */
export interface SetProtocolResult {
  /** base_url 被自动换成了新协议的默认值 */
  baseUrlChanged: boolean;
  /** 换协议前后地址的原始值（页面要回显时用） */
  previousBaseUrl: string;
  /** 是否找到了该协议的元数据 */
  known: boolean;
}

export interface UseLlmDraftResult {
  values: LlmDraftValues;
  /** 对齐用的基线（服务端已保存的值）；`dirty` 就是和它比 */
  baseline: LlmDraftValues;
  dirty: boolean;
  /** ③ 的三个按钮是否可点（缺必填项时为 false，原因在 `blockedReason`） */
  canTest: boolean;
  /** 不能测的原因（中文人话），可测时为 null */
  blockedReason: string | null;
  setField: <K extends keyof LlmDraftValues>(key: K, value: LlmDraftValues[K]) => void;
  setValues: (patch: Partial<LlmDraftValues>) => void;
  /** 切协议：默认地址跟随、清空模型与路径覆盖、认证方式取新协议默认值 */
  setProtocol: (protocolKey: string) => SetProtocolResult;
  /** 把草稿对齐到给定值（GET 回来 / 保存成功后调用） */
  reset: (next?: Partial<LlmDraftValues>) => void;
  /** 切走或提交后立刻抹掉内存里的密钥 */
  clearSecret: () => void;
  /** 请求体构造（已绑定当前草稿与协议目录） */
  buildProbeTarget: (options?: Omit<BuildProbeTargetOptions, 'protocols'>) => LlmProbeTarget;
}

function mergeValues(base: LlmDraftValues, patch?: Partial<LlmDraftValues> | null): LlmDraftValues {
  if (!patch) return { ...base };
  return {
    protocol: patch.protocol ?? base.protocol,
    wire: patch.wire ?? base.wire,
    baseUrl: patch.baseUrl ?? base.baseUrl,
    authStyle: patch.authStyle ?? base.authStyle,
    headerName: patch.headerName ?? base.headerName,
    credentialId: patch.credentialId ?? base.credentialId,
    secret: patch.secret ?? base.secret,
    model: patch.model ?? base.model,
    paths: patch.paths === undefined ? base.paths : patch.paths,
  };
}

function sameValues(a: LlmDraftValues, b: LlmDraftValues): boolean {
  return (
    a.protocol === b.protocol &&
    a.wire === b.wire &&
    a.baseUrl === b.baseUrl &&
    a.authStyle === b.authStyle &&
    a.headerName === b.headerName &&
    a.credentialId === b.credentialId &&
    a.secret === b.secret &&
    a.model === b.model &&
    (a.paths?.models ?? '') === (b.paths?.models ?? '') &&
    (a.paths?.chat ?? '') === (b.paths?.chat ?? '')
  );
}

/**
 * 草稿状态机。
 *
 * 生命周期约定（页面照抄即可）：
 * 1. `GET /agent/config` 回来后 `reset(fromServer)`（同时把 baseline 对齐）；
 * 2. 用户改了任何字段 → `dirty=true`，页面应把上一次测试结论置空（测试结论作废）；
 * 3. `setProtocol(key)` 会清空 `model`（换协议后旧清单必然失效）；
 * 4. 保存成功 → `reset(patch 返回的 model)` + `clearSecret()`。
 */
export function useLlmDraft(options: UseLlmDraftOptions = {}): UseLlmDraftResult {
  const { protocols } = options;
  const [values, setValuesState] = React.useState<LlmDraftValues>(() =>
    mergeValues(EMPTY_LLM_DRAFT_VALUES, options.initial),
  );
  const [baseline, setBaseline] = React.useState<LlmDraftValues>(() =>
    mergeValues(EMPTY_LLM_DRAFT_VALUES, options.initial),
  );

  const setField = React.useCallback(
    <K extends keyof LlmDraftValues>(key: K, value: LlmDraftValues[K]) => {
      setValuesState((prev) => ({ ...prev, [key]: value }));
    },
    [],
  );

  const setValues = React.useCallback((patch: Partial<LlmDraftValues>) => {
    setValuesState((prev) => ({ ...prev, ...patch }));
  }, []);

  const reset = React.useCallback(
    (next?: Partial<LlmDraftValues>) => {
      const merged = mergeValues(values, next);
      setValuesState(merged);
      setBaseline(merged);
    },
    [values],
  );

  const clearSecret = React.useCallback(() => {
    setValuesState((prev) => (prev.secret === '' ? prev : { ...prev, secret: '' }));
  }, []);

  const setProtocol = React.useCallback(
    (protocolKey: string): SetProtocolResult => {
      const spec = protocols?.find((item) => item.key === protocolKey);
      const previousSpec = protocols?.find((item) => item.key === values.protocol);
      const previousBaseUrl = values.baseUrl;
      // 只有「用户没改过地址」（空，或仍是上一个协议的默认值）才自动跟随新协议默认值
      const untouched =
        previousBaseUrl === '' ||
        (previousSpec ? previousBaseUrl === previousSpec.default_base_url : false);
      const nextBaseUrl = spec && untouched ? spec.default_base_url : previousBaseUrl;
      const next: LlmDraftValues = {
        ...values,
        protocol: protocolKey,
        wire: spec ? (spec.wire ?? values.wire) : '',
        baseUrl: nextBaseUrl,
        authStyle: spec?.default_auth_style ?? values.authStyle,
        // 换协议后旧清单与旧模型必然失效：留着就是「模型与协议不匹配」的来源
        model: '',
        paths: null,
      };
      setValuesState(next);
      return {
        baseUrlChanged: nextBaseUrl !== previousBaseUrl,
        previousBaseUrl,
        known: Boolean(spec),
      };
    },
    [protocols, values],
  );

  const spec = React.useMemo(
    () => protocols?.find((item) => item.key === values.protocol) ?? null,
    [protocols, values.protocol],
  );

  const baseUrlRequired = spec ? spec.default_base_url === '' : false;
  const hasProtocol = values.protocol.trim() !== '';
  const hasCredential = values.credentialId.trim() !== '';
  const hasDraftSecret = values.secret.trim() !== '';
  const hasBaseUrl = values.baseUrl.trim() !== '';

  let blockedReason: string | null = null;
  if (!hasProtocol) {
    blockedReason = '先选一个协议';
  } else if (!hasCredential && !hasDraftSecret) {
    blockedReason = '选择已有凭证，或（草稿测试）填写密钥与地址';
  } else if (baseUrlRequired && !hasBaseUrl) {
    blockedReason = `协议 ${spec?.label_zh ?? values.protocol} 需要填写地址`;
  } else if (!hasCredential && !hasBaseUrl) {
    blockedReason = '草稿测试需要填写地址';
  }

  const canTest = blockedReason === null;

  const buildTarget = React.useCallback(
    (override: Omit<BuildProbeTargetOptions, 'protocols'> = {}) =>
      buildProbeTarget(values, { ...override, protocols }),
    [values, protocols],
  );

  const dirty = !sameValues(values, baseline);

  return {
    values,
    baseline,
    dirty,
    canTest,
    blockedReason,
    setField,
    setValues,
    setProtocol,
    reset,
    clearSecret,
    buildProbeTarget: buildTarget,
  };
}
