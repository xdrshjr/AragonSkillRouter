'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, ArrowRight, Bot, CircleAlert, FileJson, PenLine } from 'lucide-react';
import { useRouter } from 'next/navigation';
import * as React from 'react';
import { FormField } from '@/components/common/FormField';
import { PageHeader } from '@/components/common/PageHeader';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/components/ui/cn';
import { Input } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import { Textarea } from '@/components/ui/textarea';
import { useToast } from '@/components/ui/toast';
import { apiGet, apiPost, describeError } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import type { CallMode, Capability, JsonObject, Paginated, Provider, ProviderDetail } from '@/lib/types';

/**
 * 新建能力向导（spec §13.2 `/capabilities/new`）：
 * 三选一入口（手工配置 / 从 OpenAPI 导入 / 交给 Agent），每步可返回上一步，
 * 离开有未保存提示（beforeunload）。最终只创建**草稿**，不伪造发布。
 */

type EntryMethod = 'manual' | 'openapi' | 'agent';

const KINDS = [
  { value: 'text.generate', label: '文本生成（text.generate）' },
  { value: 'image.generate', label: '图像生成（image.generate）' },
  { value: 'video.generate', label: '视频生成（video.generate）' },
  { value: 'audio.speech', label: '语音合成（audio.speech）' },
  { value: 'audio.transcribe', label: '语音转写（audio.transcribe）' },
  { value: 'generic.http', label: '通用 HTTP（generic.http）' },
];

const MODES: { value: CallMode; label: string }[] = [
  { value: 'sync', label: 'sync（同步）' },
  { value: 'async', label: 'async（异步任务）' },
  { value: 'stream', label: 'stream（流式）' },
];

interface OpenApiOperation {
  operationId?: string;
  path: string;
  method: string;
  summary?: string;
  requestContentType?: string;
}

function extractOperations(spec: JsonObject): OpenApiOperation[] {
  const paths = spec['paths'];
  if (!paths || typeof paths !== 'object' || Array.isArray(paths)) return [];
  const result: OpenApiOperation[] = [];
  for (const [path, methodsRaw] of Object.entries(paths as Record<string, unknown>)) {
    if (!methodsRaw || typeof methodsRaw !== 'object' || Array.isArray(methodsRaw)) continue;
    for (const [method, operationRaw] of Object.entries(methodsRaw as Record<string, unknown>)) {
      if (!operationRaw || typeof operationRaw !== 'object') continue;
      const operation = operationRaw as Record<string, unknown>;
      const requestBody = operation['requestBody'];
      let contentType: string | undefined;
      if (requestBody && typeof requestBody === 'object') {
        const content = (requestBody as Record<string, unknown>)['content'];
        if (content && typeof content === 'object') {
          const first = Object.keys(content as Record<string, unknown>)[0];
          contentType = first;
        }
      }
      result.push({
        path,
        method: method.toUpperCase(),
        operationId: typeof operation['operationId'] === 'string' ? operation['operationId'] : undefined,
        summary: typeof operation['summary'] === 'string' ? operation['summary'] : undefined,
        requestContentType: contentType,
      });
    }
  }
  return result;
}

