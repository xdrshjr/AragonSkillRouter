'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowRight, ExternalLink, KeyRound, Plus, Trash2 } from 'lucide-react';
import Link from 'next/link';
import { useParams, useSearchParams } from 'next/navigation';
import * as React from 'react';
import { zodResolver } from '@hookform/resolvers/zod';
import { Controller, useForm } from 'react-hook-form';
import { z } from 'zod';
import { DangerConfirm } from '@/components/common/DangerConfirm';
import { EmptyState } from '@/components/common/EmptyState';
import { FormField } from '@/components/common/FormField';
import { PageHeader } from '@/components/common/PageHeader';
import { StatusBadge } from '@/components/common/StatusBadge';
import {
  CredentialProtocolFields,
  type CredentialProtocolFieldName,
  type CredentialProtocolFieldsValue,
} from '@/components/llm/CredentialProtocolFields';
import { LLM_STAGE_LABELS, type LlmProbeTransportError } from '@/components/llm/ConnectionTester';
import { buildProbeTarget } from '@/components/llm/useLlmDraft';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Drawer, DrawerContent } from '@/components/ui/drawer';
import { Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import { useToast } from '@/components/ui/toast';
import { apiDelete, apiGet, apiPost, describeError } from '@/lib/api';
import { formatDuration, formatRelative } from '@/lib/format';
import { getProtocols, testConnection } from '@/lib/llm';
import { queryKeys } from '@/lib/query';
import type { Credential, LlmProbeResponse, ProviderDetail } from '@/lib/types';

/**
 * 凭证表单 schema（spec §3.8 表单状态归属 / R-19）。
 *
 * `protocol` / `base_url` / `auth_style` / `header_name`（以及 `wire` / 路径覆盖）
 * 全部声明在**这一份** schema 里，抽屉里的 `CredentialProtocolFields`
 * 通过 `Controller` + `setValue` 注册进同一份表单——抽屉里不存在第二个 useState 表单真相源。
 */
const credentialSchema = z.object({
  name: z.string().min(1, '请输入凭证名称').max(128),
  auth_type: z.string().min(1),
  header_name: z.string().max(64),
  secret: z.string().min(1, '请粘贴密钥（不会回显）'),
  expires_at: z.string(),
  /** 协议 key（来自 `GET /admin/llm/protocols`） */
  protocol: z.string(),
  /** 提供商拥有 base_url；这里填的只作为 meta.base_url_hint 记录 */
  base_url: z
    .string()
    .refine(
      (value) => value.trim() === '' || /^https?:\/\/\S+$/i.test(value.trim()),
      '地址必须是 http(s) URL，或留空用提供商的 base_url',
    ),
  auth_style: z.string(),
  wire: z.string(),
  paths_models: z.string(),
  paths_chat: z.string(),
});

type CredentialForm = z.infer<typeof credentialSchema>;

/**
 * 抽屉里只有「认证方式」一个控件（`CredentialProtocolFields`），落库的 `auth_type`
 * 由它派生。`none`（内网免鉴权）在凭证模型里没有对应枚举，按 `api_key` 落库并保留密钥。
 */
const AUTH_STYLE_TO_AUTH_TYPE: Record<string, string> = {
  bearer: 'bearer',
  api_key: 'api_key',
  basic: 'basic',
  header: 'header',
  none: 'api_key',
};

/** 提供商详情（spec §13.2）：凭证列表、能力列表、关联图；无凭证时提示先添加。 */
export default function ProviderDetailPage() {
  // `useSearchParams()` 需要在 Suspense 边界内（Next 15 静态预渲染约定）
  return (
    <React.Suspense fallback={<ProviderDetailSkeleton />}>
      <ProviderDetailContent />
    </React.Suspense>
  );
}

function ProviderDetailSkeleton() {
  return (
    <div className="flex flex-col gap-4" aria-busy="true">
      <Skeleton height={64} rounded="md" />
      <Skeleton height={180} rounded="md" />
      <Skeleton height={180} rounded="md" />
    </div>
  );
}

function ProviderDetailContent() {
  const params = useParams<{ id: string }>();
  const providerId = params?.id ?? '';
  const search = useSearchParams();
  const queryClient = useQueryClient();
  const { toast } = useToast();

  const [credentialOpen, setCredentialOpen] = React.useState(false);
  const [deleteOpen, setDeleteOpen] = React.useState(false);
  /** 抽屉内的「立即测试」结论（表单元数据，不是第二份表单值） */
  const [probeResult, setProbeResult] = React.useState<LlmProbeResponse | null>(null);
  const [probeError, setProbeError] = React.useState<LlmProbeTransportError | null>(null);
  const [probeRunning, setProbeRunning] = React.useState(false);

  const detail = useQuery<ProviderDetail>({
    queryKey: queryKeys.providers.detail(providerId),
    queryFn: () => apiGet<ProviderDetail>(`/admin/providers/${providerId}`),
    enabled: Boolean(providerId),
  });

  const protocols = useQuery({
    queryKey: queryKeys.llm.protocols(),
    queryFn: ({ signal }) => getProtocols(signal),
  });

  const form = useForm<CredentialForm>({
    resolver: zodResolver(credentialSchema),
    defaultValues: {
      name: '',
      auth_type: 'api_key',
      header_name: '',
      secret: '',
      expires_at: '',
      protocol: '',
      base_url: '',
      auth_style: '',
      wire: '',
      paths_models: '',
      paths_chat: '',
    },
  });

  /* ── `?createCredential=1&protocol=<p>&base_url=<u>`：打开抽屉并预选协议（从 /settings/agent 跳回来） ── */
  const createFlag = search?.get('createCredential') ?? '';
  const protocolParam = (search?.get('protocol') ?? '').trim();
  const baseUrlParam = (search?.get('base_url') ?? '').trim();

  React.useEffect(() => {
    if (createFlag !== '1') return;
    setCredentialOpen(true);
    setProbeResult(null);
    setProbeError(null);
    if (protocolParam !== '') form.setValue('protocol', protocolParam, { shouldDirty: false });
    // 设置页把「用户填过的地址」一并带过来：否则新建的凭证会退回提供商地址
    if (baseUrlParam !== '') form.setValue('base_url', baseUrlParam, { shouldDirty: false });
    // 只在 URL 参数变化时跑一次（`form` 每次渲染都是同一个实例）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [createFlag, protocolParam, baseUrlParam]);

  // 协议元数据到齐后补齐认证方式与地址：值全部写回 RHF（单一真相源）
  const selectedProtocol = form.watch('protocol');
  const protocolSpec = React.useMemo(
    () => protocols.data?.items.find((item) => item.key === selectedProtocol) ?? null,
    [protocols.data, selectedProtocol],
  );

  React.useEffect(() => {
    if (!protocolSpec) return;
    if (form.getValues('auth_style').trim() === '') {
      form.setValue('auth_style', protocolSpec.default_auth_style, { shouldDirty: false });
    }
    if (form.getValues('wire').trim() === '' && protocolSpec.wire !== null) {
      form.setValue('wire', protocolSpec.wire, { shouldDirty: false });
    }
    // 提供商拥有 base_url：抽屉里留空即用提供商地址，这里只把已知值预填出来给人看
    const providerBaseUrl = detail.data?.base_url ?? '';
    if (providerBaseUrl !== '' && form.getValues('base_url').trim() === '') {
      form.setValue('base_url', providerBaseUrl, { shouldDirty: false });
    }
  }, [protocolSpec, detail.data, form]);

  /** 把组件回抛的补丁写回同一份 RHF 表单（不做本地 state 中转） */
  const applyProtocolPatch = React.useCallback(
    (patch: Partial<CredentialProtocolFieldsValue>) => {
      for (const key of Object.keys(patch) as CredentialProtocolFieldName[]) {
        const value = patch[key];
        if (typeof value !== 'string') continue;
        form.setValue(key, value, { shouldDirty: true, shouldValidate: false });
      }
    },
    [form],
  );

  /** 抽屉内的「立即测试」：草稿也能测，只用当前表单值（spec §3.8 ①③） */
  const runDrawerTest = React.useCallback(async () => {
    setProbeError(null);
    setProbeRunning(true);
    try {
      const values = form.getValues();
      const target = buildProbeTarget(values, {
        mode: 'auth',
        secret: values.secret,
        protocols: protocols.data?.items,
      });
      const result = await testConnection(target, { mode: 'auth' });
      setProbeResult(result);
    } catch (cause) {
      const info = describeError(cause);
      setProbeError({
        code: info.code,
        message: info.message,
        hint: info.hint,
        requestId: info.requestId,
      });
    } finally {
      setProbeRunning(false);
    }
  }, [form, protocols.data]);

  const createCredential = useMutation({
    mutationFn: async (values: CredentialForm) => {
      const protocol = values.protocol.trim();
      const baseUrlHint = values.base_url.trim();
      // `meta` 是**唯一**能被 `services/llm/identity.py` 读到的落库位置：
      // 协议 → `llm_protocol`、线协议 → `llm_wire`、路径覆盖 → `llm_paths`
      // （后端读的就是这三个键；只收集不落库等于用户配了白配）
      const meta: Record<string, unknown> = {};
      if (protocol !== '') meta.llm_protocol = protocol;
      // 提供商拥有 base_url：这里填的地址只作为提示随 meta 落库，不覆盖提供商地址
      if (baseUrlHint !== '') meta.base_url_hint = baseUrlHint;
      const wire = values.wire.trim();
      // 只有「协议不绑定线协议」（custom）时 wire 才有意义；其它协议由后端注册表固定
      if (wire !== '' && protocolSpec?.wire === null) meta.llm_wire = wire;
      const pathsModels = values.paths_models.trim();
      const pathsChat = values.paths_chat.trim();
      if (pathsModels !== '' || pathsChat !== '') {
        meta.llm_paths = {
          ...(pathsModels === '' ? {} : { models: pathsModels }),
          ...(pathsChat === '' ? {} : { chat: pathsChat }),
        };
      }
      return apiPost<Credential>(`/admin/providers/${providerId}/credentials`, {
        name: values.name,
        kind: detail.data?.category ?? 'llm',
        auth_type: AUTH_STYLE_TO_AUTH_TYPE[values.auth_style] ?? values.auth_type ?? 'api_key',
        header_name: values.header_name || null,
        secret: values.secret,
        expires_at: values.expires_at || null,
        ...(Object.keys(meta).length > 0 ? { meta } : {}),
      });
    },
    onSuccess: async () => {
      toast({ title: '凭证已添加', description: '密钥已加密存储，页面之后只显示掩码。', tone: 'ok' });
      setCredentialOpen(false);
      setProbeResult(null);
      setProbeError(null);
      form.reset();
      await queryClient.invalidateQueries({ queryKey: queryKeys.providers.detail(providerId) });
      await queryClient.invalidateQueries({ queryKey: queryKeys.providers.all });
      // /settings/agent 的凭证下拉用 `queryKeys.credentials.all` 作前缀，
      // 所以必须失效这一族（只失效 `llm.all` 前缀不匹配，等于没失效）
      await queryClient.invalidateQueries({ queryKey: queryKeys.credentials.all });
      await queryClient.invalidateQueries({ queryKey: queryKeys.llm.all });
    },
    onError: (error) => {
      const info = describeError(error);
      toast({ title: '添加凭证失败', description: info.message, tone: 'danger', requestId: info.requestId });
    },
  });

  const deleteProvider = useMutation({
    mutationFn: async () => apiDelete<null>(`/admin/providers/${providerId}`),
    onSuccess: async () => {
      toast({ title: '提供商已删除', tone: 'ok' });
      window.location.assign('/providers');
    },
    onError: (error) => {
      const info = describeError(error);
      toast({
        title: info.code === 'PROVIDER_IN_USE' ? '仍有关联对象' : '删除失败',
        description: info.hint ? `${info.message}（${info.hint}）` : info.message,
        tone: 'danger',
        requestId: info.requestId,
      });
    },
  });

  if (detail.isLoading) {
    return <ProviderDetailSkeleton />;
  }

  if (detail.isError) {
    const info = describeError(detail.error);
    return (
      <EmptyState
        kind={info.code === 'NOT_FOUND' ? 'empty' : info.code === 'NETWORK_OFFLINE' ? 'offline' : 'unavailable'}
        title={info.code === 'NOT_FOUND' ? '提供商不存在或已被删除' : info.message}
        description={info.hint ?? undefined}
        requestId={info.requestId}
        actionLabel="返回列表"
        onAction={() => window.location.assign('/providers')}
      />
    );
  }

  const provider = detail.data;
  if (!provider) return null;

  const credentials = provider.credentials ?? [];
  const capabilities = provider.capabilities ?? [];
  const associationCount = credentials.length + capabilities.length;
  const activeCredentials = credentials.filter((credential) => credential.status === 'active').length;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={provider.name}
        breadcrumbs={[{ label: '提供商', href: '/providers' }]}
        description={provider.notes ?? `分类 ${provider.category} · 环境 ${provider.environment}`}
        status={
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge kind="provider" status={provider.status} />
            <Badge tone="outline" mono>
              {provider.slug}
            </Badge>
            <Badge tone={activeCredentials > 0 ? 'ok' : 'warn'} dot>
              有效凭证 {activeCredentials} / {credentials.length}
            </Badge>
            <Badge tone="neutral">关联能力 {capabilities.length}</Badge>
          </div>
        }
        actions={
          <>
            {provider.docs_url ? (
              <Button asChild variant="secondary" size="sm">
                <a href={provider.docs_url} target="_blank" rel="noreferrer noopener">
                  文档
                  <ExternalLink size={14} strokeWidth={1.75} aria-hidden />
                </a>
              </Button>
            ) : null}
            <Button
              variant="primary"
              size="sm"
              onClick={() => setCredentialOpen(true)}
              icon={<Plus size={16} strokeWidth={1.75} />}
            >
              添加凭证
            </Button>
            <Button
              variant="danger"
              size="sm"
              disabled={associationCount > 0}
              onClick={() => setDeleteOpen(true)}
              icon={<Trash2 size={16} strokeWidth={1.75} />}
            >
              删除提供商
            </Button>
          </>
        }
      />

      {associationCount > 0 ? (
        <p className="rounded border border-border bg-[var(--sr-surface-sunken)] px-3 py-2 text-12 text-content-muted">
          删除已禁用：该提供商仍关联 {credentials.length} 个凭证与 {capabilities.length} 个能力。
          请先迁移或删除这些引用（能力页面可切换路由的凭证）。
        </p>
      ) : null}

      {/* 凭证 */}
      <section aria-label="凭证列表" className="sr-card p-4">
        <div className="flex items-center justify-between">
          <h2 className="flex items-center gap-1.5 text-16 font-semibold text-content">
            <KeyRound size={16} strokeWidth={1.75} aria-hidden />
            凭证（{credentials.length}）
          </h2>
          <span className="text-12 text-content-muted">密文只在服务端解密，页面仅显示掩码</span>
        </div>

        {credentials.length === 0 ? (
          <EmptyState
            kind="first-use"
            compact
            title="还没有凭证"
            description="先添加凭证，能力路由才有可用的鉴权信息。"
            actionLabel="添加凭证"
            onAction={() => setCredentialOpen(true)}
            className="mt-3"
          />
        ) : (
          <div className="mt-3 overflow-x-auto sr-scroll">
            <table className="w-full text-13">
              <caption className="px-1 py-1 text-left text-12 text-content-muted">
                该提供商下的凭证（掩码展示，不可回显明文）
              </caption>
              <thead>
                <tr className="border-b border-border">
                  <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">名称</th>
                  <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">鉴权</th>
                  <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">掩码</th>
                  <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">状态</th>
                  <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">最近验证</th>
                  <th scope="col" className="px-2 py-1.5 text-right text-12 font-medium text-content-muted">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--sr-border)]">
                {credentials.map((credential) => (
                  <tr key={credential.id}>
                    <td className="px-2 py-2 text-content">{credential.name}</td>
                    <td className="px-2 py-2 text-content-muted">
                      {credential.auth_type}
                      {credential.header_name ? `（${credential.header_name}）` : ''}
                    </td>
                    <td className="px-2 py-2 sr-mono text-content-muted">{credential.secret_mask}</td>
                    <td className="px-2 py-2">
                      <StatusBadge kind="credential" status={credential.status} />
                    </td>
                    <td className="px-2 py-2 text-content-muted">
                      {credential.last_verified_at ? (
                        <span className="flex items-center gap-1.5">
                          {formatRelative(credential.last_verified_at)}
                          <Badge tone={credential.last_verify_ok ? 'ok' : 'danger'} dot>
                            {credential.last_verify_ok ? '有效' : '失败'}
                          </Badge>
                        </span>
                      ) : (
                        '未验证'
                      )}
                    </td>
                    <td className="px-2 py-2 text-right">
                      <Button asChild variant="ghost" size="sm">
                        <Link href={`/providers/${provider.id}/credentials/${credential.id}`}>管理</Link>
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* 关联图：凭证 → 能力 → SKILL */}
      <section aria-label="关联关系" className="sr-card p-4">
        <h2 className="text-16 font-semibold text-content">关联关系</h2>
        <p className="mt-0.5 text-12 text-content-muted">
          凭证 → 能力 → SKILL。点击能力可查看它具体引用哪条 route 的哪个凭证。
        </p>
        {credentials.length === 0 || capabilities.length === 0 ? (
          <p className="mt-2 text-13 text-content-muted">
            {credentials.length === 0
              ? '先添加凭证，能力才能绑定到该提供商。'
              : '该提供商还没有被任何能力引用。'}
          </p>
        ) : (
          <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-3">
            <div>
              <h3 className="text-12 font-medium text-content-muted">凭证（{credentials.length}）</h3>
              <ul className="mt-1 flex flex-col gap-1">
                {credentials.map((credential) => (
                  <li key={credential.id} className="rounded border border-border px-2 py-1 text-13">
                    <span className="text-content">{credential.name}</span>
                    <span className="ml-1 sr-mono text-12 text-content-muted">{credential.secret_mask}</span>
                  </li>
                ))}
              </ul>
            </div>
            <div>
              <h3 className="text-12 font-medium text-content-muted">能力（{capabilities.length}）</h3>
              <ul className="mt-1 flex flex-col gap-1">
                {capabilities.map((capability) => (
                  <li key={capability.id} className="rounded border border-border px-2 py-1 text-13">
                    <Link href={`/capabilities/${capability.id}`} className="flex items-center gap-1 hover:underline">
                      <span className="sr-mono text-12 text-content">{capability.key}</span>
                      <StatusBadge kind="capability" status={capability.status} />
                    </Link>
                  </li>
                ))}
              </ul>
            </div>
            <div>
              <h3 className="text-12 font-medium text-content-muted">SKILL</h3>
              <p className="mt-1 flex items-center gap-1 text-12 text-content-muted">
                在 <Link href="/skills" className="text-accent hover:underline">SKILL 页</Link> 查看依赖
                <ArrowRight size={12} strokeWidth={1.75} aria-hidden />
              </p>
            </div>
          </div>
        )}
      </section>

      {/* 添加凭证抽屉 */}
      <Drawer open={credentialOpen} onOpenChange={setCredentialOpen}>
        <DrawerContent
          title="添加凭证"
          description="先测后存：选协议、填密钥后直接「立即测试」，通过再保存。密钥会以主密钥加密存储，创建后只显示掩码与指纹。"
        >
          <form
            noValidate
            className="flex flex-col gap-1"
            onSubmit={form.handleSubmit((values) => createCredential.mutate(values))}
          >
            <FormField id="credential-name" label="名称" required error={form.formState.errors.name?.message}>
              <Input id="credential-name" invalid={Boolean(form.formState.errors.name)} {...form.register('name')} />
            </FormField>

            {/*
              协议 + 地址 + 认证方式 + 头名 + 路径覆盖 + 「立即测试」：受控组件，
              值全部来自并写回同一份 RHF 表单（Controller 注册，spec R-19：不另开 useState）。
            */}
            <Controller
              control={form.control}
              name="protocol"
              render={({ field }) => (
                <CredentialProtocolFields
                  idPrefix="new-credential"
                  value={{
                    protocol: field.value,
                    wire: form.watch('wire'),
                    base_url: form.watch('base_url'),
                    auth_style: form.watch('auth_style'),
                    header_name: form.watch('header_name'),
                    paths_models: form.watch('paths_models'),
                    paths_chat: form.watch('paths_chat'),
                  }}
                  onChange={applyProtocolPatch}
                  protocols={protocols.data?.items ?? []}
                  protocolsLoading={protocols.isLoading}
                  protocolsError={
                    protocols.isError
                      ? {
                          code: describeError(protocols.error).code,
                          message: describeError(protocols.error).message,
                          hint: describeError(protocols.error).hint,
                        }
                      : null
                  }
                  onProtocolsRetry={() => void protocols.refetch()}
                  onProtocolChange={() => {
                    // 换协议后旧的测试结论不再成立
                    setProbeResult(null);
                    setProbeError(null);
                  }}
                  onTest={() => void runDrawerTest()}
                  testResult={probeResult}
                  testing={probeRunning}
                  error={probeError}
                  errors={{ base_url: form.formState.errors.base_url?.message }}
                  disabled={createCredential.isPending}
                />
              )}
            />

            <p className="text-12 text-content-muted">
              地址由提供商持有：这里填的地址只作为 <code className="sr-mono">meta.base_url_hint</code>{' '}
              记录，不会覆盖提供商的 base_url；留空即用提供商的地址。
            </p>

            {/* 阶段时间线（drawer 内的完整版：stage / HTTP / 耗时 / 已脱敏 detail） */}
            {probeResult ? (
              <div className="rounded border border-border bg-surface p-2" aria-live="polite">
                <p className="flex flex-wrap items-center gap-2 text-12">
                  <Badge tone={probeResult.ok ? 'ok' : 'danger'} dot>
                    {probeResult.ok ? '测试通过' : '测试失败'}
                  </Badge>
                  {probeResult.error_code ? (
                    <Badge tone="danger" mono>
                      {probeResult.error_code}
                    </Badge>
                  ) : null}
                  {probeResult.probe_id ? (
                    <code className="sr-mono break-all text-content-muted">{probeResult.probe_id}</code>
                  ) : null}
                </p>
                <ul className="mt-1 flex flex-col gap-1">
                  {probeResult.stages.map((stage, index) => (
                    <li
                      key={`${stage.stage}-${index}`}
                      className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-12"
                    >
                      <span className="min-w-[104px] text-content">
                        {LLM_STAGE_LABELS[stage.stage] ?? stage.stage}
                      </span>
                      <Badge tone={stage.ok ? 'ok' : 'danger'}>{stage.ok ? '通过' : '失败'}</Badge>
                      <span className="text-content-muted">
                        HTTP {stage.http_status ?? '—'} · {formatDuration(stage.latency_ms)}
                      </span>
                      {stage.model_count !== undefined && stage.model_count !== null ? (
                        <span className="text-content-muted">{stage.model_count} 个模型</span>
                      ) : null}
                      {stage.detail ? (
                        <span className="basis-full break-words text-content-muted">{stage.detail}</span>
                      ) : null}
                    </li>
                  ))}
                </ul>
                {probeResult.hint ? (
                  <p className="mt-1 text-12 text-content-muted">下一步：{probeResult.hint}</p>
                ) : null}
              </div>
            ) : null}

            <FormField
              id="secret"
              label="密钥"
              required
              hint="仅提交一次，不会回显"
              error={form.formState.errors.secret?.message}
            >
              <Input
                id="secret"
                type="password"
                autoComplete="new-password"
                invalid={Boolean(form.formState.errors.secret)}
                {...form.register('secret')}
              />
            </FormField>
            <FormField id="expires_at" label="过期时间" hint="可选，ISO 时间">
              <Input id="expires_at" type="datetime-local" {...form.register('expires_at')} />
            </FormField>
            <div className="mt-2 flex items-center justify-end gap-2">
              <Button variant="secondary" size="sm" type="button" onClick={() => setCredentialOpen(false)}>
                取消
              </Button>
              <Button variant="primary" size="sm" type="submit" loading={createCredential.isPending}>
                保存凭证
              </Button>
            </div>
          </form>
        </DrawerContent>
      </Drawer>

      <DangerConfirm
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        title="删除提供商"
        action="删除"
        description="软删除后不再出现在列表，历史调用记录仍保留。"
        impacts={[{ id: provider.id, label: provider.name, detail: `slug ${provider.slug}` }]}
        blockedReason={
          associationCount > 0
            ? `仍关联 ${credentials.length} 个凭证与 ${capabilities.length} 个能力，先解除引用后再删除。`
            : null
        }
        loading={deleteProvider.isPending}
        onConfirm={() => deleteProvider.mutateAsync()}
      />
    </div>
  );
}
