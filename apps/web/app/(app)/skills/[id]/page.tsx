'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Bot, CircleAlert, Download, FileCode2, Play, Send } from 'lucide-react';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import * as React from 'react';
import { DangerConfirm } from '@/components/common/DangerConfirm';
import { EmptyState, RequestIdCopy } from '@/components/common/EmptyState';
import { PageHeader } from '@/components/common/PageHeader';
import { StatusBadge } from '@/components/common/StatusBadge';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { Switch } from '@/components/ui/switch';
import { useToast } from '@/components/ui/toast';
import { apiGet, apiPost, describeError } from '@/lib/api';
import { formatDateTime } from '@/lib/format';
import { queryKeys } from '@/lib/query';
import type { JsonObject, SkillDetail, SkillPlanStep, SkillPreview } from '@/lib/types';

/**
 * SKILL 详情（spec §13.2 `/skills/[id]`）：
 * 说明 / 适用范围 / 输入输出 / 依赖能力（版本区间）/ 执行计划树 / 权限 / 调用测试 / 导出与 SKILL.md 预览。
 * 依赖缺失时发布按钮禁用并列出缺失项；agent_reasoning 步骤用徽标标注；停用后顶部显示横幅。
 */
export default function SkillDetailPage() {
  const params = useParams<{ id: string }>();
  const skillId = params?.id ?? '';
  const queryClient = useQueryClient();
  const { toast } = useToast();

  const [previewOpen, setPreviewOpen] = React.useState(false);
  const [publishOpen, setPublishOpen] = React.useState(false);
  const [publishVersion, setPublishVersion] = React.useState('');
  const [testInput, setTestInput] = React.useState('{\n  "prompt": "你好"\n}');
  const [testResult, setTestResult] = React.useState<string | null>(null);

  const detail = useQuery<SkillDetail>({
    queryKey: queryKeys.skills.detail(skillId),
    queryFn: () => apiGet<SkillDetail>(`/admin/skills/${skillId}`),
    enabled: Boolean(skillId),
  });

  const preview = useQuery<SkillPreview>({
    queryKey: queryKeys.skills.preview(skillId),
    queryFn: () => apiGet<SkillPreview>(`/admin/skills/${skillId}/preview`),
    enabled: previewOpen && Boolean(skillId),
    retry: false,
  });

  const invalidate = async (): Promise<void> => {
    await queryClient.invalidateQueries({ queryKey: queryKeys.skills.detail(skillId) });
    await queryClient.invalidateQueries({ queryKey: queryKeys.skills.all });
  };

  const toggleEnabled = useMutation({
    mutationFn: async (enable: boolean) => apiPost(`/admin/skills/${skillId}/${enable ? 'enable' : 'disable'}`, {}),
    onSuccess: async (_data, enable) => {
      toast({
        title: enable ? '已启用' : '已停用',
        description: enable ? '对外重新可见并可执行。' : '对外立即不可见；版本号不变。',
        tone: 'ok',
      });
      await invalidate();
    },
    onError: (cause) => {
      const info = describeError(cause);
      toast({ title: '操作失败', description: info.message, tone: 'danger', requestId: info.requestId });
    },
  });

  const test = useMutation({
    mutationFn: async () => {
      const parsed = JSON.parse(testInput) as Record<string, unknown>;
      return apiPost<{ ok?: boolean; output?: unknown; error?: { code?: string; message?: string } }>(
        `/admin/skills/${skillId}/test`,
        { input: parsed },
      );
    },
    onSuccess: (result) => {
      setTestResult(JSON.stringify(result, null, 2));
      toast({ title: result.ok === false ? '试运行未通过' : '试运行完成', tone: result.ok === false ? 'warn' : 'ok' });
    },
    onError: (cause) => {
      const info = describeError(cause);
      setTestResult(`失败：${info.message}${info.hint ? `（${info.hint}）` : ''}`);
      toast({ title: '试运行失败', description: info.message, tone: 'danger', requestId: info.requestId });
    },
  });

  const publish = useMutation({
    mutationFn: async () => apiPost(`/admin/skills/${skillId}/publish`, { version: publishVersion }),
    onSuccess: async () => {
      toast({ title: '已发布', description: `版本 ${publishVersion}`, tone: 'ok' });
      setPublishOpen(false);
      await invalidate();
    },
    onError: (cause) => {
      const info = describeError(cause);
      toast({
        title: info.code === 'SKILL_DEPENDENCY_MISSING' ? '依赖不满足，无法发布' : '发布失败',
        description: info.hint ? `${info.message}（${info.hint}）` : info.message,
        tone: 'danger',
        requestId: info.requestId,
      });
    },
  });

  const exportSkill = useMutation({
    mutationFn: async (format: 'npm' | 'zip' | 'skill_md') =>
      apiPost<{ file_id: string; filename: string; download_url: string; install_command: string }>(
        `/admin/skills/${skillId}/export`,
        { format },
      ),
    onSuccess: (result, format) => {
      // 后端只返回产物 file_id 与下载地址（连接包不含密钥），这里按真实地址打开，
      // 不能声称「已开始下载」而不给下载入口。
      if (result.download_url) {
        window.open(result.download_url, '_blank');
      }
      toast({
        title: '导出已生成',
        description:
          format === 'npm'
            ? `npm 包 ${result.filename}（连接包不含密钥）`
            : `产物 ${result.filename}（连接包不含密钥）`,
        tone: 'ok',
      });
    },
    onError: (cause) => {
      const info = describeError(cause);
      toast({ title: '导出失败', description: info.message, tone: 'danger', requestId: info.requestId });
    },
  });

  if (detail.isLoading) {
    return (
      <div className="flex flex-col gap-4" aria-busy="true">
        <Skeleton height={72} rounded="md" />
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
          <Skeleton height={360} rounded="md" className="lg:col-span-2" />
          <Skeleton height={360} rounded="md" />
        </div>
      </div>
    );
  }

  if (detail.isError) {
    const info = describeError(detail.error);
    return (
      <EmptyState
        kind={info.code === 'NOT_FOUND' ? 'empty' : 'unavailable'}
        title={info.code === 'NOT_FOUND' ? 'SKILL 不存在' : info.message}
        description={info.hint ?? undefined}
        requestId={info.requestId}
        actionLabel="返回列表"
        onAction={() => window.location.assign('/skills')}
      />
    );
  }

  const skill = detail.data;
  if (!skill) return null;

  // 后端直接算出「哪些依赖不满足」，不要在前端靠 dep.published 猜（该字段不存在）
  const missingDependencies = skill.missing_dependencies ?? [];
  const canPublish = missingDependencies.length === 0;
  const planSteps = ((skill.definition?.execution_plan as JsonObject | undefined)?.steps ??
    []) as unknown as SkillPlanStep[];
  const permissions = (skill.definition?.permissions as JsonObject | undefined) ?? {};

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={skill.title}
        breadcrumbs={[{ label: 'SKILL', href: '/skills' }]}
        description={skill.description || '（无说明）'}
        status={
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge kind="skill" status={skill.status} />
            <Badge tone={skill.enabled ? 'ok' : 'danger'} dot>
              {skill.enabled ? '已启用' : '已停用'}
            </Badge>
            <Badge tone="outline" mono>
              {skill.latest_version ?? '未发布'}
            </Badge>
            <Badge tone="neutral">{skill.dependencies.length} 个依赖</Badge>
          </div>
        }
        actions={
          <>
            <Switch
              id="skill-enabled"
              checked={skill.enabled}
              disabled={toggleEnabled.isPending}
              onCheckedChange={(checked) => toggleEnabled.mutate(checked)}
              label={`${skill.enabled ? '停用' : '启用'} SKILL`}
            />
            <Button variant="secondary" size="sm" onClick={() => setPreviewOpen((v) => !v)} icon={<FileCode2 size={16} strokeWidth={1.75} />}>
              预览 SKILL.md
            </Button>
            <Button
              variant="secondary"
              size="sm"
              loading={exportSkill.isPending}
              onClick={() => exportSkill.mutate('zip')}
              icon={<Download size={16} strokeWidth={1.75} />}
            >
              导出连接包
            </Button>
            <Button
              variant="primary"
              size="sm"
              disabled={!canPublish}
              onClick={() => {
                setPublishVersion(skill.latest_version ? bumpVersion(skill.latest_version) : '1.0.0');
                setPublishOpen(true);
              }}
              icon={<Send size={16} strokeWidth={1.75} />}
            >
              发布
            </Button>
          </>
        }
      />

      {!skill.enabled ? (
        <div role="status" className="rounded border border-danger bg-[var(--sr-danger-weak)] px-3 py-2 text-13 text-content">
          当前对外不可见：该 SKILL 已停用。启用后立即恢复分发与执行，版本号不会改变。
        </div>
      ) : null}

      {missingDependencies.length > 0 ? (
        <div role="alert" className="rounded border border-warn bg-[var(--sr-warn-weak)] px-3 py-2">
          <p className="flex items-center gap-1.5 text-13 text-content">
            <CircleAlert size={16} strokeWidth={1.75} className="text-warn" aria-hidden />
            依赖缺失，发布已禁用。缺失项：
          </p>
          <ul className="mt-1 flex flex-col gap-0.5">
            {missingDependencies.map((dep) => (
              <li key={dep.capability_key} className="text-12 text-content-muted">
                · <code className="sr-mono">{dep.capability_key}</code>
                {dep.version_range ? ` 需要 ${dep.version_range}` : ''}（{dep.reason}）
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {previewOpen ? (
        <section aria-label="SKILL.md 预览" className="sr-card p-4">
          <h2 className="text-16 font-semibold text-content">SKILL.md 预览</h2>
          {preview.isLoading ? (
            <Skeleton height={160} className="mt-2" />
          ) : preview.isError ? (
            <p className="mt-2 text-13 text-content-muted">
              预览失败：{describeError(preview.error).message}
            </p>
          ) : (
            <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-words rounded bg-[var(--sr-surface-sunken)] p-3 text-12 sr-mono sr-scroll">
              {preview.data?.skill_md ?? '（空）'}
            </pre>
          )}
        </section>
      ) : null}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="flex flex-col gap-4 lg:col-span-2">
          {/* 依赖 */}
          <section aria-label="依赖能力" className="sr-card p-4">
            <h2 className="text-16 font-semibold text-content">依赖能力（{skill.dependencies.length}）</h2>
            {skill.dependencies.length === 0 ? (
              <p className="mt-2 text-13 text-content-muted">该 SKILL 不依赖任何平台能力。</p>
            ) : (
              <table className="mt-2 w-full text-13">
                <caption className="sr-only">依赖能力与版本区间</caption>
                <thead>
                  <tr className="border-b border-border">
                    <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">能力</th>
                    <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">版本区间</th>
                    <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">状态</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--sr-border)]">
                  {skill.dependencies.map((dep) => {
                    const missing = missingDependencies.find((item) => item.capability_key === dep.capability_key);
                    return (
                      <tr key={dep.capability_key}>
                        <td className="px-2 py-1.5 sr-mono text-12 text-content">{dep.capability_key}</td>
                        <td className="px-2 py-1.5 sr-mono text-12 text-content-muted">{dep.version_range ?? '任意'}</td>
                        <td className="px-2 py-1.5">
                          <Badge tone={missing ? 'danger' : 'ok'} dot>
                            {missing ? missing.reason : '满足'}
                          </Badge>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </section>

          {/* 执行计划 */}
          <section aria-label="执行计划" className="sr-card p-4">
            <h2 className="text-16 font-semibold text-content">执行计划</h2>
            {planSteps.length === 0 ? (
              <p className="mt-2 text-13 text-content-muted">还没有执行计划步骤。</p>
            ) : (
              <ol className="mt-2 flex flex-col gap-1.5">
                {planSteps.map((step, index) => (
                  <PlanNode key={step.id} step={step} index={index} />
                ))}
              </ol>
            )}
          </section>

          {/* 权限要求 */}
          <section aria-label="权限要求" className="sr-card p-4">
            <h2 className="text-16 font-semibold text-content">权限要求</h2>
            <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded bg-[var(--sr-surface-sunken)] p-2 text-12 sr-mono sr-scroll">
              {JSON.stringify(permissions, null, 2)}
            </pre>
          </section>
        </div>

        <aside className="flex flex-col gap-4">
          {/* 调用测试 */}
          <section aria-label="调用测试" className="sr-card p-4">
            <h2 className="text-16 font-semibold text-content">调用测试</h2>
            <p className="mt-0.5 text-12 text-content-muted">按 execution_plan 试运行，会校验依赖能力版本。</p>
            <label className="mt-2 flex flex-col gap-1">
              <span className="text-12 text-content-muted">输入（JSON）</span>
              <textarea
                value={testInput}
                onChange={(event) => setTestInput(event.target.value)}
                rows={6}
                aria-label="测试输入"
                className="w-full rounded border border-border bg-surface px-2 py-1.5 text-12 sr-mono text-content focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
              />
            </label>
            <Button
              variant="primary"
              size="sm"
              className="mt-2"
              loading={test.isPending}
              onClick={() => test.mutate()}
              icon={<Play size={14} strokeWidth={1.75} />}
            >
              试运行
            </Button>
            {testResult ? (
              <>
                <pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-words rounded bg-[var(--sr-surface-sunken)] p-2 text-12 sr-mono sr-scroll">
                  {testResult}
                </pre>
                {test.isError ? (
                  <RequestIdCopy requestId={describeError(test.error).requestId ?? ''} className="mt-1" />
                ) : null}
              </>
            ) : null}
          </section>

          {/* 元信息 */}
          <section aria-label="元信息" className="sr-card p-4">
            <h2 className="text-16 font-semibold text-content">元信息</h2>
            <dl className="mt-2 flex flex-col gap-1.5 text-13">
              <div className="flex justify-between">
                <dt className="text-content-muted">name</dt>
                <dd className="sr-mono text-content">{skill.name}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-content-muted">分类</dt>
                <dd className="text-content">{skill.category ?? '—'}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-content-muted">来源</dt>
                <dd className="text-content">
                  {skill.source_type}
                  {skill.source_ref ? <span className="sr-mono ml-1 text-12">{skill.source_ref}</span> : null}
                </dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-content-muted">发布版本</dt>
                <dd className="sr-mono text-content">{skill.latest_version ?? '—'}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-content-muted">更新时间</dt>
                <dd className="text-content">{formatDateTime(skill.updated_at)}</dd>
              </div>
            </dl>
            <div className="mt-3 flex flex-wrap gap-1">
              <Button variant="secondary" size="sm" onClick={() => exportSkill.mutate('npm')}>
                导出 npm
              </Button>
              <Button variant="secondary" size="sm" onClick={() => exportSkill.mutate('zip')}>
                导出 zip
              </Button>
              <Button variant="secondary" size="sm" onClick={() => exportSkill.mutate('skill_md')}>
                导出 SKILL.md
              </Button>
            </div>
            <p className="mt-2 text-12 text-content-muted">连接包不含任何密钥；令牌需外部 Agent 自行配置。</p>
          </section>
        </aside>
      </div>

      <DangerConfirm
        open={publishOpen}
        onOpenChange={setPublishOpen}
        title="发布 SKILL 版本"
        action="发布"
        description="发布后该版本对全部持有令牌的 Agent 可见（前提是已启用）。"
        impacts={[
          { id: skill.id, label: `${skill.name} ${publishVersion}`, detail: `${skill.dependencies.length} 个依赖能力` },
        ]}
        confirmLabel={`发布 ${publishVersion}`}
        loading={publish.isPending}
        extra={
          <label className="flex flex-col gap-1">
            <span className="text-13 text-content">版本号（语义化）</span>
            <input
              value={publishVersion}
              onChange={(event) => setPublishVersion(event.target.value)}
              aria-label="发布版本号"
              className="h-9 w-full rounded border border-border bg-surface px-3 sr-mono text-13 focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
            />
          </label>
        }
        onConfirm={() => publish.mutateAsync()}
      />
    </div>
  );
}

function PlanNode({ step, index }: { step: SkillPlanStep; index: number }) {
  return (
    <li className="rounded border border-border px-2 py-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <span aria-hidden className="text-12 text-content-muted">
          {index + 1}.
        </span>
        <span className="sr-mono text-13 text-content">{step.id}</span>
        <Badge tone="outline" mono>
          {step.type}
        </Badge>
        {step.type === 'agent_reasoning' ? (
          <Badge tone="info">
            <Bot size={12} strokeWidth={1.75} aria-hidden />
            需实时 Agent 推理
          </Badge>
        ) : null}
        {step.capability_key ? (
          <Link
            href={`/capabilities?q=${encodeURIComponent(step.capability_key)}`}
            className="sr-mono text-12 text-accent hover:underline"
          >
            {step.capability_key}
            {step.version ? `@${step.version}` : ''}
          </Link>
        ) : null}
        {step.output_as ? <Badge tone="neutral">→ {step.output_as}</Badge> : null}
        {step.on_error ? <Badge tone="neutral">on_error {step.on_error}</Badge> : null}
      </div>
    </li>
  );
}

function bumpVersion(version: string): string {
  const match = /^(\d+)\.(\d+)\.(\d+)$/.exec(version);
  if (!match) return '1.0.0';
  const major = Number(match[1] ?? 0);
  const minor = Number(match[2] ?? 0);
  return `${major}.${minor + 1}.0`;
}