export default function NewCapabilityPage() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const { toast } = useToast();

  const [step, setStep] = React.useState<1 | 2 | 3>(1);
  const [method, setMethod] = React.useState<EntryMethod | null>(null);
  const [dirty, setDirty] = React.useState(false);
  const [error, setError] = React.useState<{ message: string; requestId: string | null; code: string } | null>(null);

  // 基础信息
  const [key, setKey] = React.useState('');
  const [name, setName] = React.useState('');
  const [kind, setKind] = React.useState('generic.http');
  const [modes, setModes] = React.useState<CallMode[]>(['sync']);
  const [compatGroup, setCompatGroup] = React.useState('');

  // 路由
  const [providerId, setProviderId] = React.useState('');
  const [credentialId, setCredentialId] = React.useState('');
  const [urlTemplate, setUrlTemplate] = React.useState('{base_url}/v1/…');
  const [method2, setMethod2] = React.useState('POST');

  // OpenAPI
  const [specText, setSpecText] = React.useState('');
  const [specError, setSpecError] = React.useState<string | null>(null);
  const [operations, setOperations] = React.useState<OpenApiOperation[]>([]);
  const [selectedOperation, setSelectedOperation] = React.useState<OpenApiOperation | null>(null);

  const providers = useQuery<Paginated<Provider>>({
    queryKey: queryKeys.providers.list({ page: 1, page_size: 100, sort: 'name' }),
    queryFn: () => apiGet<Paginated<Provider>>('/admin/providers', { page: 1, page_size: 100, sort: 'name' }),
  });

  const providerDetail = useQuery<ProviderDetail>({
    queryKey: queryKeys.providers.detail(providerId || 'none'),
    queryFn: () => apiGet<ProviderDetail>(`/admin/providers/${providerId}`),
    enabled: Boolean(providerId),
  });

  // 未保存离开提示
  React.useEffect(() => {
    if (!dirty) return;
    const handler = (event: BeforeUnloadEvent): void => {
      event.preventDefault();
      event.returnValue = '有未保存的配置，确定离开？';
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [dirty]);

  const parsedSpec = (): JsonObject | null => {
    try {
      const value = JSON.parse(specText) as unknown;
      if (!value || typeof value !== 'object' || Array.isArray(value)) {
        setSpecError('OpenAPI 文档顶层必须是 JSON 对象');
        return null;
      }
      setSpecError(null);
      return value as JsonObject;
    } catch (cause) {
      setSpecError(cause instanceof Error ? cause.message : 'JSON 解析失败');
      return null;
    }
  };

  const createDraft = useMutation({
    mutationFn: async () => {
      const definition: JsonObject = {
        key,
        name,
        kind,
        modes,
        file_io: { input: [], output: [] },
        input_schema: {
          $schema: 'https://json-schema.org/draft/2020-12/schema',
          type: 'object',
          properties: { prompt: { type: 'string' } },
          required: ['prompt'],
          additionalProperties: false,
        },
        // 平台规则（spec §7.6 规则 4，见规划文档 I10）要求 input_schema 与 output_schema
        // 的 additionalProperties 都必须是 false。这里曾写成 additionalProperties: true，
        // 结果向导建出来的草稿在「发布」时必然 400 INPUT_SCHEMA_INVALID，
        // 用户只能去 Schema 页手改才能继续。
        output_schema: {
          $schema: 'https://json-schema.org/draft/2020-12/schema',
          type: 'object',
          properties: {},
          additionalProperties: false,
        },
        examples: [{ title: '最小示例', input: { prompt: '你好' } }],
        compatibility_group: compatGroup || null,
        failover: { enabled: true, max_switches: 2 },
        tags: [],
        routes: [
          {
            id: 'rt_primary',
            provider_id: providerId,
            credential_id: credentialId,
            priority: 0,
            enabled: true,
            model_hint: null,
            request: {
              method: method2,
              url_template: urlTemplate,
              encoding: 'json',
              headers: { 'Content-Type': 'application/json' },
            },
            param_map: {},
            response_map: { format: 'json', jsonpath: '$' },
            limits: {},
            errors: [],
            timeout_ms: 60000,
            retry: { max_attempts: 3, base_ms: 500, max_ms: 8000 },
          },
        ],
      };
      return apiPost<Capability>('/admin/capabilities', {
        key,
        name,
        kind,
        modes,
        definition,
        source: method ?? 'manual',
        openapi_operation: selectedOperation
          ? { method: selectedOperation.method, path: selectedOperation.path }
          : null,
      });
    },
    onSuccess: async (created) => {
      setDirty(false);
      toast({ title: '草稿已创建', description: '下一步：补齐 Schema 与映射，然后跑一次真实测试。', tone: 'ok' });
      await queryClient.invalidateQueries({ queryKey: queryKeys.capabilities.all });
      router.push(`/capabilities/${created.id}`);
    },
    onError: (cause) => {
      const info = describeError(cause);
      setError({ message: info.message, requestId: info.requestId, code: info.code });
      toast({
        title: '创建草稿失败',
        description: info.hint ? `${info.message}（${info.hint}）` : info.message,
        tone: 'danger',
        requestId: info.requestId,
      });
    },
  });

  const keyValid = /^[a-z0-9]+(\.[a-z0-9][a-z0-9-]*)+$/.test(key);
  const canContinueStep2 = keyValid && name.trim().length > 0 && modes.length > 0;
  const canCreate = canContinueStep2 && Boolean(providerId) && Boolean(credentialId);

  const credentialOptions = (providerDetail.data?.credentials ?? []).map((credential) => ({
    value: credential.id,
    label: `${credential.name}（${credential.status}）`,
    disabled: credential.status !== 'active',
  }));

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="新建能力"
        breadcrumbs={[{ label: '能力', href: '/capabilities' }]}
        description="三种入口任选其一；创建的是草稿，发布需要测试证据与你手动确认。"
        status={
          <ol className="flex flex-wrap items-center gap-2 text-12" aria-label="向导步骤">
            {[1, 2, 3].map((index) => (
              <li key={index} className="flex items-center gap-1">
                <span
                  className={cn(
                    'flex h-5 w-5 items-center justify-center rounded-full border text-12',
                    step === index
                      ? 'border-accent bg-[var(--sr-accent-weak)] text-accent'
                      : step > index
                        ? 'border-ok text-ok'
                        : 'border-border text-content-muted',
                  )}
                >
                  {index}
                </span>
                <span className={step === index ? 'text-content' : 'text-content-muted'}>
                  {index === 1 ? '选择入口' : index === 2 ? '基础信息' : '首条路由'}
                </span>
                {index < 3 ? <ArrowRight size={12} strokeWidth={1.75} aria-hidden /> : null}
              </li>
            ))}
          </ol>
        }
      />

      {error ? (
        <div role="alert" className="rounded border border-danger bg-[var(--sr-danger-weak)] px-3 py-2 text-13">
          <p className="text-content">{error.message}</p>
          {error.requestId ? (
            <p className="mt-1 break-all text-12 text-content-muted">request_id: {error.requestId}</p>
          ) : null}
        </div>
      ) : null}

      {/* 第 1 步：入口 */}
      {step === 1 ? (
        <section aria-label="选择入口" className="grid grid-cols-1 gap-3 md:grid-cols-3">
          {(
            [
              {
                id: 'manual' as EntryMethod,
                icon: <PenLine size={24} strokeWidth={1.75} aria-hidden />,
                title: '手工配置',
                detail: '已知端点与参数，直接填 Schema、映射与路由。',
              },
              {
                id: 'openapi' as EntryMethod,
                icon: <FileJson size={24} strokeWidth={1.75} aria-hidden />,
                title: '从 OpenAPI 导入',
                detail: '粘贴规范（或 URL 抓取），选取一个操作自动预填。',
              },
              {
                id: 'agent' as EntryMethod,
                icon: <Bot size={24} strokeWidth={1.75} aria-hidden />,
                title: '交给 Agent',
                detail: '让 Agent 读文档并生成可复核草稿，七步流程留证据。',
              },
            ] as const
          ).map((option) => (
            <button
              key={option.id}
              type="button"
              aria-pressed={method === option.id}
              onClick={() => {
                setMethod(option.id);
                setDirty(true);
              }}
              className={cn(
                'flex flex-col items-start gap-2 rounded-md border p-4 text-left transition-colors duration-150 ease-sr',
                'focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]',
                method === option.id
                  ? 'border-accent bg-[var(--sr-accent-weak)]'
                  : 'border-border bg-surface hover:bg-hover',
              )}
            >
              <span className="text-content-muted">{option.icon}</span>
              <span className="text-14 font-medium text-content">{option.title}</span>
              <span className="text-12 text-content-muted">{option.detail}</span>
            </button>
          ))}
        </section>
      ) : null}

      {/* 第 2 步：基础信息 */}
      {step === 2 ? (
        <section aria-label="基础信息" className="sr-card grid grid-cols-1 gap-3 p-4 md:grid-cols-2">
          <FormField
            id="key"
            label="能力 key"
            required
            hint="小写点分，如 text.generate.gpt4o-mini"
            error={key && !keyValid ? '格式不合法：需小写点分且以字母数字开头' : undefined}
            className="md:col-span-2"
          >
            <Input
              id="key"
              value={key}
              onChange={(event) => {
                setKey(event.target.value);
                setDirty(true);
              }}
              invalid={Boolean(key) && !keyValid}
              className="sr-mono"
            />
          </FormField>
          <FormField id="name" label="名称" required className="md:col-span-2">
            <Input
              id="name"
              value={name}
              onChange={(event) => {
                setName(event.target.value);
                setDirty(true);
              }}
            />
          </FormField>
          <FormField id="kind" label="类型" required>
            <Select
              value={kind}
              onValueChange={(value) => {
                setKind(value);
                setDirty(true);
              }}
              options={KINDS}
            />
          </FormField>
          <FormField id="compat" label="兼容组" hint="可选；同组能力才可互为故障切换候选">
            <Input
              value={compatGroup}
              onChange={(event) => {
                setCompatGroup(event.target.value);
                setDirty(true);
              }}
            />
          </FormField>
          <fieldset className="md:col-span-2">
            <legend className="text-13 font-medium text-content">调用模式（可多选，至少一个）</legend>
            <div className="mt-1 flex flex-wrap gap-2">
              {MODES.map((mode) => {
                const active = modes.includes(mode.value);
                return (
                  <button
                    key={mode.value}
                    type="button"
                    aria-pressed={active}
                    onClick={() => {
                      setModes((prev) =>
                        prev.includes(mode.value)
                          ? prev.filter((item) => item !== mode.value)
                          : [...prev, mode.value],
                      );
                      setDirty(true);
                    }}
                    className={cn(
                      'rounded-sm border px-2 py-1 text-13 focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]',
                      active
                        ? 'border-accent bg-[var(--sr-accent-weak)] text-accent'
                        : 'border-border text-content-muted hover:bg-hover',
                    )}
                  >
                    {mode.label}
                  </button>
                );
              })}
            </div>
            {modes.includes('async') || modes.includes('stream') ? (
              <p className="mt-1 flex items-start gap-1 text-12 text-content-muted">
                <CircleAlert size={12} strokeWidth={1.75} className="mt-0.5" aria-hidden />
                {modes.includes('stream')
                  ? 'stream 要求至少一条启用 route 声明 stream.protocol 与 delta_path。'
                  : 'async 要求至少一条启用 route 同时提供 async.submit 与 async.query。'}
              </p>
            ) : null}
          </fieldset>
        </section>
      ) : null}

      {/* 第 3 步：首条路由 / OpenAPI / Agent */}
      {step === 3 ? (
        <div className="flex flex-col gap-3">
          {method === 'openapi' ? (
            <section aria-label="OpenAPI 导入" className="sr-card p-4">
              <h2 className="text-16 font-semibold text-content">粘贴 OpenAPI 文档</h2>
              <p className="mt-0.5 text-12 text-content-muted">
                支持 JSON 规范。会列出全部操作供你选一个作为该能力的首个端点。
              </p>
              <Textarea
                value={specText}
                onChange={(event) => setSpecText(event.target.value)}
                rows={8}
                spellCheck={false}
                aria-label="OpenAPI JSON"
                placeholder='{"openapi":"3.1.0","paths":{"/v1/chat/completions":{"post":{...}}}}'
                className="mt-2 sr-mono text-12"
              />
              {specError ? <p className="mt-1 text-12 text-danger">{specError}</p> : null}
              <div className="mt-2 flex items-center gap-2">
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => {
                    const spec = parsedSpec();
                    if (!spec) return;
                    setOperations(extractOperations(spec));
                  }}
                >
                  解析操作
                </Button>
                <span className="text-12 text-content-muted">已解析 {operations.length} 个操作</span>
              </div>
              {operations.length > 0 ? (
                <ul className="mt-2 max-h-56 divide-y divide-[var(--sr-border)] overflow-y-auto rounded border border-border sr-scroll">
                  {operations.map((operation) => {
                    const active =
                      selectedOperation?.path === operation.path && selectedOperation.method === operation.method;
                    return (
                      <li key={`${operation.method}-${operation.path}`}>
                        <button
                          type="button"
                          aria-pressed={active}
                          onClick={() => {
                            setSelectedOperation(operation);
                            setUrlTemplate(`{base_url}${operation.path}`);
                            setMethod2(operation.method);
                            setDirty(true);
                          }}
                          className={cn(
                            'flex w-full items-center gap-2 px-2 py-1.5 text-left text-13 focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]',
                            active ? 'bg-[var(--sr-accent-weak)] text-accent' : 'hover:bg-hover',
                          )}
                        >
                          <Badge tone="outline" mono>
                            {operation.method}
                          </Badge>
                          <span className="sr-mono">{operation.path}</span>
                          {operation.summary ? (
                            <span className="truncate text-content-muted">{operation.summary}</span>
                          ) : null}
                        </button>
                      </li>
                    );
                  })}
                </ul>
              ) : null}
            </section>
          ) : null}

          {method === 'agent' ? (
            <section aria-label="交给 Agent" className="sr-card p-4">
              <h2 className="text-16 font-semibold text-content">交给 Agent 处理</h2>
              <p className="mt-1 text-13 text-content-muted">
                Agent 会创建七步接入流程（解析 → 生成草稿 → 校验 → 测试 → 样例 → 检查 → 发布），
                每一步都留证据；涉及越权或超预算的操作会先请求你审批。
              </p>
              <ul className="mt-2 flex flex-col gap-1 text-13 text-content-muted">
                <li>· 草稿由 Agent 生成，但你仍需在发布前复核 Schema 与映射。</li>
                <li>· 过程中真实产生的费用与调用会记入调用记录。</li>
              </ul>
              <Button
                variant="primary"
                size="sm"
                className="mt-3"
                onClick={() => router.push('/agent')}
                icon={<Bot size={16} strokeWidth={1.75} />}
              >
                去 Agent 工作台
              </Button>
            </section>
          ) : (
            <section aria-label="首条路由" className="sr-card grid grid-cols-1 gap-3 p-4 md:grid-cols-2">
              <FormField id="provider" label="提供商" required>
                <Select
                  value={providerId}
                  onValueChange={(value) => {
                    setProviderId(value);
                    setCredentialId('');
                    setDirty(true);
                  }}
                  placeholder={providers.isLoading ? '加载中…' : '选择提供商'}
                  options={(providers.data?.items ?? []).map((provider) => ({
                    value: provider.id,
                    label: provider.name,
                  }))}
                />
              </FormField>
              <FormField
                id="credential"
                label="凭证"
                required
                hint={providerId && credentialOptions.length === 0 ? '该提供商暂无凭证，先去添加' : undefined}
              >
                <Select
                  value={credentialId}
                  onValueChange={(value) => {
                    setCredentialId(value);
                    setDirty(true);
                  }}
                  disabled={credentialOptions.length === 0}
                  placeholder={credentialOptions.length === 0 ? '暂无可用凭证' : '选择凭证'}
                  options={credentialOptions}
                />
              </FormField>
              <FormField id="http-method" label="HTTP 方法">
                <Select
                  value={method2}
                  onValueChange={(value) => {
                    setMethod2(value);
                    setDirty(true);
                  }}
                  options={['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].map((value) => ({ value, label: value }))}
                />
              </FormField>
              <FormField
                id="url-template"
                label="请求地址模板"
                required
                hint="必须含 {base_url} 或命中出站白名单"
              >
                <Input
                  value={urlTemplate}
                  onChange={(event) => {
                    setUrlTemplate(event.target.value);
                    setDirty(true);
                  }}
                  className="sr-mono"
                />
              </FormField>
            </section>
          )}
        </div>
      ) : null}

      {/* 底部导航 */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border pt-3">
        <Button
          variant="secondary"
          size="sm"
          onClick={() => setStep((prev) => (prev === 1 ? prev : ((prev - 1) as 1 | 2 | 3)))}
          disabled={step === 1}
          icon={<ArrowLeft size={16} strokeWidth={1.75} />}
        >
          上一步
        </Button>
        <div className="flex items-center gap-2">
          {step === 2 && !canContinueStep2 ? (
            <span className="text-12 text-warn">请先补全合法的 key、名称与至少一个模式</span>
          ) : null}
          {step === 3 && method !== 'agent' && !canCreate ? (
            <span className="text-12 text-warn">需要选择提供商与可用凭证</span>
          ) : null}
          {step < 3 ? (
            <Button
              variant="primary"
              size="sm"
              disabled={step === 1 ? !method : !canContinueStep2}
              onClick={() => setStep((prev) => ((prev + 1) as 1 | 2 | 3))}
            >
              下一步
              <ArrowRight size={16} strokeWidth={1.75} aria-hidden />
            </Button>
          ) : method === 'agent' ? null : (
            <Button
              variant="primary"
              size="sm"
              loading={createDraft.isPending}
              disabled={!canCreate}
              onClick={() => createDraft.mutate()}
            >
              创建草稿
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
