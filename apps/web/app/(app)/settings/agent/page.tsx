'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Bot, CircleAlert, PlugZap, Save } from 'lucide-react';
import * as React from 'react';
import { EmptyState } from '@/components/common/EmptyState';
import { FormField } from '@/components/common/FormField';
import { PageHeader } from '@/components/common/PageHeader';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import type { SelectOption } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { useToast } from '@/components/ui/toast';
import { apiGet, apiPatch, apiPost, describeError } from '@/lib/api';
import { formatRelative } from '@/lib/format';
import { queryKeys } from '@/lib/query';
import type { Credential, Paginated } from '@/lib/types';

/**
 * Agent 模型设置（`/settings/agent`）。
 *
 * 页面只做三件事：把 `agent_model` 设置（协议族 / 模型名 / base_url / 凭证 / max_tokens）
 * 写成后端白名单键；把后端解析出来的 `effective`（网关发轮次真正用的引用）摊开给人看；
 * 用一次**真实**调用当证据（`POST /admin/agent/config/test`）。
 * 没有配置时 Agent 面板发不出消息——所以未就绪的 `problems` 必须逐条可见。
 */

/** 后端 `agent_model` 设置里的可编辑键（PATCH 白名单见 `services.agent_model.EDITABLE`） */
interface AgentModelSettings {
  provider?: string | null;
  model?: string | null;
  base_url?: string | null;
  temperature?: number | null;
  max_tokens?: number | null;
  credential_id?: string | null;
}

/** 网关 `model{providerId,modelId,baseUrl?}` */
interface EffectiveModelRef {
  providerId: string;
  modelId: string;
  baseUrl?: string;
}

interface EffectiveCredential {
  id: string;
  name: string;
  provider_id: string | null;
  provider_name: string | null;
  secret_mask: string | null;
}

/** `GET/PATCH /admin/agent/config` 的 `effective` */
interface AgentEffective {
  ready: boolean;
  model: EffectiveModelRef | null;
  credential_refs: Record<string, string>;
  credential: EffectiveCredential | null;
  problems: string[];
}

interface AgentConfigResponse {
  model: AgentModelSettings;
  effective: AgentEffective;
  providers: string[];
}

interface AgentConfigTestResult {
  ok: boolean;
  checked: boolean;
  provider?: string | null;
  model?: string | null;
  base_url?: string | null;
  status?: number | null;
  latency_ms?: number | null;
  detail?: string | null;
  credential?: EffectiveCredential | null;
  problems?: string[];
}

interface AgentConfigPatch extends Record<string, unknown> {
  provider: string;
  model: string;
  base_url: string;
  credential_id: string;
  max_tokens?: number;
}

interface AgentFormState {
  provider: string;
  model: string;
  baseUrl: string;
  credentialId: string;
  maxTokens: string;
}

/** Radix Select 不允许空字符串作为 item value（会抛错），所以「自动选择」用哨兵值承载 */
const AUTO_CREDENTIAL = '__auto__';

const BASE_URL_PLACEHOLDER = 'https://open.bigmodel.cn/api/anthropic';

const PROVIDER_OPTIONS: SelectOption[] = [
  { value: 'anthropic', label: 'anthropic', hint: 'Messages API' },
  { value: 'openai', label: 'openai', hint: 'Chat Completions' },
  { value: 'google', label: 'google', hint: 'generateContent' },
];

const EMPTY_FORM: AgentFormState = {
  provider: 'anthropic',
  model: '',
  baseUrl: '',
  credentialId: '',
  maxTokens: '',
};

function formFromModel(model: AgentModelSettings | null | undefined): AgentFormState {
  const provider = typeof model?.provider === 'string' ? model.provider.trim() : '';
  return {
    provider: provider === '' ? 'anthropic' : provider,
    model: typeof model?.model === 'string' ? model.model : '',
    baseUrl: typeof model?.base_url === 'string' ? model.base_url : '',
    credentialId: typeof model?.credential_id === 'string' ? model.credential_id : '',
    maxTokens: typeof model?.max_tokens === 'number' ? String(model.max_tokens) : '',
  };
}

