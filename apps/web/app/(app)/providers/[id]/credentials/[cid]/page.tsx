'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { KeyRound, RefreshCw, ShieldCheck, Trash2 } from 'lucide-react';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import * as React from 'react';
import { DangerConfirm } from '@/components/common/DangerConfirm';
import { EmptyState } from '@/components/common/EmptyState';
import { PageHeader } from '@/components/common/PageHeader';
import { StatusBadge } from '@/components/common/StatusBadge';
import { LLM_STAGE_LABELS } from '@/components/llm/ConnectionTester';
import { ProbeHistory } from '@/components/llm/ProbeHistory';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Dialog, DialogClose, DialogContent } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import { useToast } from '@/components/ui/toast';
import { apiDelete, apiGet, apiPost, describeError, isApiError } from '@/lib/api';
import { formatDateTime, formatDuration, formatRelative, prettyJson } from '@/lib/format';
import { queryKeys } from '@/lib/query';
import type { Credential, CredentialImpact, CredentialVerification, LlmStageResult } from '@/lib/types';

/**
 * `POST /admin/credentials/{id}/verify` 的响应（spec §5.7：在原字段上增加
 * `protocol` / `resolved_url` / `stage` / `model_count`）。
 * 字段全部可选：后端若只回 `verification_id`（异步），这里就只显示「已提交」，
 * 不假装拿到了阶段结果。真实的阶段明细也可以从 `GET /llm/probes` 的历史里读到。
 */
interface CredentialVerifyResult {
  verification_id?: string;
  ok?: boolean;
  protocol?: string;
  resolved_url?: string;
  stage?: string;
  model_count?: number | null;
  /** `record_verification()` 的返回键就是 `status_code`（HTTP 状态码） */
  status_code?: number | null;
  latency_ms?: number | null;
  error_code?: string | null;
  detail_redacted?: string | null;
  hint?: string | null;
  stages?: LlmStageResult[];
}

/**
 * 凭证详情（spec §8.4）：掩码、验证历史、影响面、替换 / 轮换 / 启停 / 删除。
 * 密码类字段永不回显；替换与轮换都必须重新输入明文。
 */
