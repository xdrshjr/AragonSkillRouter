'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { zodResolver } from '@hookform/resolvers/zod';
import { Database, KeyRound, Save, ShieldAlert } from 'lucide-react';
import * as React from 'react';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { DangerConfirm } from '@/components/common/DangerConfirm';
import { EmptyState, RequestIdCopy } from '@/components/common/EmptyState';
import { FormField } from '@/components/common/FormField';
import { PageHeader } from '@/components/common/PageHeader';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Textarea } from '@/components/ui/textarea';
import { useToast } from '@/components/ui/toast';
import { apiGet, apiPost, describeError, isApiError, saveSettings, triggerBackupNow } from '@/lib/api';
import { formatBytes, formatDateTime, formatRelative } from '@/lib/format';
import { queryKeys } from '@/lib/query';
import type {
  AuditLogEntry,
  BackupNowResult,
  Paginated,
  PlatformSettings,
  RotateMasterKeyResult,
} from '@/lib/types';

const schema = z.object({
  payload_policy: z.enum(['none', 'redacted_preview', 'full']),
  retention_days: z.coerce.number().int().min(1, '至少 1 天').max(3650, '最多 3650 天'),
  fetch_allowlist: z.string(),
  default_budget_usd: z.coerce.number().min(0, '不能为负'),
  engine_policy: z.enum(['auto', 'notify', 'manual']),
});

type SettingsForm = z.infer<typeof schema>;

/**
 * 设置（spec §13.2 `/settings`）：
 * 分区：通用 / 安全 / 预算与配额 / 外部抓取白名单 / 数据保留 / 审计 / 备份。
 * 保存成功的 toast 带 request_id；危险区独立（主密钥轮换）。
 */
