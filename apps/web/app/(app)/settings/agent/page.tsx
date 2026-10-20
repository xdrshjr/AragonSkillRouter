'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowRight, ChevronRight, CircleAlert, Save, Undo2 } from 'lucide-react';
import * as React from 'react';
import { EmptyState } from '@/components/common/EmptyState';
import { FormField } from '@/components/common/FormField';
import { PageHeader } from '@/components/common/PageHeader';
import {
  ConnectionTester,
  type LlmProbeTransportError,
  type LlmTesterAction,
} from '@/components/llm/ConnectionTester';
import { ModelPicker } from '@/components/llm/ModelPicker';
import { ProbeHistory } from '@/components/llm/ProbeHistory';
import { ProtocolPicker } from '@/components/llm/ProtocolPicker';
import { buildProbeTarget, useLlmDraft, type LlmDraftValues } from '@/components/llm/useLlmDraft';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import type { SelectOption } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { useToast } from '@/components/ui/toast';
import { apiGet, apiPatch, describeError } from '@/lib/api';
import { formatRelative } from '@/lib/format';
import { fetchModels, getProtocols, llmTargetKey, testConnection } from '@/lib/llm';
import { queryKeys } from '@/lib/query';
import type {
  AgentEffective,
  Credential,
  LlmModelsResponse,
  LlmPaths,
  LlmProbeMode,
  LlmProbeResponse,
  LlmProbeTarget,
  Paginated,
} from '@/lib/types';

/**
 * 模型服务（`/settings/agent`，URL 不变）：spec §3.8 的六段式。
 *
 * ① 协议（来自 `GET /admin/llm/protocols`，前端不认识任何具体协议）
 * ② 连接信息（凭证 / 地址 / 高级）
 * ③ 实时验证（草稿可用，不要求先保存）
 * ④ 模型（来自 ③ 的清单，也可手填）
 * ⑤ 当前生效引用（readonly，排错用）
 * ⑥ 探测历史
 *
 * 三条硬规则（都能被 Playwright 断言）：
 * - 草稿「能测不能存」：没有 `credential_id` 时保存按钮变成「去创建凭证并保存」，
 *   跳凭证抽屉；**禁止** `PATCH /agent/config`（spec §3.3.1 / C-2）。
 * - 状态诚实：`ready` 与「测试通过」是两个独立徽标；配置一改测试结论即作废。
 * - 整页只有一个 `role="alert"`（沿用既有约定），其余提示走 `aria-live="polite"`。
 */

/** 后端 `agent_model` 设置里的可编辑键（PATCH 白名单见 `services.agent_model.EDITABLE`） */
interface AgentModelSettings {
  protocol?: string | null;
  wire?: string | null;
  /** 兼容别名：旧响应只有 `provider` */
  provider?: string | null;
  model?: string | null;
  base_url?: string | null;
  temperature?: number | null;
  max_tokens?: number | null;
  credential_id?: string | null;
  paths?: LlmPaths | null;
}

interface AgentConfigResponse {
  model: AgentModelSettings;
  effective: AgentEffective;
  providers?: string[];
}

/** Radix Select 不接受空字符串作为选项值，「草稿」用哨兵值承载 */
const DRAFT_CREDENTIAL = '__draft__';

/** 认证方式的界面词表。哪些认证方式**可选**由后端 `auth_styles` 决定，这里不判断协议差异。 */
const AUTH_STYLE_LABELS: Record<string, string> = {
  bearer: 'Bearer Token（Authorization: Bearer …）',
  api_key: 'API Key（放进指定请求头）',
  basic: 'Basic Auth（用户名:密码）',
  header: '自定义请求头',
  none: '无认证（内网免鉴权）',
};

/** 需要显式头名的认证方式（词表，不是协议差异） */
const HEADER_NAME_STYLES: readonly string[] = ['api_key', 'header'];

/** 从服务端已保存的配置翻译成草稿（`protocol` 缺失时回退旧别名 `provider`） */
function draftFromModel(model: AgentModelSettings | null | undefined): Partial<LlmDraftValues> {
  const explicit = typeof model?.protocol === 'string' ? model.protocol.trim() : '';
  const legacy = typeof model?.provider === 'string' ? model.provider.trim() : '';
  return {
    protocol: explicit !== '' ? explicit : legacy,
    wire: typeof model?.wire === 'string' ? model.wire : '',
    baseUrl: typeof model?.base_url === 'string' ? model.base_url : '',
    credentialId: typeof model?.credential_id === 'string' ? model.credential_id : '',
    model: typeof model?.model === 'string' ? model.model : '',
    paths: model?.paths ?? null,
    // 密钥永不出现在服务端响应里；恢复/对齐时一并抹掉内存中的草稿密钥
    secret: '',
  };
}

/**
 * 「key: value」多行文本 → 对象（额外请求头 / 查询参数用）。
 * 空行与没有分隔符的行直接忽略，避免把一句中文注释当成头名发出去。
 */
function parsePairs(text: string): Record<string, string> | undefined {
  const entries: [string, string][] = [];
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    if (line === '') continue;
    const colon = line.indexOf(':');
    const equals = line.indexOf('=');
    const index = colon === -1 ? equals : equals === -1 ? colon : Math.min(colon, equals);
    if (index <= 0) continue;
    const key = line.slice(0, index).trim();
    const value = line.slice(index + 1).trim();
    if (key === '') continue;
    entries.push([key, value]);
  }
  return entries.length > 0 ? Object.fromEntries(entries) : undefined;
}