export default function AgentModelSettingsPage() {
  const queryClient = useQueryClient();
  const { toast } = useToast();

  const [form, setForm] = React.useState<AgentFormState>(EMPTY_FORM);
  const [baseline, setBaseline] = React.useState<AgentFormState | null>(null);
  const [maxTokensError, setMaxTokensError] = React.useState<string | null>(null);
  const [testResult, setTestResult] = React.useState<AgentConfigTestResult | null>(null);
  const [savedEffective, setSavedEffective] = React.useState<AgentEffective | null>(null);
  const [savedAt, setSavedAt] = React.useState<string | null>(null);

  const config = useQuery<AgentConfigResponse>({
    queryKey: queryKeys.agent.config(),
    queryFn: () => apiGet<AgentConfigResponse>('/admin/agent/config'),
  });

  const credentials = useQuery<Paginated<Credential>>({
    queryKey: [...queryKeys.credentials.all, 'agent-model-options'],
    queryFn: () => apiGet<Paginated<Credential>>('/admin/credentials', { page_size: 100 }),
  });

  // 初值来自 GET；保存后由 PATCH 的返回再对齐一次，避免服务端规范化（如凭证自动选中）被本地值盖回去
  const hydrated = React.useRef(false);
  React.useEffect(() => {
    if (hydrated.current || !config.data) return;
    const next = formFromModel(config.data.model);
    setForm(next);
    setBaseline(next);
    hydrated.current = true;
  }, [config.data]);

  const save = useMutation({
    mutationFn: async (patch: AgentConfigPatch) =>
      apiPatch<AgentConfigResponse>('/admin/agent/config', patch),
    onSuccess: async (result) => {
      const next = formFromModel(result.model);
      setForm(next);
      setBaseline(next);
      setMaxTokensError(null);
      setSavedEffective(result.effective);
      setSavedAt(new Date().toISOString());
      // 配置变了，上一次连通性测试的结论就不再生效：留着「通过」徽标等于撒谎
      setTestResult(null);
      toast({
        title: 'Agent 模型配置已保存',
        description: result.effective.ready
          ? '解析就绪：Agent 面板可以发消息了。'
          : `仍未就绪：${result.effective.problems[0] ?? '配置不完整'}`,
        tone: result.effective.ready ? 'ok' : 'warn',
      });
      await queryClient.invalidateQueries({ queryKey: queryKeys.agent.config() });
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

  const test = useMutation({
    mutationFn: async () => apiPost<AgentConfigTestResult>('/admin/agent/config/test', {}),
    onSuccess: (result) => {
      setTestResult(result);
      if (result.ok) {
        toast({
          title: '连通性测试通过',
          description: `${result.provider ?? '—'} / ${result.model ?? '—'} · HTTP ${result.status ?? '—'} · ${result.latency_ms ?? 0}ms`,
          tone: 'ok',
        });
      } else {
        toast({
          title: result.checked ? '连通性测试失败' : '测试未发出真实请求',
          description: result.detail ?? result.problems?.[0] ?? '模型配置不完整',
          tone: 'danger',
        });
      }
    },
    onError: (cause) => {
      const info = describeError(cause);
      toast({ title: '测试请求失败', description: info.message, tone: 'danger', requestId: info.requestId });
    },
  });

  const credentialOptions = React.useMemo<SelectOption[]>(() => {
    const items = credentials.data?.items ?? [];
    const options: SelectOption[] = [
      { value: AUTO_CREDENTIAL, label: '自动选择（按 base_url 匹配）' },
      ...items.map((item) => ({
        value: item.id,
        label: `${item.name} · ${item.provider_name ?? '未知提供商'} · ${item.secret_mask}`,
        hint: item.status,
      })),
    ];
    if (form.credentialId !== '' && !items.some((item) => item.id === form.credentialId)) {
      options.push({
        value: form.credentialId,
        label: `当前凭证（不在前 100 条里）：${form.credentialId}`,
      });
    }
    return options;
  }, [credentials.data, form.credentialId]);

  const isDirty =
    baseline !== null &&
    (form.provider !== baseline.provider ||
      form.model !== baseline.model ||
      form.baseUrl !== baseline.baseUrl ||
      form.credentialId !== baseline.credentialId ||
      form.maxTokens !== baseline.maxTokens);

  function submit(): void {
    const maxTokensRaw = form.maxTokens.trim();
    if (maxTokensRaw !== '') {
      const parsed = Number(maxTokensRaw);
      if (!Number.isInteger(parsed) || parsed < 1 || parsed > 1_000_000) {
        setMaxTokensError('max_tokens 必须是 1-1000000 的整数');
        return;
      }
    }
    setMaxTokensError(null);
    save.mutate({
      provider: form.provider,
      model: form.model.trim(),
      base_url: form.baseUrl.trim(),
      credential_id: form.credentialId,
      ...(maxTokensRaw === '' ? {} : { max_tokens: Number(maxTokensRaw) }),
    });
  }

  if (config.isLoading) {
    return (
      <div className="flex flex-col gap-4" aria-busy="true">
        <Skeleton height={64} rounded="md" />
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
        title={info.code === 'NOT_FOUND' ? 'Agent 配置接口不存在' : info.message}
        description={info.hint ?? '检查 API 进程是否运行，然后重试。'}
        requestId={info.requestId}
        actionLabel="重试"
        onAction={() => void config.refetch()}
      />
    );
  }

  if (!config.data) return null;

  const effective = config.data.effective;
  const problems = effective.problems ?? [];
  const declaredProviders = config.data.providers ?? [];
  const providerList = declaredProviders.length > 0 ? declaredProviders : ['anthropic', 'openai', 'google'];
  const effectiveCredential = effective.credential;
  const effectiveCredentialText = effectiveCredential
    ? `${effectiveCredential.name} · ${effectiveCredential.provider_name ?? '未知提供商'} · ${effectiveCredential.secret_mask ?? '—'}`
    : '未解析到凭证';
  const credentialRefEntries = Object.entries(effective.credential_refs);

  // 页面唯一的 role="alert" 区域：保存失败 / 测试请求失败 / 测试结论为失败
  let alertTitle: string | null = null;
  let alertLines: string[] = [];
  if (save.isError) {
    const info = describeError(save.error);
    alertTitle = `保存失败：${info.message}`;
    alertLines = info.hint ? [info.requestId ? `${info.hint} · request_id ${info.requestId}` : info.hint] : [];
  } else if (test.isError) {
    const info = describeError(test.error);
    alertTitle = `测试请求失败：${info.message}`;
    alertLines = info.hint ? [info.requestId ? `${info.hint} · request_id ${info.requestId}` : info.hint] : [];
  } else if (testResult && !testResult.ok) {
    alertTitle = testResult.checked
      ? '连通性测试失败：上游没有正常回话'
      : '测试未发出真实请求：模型配置还不完整';
    alertLines = [...(testResult.problems ?? []), ...(testResult.detail ? [testResult.detail] : [])];
  }

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Agent 模型"
        breadcrumbs={[{ label: '设置', href: '/settings' }]}
        description="内置 Agent 用哪个上游模型；没有配置时 Agent 面板发不出消息。改动保存后立即生效（发轮次时按这里的解析结果）。"
        actions={
          <>
            <Button
              variant="primary"
              size="sm"
              loading={save.isPending}
              onClick={submit}
              icon={<Save size={16} strokeWidth={1.75} />}
            >
              {save.isPending ? '保存中…' : '保存'}
            </Button>
            <Button
              variant="secondary"
              size="sm"
              loading={test.isPending}
              onClick={() => test.mutate()}
              icon={<PlugZap size={16} strokeWidth={1.75} />}
            >
              {test.isPending ? '测试中…' : '测试连通性'}
            </Button>
          </>
        }
        status={
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={effective.ready ? 'ok' : 'warn'} dot>
              {effective.ready ? '就绪' : '未就绪'}
            </Badge>
            {effective.model ? (
              <>
                <Badge tone="outline" mono>
                  {effective.model.providerId}
                </Badge>
                <Badge tone="outline" mono>
                  {effective.model.modelId}
                </Badge>
              </>
            ) : (
              <Badge tone="neutral">未解析到模型引用</Badge>
            )}
            <Badge tone="neutral">协议族支持 {providerList.join(' / ')}</Badge>
            <Badge tone={credentials.isError ? 'warn' : 'neutral'}>
              可选凭证 {credentials.data?.items.length ?? 0}
            </Badge>
          </div>
        }
      />

      {/* 1. 就绪状态 + 未就绪原因（逐条可见，不塞进 title） */}
      <section aria-label="Agent 模型就绪状态" className="sr-card p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="flex items-center gap-1.5 text-16 font-semibold text-content">
            <Bot size={16} strokeWidth={1.75} aria-hidden />
            就绪状态
          </h2>
          <Badge tone={effective.ready ? 'ok' : 'warn'} dot>
            {effective.ready ? '就绪：Agent 可以发消息' : `未就绪（${problems.length} 个问题）`}
          </Badge>
        </div>
        {effective.ready ? (
          <p className="mt-2 text-13 text-content">
            解析就绪。网关发轮次时会用下面的「当前生效的模型」；改动配置后请重新点一次保存。
          </p>
        ) : (
          <div className="mt-2" aria-live="polite">
            <p className="text-13 text-content">
              未就绪：Agent 面板发不出消息，先按下面这些原因逐条修好再回来。
            </p>
            {problems.length === 0 ? (
              <p className="mt-1 text-12 text-content-muted">
                后端没有返回具体原因；先填模型名并选一个可用凭证，再点「保存」看解析结果。
              </p>
            ) : (
              <ul className="mt-1 flex flex-col gap-0.5 text-12 text-content-muted">
                {problems.map((problem) => (
                  <li key={problem}>· {problem}</li>
                ))}
              </ul>
            )}
          </div>
        )}
        {savedEffective ? (
          <p
            role="status"
            className="mt-3 flex flex-wrap items-center gap-2 rounded border border-border bg-[var(--sr-surface-sunken)] px-3 py-2 text-12 text-content-muted"
          >
            <Badge tone={savedEffective.ready ? 'ok' : 'warn'} dot>
              {savedEffective.ready ? '已保存且就绪' : '已保存但仍未就绪'}
            </Badge>
            <span>保存于 {savedAt ? formatRelative(savedAt) : '刚刚'}（PATCH 返回的 effective）</span>
            {savedEffective.problems.length > 0 ? <span>· {savedEffective.problems.join('；')}</span> : null}
          </p>
        ) : null}
      </section>

      {alertTitle ? (
        <div
          role="alert"
          className="rounded-md border border-danger bg-[var(--sr-danger-weak)] p-4 text-13 text-content"
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

      {/* 2. 表单 */}
      <section aria-label="Agent 模型配置表单" className="sr-card p-4">
        <h2 className="text-16 font-semibold text-content">模型配置</h2>
        <p className="mt-0.5 text-12 text-content-muted">
          协议族决定网关用哪个适配器；模型名与上游地址由该适配器直接透传。保存只写白名单键，未知键会被后端拒绝。
        </p>
        <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
          <FormField
            id="agent-provider"
            label="协议族"
            description="网关按协议族选适配器（anthropic / openai / google），不是按提供商的命名。"
          >
            <Select
              id="agent-provider"
              aria-label="协议族"
              value={form.provider}
              onValueChange={(value) => setForm((prev) => ({ ...prev, provider: value }))}
              options={PROVIDER_OPTIONS}
              placeholder="选择协议族"
            />
          </FormField>

          <FormField
            id="agent-model"
            label="模型名"
            hint="留空 = 清空，Agent 会回到未配置"
            description="必须与上游真实模型 id 一致，例如 glm-5.3-flash / claude-sonnet-4-5 / gpt-4o-mini。"
          >
            <Input
              id="agent-model"
              aria-label="模型名"
              value={form.model}
              onChange={(event) => setForm((prev) => ({ ...prev, model: event.target.value }))}
              placeholder="glm-5.3-flash"
              autoComplete="off"
              spellCheck={false}
            />
          </FormField>

          <FormField
            id="agent-base-url"
            label="上游地址"
            hint="可留空"
            description="留空时用凭证所属提供商的 base_url；手填会覆盖它（只允许 http/https）。"
          >
            <Input
              id="agent-base-url"
              aria-label="上游地址"
              value={form.baseUrl}
              onChange={(event) => setForm((prev) => ({ ...prev, baseUrl: event.target.value }))}
              placeholder={BASE_URL_PLACEHOLDER}
              autoComplete="off"
              spellCheck={false}
              className="sr-mono text-13"
            />
          </FormField>

          <FormField
            id="agent-credential"
            label="凭证"
            description="密文只在服务端解密；这里只显示掩码。空选项 = 按 base_url 自动匹配一个可用 LLM 凭证。"
          >
            <Select
              id="agent-credential"
              aria-label="凭证"
              value={form.credentialId === '' ? AUTO_CREDENTIAL : form.credentialId}
              onValueChange={(value) =>
                setForm((prev) => ({ ...prev, credentialId: value === AUTO_CREDENTIAL ? '' : value }))
              }
              options={credentialOptions}
              placeholder={credentials.isLoading ? '正在加载凭证…' : '自动选择（按 base_url 匹配）'}
              disabled={credentials.isLoading}
            />
          </FormField>

          <FormField
            id="agent-max-tokens"
            label="最大输出 tokens"
            hint="1-1000000"
            error={maxTokensError ?? undefined}
            description="单次回复的 token 上限。注意：网关目前按自己的 AGENT_MODEL_MAX_TOKENS 走，这里只落库、不会改变实际输出；留空则沿用已保存的值。"
          >
            <Input
              id="agent-max-tokens"
              aria-label="最大输出 tokens"
              type="number"
              min={1}
              max={1000000}
              step={1}
              inputMode="numeric"
              value={form.maxTokens}
              onChange={(event) => {
                setForm((prev) => ({ ...prev, maxTokens: event.target.value }));
                setMaxTokensError(null);
              }}
              placeholder="4096"
            />
          </FormField>
        </div>

        {credentials.isError ? (
          <p className="mt-1 text-12 text-content-muted">
            凭证列表读取失败：{describeError(credentials.error).message}。可以直接填 base_url 并保存，或刷新重试。
          </p>
        ) : null}

        <p className="mt-1 text-12 text-content-muted">
          {isDirty ? (
            <>
              表单有未保存的修改：「测试连通性」打的是<strong className="font-medium text-content">已保存</strong>
              的配置，请先点「保存」。
            </>
          ) : (
            '当前表单与已保存配置一致；「测试连通性」用的就是这份配置。'
          )}
        </p>
      </section>

      {/* 3. 当前实际生效的引用（排错用） */}
      <section aria-label="当前生效的模型" className="sr-card p-4">
        <h2 className="flex items-center gap-1.5 text-16 font-semibold text-content">
          <Bot size={16} strokeWidth={1.75} aria-hidden />
          当前生效的模型
        </h2>
        <p className="mt-0.5 text-12 text-content-muted">
          这是后端把设置翻译成网关请求后的引用（effective）。发消息仍失败时先对照这里。
          temperature / max_tokens 只保存不生效：网关按自己的 AGENT_MODEL_* 环境变量决定这两项。
        </p>
        <dl className="mt-2 grid grid-cols-1 gap-1 text-12 sm:grid-cols-2">
          <div>
            <dt className="text-content-muted">协议族（providerId）</dt>
            <dd className="sr-mono break-all text-content">{effective.model?.providerId ?? '—'}</dd>
          </div>
          <div>
            <dt className="text-content-muted">模型（modelId）</dt>
            <dd className="sr-mono break-all text-content">{effective.model?.modelId ?? '—'}</dd>
          </div>
          <div>
            <dt className="text-content-muted">上游地址（baseUrl）</dt>
            <dd className="sr-mono break-all text-content">
              {effective.model?.baseUrl ?? '未设置（用凭证所属提供商的 base_url）'}
            </dd>
          </div>
          <div>
            <dt className="text-content-muted">选中的凭证</dt>
            <dd className="break-all text-content">{effectiveCredentialText}</dd>
          </div>
          <div>
            <dt className="text-content-muted">温度 / 最大输出 tokens（只保存，本轮不生效）</dt>
            <dd className="text-content">
              {config.data.model.temperature ?? '—'} / {config.data.model.max_tokens ?? '—'}
            </dd>
          </div>
          <div>
            <dt className="text-content-muted">凭证引用（credential_refs）</dt>
            <dd className="sr-mono break-all text-content">
              {credentialRefEntries.length === 0
                ? '空（网关会回 no model configured for this turn）'
                : credentialRefEntries.map(([providerId, credentialId]) => `${providerId} → ${credentialId}`).join('、')}
            </dd>
          </div>
        </dl>
        {effectiveCredential ? (
          <p className="mt-2 flex flex-wrap items-center gap-2 text-12 text-content-muted">
            <Badge tone="outline" mono>
              {effectiveCredential.id}
            </Badge>
            <span>凭证 {effectiveCredential.name}</span>
            <span>· 提供商 {effectiveCredential.provider_name ?? '未知'}</span>
            <span>· 掩码 {effectiveCredential.secret_mask ?? '—'}</span>
          </p>
        ) : null}
      </section>

      {/* 4. 连通性测试结果 */}
      <section aria-label="连通性测试结果" className="sr-card p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-16 font-semibold text-content">连通性测试</h2>
          <div className="flex flex-wrap items-center gap-1">
            {testResult ? (
              <Badge tone={testResult.ok ? 'ok' : testResult.checked ? 'danger' : 'warn'} dot>
                {testResult.ok ? '通过' : testResult.checked ? '失败' : '未发出请求'}
              </Badge>
            ) : (
              <Badge tone="neutral">还没测过</Badge>
            )}
          </div>
        </div>
        <p className="mt-0.5 text-12 text-content-muted">
          点「测试连通性」会用已保存的凭证真实打一次上游最小请求（会计费），并把 HTTP 状态与耗时记在这里。
        </p>
        {testResult ? (
          <>
            <dl className="mt-2 grid grid-cols-1 gap-1 text-12 sm:grid-cols-2">
              <div>
                <dt className="text-content-muted">结果</dt>
                <dd className="text-content">{testResult.ok ? '模型正常回话' : '未通过'}</dd>
              </div>
              <div>
                <dt className="text-content-muted">HTTP 状态</dt>
                <dd className="sr-mono text-content">{testResult.status ?? '—'}</dd>
              </div>
              <div>
                <dt className="text-content-muted">耗时</dt>
                <dd className="text-content">{testResult.latency_ms === null || testResult.latency_ms === undefined ? '—' : `${testResult.latency_ms} ms`}</dd>
              </div>
              <div>
                <dt className="text-content-muted">协议族 / 模型</dt>
                <dd className="sr-mono break-all text-content">
                  {testResult.provider ?? '—'} / {testResult.model ?? '—'}
                </dd>
              </div>
              <div className="sm:col-span-2">
                <dt className="text-content-muted">上游地址</dt>
                <dd className="sr-mono break-all text-content">{testResult.base_url ?? '—'}</dd>
              </div>
              {testResult.detail ? (
                <div className="sm:col-span-2">
                  <dt className="text-content-muted">detail</dt>
                  <dd className="break-words text-content">{testResult.detail}</dd>
                </div>
              ) : null}
            </dl>
            {testResult.credential ? (
              <p className="mt-2 text-12 text-content-muted">
                使用凭证 {testResult.credential.name} · {testResult.credential.provider_name ?? '未知提供商'} ·{' '}
                <span className="sr-mono">{testResult.credential.secret_mask ?? '—'}</span>
              </p>
            ) : null}
            {testResult.problems && testResult.problems.length > 0 ? (
              <ul className="mt-1 flex flex-col gap-0.5 text-12 text-content-muted">
                {testResult.problems.map((problem) => (
                  <li key={problem}>· {problem}</li>
                ))}
              </ul>
            ) : null}
          </>
        ) : (
          <p className="mt-2 text-13 text-content-muted">
            还没有测试记录。配置齐了以后点一次「测试连通性」，通过才说明这个凭证 + 这个模型 + 这个地址真的能用。
          </p>
        )}
      </section>
    </div>
  );
}