export default function CredentialDetailPage() {
  const params = useParams<{ id: string; cid: string }>();
  const providerId = params?.id ?? '';
  const credentialId = params?.cid ?? '';
  const queryClient = useQueryClient();
  const { toast } = useToast();

  const [replaceOpen, setReplaceOpen] = React.useState(false);
  const [rotateOpen, setRotateOpen] = React.useState(false);
  const [deleteOpen, setDeleteOpen] = React.useState(false);
  const [secretDraft, setSecretDraft] = React.useState('');
  /** 「重新探测」返回的阶段结果（spec §5.7）；没有就是还没测过 */
  const [verifyResult, setVerifyResult] = React.useState<CredentialVerifyResult | null>(null);

  const credential = useQuery<Credential>({
    queryKey: queryKeys.credentials.detail(credentialId),
    queryFn: () => apiGet<Credential>(`/admin/credentials/${credentialId}`),
    enabled: Boolean(credentialId),
  });

  const impact = useQuery<CredentialImpact>({
    queryKey: queryKeys.credentials.impact(credentialId),
    queryFn: () => apiGet<CredentialImpact>(`/admin/credentials/${credentialId}/impact`),
    enabled: Boolean(credentialId),
  });

  // 验证历史：spec 未在 §9.2 列出独立路径，这里按 `/verifications` 读取；
  // 若后端未提供该端点（404），如实提示而不是伪造历史。
  const verifications = useQuery<CredentialVerification[]>({
    queryKey: queryKeys.credentials.verifications(credentialId),
    queryFn: async () => {
      const result = await apiGet<{ items: CredentialVerification[] } | CredentialVerification[]>(
        `/admin/credentials/${credentialId}/verifications`,
      );
      return Array.isArray(result) ? result : result.items;
    },
    enabled: Boolean(credentialId),
    retry: false,
  });

  const invalidate = async (): Promise<void> => {
    await queryClient.invalidateQueries({ queryKey: queryKeys.credentials.detail(credentialId) });
    await queryClient.invalidateQueries({ queryKey: queryKeys.credentials.impact(credentialId) });
    await queryClient.invalidateQueries({ queryKey: queryKeys.providers.all });
  };

  const verify = useMutation({
    mutationFn: async () =>
      apiPost<CredentialVerifyResult>(`/admin/credentials/${credentialId}/verify`, {}),
    onSuccess: async (data) => {
      setVerifyResult(data);
      toast({
        title: data.protocol ? `已按协议重新探测（${data.protocol}）` : '已触发有效性检测',
        description: data.verification_id
          ? `检测任务 ${data.verification_id} 已提交。`
          : '结果见下方「协议与重新探测」。',
        tone: 'info',
      });
      await invalidate();
      void verifications.refetch();
    },
    onError: (error) => {
      const info = describeError(error);
      toast({ title: '触发检测失败', description: info.message, tone: 'danger', requestId: info.requestId });
    },
  });

  const toggleStatus = useMutation({
    mutationFn: async (action: 'enable' | 'disable') =>
      apiPost<Credential>(`/admin/credentials/${credentialId}/${action}`, {}),
    onSuccess: async (_data) => {
      toast({ title: '状态已更新', tone: 'ok' });
      await invalidate();
    },
    onError: (error) => {
      const info = describeError(error);
      toast({ title: '操作失败', description: info.message, tone: 'danger', requestId: info.requestId });
    },
  });

  const replaceSecret = useMutation({
    mutationFn: async () =>
      apiPost<Credential>(`/admin/credentials/${credentialId}/replace`, { secret: secretDraft }),
    onSuccess: async () => {
      toast({ title: '密文已替换', description: 'id 与关联关系保持不变。', tone: 'ok' });
      setReplaceOpen(false);
      setSecretDraft('');
      await invalidate();
    },
    onError: (error) => {
      const info = describeError(error);
      toast({ title: '替换失败', description: info.message, tone: 'danger', requestId: info.requestId });
    },
  });

  const rotate = useMutation({
    mutationFn: async () =>
      apiPost<Credential>(`/admin/credentials/${credentialId}/rotate`, { secret: secretDraft }),
    onSuccess: async (created) => {
      toast({
        title: '已生成新凭证并原子切换引用',
        description: `新凭证 ${created.id}`,
        tone: 'ok',
      });
      setRotateOpen(false);
      setSecretDraft('');
      await invalidate();
    },
    onError: (error) => {
      const info = describeError(error);
      toast({ title: '轮换失败', description: info.message, tone: 'danger', requestId: info.requestId });
    },
  });

  const remove = useMutation({
    mutationFn: async () => apiDelete<null>(`/admin/credentials/${credentialId}`),
    onSuccess: async () => {
      toast({ title: '凭证已删除', tone: 'ok' });
      window.location.assign(`/providers/${providerId}`);
    },
    onError: (error) => {
      const info = describeError(error);
      toast({
        title: info.code === 'CREDENTIAL_IN_USE' ? '凭证仍被引用' : '删除失败',
        description: info.hint ? `${info.message}（${info.hint}）` : info.message,
        tone: 'danger',
        requestId: info.requestId,
      });
    },
  });

  if (credential.isLoading) {
    return (
      <div className="flex flex-col gap-4" aria-busy="true">
        <Skeleton height={64} rounded="md" />
        <Skeleton height={160} rounded="md" />
        <Skeleton height={200} rounded="md" />
      </div>
    );
  }

  if (credential.isError) {
    const info = describeError(credential.error);
    return (
      <EmptyState
        kind={info.code === 'NOT_FOUND' ? 'empty' : 'unavailable'}
        title={info.code === 'NOT_FOUND' ? '凭证不存在或已被删除' : info.message}
        description={info.hint ?? undefined}
        requestId={info.requestId}
        actionLabel="返回提供商"
        onAction={() => window.location.assign(`/providers/${providerId}`)}
      />
    );
  }

  const data = credential.data;
  if (!data) return null;

  // meta 投影：只展示白名单键的键值；被剔除/密封的键只给键名（spec §A.3.2）
  const publicMetaKeys = Object.keys(data.meta ?? {});
  const metaRedactedKeys = data.meta_redacted_keys ?? [];
  /** 协议感知凭证的协议与 api-version（`_PUBLIC_META_KEYS` 可明文键，spec §3.3.1） */
  const llmProtocol =
    typeof data.meta?.llm_protocol === 'string' && data.meta.llm_protocol.trim() !== ''
      ? String(data.meta.llm_protocol)
      : null;
  const llmApiVersion =
    typeof data.meta?.api_version === 'string' && data.meta.api_version.trim() !== ''
      ? String(data.meta.api_version)
      : null;

  const impactedCapabilities = impact.data?.capabilities ?? [];
  const impactedSkills = impact.data?.skills ?? [];
  const impactedTokens = impact.data?.tokens ?? [];
  const impactedTasks = impact.data?.tasks ?? [];
  const impactCount =
    impactedCapabilities.length + impactedSkills.length + impactedTokens.length + impactedTasks.length;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={data.name}
        breadcrumbs={[
          { label: '提供商', href: '/providers' },
          { label: '提供商详情', href: `/providers/${providerId}` },
        ]}
        description={`鉴权方式 ${data.auth_type}${data.header_name ? `（${data.header_name}）` : ''}`}
        status={
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge kind="credential" status={data.status} />
            <Badge tone="outline" mono>
              {data.secret_mask}
            </Badge>
            {llmProtocol ? (
              <Badge tone="info" mono>
                协议 {llmProtocol}
              </Badge>
            ) : null}
            {llmApiVersion ? (
              <Badge tone="outline" mono>
                api-version {llmApiVersion}
              </Badge>
            ) : null}
            {data.last_verify_ok !== null ? (
              <Badge tone={data.last_verify_ok ? 'ok' : 'danger'} dot>
                {data.last_verify_ok ? '最近验证通过' : '最近验证失败'}
              </Badge>
            ) : null}
          </div>
        }
        actions={
          <>
            <Button
              variant="secondary"
              size="sm"
              loading={verify.isPending}
              onClick={() => verify.mutate()}
              icon={<ShieldCheck size={16} strokeWidth={1.75} />}
            >
              重新探测
            </Button>
            <Button variant="secondary" size="sm" onClick={() => setReplaceOpen(true)} icon={<KeyRound size={16} strokeWidth={1.75} />}>
              替换密文
            </Button>
            <Button variant="secondary" size="sm" onClick={() => setRotateOpen(true)} icon={<RefreshCw size={16} strokeWidth={1.75} />}>
              轮换
            </Button>
            <Button
              variant={data.status === 'active' ? 'secondary' : 'primary'}
              size="sm"
              loading={toggleStatus.isPending}
              onClick={() => toggleStatus.mutate(data.status === 'active' ? 'disable' : 'enable')}
            >
              {data.status === 'active' ? '停用' : '启用'}
            </Button>
            <Button variant="danger" size="sm" onClick={() => setDeleteOpen(true)} icon={<Trash2 size={16} strokeWidth={1.75} />}>
              删除
            </Button>
          </>
        }
      />

      {/* 协议感知探测（spec §5.7）：meta 里的协议 + 「重新探测」的阶段结果 + 最近探测历史 */}
      <section aria-label="协议与重新探测" className="sr-card p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-16 font-semibold text-content">协议与重新探测</h2>
          <div className="flex flex-wrap items-center gap-1">
            <Badge tone={llmProtocol ? 'info' : 'neutral'} dot>
              {llmProtocol ? `协议 ${llmProtocol}` : '未声明协议'}
            </Badge>
            {llmApiVersion ? (
              <Badge tone="outline" mono>
                api-version {llmApiVersion}
              </Badge>
            ) : null}
          </div>
        </div>
        <p className="mt-0.5 text-12 text-content-muted">
          协议来自凭证的 <code className="sr-mono">meta.llm_protocol</code>
          （未声明时后端会按提供商名推断）。「重新探测」对 llm 类凭证走协议感知的模型清单探测，不计费。
        </p>

        {verifyResult ? (
          <div className="mt-2 rounded border border-border bg-surface p-2" aria-live="polite">
            <p className="flex flex-wrap items-center gap-2 text-12">
              <Badge tone={verifyResult.ok === false ? 'danger' : verifyResult.ok ? 'ok' : 'info'} dot>
                {verifyResult.ok === false ? '探测失败' : verifyResult.ok ? '探测通过' : '已提交'}
              </Badge>
              {verifyResult.protocol ? (
                <Badge tone="outline" mono>
                  {verifyResult.protocol}
                </Badge>
              ) : null}
              {verifyResult.error_code ? (
                <Badge tone="danger" mono>
                  {verifyResult.error_code}
                </Badge>
              ) : null}
              {verifyResult.verification_id ? (
                <code className="sr-mono break-all text-content-muted">{verifyResult.verification_id}</code>
              ) : null}
            </p>
            {verifyResult.resolved_url ? (
              <p className="mt-1 break-all text-12 text-content-muted">
                实际请求地址：<code className="sr-mono text-content">{verifyResult.resolved_url}</code>
              </p>
            ) : null}
            {verifyResult.stages && verifyResult.stages.length > 0 ? (
              <ul className="mt-1 flex flex-col gap-1">
                {verifyResult.stages.map((stage, index) => (
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
            ) : verifyResult.stage || verifyResult.model_count !== undefined ? (
              <p className="mt-1 flex flex-wrap items-baseline gap-x-2 text-12 text-content-muted">
                <span className="min-w-[104px] text-content">
                  {LLM_STAGE_LABELS[verifyResult.stage ?? ''] ?? verifyResult.stage ?? '阶段'}
                </span>
                <span>HTTP {verifyResult.status_code ?? '—'}</span>
                <span>· {formatDuration(verifyResult.latency_ms)}</span>
                {verifyResult.model_count !== undefined && verifyResult.model_count !== null ? (
                  <span>· 识别到 {verifyResult.model_count} 个模型</span>
                ) : null}
              </p>
            ) : null}
            {verifyResult.hint ? (
              <p className="mt-1 text-12 text-content-muted">下一步：{verifyResult.hint}</p>
            ) : null}
            {verifyResult.detail_redacted ? (
              <p className="mt-1 break-words text-12 text-content-muted">
                {verifyResult.detail_redacted}
              </p>
            ) : null}
          </div>
        ) : (
          <p className="mt-2 text-13 text-content-muted">
            还没有本次探测结果。点上方「重新探测」跑一次真实请求（鉴权阶段不计费）；历史记录见下方。
          </p>
        )}

        <div className="mt-3 border-t border-border pt-3">
          <ProbeHistory limit={5} credentialId={data.id} />
        </div>
      </section>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <section aria-label="基本信息" className="sr-card p-4">
          <h2 className="text-16 font-semibold text-content">基本信息</h2>
          <dl className="mt-3 grid grid-cols-1 gap-2 text-13 sm:grid-cols-2">
            <div>
              <dt className="text-content-muted">凭证 ID</dt>
              <dd className="sr-mono text-content">{data.id}</dd>
            </div>
            <div>
              <dt className="text-content-muted">指纹</dt>
              <dd className="sr-mono text-content">{data.secret_fingerprint || '—'}</dd>
            </div>
            <div>
              <dt className="text-content-muted">过期时间</dt>
              <dd className="text-content">{data.expires_at ? formatDateTime(data.expires_at) : '不自动过期'}</dd>
            </div>
            <div>
              <dt className="text-content-muted">最近验证</dt>
              <dd className="text-content">
                {data.last_verified_at ? formatRelative(data.last_verified_at) : '未验证'}
              </dd>
            </div>
            <div>
              <dt className="text-content-muted">创建时间</dt>
              <dd className="text-content">{formatDateTime(data.created_at)}</dd>
            </div>
            <div>
              <dt className="text-content-muted">轮换自</dt>
              <dd className="sr-mono text-content">{data.rotation_of ?? '—'}</dd>
            </div>
          </dl>
          <p className="mt-3 rounded border border-border bg-[var(--sr-surface-sunken)] px-2 py-1.5 text-12 text-content-muted">
            明文密钥不会通过任何接口返回；如需更换，请使用「替换密文」（保留 id 与关联）或「轮换」（新建一行并原子切换引用）。
          </p>
        </section>

        <section aria-label="影响面" className="sr-card p-4">
          <h2 className="text-16 font-semibold text-content">影响面（{impactCount}）</h2>
          {impact.isLoading ? (
            <Skeleton height={60} className="mt-3" />
          ) : impact.isError ? (
            <p className="mt-2 text-13 text-content-muted">
              影响面读取失败：{describeError(impact.error).message}
            </p>
          ) : impactCount === 0 ? (
            <p className="mt-2 text-13 text-content-muted">当前没有对象引用该凭证，可以安全删除。</p>
          ) : (
            <ul className="mt-2 flex flex-col gap-1.5 text-13">
              {[
                { label: '能力', items: impactedCapabilities },
                { label: 'SKILL', items: impactedSkills },
                { label: '令牌', items: impactedTokens },
                { label: '任务', items: impactedTasks },
              ].map((group) =>
                group.items.length === 0 ? null : (
                  <li key={group.label}>
                    <span className="text-content-muted">{group.label}（{group.items.length}）</span>
                    <ul className="mt-0.5 flex flex-wrap gap-1">
                      {group.items.map((item) => (
                        <li key={item.id} className="rounded border border-border px-1.5 py-0.5 text-12">
                          {item.name}
                        </li>
                      ))}
                    </ul>
                  </li>
                ),
              )}
            </ul>
          )}
        </section>
      </div>

      <section aria-label="密钥元数据" className="sr-card p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-16 font-semibold text-content">密钥元数据（meta）</h2>
          <div className="flex flex-wrap items-center gap-1">
            <Badge tone={data.meta_sealed ? 'info' : 'neutral'} dot>
              {data.meta_sealed ? '敏感键已密封' : '无密封键'}
            </Badge>
            <Badge tone="outline" mono>
              掩码 {data.secret_mask || '—'}
            </Badge>
            <Badge tone="outline" mono>
              指纹 {data.secret_fingerprint || '—'}
            </Badge>
          </div>
        </div>
        <p className="mt-1 text-12 text-content-muted">
          接口只返回白名单键；共享密钥、token、签名类字段被密封在 <code className="sr-mono">meta_sealed</code> 里，
          只以键名回显，任何页面都不会出现明文。
        </p>

        <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
          <div>
            <h3 className="text-12 font-medium text-content-muted">可见键（白名单）</h3>
            {publicMetaKeys.length === 0 ? (
              <p className="mt-1 text-13 text-content-muted">没有可展示的 meta 键。</p>
            ) : (
              <dl className="mt-1 flex flex-col gap-1 text-13">
                {publicMetaKeys.map((key) => (
                  <div key={key} className="flex items-start justify-between gap-2">
                    <dt className="sr-mono text-12 text-content-muted">{key}</dt>
                    <dd className="max-w-[60%] break-all text-right text-12 text-content">
                      {prettyJson(data.meta[key], 200)}
                    </dd>
                  </div>
                ))}
              </dl>
            )}
          </div>
          <div>
            <h3 className="text-12 font-medium text-content-muted">
              被密封 / 被剔除的键（{metaRedactedKeys.length}）
            </h3>
            {data.meta_redacted_keys.length === 0 ? (
              <p className="mt-1 text-13 text-content-muted">没有被剔除的键。</p>
            ) : (
              <ul className="mt-1 flex flex-wrap gap-1" aria-label="被剔除的 meta 键名">
                {metaRedactedKeys.map((key) => (
                  <li key={key}>
                    <Badge tone="warn" mono>
                      {key}
                    </Badge>
                  </li>
                ))}
              </ul>
            )}
            <p className="mt-1 text-12 text-content-muted">
              这里只有键名。需要修改时请用「替换密文」或更新凭证的 meta。
            </p>
          </div>
        </div>
      </section>

      <section aria-label="验证历史" className="sr-card p-4">
        <h2 className="text-16 font-semibold text-content">验证历史</h2>
        {verifications.isLoading ? (
          <Skeleton height={60} className="mt-3" />
        ) : verifications.isError ? (
          <p className="mt-2 text-13 text-content-muted">
            验证历史不可用（该端点未在接口契约 §9.2 中定义，错误码{' '}
            <code className="sr-mono">{describeError(verifications.error).code}</code>）。
            当前仅能显示最近一次验证结果：
            {data.last_verified_at ? formatDateTime(data.last_verified_at) : '暂无记录'}。
          </p>
        ) : (verifications.data?.length ?? 0) === 0 ? (
          <p className="mt-2 text-13 text-content-muted">还没有验证记录。点击「检测有效性」发起一次。</p>
        ) : (
          <div className="mt-2 overflow-x-auto sr-scroll">
            <table className="w-full text-13">
              <caption className="px-1 py-1 text-left text-12 text-content-muted">最近 20 次有效性检测</caption>
              <thead>
                <tr className="border-b border-border">
                  <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">时间</th>
                  <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">结果</th>
                  <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">HTTP</th>
                  <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">耗时</th>
                  <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">详情</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--sr-border)]">
                {verifications.data?.slice(0, 20).map((item) => (
                  <tr key={item.id}>
                    <td className="px-2 py-1.5 text-content-muted">{formatDateTime(item.at)}</td>
                    <td className="px-2 py-1.5">
                      <Badge tone={item.ok ? 'ok' : 'danger'} dot>
                        {item.ok ? '通过' : '失败'}
                      </Badge>
                    </td>
                    <td className="px-2 py-1.5 text-content">{item.status_code ?? '—'}</td>
                    <td className="px-2 py-1.5 text-content-muted">{formatDuration(item.latency_ms)}</td>
                    <td className="px-2 py-1.5 text-content-muted">
                      {item.error_code ? <code className="sr-mono text-danger">{item.error_code}</code> : null}
                      {item.detail_redacted ? <span className="ml-1">{item.detail_redacted}</span> : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* 替换 / 轮换 对话框 */}
      <Dialog
        open={replaceOpen || rotateOpen}
        onOpenChange={(open) => {
          if (!open) {
            setReplaceOpen(false);
            setRotateOpen(false);
            setSecretDraft('');
          }
        }}
      >
        <DialogContent
          title={replaceOpen ? '替换密文（保留 id 与关联）' : '轮换凭证（新建一行并原子切换）'}
          description={
            replaceOpen
              ? '适用于密钥续期但引用不变的情况。'
              : '会创建新的凭证行并把所有引用切换过去，旧行保留用于回查。'
          }
          width="sm"
          footer={
            <>
              <DialogClose asChild>
                <Button variant="secondary" size="sm">
                  取消
                </Button>
              </DialogClose>
              <Button
                variant="primary"
                size="sm"
                loading={replaceSecret.isPending || rotate.isPending}
                disabled={secretDraft.trim().length === 0}
                onClick={() => (replaceOpen ? replaceSecret.mutate() : rotate.mutate())}
              >
                {replaceOpen ? '替换' : '轮换'}
              </Button>
            </>
          }
        >
          <label className="flex flex-col gap-1">
            <span className="text-13 text-content">新密钥</span>
            <Input
              type="password"
              autoComplete="new-password"
              value={secretDraft}
              onChange={(event) => setSecretDraft(event.target.value)}
              placeholder="粘贴新的密钥"
            />
            <span className="text-12 text-content-muted">明文只在本请求内传输，不会回显或写日志。</span>
          </label>
          {replaceSecret.isError && isApiError(replaceSecret.error) ? (
            <p className="mt-2 text-12 text-danger">{replaceSecret.error.message}</p>
          ) : null}
          {rotate.isError && isApiError(rotate.error) ? (
            <p className="mt-2 text-12 text-danger">{rotate.error.message}</p>
          ) : null}
        </DialogContent>
      </Dialog>

      <DangerConfirm
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        title="删除凭证"
        action="删除"
        description="删除后引用该凭证的能力路由会立即不可用。"
        impacts={[
          { id: data.id, label: data.name, detail: `掩码 ${data.secret_mask}` },
          ...impactedCapabilities.map((item) => ({ id: item.id, label: `能力：${item.name}` })),
          ...impactedSkills.map((item) => ({ id: item.id, label: `SKILL：${item.name}` })),
        ]}
        loading={remove.isPending}
        onConfirm={() => remove.mutateAsync()}
      />

      <p className="text-12 text-content-muted">
        返回 <Link href={`/providers/${providerId}`} className="text-accent hover:underline">提供商详情</Link>
      </p>
    </div>
  );
}
