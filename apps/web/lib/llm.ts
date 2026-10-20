/**
 * LLM 配置的类型化 API 封装（spec §5.1–5.5）。
 *
 * 约定与 `lib/api.ts` 一致：同源 `/api`、Cookie 会话、写请求由 api 层自动带 CSRF。
 * 这里只做三件事：收敛路径、收敛请求体字段名、给出可放进 queryKey 的稳定 key。
 *
 * INV-1：本文件**不判断**任何协议差异（哪个协议用哪个头、路径是什么），
 * 全部由后端 `GET /admin/llm/protocols` 决定。
 * INV-3：`secret` 只随请求体发出一次，绝不进 queryKey、不进日志。
 */

import { apiGet, apiPost } from './api';
import type {
  LlmModelsResponse,
  LlmProbeHistoryItem,
  LlmProbeHistoryResponse,
  LlmProbeMode,
  LlmProbeResponse,
  LlmProbeTarget,
  LlmProtocolsResponse,
} from './types';

/** `GET /api/admin/llm/protocols`：前端唯一的协议知识来源 */
export const getProtocols = (signal?: AbortSignal): Promise<LlmProtocolsResponse> =>
  apiGet<LlmProtocolsResponse>('/admin/llm/protocols', undefined, signal);

export interface TestConnectionOptions {
  /** 缺省 `auth`（不计费）；`both` = 先鉴权再发一次计费生成 */
  mode?: LlmProbeMode;
  signal?: AbortSignal;
}

/**
 * `POST /api/admin/llm/test-connection`：草稿值也能测，不要求先保存。
 *
 * 注意两类结果：上游鉴权失败 / 超时返回 `200 + ok:false + error_code`（不抛错）；
 * 请求不成立或凭证不可用才抛 `ApiError`（400/404/409/429/5xx）。
 */
export function testConnection(
  target: LlmProbeTarget,
  options: TestConnectionOptions = {},
): Promise<LlmProbeResponse> {
  const payload: LlmProbeTarget = { ...target };
  if (options.mode) payload.mode = options.mode;
  // 展开成对象字面量再交给 api 层：接口类型没有索引签名，不能直接当 `Record<string, unknown>`
  return apiPost<LlmProbeResponse>('/admin/llm/test-connection', { ...payload }, { signal: options.signal });
}

export interface FetchModelsOptions {
  /** 忽略 60s 缓存，强制打上游 */
  refresh?: boolean;
  /** 是否包含 embedding / audio / image 等非对话模型（默认 false） */
  includeNonLlm?: boolean;
  /** 上限（后端上限 500） */
  limit?: number;
  signal?: AbortSignal;
}

/** `POST /api/admin/llm/models`：草稿值一键取回该上游的全部可用模型 */
export function fetchModels(
  target: LlmProbeTarget,
  options: FetchModelsOptions = {},
): Promise<LlmModelsResponse> {
  return apiPost<LlmModelsResponse>(
    '/admin/llm/models',
    {
      ...target,
      refresh: options.refresh ?? false,
      include_non_llm: options.includeNonLlm ?? false,
      limit: options.limit ?? 500,
    },
    { signal: options.signal },
  );
}

export interface CredentialModelsOptions extends FetchModelsOptions {
  /** 覆盖凭证上记录的协议族 */
  protocol?: string;
}

/**
 * `GET /api/admin/credentials/{id}/models`：选了已有凭证 → 立刻有模型清单。
 * `ProbeTarget` 由凭证推导（协议可用 `protocol` 覆盖）。
 */
export function fetchCredentialModels(
  credentialId: string,
  options: CredentialModelsOptions = {},
  signal?: AbortSignal,
): Promise<LlmModelsResponse> {
  return apiGet<LlmModelsResponse>(
    `/admin/credentials/${encodeURIComponent(credentialId)}/models`,
    {
      protocol: options.protocol,
      refresh: options.refresh ?? false,
      include_non_llm: options.includeNonLlm ?? false,
      limit: options.limit ?? 500,
    },
    options.signal ?? signal,
  );
}

export interface ProbeHistoryParams {
  page?: number;
  page_size?: number;
  /** 只看某条凭证的探测记录 */
  credential_id?: string;
}

/**
 * `GET /api/admin/llm/probes`：探测历史（最近 N 条）。
 *
 * 列表项**已含完整 `stages`**，所以「点开看阶段」是纯前端就地展开，
 * **没有** `GET /llm/probes/{probe_id}` 详情端点，不要为一次点击多打一次请求。
 */
export function getProbes(
  params: ProbeHistoryParams = {},
  signal?: AbortSignal,
): Promise<LlmProbeHistoryResponse> {
  return apiGet<LlmProbeHistoryResponse>(
    '/admin/llm/probes',
    { page: params.page ?? 1, page_size: params.page_size ?? 10, credential_id: params.credential_id },
    signal,
  );
}

/** 探测历史的最新 N 条（默认 10）：给 `ProbeHistory` 用，`[...]` 里也能直接展开 */
export const listRecentProbes = (
  params: ProbeHistoryParams = {},
  signal?: AbortSignal,
): Promise<LlmProbeHistoryItem[]> =>
  getProbes({ page: 1, page_size: params.page_size ?? 10, credential_id: params.credential_id }, signal).then(
    (response) => response.items,
  );

/* ── 模型清单缓存 key ──────────────────────────────────── */

/**
 * 模型清单的 queryKey 片段：`protocol|wire|base_url|credential_id`。
 *
 * 与后端 `llm_model_catalog.dedupe_key` 的口径一致（不含 secret、不含 paths），
 * 这样「同一身份换一次点击」会命中 TanStack Query 的缓存，而不是每次都打上游。
 */
export function llmTargetKey(target: {
  protocol?: string | null;
  wire?: string | null;
  base_url?: string | null;
  credential_id?: string | null;
}): string {
  return [
    (target.protocol ?? '').trim().toLowerCase(),
    (target.wire ?? '').trim().toLowerCase(),
    (target.base_url ?? '').trim().replace(/\/+$/, ''),
    (target.credential_id ?? '').trim() || 'draft',
  ].join('|');
}