export default function SettingsPage() {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [rotateOpen, setRotateOpen] = React.useState(false);
  const [backupResult, setBackupResult] = React.useState<BackupNowResult | null>(null);
  const [savedAt, setSavedAt] = React.useState<string | null>(null);

  const settings = useQuery<PlatformSettings>({
    queryKey: queryKeys.settings.detail(),
    queryFn: () => apiGet<PlatformSettings>('/admin/settings'),
  });

  const audit = useQuery<Paginated<AuditLogEntry>>({
    queryKey: queryKeys.settings.audit({ page: 1, page_size: 20, sort: '-at' }),
    queryFn: () => apiGet<Paginated<AuditLogEntry>>('/admin/settings/audit', { page: 1, page_size: 20, sort: '-at' }),
  });

  const form = useForm<SettingsForm>({
    resolver: zodResolver(schema),
    defaultValues: {
      payload_policy: 'redacted_preview',
      retention_days: 30,
      fetch_allowlist: '',
      default_budget_usd: 0,
      engine_policy: 'manual',
    },
  });

  React.useEffect(() => {
    if (!settings.data) return;
    form.reset({
      payload_policy: settings.data.payload_policy,
      retention_days: settings.data.retention_days,
      // 后端已统一成 string[]；历史行可能是逗号分隔字符串，这里兜住不让整页崩掉
      fetch_allowlist: Array.isArray(settings.data.fetch_allowlist)
        ? settings.data.fetch_allowlist.join('\n')
        : String(settings.data.fetch_allowlist ?? '')
            .split(',')
            .map((item) => item.trim())
            .filter(Boolean)
            .join('\n'),
      // 后端 GET /admin/settings 的字段是 default_budget_usd（没有嵌套 budget 对象）
      default_budget_usd: settings.data.default_budget_usd ?? 0,
      engine_policy: settings.data.engine_policy,
    });
  }, [settings.data, form]);

  const save = useMutation({
    mutationFn: async (values: SettingsForm) =>
      saveSettings({
        payload_policy: values.payload_policy,
        retention_days: values.retention_days,
        fetch_allowlist: values.fetch_allowlist
          .split('\n')
          .map((line) => line.trim())
          .filter(Boolean),
        default_budget_usd: values.default_budget_usd,
        engine_policy: values.engine_policy,
      }),
    onSuccess: async (saved, _values, context) => {
      // request_id 由 api 层回显；这里在 toast 中展示以便报障
      const requestId = typeof context === 'string' ? context : null;
      const workerScope = saved?.effective?.worker;
      const workerNote = workerScope
        ? `worker 最多 60 秒内生效（服务端声明 ${workerScope}）`
        : 'worker 最多 60 秒内生效';
      toast({
        title: '设置已保存',
        description: `API 立即生效；${workerNote}。${requestId ? `request_id ${requestId}` : ''}`.trim(),
        tone: 'ok',
        requestId,
      });
      setSavedAt(saved?.saved_at ?? new Date().toISOString());
      await queryClient.invalidateQueries({ queryKey: queryKeys.settings.detail() });
      await queryClient.invalidateQueries({ queryKey: queryKeys.settings.audit({ page: 1, page_size: 20, sort: '-at' }) });
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

  const backup = useMutation({
    // `POST /settings/backup/now` 同步执行并返回真实产物名 + sha256；旧 GET 已删除（GET 会 405）
    mutationFn: async () => triggerBackupNow(),
    onSuccess: (result) => {
      setBackupResult(result);
      toast({
        title: '备份已生成',
        description: `产物 ${result.product} · sha256 ${result.sha256.slice(0, 12)}…`,
        tone: 'ok',
      });
    },
    onError: (cause) => {
      const info = describeError(cause);
      toast({
        title: isApiError(cause) && cause.status === 405 ? '备份接口已改为 POST' : '备份失败',
        description: info.hint ? `${info.message}（${info.hint}）` : info.message,
        tone: 'danger',
        requestId: info.requestId,
      });
    },
  });

  const rotateKey = useMutation({
    mutationFn: async () => apiPost<RotateMasterKeyResult>('/admin/settings/rotate-master-key', {}),
    onSuccess: (result) => {
      toast({
        title: result.status === 'done' ? '主密钥轮换完成' : '主密钥轮换进行中',
        description: `轮换记录 ${result.rotation_id}（${result.rows_done ?? 0}/${result.rows_total ?? 0} 行已重加密）`,
        tone: result.status === 'done' ? 'ok' : 'info',
      });
      setRotateOpen(false);
    },
    onError: (cause) => {
      const info = describeError(cause);
      toast({ title: '轮换失败', description: info.message, tone: 'danger', requestId: info.requestId });
    },
  });

  if (settings.isLoading) {
    return (
      <div className="flex flex-col gap-4" aria-busy="true">
        <Skeleton height={64} rounded="md" />
        <Skeleton height={320} rounded="md" />
      </div>
    );
  }

  if (settings.isError) {
    const info = describeError(settings.error);
    return (
      <EmptyState
        kind={info.code === 'NETWORK_OFFLINE' ? 'offline' : 'unavailable'}
        title={info.message}
        description={info.hint ?? undefined}
        requestId={info.requestId}
        actionLabel="重试"
        onAction={() => void settings.refetch()}
      />
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="设置"
        description="这些设置影响审计、数据保留与费用控制。保存后立即生效并写审计。"
        actions={
          <Button
            variant="primary"
            size="sm"
            loading={save.isPending}
            onClick={form.handleSubmit((values) => save.mutate(values))}
            icon={<Save size={16} strokeWidth={1.75} />}
          >
            保存设置
          </Button>
        }
        status={
          settings.data ? (
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={settings.data.env === 'production' ? 'danger' : 'warn'} dot>
                {settings.data.env}
              </Badge>
              <Badge tone="outline">保留 {settings.data.retention_days} 天</Badge>
              <Badge tone="outline">载荷策略 {settings.data.payload_policy}</Badge>
            </div>
          ) : null
        }
      />

      {savedAt ? (
        <p
          role="status"
          className="flex flex-wrap items-center gap-2 rounded border border-border bg-[var(--sr-surface-sunken)] px-3 py-2 text-12 text-content-muted"
        >
          <Badge tone="ok" dot>
            已保存
          </Badge>
          <span>保存于 {formatRelative(savedAt)}</span>
          <span>· API 立即生效；worker 最多 60 秒内生效</span>
        </p>
      ) : null}

      <Tabs defaultValue="general">
        <TabsList>
          <TabsTrigger value="general">通用</TabsTrigger>
          <TabsTrigger value="security">安全</TabsTrigger>
          <TabsTrigger value="budget">预算与配额</TabsTrigger>
          <TabsTrigger value="fetch">外部抓取白名单</TabsTrigger>
          <TabsTrigger value="retention">数据保留</TabsTrigger>
          <TabsTrigger value="audit">审计</TabsTrigger>
          <TabsTrigger value="backup">备份与危险区</TabsTrigger>
        </TabsList>

        <TabsContent value="general">
          <section className="sr-card flex flex-col gap-3 p-4">
            <h2 className="text-16 font-semibold text-content">通用</h2>
            <FormField id="engine-policy" label="引擎升级策略" description="auto：检测到候选自动验证并部署；notify：验证通过后通知管理员；manual：只手动触发。">
              <Select
                value={form.watch('engine_policy')}
                onValueChange={(value) => form.setValue('engine_policy', value as SettingsForm['engine_policy'], { shouldDirty: true })}
                options={[
                  { value: 'auto', label: 'auto（自动部署）' },
                  { value: 'notify', label: 'notify（验证后通知）' },
                  { value: 'manual', label: 'manual（仅手动）' },
                ]}
              />
            </FormField>
            <p className="text-12 text-content-muted">
              当前环境：{settings.data?.env ?? '—'}。生产环境强制 HTTPS Cookie，且禁止开发热重载。
            </p>
          </section>
        </TabsContent>

        <TabsContent value="security">
          <section className="sr-card flex flex-col gap-3 p-4">
            <h2 className="text-16 font-semibold text-content">安全</h2>
            <FormField
              id="payload-policy"
              label="调用记录载荷策略"
              description="none 不保存载荷；redacted_preview 保存脱敏预览；full 保存完整（仅建议测试环境）。"
            >
              <Select
                value={form.watch('payload_policy')}
                onValueChange={(value) =>
                  form.setValue('payload_policy', value as SettingsForm['payload_policy'], { shouldDirty: true })
                }
                options={[
                  { value: 'redacted_preview', label: '脱敏预览（推荐）' },
                  { value: 'none', label: '不保存载荷' },
                  { value: 'full', label: '完整载荷（谨慎）' },
                ]}
              />
            </FormField>
            <div className="rounded border border-border bg-[var(--sr-surface-sunken)] px-3 py-2 text-12 text-content-muted">
              凭证密文使用主密钥加密；页面永不回显明文。CSRF 令牌随会话 cookie（sr_csrf）下发，写请求必须回带。
            </div>
          </section>
        </TabsContent>

        <TabsContent value="budget">
          <section className="sr-card flex flex-col gap-3 p-4">
            <h2 className="text-16 font-semibold text-content">预算与配额</h2>
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              <FormField
                id="default-budget-usd"
                label="默认月预算（USD）"
                hint="新建令牌时的默认预算；0 表示不限制"
                error={form.formState.errors.default_budget_usd?.message}
              >
                <Input type="number" step="0.01" {...form.register('default_budget_usd')} />
              </FormField>
            </div>
            <p className="text-12 text-content-muted">
              令牌级预算在「接入与令牌」页配置；超限时返回 402 BUDGET_EXCEEDED，不做静默降级。
            </p>
            {/* F-05 / F-13：计费口径变更必须显式告知，避免被当成 bug 报障 */}
            <div className="rounded border border-warn bg-[var(--sr-warn-weak)] px-3 py-2 text-12 text-content">
              <p className="font-medium">计费口径变更（本轮起）：</p>
              <ul className="mt-1 flex flex-col gap-0.5 text-content-muted">
                <li>· 流式（stream）、异步（async）与测试调用（测试面板 / Agent 自测）都会计费，不再只有 sync 计费。</li>
                <li>· 因此存量令牌里的小预算可能提前耗尽，随后调用返回 402 BUDGET_EXCEEDED（不是 429）。</li>
                <li>· 遇到 402 请到「接入与令牌」调高该令牌预算，或等到下个计费周期。</li>
              </ul>
            </div>
          </section>
        </TabsContent>

        <TabsContent value="fetch">
          <section className="sr-card flex flex-col gap-3 p-4">
            <h2 className="text-16 font-semibold text-content">外部抓取白名单</h2>
            <FormField
              id="fetch-allowlist"
              label="允许的域名/地址"
              description="每行一条。Agent 的 sr_fetch_source 只允许访问白名单目标；内网地址默认拒绝（SSRF_BLOCKED）。"
            >
              <Textarea rows={8} className="sr-mono text-12" {...form.register('fetch_allowlist')} />
            </FormField>
          </section>
        </TabsContent>

        <TabsContent value="retention">
          <section className="sr-card flex flex-col gap-3 p-4">
            <h2 className="text-16 font-semibold text-content">数据保留</h2>
            <FormField
              id="retention-days"
              label="调用记录 / 任务保留天数"
              error={form.formState.errors.retention_days?.message}
              description="超过保留期的记录会被清理；审计日志单独按合规要求保留。"
            >
              <Input type="number" {...form.register('retention_days')} />
            </FormField>
          </section>
        </TabsContent>

        <TabsContent value="audit">
          <section className="sr-card p-4">
            <h2 className="text-16 font-semibold text-content">审计（最近 20 条）</h2>
            {audit.isLoading ? (
              <Skeleton height={120} className="mt-2" />
            ) : audit.isError ? (
              <p className="mt-2 text-13 text-content-muted">
                审计读取失败：{describeError(audit.error).message}
              </p>
            ) : (audit.data?.items.length ?? 0) === 0 ? (
              <p className="mt-2 text-13 text-content-muted">还没有审计记录。</p>
            ) : (
              <div className="mt-2 overflow-x-auto sr-scroll">
                <table className="w-full text-13">
                  <caption className="sr-only">审计日志</caption>
                  <thead>
                    <tr className="border-b border-border">
                      <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">时间</th>
                      <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">动作</th>
                      <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">对象</th>
                      <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">操作者</th>
                      <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">request_id</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[var(--sr-border)]">
                    {audit.data?.items.map((entry) => (
                      <tr key={entry.id}>
                        <td className="px-2 py-1.5 text-content-muted">{formatDateTime(entry.at)}</td>
                        <td className="px-2 py-1.5 sr-mono text-12 text-content">{entry.action}</td>
                        <td className="px-2 py-1.5 text-content-muted">
                          {entry.object_type ?? '—'}
                          {entry.object_id ? ` · ${entry.object_id}` : ''}
                        </td>
                        <td className="px-2 py-1.5 text-content-muted">
                          {entry.actor_type}
                          {entry.actor_id ? `:${entry.actor_id}` : ''}
                        </td>
                        <td className="px-2 py-1.5 sr-mono text-12 text-content-muted">{entry.request_id ?? '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </TabsContent>

        <TabsContent value="backup">
          <div className="flex flex-col gap-4">
            <section className="sr-card p-4">
              <h2 className="flex items-center gap-1.5 text-16 font-semibold text-content">
                <Database size={16} strokeWidth={1.75} aria-hidden />
                备份
              </h2>
              <p className="mt-1 text-13 text-content-muted">
                备份包含数据库与文件索引（不含主密钥）。恢复需要匹配的主密钥，详见 runbook。
              </p>
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <Button variant="secondary" size="sm" loading={backup.isPending} onClick={() => backup.mutate()}>
                  立即备份
                </Button>
                <span className="text-12 text-content-muted">同步执行（POST /settings/backup/now），返回真实产物名与 sha256。</span>
              </div>
              {backupResult ? (
                <dl className="mt-2 grid grid-cols-1 gap-1 rounded border border-border bg-[var(--sr-surface-sunken)] px-3 py-2 text-12 sm:grid-cols-2">
                  <div>
                    <dt className="text-content-muted">产物名</dt>
                    <dd className="sr-mono break-all text-content">{backupResult.product}</dd>
                  </div>
                  <div>
                    <dt className="text-content-muted">sha256</dt>
                    <dd className="sr-mono break-all text-content">{backupResult.sha256}</dd>
                  </div>
                  <div>
                    <dt className="text-content-muted">file_id</dt>
                    <dd className="sr-mono break-all text-content">{backupResult.file_id}</dd>
                  </div>
                  <div>
                    <dt className="text-content-muted">大小</dt>
                    <dd className="text-content">
                      {backupResult.size === null ? "—" : formatBytes(backupResult.size)}
                    </dd>
                  </div>
                </dl>
              ) : null}
            </section>

            {/* 危险区（独立） */}
            <section aria-label="危险操作" className="rounded-md border border-danger p-4">
              <h2 className="flex items-center gap-1.5 text-16 font-semibold text-danger">
                <ShieldAlert size={16} strokeWidth={1.75} aria-hidden />
                危险区
              </h2>
              <p className="mt-1 text-13 text-content">
                轮换主密钥会在后台重加密所有凭证密文。期间旧密钥仍可解密（渐进式），完成后旧密钥失效。
              </p>
              <ul className="mt-1 flex flex-col gap-0.5 text-12 text-content-muted">
                <li>· 影响：全部凭证行（重加密），不影响调用方。</li>
                <li>· 必须已验证备份可用，否则轮换失败将难以恢复。</li>
              </ul>
              <Button
                variant="danger"
                size="sm"
                className="mt-2"
                onClick={() => setRotateOpen(true)}
                icon={<KeyRound size={16} strokeWidth={1.75} />}
              >
                轮换主密钥
              </Button>
            </section>
          </div>
        </TabsContent>
      </Tabs>

      {save.isError ? <RequestIdCopy requestId={describeError(save.error).requestId ?? ''} /> : null}

      <DangerConfirm
        open={rotateOpen}
        onOpenChange={setRotateOpen}
        title="轮换主密钥"
        action="轮换"
        description="将重新加密全部凭证密文。请确认已具备可用备份。"
        impacts={[
          { id: 'master-key', label: '全部凭证密文（重新加密）', detail: '完成后旧密钥失效' },
          { id: 'backup', label: '需要可用备份', detail: backupResult?.file_id ?? '尚未在本页生成备份' },
        ]}
        blockedReason={backupResult ? null : '建议先完成一次备份再轮换（可继续，但风险自担）。'}
        loading={rotateKey.isPending}
        onConfirm={() => rotateKey.mutateAsync()}
      />
    </div>
  );
}
