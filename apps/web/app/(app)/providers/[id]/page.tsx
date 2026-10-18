'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowRight, ExternalLink, KeyRound, Plus, Trash2 } from 'lucide-react';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import * as React from 'react';
import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { DangerConfirm } from '@/components/common/DangerConfirm';
import { EmptyState } from '@/components/common/EmptyState';
import { FormField } from '@/components/common/FormField';
import { PageHeader } from '@/components/common/PageHeader';
import { StatusBadge } from '@/components/common/StatusBadge';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Drawer, DrawerContent } from '@/components/ui/drawer';
import { Input } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { useToast } from '@/components/ui/toast';
import { apiDelete, apiGet, apiPost, describeError } from '@/lib/api';
import { formatRelative } from '@/lib/format';
import { queryKeys } from '@/lib/query';
import type { Credential, ProviderDetail } from '@/lib/types';

const credentialSchema = z.object({
  name: z.string().min(1, '请输入凭证名称').max(128),
  auth_type: z.string().min(1),
  header_name: z.string().max(64),
  secret: z.string().min(1, '请粘贴密钥（不会回显）'),
  expires_at: z.string(),
});

type CredentialForm = z.infer<typeof credentialSchema>;

/** 提供商详情（spec §13.2）：凭证列表、能力列表、关联图；无凭证时提示先添加。 */
export default function ProviderDetailPage() {
  const params = useParams<{ id: string }>();
  const providerId = params?.id ?? '';
  const queryClient = useQueryClient();
  const { toast } = useToast();

  const [credentialOpen, setCredentialOpen] = React.useState(false);
  const [deleteOpen, setDeleteOpen] = React.useState(false);

  const detail = useQuery<ProviderDetail>({
    queryKey: queryKeys.providers.detail(providerId),
    queryFn: () => apiGet<ProviderDetail>(`/admin/providers/${providerId}`),
    enabled: Boolean(providerId),
  });

  const form = useForm<CredentialForm>({
    resolver: zodResolver(credentialSchema),
    defaultValues: { name: '', auth_type: 'api_key', header_name: 'Authorization', secret: '', expires_at: '' },
  });

  const createCredential = useMutation({
    mutationFn: async (values: CredentialForm) =>
      apiPost<Credential>(`/admin/providers/${providerId}/credentials`, {
        name: values.name,
        kind: detail.data?.category ?? 'llm',
        auth_type: values.auth_type,
        header_name: values.header_name || null,
        secret: values.secret,
        expires_at: values.expires_at || null,
      }),
    onSuccess: async () => {
      toast({ title: '凭证已添加', description: '密钥已加密存储，页面之后只显示掩码。', tone: 'ok' });
      setCredentialOpen(false);
      form.reset();
      await queryClient.invalidateQueries({ queryKey: queryKeys.providers.detail(providerId) });
      await queryClient.invalidateQueries({ queryKey: queryKeys.providers.all });
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
    return (
      <div className="flex flex-col gap-4" aria-busy="true">
        <Skeleton height={64} rounded="md" />
        <Skeleton height={180} rounded="md" />
        <Skeleton height={180} rounded="md" />
      </div>
    );
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
          description="密钥会以主密钥加密存储；创建后页面只显示掩码与指纹。"
        >
          <form
            noValidate
            className="flex flex-col gap-1"
            onSubmit={form.handleSubmit((values) => createCredential.mutate(values))}
          >
            <FormField id="credential-name" label="名称" required error={form.formState.errors.name?.message}>
              <Input id="credential-name" invalid={Boolean(form.formState.errors.name)} {...form.register('name')} />
            </FormField>
            <FormField id="auth_type" label="鉴权方式" required>
              <Select
                value={form.watch('auth_type')}
                onValueChange={(value) => form.setValue('auth_type', value)}
                options={[
                  { value: 'api_key', label: 'API Key' },
                  { value: 'bearer', label: 'Bearer Token' },
                  { value: 'basic', label: 'Basic Auth' },
                  { value: 'header', label: '自定义 Header' },
                ]}
              />
            </FormField>
            <FormField id="header_name" label="Header 名称" hint="如 Authorization / x-api-key">
              <Input id="header_name" {...form.register('header_name')} />
            </FormField>
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