/** 只保留有值的路径覆盖；两个都空就相当于「不覆盖」 */
function compactPaths(paths: LlmPaths | null): LlmPaths | undefined {
  const models = (paths?.models ?? '').trim();
  const chat = (paths?.chat ?? '').trim();
  if (models === '' && chat === '') return undefined;
  const next: LlmPaths = {};
  if (models !== '') next.models = models;
  if (chat !== '') next.chat = chat;
  return next;
}

export default function ModelServicePage() {
  const queryClient = useQueryClient();
  const { toast } = useToast();

  /* ── 服务端状态 ─────────────────────────────────────── */
  const config = useQuery<AgentConfigResponse>({
    queryKey: queryKeys.agent.config(),
    queryFn: () => apiGet<AgentConfigResponse>('/admin/agent/config'),
  });

  const protocols = useQuery({
    queryKey: queryKeys.llm.protocols(),
    queryFn: ({ signal }) => getProtocols(signal),
  });

  const credentials = useQuery<Paginated<Credential>>({
    queryKey: [...queryKeys.credentials.all, 'llm-settings-options'],
    // 只取 `kind=llm`：图 / 视频 / 语音凭证与「模型服务」无关，混进下拉只会让人选错钥匙
    queryFn: () =>
      apiGet<Paginated<Credential>>('/admin/credentials', { page_size: 100, kind: 'llm' }),
  });

  /* ── 草稿状态（本页没有 RHF，草稿由 useLlmDraft 持有：唯一真相源） ── */
  const draft = useLlmDraft({ protocols: protocols.data?.items });
  const { reset: resetDraft, setField, clearSecret } = draft;

  const [maxTokens, setMaxTokens] = React.useState('');
  const [maxTokensError, setMaxTokensError] = React.useState<string | null>(null);
  const [advancedOpen, setAdvancedOpen] = React.useState(false);
  const [extraHeadersText, setExtraHeadersText] = React.useState('');
  const [queryParamsText, setQueryParamsText] = React.useState('');
  const [apiVersion, setApiVersion] = React.useState('');
  const [revealSecret, setRevealSecret] = React.useState(false);

  const [models, setModels] = React.useState<LlmModelsResponse | null>(null);
  const [runningAction, setRunningAction] = React.useState<LlmTesterAction | null>(null);
  const [testResult, setTestResult] = React.useState<LlmProbeResponse | null>(null);
  const [transportError, setTransportError] = React.useState<LlmProbeTransportError | null>(null);
  const [savedEffective, setSavedEffective] = React.useState<AgentEffective | null>(null);
  const [savedAt, setSavedAt] = React.useState<string | null>(null);

  const hydrated = React.useRef(false);
  const { values } = draft;

  // 初值来自 GET；保存成功后由 PATCH 的返回再对齐一次（避免服务端规范化被本地值盖回去）
  React.useEffect(() => {
    if (hydrated.current || !config.data) return;
    resetDraft(draftFromModel(config.data.model));
    setMaxTokens(
      typeof config.data.model.max_tokens === 'number' ? String(config.data.model.max_tokens) : '',
    );
    hydrated.current = true;
  }, [config.data, resetDraft]);

  const currentSpec = React.useMemo(
    () => protocols.data?.items.find((item) => item.key === values.protocol) ?? null,
    [protocols.data, values.protocol],
  );

  // 认证方式缺省取该协议声明的默认值（仍然来自后端注册表，不是前端硬编码）
  React.useEffect(() => {
    if (!currentSpec) return;
    if (values.authStyle.trim() === '') setField('authStyle', currentSpec.default_auth_style);
  }, [currentSpec, values.authStyle, setField]);

  // 离开页面即抹掉内存里的草稿密钥（spec §3.8 密钥输入）
  React.useEffect(() => () => clearSecret(), [clearSecret]);

  /**
   * 配置一改，上一次测试结论立刻作废（`setTestResult(null)`）。
   * 用「值的指纹」做依赖：密钥只以「有没有填」参与，明文不进这个字符串。
   */
  const valuesFingerprint = React.useMemo(
    () =>
      JSON.stringify({
        protocol: values.protocol,
        wire: values.wire,
        baseUrl: values.baseUrl,
        authStyle: values.authStyle,
        headerName: values.headerName,
        credentialId: values.credentialId,
        model: values.model,
        hasSecret: values.secret !== '',
        paths: values.paths ?? null,
        // 它们也进请求体（额外请求头 / 查询参数 / api-version / max_tokens），
        // 改了却不清测试结论 = 用旧结论冒充新配置的结论
        extraHeadersText,
        queryParamsText,
        apiVersion,
        maxTokens,
      }),
    [values, extraHeadersText, queryParamsText, apiVersion, maxTokens],
  );

  React.useEffect(() => {
    setTestResult(null);
    setTransportError(null);
  }, [valuesFingerprint]);

  /* ── 探测请求体：走 useLlmDraft 的纯函数，页面不自己拼字段 ── */
  const buildTarget = React.useCallback(
    (mode: LlmProbeMode, model: string): LlmProbeTarget => {
      const extraHeaders = parsePairs(extraHeadersText);
      const queryParams = parsePairs(queryParamsText);
      const version = apiVersion.trim();
      const mergedQuery =
        version === ''
          ? queryParams
          : { ...(queryParams ?? {}), 'api-version': version };
      return buildProbeTarget(
        {
          ...values,
          extraHeaders: extraHeaders ?? null,
          queryParams: mergedQuery ?? null,
        },
        {
          mode,
          model: model === '' ? null : model,
          protocols: protocols.data?.items,
        },
      );
    },
    [values, extraHeadersText, queryParamsText, apiVersion, protocols.data],
  );

  /* ── ③ 的三个按钮 ──────────────────────────────────── */
  const runProbe = React.useCallback(
    async (action: LlmTesterAction, mode: LlmProbeMode) => {
      setTransportError(null);
      const model = values.model.trim();
      if (action === 'completion' && model === '') {
        setTransportError({
          code: 'MODEL_REQUIRED',
          message: '完成态测试需要一个模型名',
          hint: '先在 ④ 里选择或手填模型，再点「测试生成（计费 1 次）」。',
          requestId: null,
        });
        return;
      }
      setRunningAction(action);
      try {
        if (action === 'models') {
          // 「获取模型列表」= 同一次 auth 探测，但**不覆盖** ③ 的阶段行，只填 ④
          const target = buildTarget('auth', model);
          const key = queryKeys.llm.models(llmTargetKey(target));
          const cached = queryClient.getQueryState<LlmModelsResponse>(key);
          if (cached?.data && Date.now() - cached.dataUpdatedAt < 60_000) {
            setModels(cached.data);
          } else {
            const response = await fetchModels(target);
            queryClient.setQueryData(key, response);
            setModels(response);
          }
          return;
        }
        const result = await testConnection(buildTarget(mode, model), { mode });
        setTestResult(result);
        // ⑥ 的列表要立刻带上这一条（后端已写库；不失效只能等下次进页面）
        void queryClient.invalidateQueries({ queryKey: queryKeys.llm.all });
        if (!result.ok) {
          toast({
            title: result.error_code ? `测试失败（${result.error_code}）` : '测试失败',
            description: result.hint ?? result.problems[0] ?? '上游没有按要求回话。',
            tone: 'danger',
          });
        }
      } catch (cause) {
        const info = describeError(cause);
        setTransportError({
          code: info.code,
          message: info.message,
          hint: info.hint,
          requestId: info.requestId,
        });
      } finally {
        setRunningAction(null);
      }
    },
    [buildTarget, queryClient, toast, values.model],
  );

  /* ── ① 切协议：地址联动 + 清空模型清单/已选模型/测试结论 ── */
  const handleProtocolChange = React.useCallback(
    (protocolKey: string) => {
      const result = draft.setProtocol(protocolKey);
      setModels(null);
      setTestResult(null);
      setTransportError(null);
      if (result.baseUrlChanged) {
        toast({
          title: '地址已切换为新协议默认值',
          description: '地址被手动改过时不会被覆盖。',
          tone: 'info',
        });
      }
    },
    [draft, toast],
  );

  /* ── 凭证来源 ──────────────────────────────────────── */
  const credentialItems = React.useMemo(() => credentials.data?.items ?? [], [credentials.data]);
  const selectedCredential = React.useMemo(
    () => credentialItems.find((item) => item.id === values.credentialId) ?? null,
    [credentialItems, values.credentialId],
  );
  /** 草稿凭证（没有 credential_id）：只能测，不能在这里落库（spec §3.3.1） */
  const draftCredential = values.credentialId.trim() === '';

  /** 「去创建凭证并保存」要跳哪个提供商：优先当前凭证所属，其次已保存配置里的凭证所属 */
  const createProviderId = React.useMemo(() => {
    if (selectedCredential?.provider_id) return selectedCredential.provider_id;
    const savedCredentialId = config.data?.model.credential_id?.trim() ?? '';
    const saved = credentialItems.find((item) => item.id === savedCredentialId);
    return saved?.provider_id ?? '';
  }, [selectedCredential, config.data, credentialItems]);

  const createCredentialHref = React.useMemo(() => {
    const protocol = values.protocol.trim();
    // 把用户填过的地址一并带过去：凭证抽屉据此预填，否则新建的凭证会退回提供商地址
    const baseUrl = values.baseUrl.trim();
    const query =
      'createCredential=1' +
      (protocol === '' ? '' : `&protocol=${encodeURIComponent(protocol)}`) +
      (baseUrl === '' ? '' : `&base_url=${encodeURIComponent(baseUrl)}`);
    return createProviderId === ''
      ? `/providers?${query}`
      : `/providers/${encodeURIComponent(createProviderId)}?${query}`;
  }, [createProviderId, values.protocol, values.baseUrl]);

  const credentialOptions = React.useMemo<SelectOption[]>(() => {
    const options: SelectOption[] = [
      { value: DRAFT_CREDENTIAL, label: '草稿（临时密钥）', hint: '只用于测试' },
      ...credentialItems.map((item) => ({
        value: item.id,
        label: `${item.name} · ${item.provider_name ?? '未知提供商'} · ${item.secret_mask}`,
        hint: item.status,
      })),
    ];
    if (values.credentialId !== '' && !credentialItems.some((item) => item.id === values.credentialId)) {
      options.push({
        value: values.credentialId,
        label: `当前凭证（不在前 100 条里）：${values.credentialId}`,
      });
    }
    return options;
  }, [credentialItems, values.credentialId]);

  /* ── 保存（先有 credential_id，再 PATCH：顺序定死，§3.9） ── */
  const save = useMutation({
    mutationFn: async () => {
      const credentialId = values.credentialId.trim();
      if (credentialId === '') {
        // 硬规则 C-2：没有 credential_id 一律不发 PATCH
        throw new Error('草稿凭证不能在这里保存：请先在凭证抽屉里创建它');
      }
      const payload: Record<string, unknown> = {
        protocol: values.protocol.trim(),
        model: values.model.trim(),
        base_url: values.baseUrl.trim(),
        credential_id: credentialId,
      };
      if (currentSpec?.wire === null && values.wire.trim() !== '') {
        payload.wire = values.wire.trim();
      }
      const paths = compactPaths(values.paths);
      if (paths) payload.paths = paths;
      const maxTokensRaw = maxTokens.trim();
      if (maxTokensRaw !== '') payload.max_tokens = Number(maxTokensRaw);
      return apiPatch<AgentConfigResponse>('/admin/agent/config', payload);
    },
    onSuccess: async (result) => {
      resetDraft(draftFromModel(result.model));
      setMaxTokens(typeof result.model.max_tokens === 'number' ? String(result.model.max_tokens) : '');
      setMaxTokensError(null);
      setSavedEffective(result.effective);
      setSavedAt(new Date().toISOString());
      setTestResult(null);
      setModels(null);
      clearSecret();
      toast({
        title: '模型服务配置已保存',
        description: result.effective.ready
          ? '解析就绪：Agent 面板可以发消息了。'
          : `仍未就绪：${result.effective.problems[0] ?? '配置不完整'}`,
        tone: result.effective.ready ? 'ok' : 'warn',
      });
      await queryClient.invalidateQueries({ queryKey: queryKeys.agent.config() });
      await queryClient.invalidateQueries({ queryKey: queryKeys.llm.all });
    },
    onError: (cause) => {
      const info = describeError(cause);
      toast({
        title: '保存失败',
        description: info.hint ? `${info.message}（${info.hint}）` : info.message,
        tone: 'danger',
        requestId: info.requestId,
      });
    },
  });

  /** 保存前的 max_tokens 校验（1–1000000 的整数），不满足就不发请求 */
  function submit(): void {
    const maxTokensRaw = maxTokens.trim();
    if (maxTokensRaw !== '') {
      const parsed = Number(maxTokensRaw);
      if (!Number.isInteger(parsed) || parsed < 1 || parsed > 1_000_000) {
        setMaxTokensError('max_tokens 必须是 1-1000000 的整数');
        return;
      }
    }
    setMaxTokensError(null);
    if (values.protocol.trim() === '') {
      toast({ title: '先选一个协议', tone: 'warn' });
      return;
    }
    save.mutate();
  }

  /** 恢复：把草稿对齐到已保存的配置，并清掉测试结论与模型清单 */
  function restore(): void {
    resetDraft(draftFromModel(config.data?.model));
    setMaxTokens(
      typeof config.data?.model.max_tokens === 'number' ? String(config.data.model.max_tokens) : '',
    );
    setMaxTokensError(null);
    setModels(null);
    setTestResult(null);
    setTransportError(null);
    setExtraHeadersText('');
    setQueryParamsText('');
    setApiVersion('');
    clearSecret();
    toast({ title: '已恢复到已保存的配置', tone: 'info' });
  }

  /* ── 渲染分支 ─────────────────────────────────────── */
  if (config.isLoading) {
    return (
      <div className="flex flex-col gap-4" aria-busy="true">
        <Skeleton height={64} rounded="md" />
        <Skeleton height={180} rounded="md" />
        <Skeleton height={160} rounded="md" />
        <Skeleton height={320} rounded="md" />
      </div>
    );
  }

  if (config.isError) {
    const info = describeError(config.error);
    return (
      <EmptyState
        kind={info.code === 'NETWORK_OFFLINE' ? 'offline' : 'unavailable'}
        title={info.code === 'NOT_FOUND' ? '模型服务配置接口不存在' : info.message}
        description={info.hint ?? '检查 API 进程是否运行，然后重试。'}
        requestId={info.requestId}
        actionLabel="重试"
        onAction={() => void config.refetch()}
      />
    );
  }

  if (!config.data) return null;

  const effective = savedEffective ?? config.data.effective;
  const problems = effective.problems ?? [];
  const warnings = effective.warnings ?? [];
  const notes = effective.notes ?? [];
  /** problems 非空一律不得显示「就绪」（spec §3.8 状态诚实 / R-15） */
  const ready = effective.ready && problems.length === 0;
  const credentialRefEntries = Object.entries(effective.credential_refs ?? {});
  const credentialCount = credentialItems.length;

  const protocolLabel = currentSpec?.label_zh ?? (values.protocol !== '' ? values.protocol : '未选协议');
  const modelLabel = values.model.trim() !== '' ? values.model : '未选模型';

  const headerNameEnabled = HEADER_NAME_STYLES.includes(values.authStyle);
  const inheritPaths = currentSpec?.paths_inherit_from_wire === true;
  const wireOptions: SelectOption[] = (protocols.data?.wires ?? []).map((wire) => ({
    value: wire,
    label: wire,
  }));

  // 页面唯一的 role="alert"：保存失败 / 测试请求被拒 / 上游返回 ok=false
  let alertTitle: string | null = null;
  let alertLines: string[] = [];
  if (save.isError) {
    const info = describeError(save.error);
    alertTitle = `保存失败：${info.message}`;
    alertLines = [
      ...(info.hint ? [info.hint] : []),
      ...(info.requestId ? [`request_id ${info.requestId}`] : []),
    ];
  } else if (transportError) {
    alertTitle = `测试请求失败：${transportError.code}`;
    alertLines = [
      transportError.message,
      ...(transportError.hint ? [transportError.hint] : []),
      ...(transportError.requestId ? [`request_id ${transportError.requestId}`] : []),
    ];
  } else if (testResult && !testResult.ok) {
    alertTitle = testResult.error_code ? `测试失败：${testResult.error_code}` : '测试失败：上游没有正常回话';
    alertLines = [
      ...(testResult.hint ? [testResult.hint] : []),
      ...testResult.problems,
    ];
  }

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="模型服务"
        breadcrumbs={[{ label: '设置', href: '/settings' }]}
        description="内置 Agent 用哪个上游模型；改动保存后下一轮生效。测试用的是当前表单值，草稿密钥也能直接测。"
        actions={
          <>
            <Button
              variant="secondary"
              size="sm"
              loading={runningAction === 'test'}
              disabled={!draft.canTest || runningAction !== null}
              onClick={() => void runProbe('test', 'auth')}
            >
              测试连接
            </Button>
            {draftCredential ? (
              <Button
                variant="primary"
                size="sm"
                onClick={() => window.location.assign(createCredentialHref)}
                icon={<ArrowRight size={16} strokeWidth={1.75} />}
              >
                去创建凭证并保存
              </Button>
            ) : (
              <Button
                variant="primary"
                size="sm"
                loading={save.isPending}
                onClick={submit}
                icon={<Save size={16} strokeWidth={1.75} />}
              >
                {save.isPending ? '保存中…' : '保存'}
              </Button>
            )}
            <Button
              variant="ghost"
              size="sm"
              onClick={restore}
              icon={<Undo2 size={16} strokeWidth={1.75} />}
            >
              恢复
            </Button>
          </>
        }
        status={
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={ready ? 'ok' : 'warn'} dot data-testid="llm-ready-badge">
              {ready ? '就绪' : '未就绪'}
              {testResult === null ? ' · 未测试' : ''}
            </Badge>
            {/* 「就绪」与「测试通过」是两个独立徽标：一个说保存后的解析结果，一个说这次请求的结果 */}
            {testResult ? (
              <Badge tone={testResult.ok ? 'ok' : 'danger'} dot data-testid="llm-test-badge">
                {testResult.ok ? '测试通过' : '测试失败'}
              </Badge>
            ) : (
              <Badge tone="neutral">未测试</Badge>
            )}
            <Badge tone="outline" mono data-testid="llm-protocol-badge">
              {protocolLabel}
            </Badge>
            <Badge tone="outline" mono data-testid="llm-model-badge">
              {modelLabel}
            </Badge>
            <Badge tone={credentialCount > 0 ? 'neutral' : 'warn'} data-testid="llm-credential-count">
              凭证 {credentialCount} 个
            </Badge>
          </div>
        }
      />

      {/* 保存状态行（PATCH 返回的 effective）：既有行为保留 */}
      {savedEffective ? (
        <p
          role="status"
          className="flex flex-wrap items-center gap-2 rounded border border-border bg-[var(--sr-surface-sunken)] px-3 py-2 text-12 text-content-muted"
        >
          <Badge tone={savedEffective.ready ? 'ok' : 'warn'} dot>
            {savedEffective.ready ? '已保存且就绪' : '已保存但仍未就绪'}
          </Badge>
          <span>保存于 {savedAt ? formatRelative(savedAt) : '刚刚'}（PATCH 返回的 effective）</span>
          {savedEffective.problems.length > 0 ? (
            <span>· {savedEffective.problems.join('；')}</span>
          ) : null}
        </p>
      ) : null}

      {alertTitle ? (
        <div
          role="alert"
          data-testid="llm-alert"
          className="rounded-md border border-danger bg-danger-weak p-4 text-13 text-content"
        >
          <p className="flex items-center gap-1.5 font-medium">
            <CircleAlert size={16} strokeWidth={1.75} className="text-danger" aria-hidden />
            {alertTitle}
          </p>
          {alertLines.length > 0 ? (
            <ul className="mt-1 flex flex-col gap-0.5 text-12 text-content-muted">
              {alertLines.map((line) => (
                <li key={line}>· {line}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}

      {/* ① 协议 */}
      <section aria-label="协议" className="sr-card p-4">
        <ProtocolPicker
          protocols={protocols.data?.items ?? []}
          value={values.protocol}
          onChange={handleProtocolChange}
          loading={protocols.isLoading}
          error={
            protocols.isError
              ? {
                  code: describeError(protocols.error).code,
                  message: describeError(protocols.error).message,
                  hint: describeError(protocols.error).hint,
                }
              : null
          }
          onRetry={() => void protocols.refetch()}
          legend="协议"
        />
      </section>

      {/* ② 连接信息 */}
      <section aria-label="连接信息" className="sr-card p-4">
        <h2 className="text-16 font-semibold text-content">连接信息</h2>
        <p className="mt-0.5 text-12 text-content-muted">
          测试用的是当前表单值；保存后才对 Agent 生效。凭证必须挂在提供商下，草稿只用于测试。
        </p>

        <div className="mt-3 grid grid-cols-1 gap-x-3 gap-y-1 md:grid-cols-2">
          <FormField
            id="llm-credential"
            label="凭证"
            description="选已有凭证，或用草稿密钥直接测；草稿不会被保存。"
          >
            <Select
              id="llm-credential"
              aria-label="凭证"
              value={values.credentialId === '' ? DRAFT_CREDENTIAL : values.credentialId}
              onValueChange={(value) => {
                if (value === DRAFT_CREDENTIAL) {
                  setField('credentialId', '');
                  return;
                }
                // 切到已保存凭证 ⇒ 内存里的草稿密钥立即抹掉（spec §3.8 密钥输入）
                setField('credentialId', value);
                clearSecret();
              }}
              options={credentialOptions}
              placeholder={credentials.isLoading ? '正在加载凭证…' : '选择凭证或草稿'}
              disabled={credentials.isLoading}
            />
          </FormField>

          <FormField
            id="llm-base-url"
            label="地址"
            required={currentSpec ? currentSpec.default_base_url.trim() === '' : false}
            hint={currentSpec?.default_base_url ? '可改' : undefined}
            description={
              currentSpec?.base_url_hint
                ? `填法：${currentSpec.base_url_hint}${currentSpec.default_base_url ? '；留空则用默认地址' : ''}`
                : undefined
            }
          >
            <Input
              id="llm-base-url"
              aria-label="地址"
              value={values.baseUrl}
              onChange={(event) => setField('baseUrl', event.target.value)}
              placeholder={currentSpec?.base_url_hint || 'https://your-gateway/v1'}
              autoComplete="off"
              spellCheck={false}
              inputMode="url"
              className="sr-mono text-13"
            />
          </FormField>

          {draftCredential ? (
            <FormField
              id="llm-secret"
              label="密钥（草稿）"
              hint="只在本次测试的内存里"
              description="type=password，切走即从页面状态清除；不会写库、不会进日志。"
              className="md:col-span-2"
              aside={
                <Button
                  variant="ghost"
                  size="sm"
                  aria-pressed={revealSecret}
                  onClick={() => setRevealSecret((prev) => !prev)}
                >
                  {revealSecret ? '隐藏' : '显示'}
                </Button>
              }
            >
              <Input
                id="llm-secret"
                aria-label="密钥（草稿）"
                type={revealSecret ? 'text' : 'password'}
                autoComplete="off"
                spellCheck={false}
                value={values.secret}
                onChange={(event) => setField('secret', event.target.value)}
                placeholder="粘贴密钥（只用于本页测试）"
              />
            </FormField>
          ) : null}
        </div>

        {!credentials.isLoading && credentialCount === 0 ? (
          <EmptyState
            kind="first-use"
            compact
            title="还没有可用凭证"
            description="凭证必须挂在提供商下（一个提供商可以有多张）。先去创建一张，再回来选它。"
            actionLabel="去创建凭证"
            onAction={() => window.location.assign('/providers')}
            className="mt-3"
          />
        ) : null}

        {credentials.isError ? (
          <p className="mt-1 text-12 text-content-muted">
            凭证列表读取失败：{describeError(credentials.error).message}。可以手动填地址后直接测试。
          </p>
        ) : null}

        {/* 高级：认证方式 / 头名 / 额外请求头 / 查询参数 / 路径覆盖 / api-version */}
        <div className="mt-3 border-t border-border pt-3">
          <Button
            variant="ghost"
            size="sm"
            aria-expanded={advancedOpen}
            onClick={() => setAdvancedOpen((prev) => !prev)}
            icon={
              <ChevronRight
                size={14}
                strokeWidth={1.75}
                aria-hidden
                className={advancedOpen ? 'rotate-90 transition-transform duration-150 ease-sr' : 'transition-transform duration-150 ease-sr'}
              />
            }
          >
            高级
          </Button>

          {advancedOpen ? (
            <div className="mt-2 grid grid-cols-1 gap-x-3 gap-y-1 md:grid-cols-2">
              {currentSpec?.wire === null ? (
                <FormField
                  id="llm-wire"
                  label="线协议"
                  description="自定义地址仍按哪种规范发请求（不选则由后端按默认线协议处理）。"
                >
                  <Select
                    id="llm-wire"
                    aria-label="线协议"
                    value={values.wire === '' ? undefined : values.wire}
                    onValueChange={(value) => setField('wire', value)}
                    options={wireOptions}
                    placeholder="默认线协议（后端决定）"
                  />
                </FormField>
              ) : null}

              <FormField
                id="llm-auth-style"
                label="认证方式"
                description={
                  currentSpec
                    ? `可选：${currentSpec.auth_styles
                        .map((style) => AUTH_STYLE_LABELS[style] ?? style)
                        .join(' / ')}`
                    : undefined
                }
              >
                <Select
                  id="llm-auth-style"
                  aria-label="认证方式"
                  value={values.authStyle === '' ? undefined : values.authStyle}
                  onValueChange={(value) => setField('authStyle', value)}
                  options={(currentSpec?.auth_styles ?? []).map((style) => ({
                    value: style,
                    label: AUTH_STYLE_LABELS[style] ?? style,
                  }))}
                  placeholder="选择认证方式"
                  disabled={!currentSpec}
                />
              </FormField>

              <FormField
                id="llm-header-name"
                label="头名"
                hint={headerNameEnabled ? '如 x-api-key' : '当前认证方式不需要'}
                description={
                  headerNameEnabled
                    ? '留空则用该协议默认头名（由后端决定）。'
                    : '只有 API Key / 自定义请求头时才会用到。'
                }
              >
                <Input
                  id="llm-header-name"
                  aria-label="头名"
                  value={values.headerName}
                  onChange={(event) => setField('headerName', event.target.value)}
                  placeholder={headerNameEnabled ? 'x-api-key' : '—'}
                  autoComplete="off"
                  spellCheck={false}
                  disabled={!headerNameEnabled}
                />
              </FormField>

              <FormField
                id="llm-api-version"
                label="api-version"
                hint="可留空"
                description="会作为查询参数 api-version 发送（Azure 风格的网关需要它）。"
              >
                <Input
                  id="llm-api-version"
                  aria-label="api-version"
                  value={apiVersion}
                  onChange={(event) => setApiVersion(event.target.value)}
                  placeholder="2024-10-21"
                  autoComplete="off"
                  spellCheck={false}
                  className="sr-mono text-13"
                />
              </FormField>

              <FormField
                id="llm-extra-headers"
                label="额外请求头"
                hint="每行 key: value"
                description="只随测试请求发出，不会写进 Agent 配置。"
                className="md:col-span-2"
              >
                <textarea
                  id="llm-extra-headers"
                  aria-label="额外请求头"
                  value={extraHeadersText}
                  onChange={(event) => setExtraHeadersText(event.target.value)}
                  rows={2}
                  spellCheck={false}
                  autoComplete="off"
                  placeholder="x-custom-header: value"
                  className="sr-mono w-full rounded border border-border bg-surface px-3 py-2 text-13 text-content placeholder:text-content-muted focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
                />
              </FormField>

              <FormField
                id="llm-query-params"
                label="查询参数"
                hint="每行 key: value"
                description="只随测试请求发出，不会写进 Agent 配置。"
                className="md:col-span-2"
              >
                <textarea
                  id="llm-query-params"
                  aria-label="查询参数"
                  value={queryParamsText}
                  onChange={(event) => setQueryParamsText(event.target.value)}
                  rows={2}
                  spellCheck={false}
                  autoComplete="off"
                  placeholder="api-version: 2024-10-21"
                  className="sr-mono w-full rounded border border-border bg-surface px-3 py-2 text-13 text-content placeholder:text-content-muted focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
                />
              </FormField>

              {inheritPaths ? (
                <>
                  <FormField
                    id="llm-paths-models"
                    label="取模型路径"
                    hint="可留空"
                    description="留空即继承所选线协议的默认路径（不是「没有模型清单」）。"
                  >
                    <Input
                      id="llm-paths-models"
                      aria-label="取模型路径"
                      value={values.paths?.models ?? ''}
                      onChange={(event) =>
                        setField('paths', { ...(values.paths ?? {}), models: event.target.value })
                      }
                      placeholder="继承所选线协议"
                      autoComplete="off"
                      spellCheck={false}
                      className="sr-mono text-13"
                    />
                  </FormField>
                  <FormField
                    id="llm-paths-chat"
                    label="对话路径"
                    hint="可留空"
                    description="留空即继承所选线协议的默认路径。"
                  >
                    <Input
                      id="llm-paths-chat"
                      aria-label="对话路径"
                      value={values.paths?.chat ?? ''}
                      onChange={(event) =>
                        setField('paths', { ...(values.paths ?? {}), chat: event.target.value })
                      }
                      placeholder="继承所选线协议"
                      autoComplete="off"
                      spellCheck={false}
                      className="sr-mono text-13"
                    />
                  </FormField>
                </>
              ) : null}

              <FormField
                id="llm-max-tokens"
                label="最大输出 tokens"
                hint="1-1000000"
                description="留空则沿用已保存的值。网关当前按自己的环境变量决定实际上限，这里只落库。"
              >
                <Input
                  id="llm-max-tokens"
                  aria-label="最大输出 tokens"
                  type="number"
                  min={1}
                  max={1000000}
                  step={1}
                  inputMode="numeric"
                  value={maxTokens}
                  onChange={(event) => {
                    setMaxTokens(event.target.value);
                    setMaxTokensError(null);
                  }}
                  placeholder="4096"
                />
              </FormField>
            </div>
          ) : null}

          {/* 字段级错误就地提示；**不用** FormField 的 error 属性（那会额外产生一个 role="alert"） */}
          {maxTokensError ? <p className="mt-1 text-12 text-danger">{maxTokensError}</p> : null}
        </div>
      </section>

      {/* ③ 实时验证 */}
      {/* 不再起一个同名 region：ConnectionTester 内部已经是 region[实时验证]，同名会让 e2e 命中两个节点 */}
      <section className="sr-card p-4">
        {/*
          传输层错误**不**再交给 ConnectionTester 渲染：它由页面顶端那唯一一个
          `role="alert"` 区域统一承担（避免同一条错误出现两遍）。
        */}
        <ConnectionTester
          result={testResult}
          runningAction={runningAction}
          onRun={(action, mode) => void runProbe(action, mode)}
          canTest={draft.canTest}
          blockedReason={draft.blockedReason}
        />
      </section>

      {/* ④ 模型 */}
      <section aria-label="模型" className="sr-card p-4">
        <h2 className="text-16 font-semibold text-content">模型</h2>
        <p className="mt-0.5 text-12 text-content-muted">
          清单来自 ③ 的「获取模型列表」（同一目标 60 秒内复用，不重复打上游）。没有清单也可以直接手填 model id。
        </p>
        <div className="mt-2 max-w-[720px]">
          <ModelPicker
            id="llm-model"
            label="模型"
            models={models?.items ?? []}
            value={values.model}
            onChange={(modelId) => setField('model', modelId)}
            loading={runningAction === 'models'}
            source={models?.source ?? null}
            warning={models?.warning ?? null}
            fetchedAt={models?.fetched_at ?? null}
            truncated={models?.truncated ?? false}
            hint="搜索后点选；也可以直接粘贴 model id。"
          />
        </div>
        {models === null ? (
          <p className="text-12 text-content-muted">还没有模型清单：点「获取模型列表」拉一次。</p>
        ) : null}
      </section>

      {/* ⑤ 当前生效引用（readonly，排错用） */}
      <section aria-label="当前生效引用" className="sr-card p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-16 font-semibold text-content">当前生效引用</h2>
          <Badge tone={ready ? 'ok' : 'warn'} dot>
            {ready ? '就绪' : '未就绪'}
          </Badge>
        </div>
        <p className="mt-0.5 text-12 text-content-muted">
          后端把设置翻译成网关请求后的引用（effective）。发消息仍失败时先对照这里；只读，不可编辑。
        </p>
        <dl className="mt-2 grid grid-cols-1 gap-1 text-12 sm:grid-cols-2">
          <div>
            <dt className="text-content-muted">协议（protocol）</dt>
            <dd className="sr-mono break-all text-content">{effective.protocol ?? '—'}</dd>
          </div>
          <div>
            <dt className="text-content-muted">线协议（wire）</dt>
            <dd className="sr-mono break-all text-content">{effective.wire ?? '—'}</dd>
          </div>
          <div>
            <dt className="text-content-muted">providerId</dt>
            <dd className="sr-mono break-all text-content">{effective.model?.providerId ?? '—'}</dd>
          </div>
          <div>
            <dt className="text-content-muted">modelId</dt>
            <dd className="sr-mono break-all text-content">{effective.model?.modelId ?? '—'}</dd>
          </div>
          <div className="sm:col-span-2">
            <dt className="text-content-muted">baseUrl</dt>
            <dd className="sr-mono break-all text-content">
              {effective.model?.baseUrl ?? '未设置（用凭证所属提供商的 base_url）'}
            </dd>
          </div>
          <div className="sm:col-span-2">
            <dt className="text-content-muted">凭证引用（credential_refs）</dt>
            <dd className="sr-mono break-all text-content">
              {credentialRefEntries.length === 0
                ? '空（网关会回 no model configured for this turn）'
                : credentialRefEntries
                    .map(([wireOrProtocol, credentialId]) => `${wireOrProtocol} → ${credentialId}`)
                    .join('、')}
            </dd>
          </div>
          <div className="sm:col-span-2">
            <dt className="text-content-muted">凭证（credential）</dt>
            <dd className="break-all text-content">
              {effective.credential
                ? `${effective.credential.name} · ${effective.credential.provider_name ?? '未知提供商'} · ${effective.credential.secret_mask ?? '—'}`
                : '未解析到凭证'}
            </dd>
          </div>
        </dl>

        {problems.length > 0 ? (
          <div className="mt-2" aria-live="polite">
            <p className="text-13 text-content">未就绪原因（{problems.length}）：</p>
            <ul className="mt-0.5 flex flex-col gap-0.5 text-12 text-content-muted">
              {problems.map((problem) => (
                <li key={problem}>· {problem}</li>
              ))}
            </ul>
          </div>
        ) : null}
        {warnings.length > 0 ? (
          <div className="mt-2" aria-live="polite">
            <p className="text-13 text-content">提示（不阻塞）：</p>
            <ul className="mt-0.5 flex flex-col gap-0.5 text-12 text-content-muted">
              {warnings.map((warning) => (
                <li key={warning}>· {warning}</li>
              ))}
            </ul>
          </div>
        ) : null}
        {notes.length > 0 ? (
          <ul className="mt-2 flex flex-col gap-0.5 text-12 text-content-muted">
            {notes.map((note) => (
              <li key={note}>· {note}</li>
            ))}
          </ul>
        ) : null}
      </section>

      {/* ⑥ 探测历史（页面末尾） */}
      <section aria-label="探测历史容器" className="sr-card p-4">
        <ProbeHistory limit={10} credentialId={values.credentialId.trim() === '' ? null : values.credentialId} />
      </section>
    </div>
  );
}
