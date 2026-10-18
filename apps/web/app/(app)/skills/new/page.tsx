'use client';

import { useMutation } from '@tanstack/react-query';
import { ArrowLeft, ArrowRight, CircleAlert, FileArchive, GitBranch, Upload } from 'lucide-react';
import { useRouter } from 'next/navigation';
import * as React from 'react';
import { EmptyState, RequestIdCopy } from '@/components/common/EmptyState';
import { FormField } from '@/components/common/FormField';
import { PageHeader } from '@/components/common/PageHeader';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/components/ui/cn';
import { Input } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import { Textarea } from '@/components/ui/textarea';
import { useToast } from '@/components/ui/toast';
import { apiPost, describeError } from '@/lib/api';
import { formatBytes } from '@/lib/format';
import type { Skill, SkillImportPreview } from '@/lib/types';

/**
 * 创建 / 导入 SKILL（spec §13.2 `/skills/new`）：
 * git / url / file 三种来源 → 预览（文件清单、commit、权限、警告）→ 用 preview_token 落地为草稿。
 * 失败原因必须可操作（如缺少 SKILL.md）。
 */

type SourceKind = 'git' | 'url' | 'file';

export default function NewSkillPage() {
  const router = useRouter();
  const { toast } = useToast();

  const [step, setStep] = React.useState<1 | 2>(1);
  const [kind, setKind] = React.useState<SourceKind>('git');
  const [url, setUrl] = React.useState('');
  const [ref, setRef] = React.useState('');
  const [uploadId, setUploadId] = React.useState('');
  const [preview, setPreview] = React.useState<SkillImportPreview | null>(null);
  const [error, setError] = React.useState<{ message: string; hint: string | null; code: string; requestId: string | null } | null>(
    null,
  );

  // 手工创建
  const [manualName, setManualName] = React.useState('');
  const [manualTitle, setManualTitle] = React.useState('');
  const [manualDescription, setManualDescription] = React.useState('');
  const [manualCategory, setManualCategory] = React.useState('');

  const previewMutation = useMutation({
    mutationFn: async () =>
      apiPost<SkillImportPreview>('/admin/skills/import/preview', {
        kind,
        url: kind === 'file' ? null : url,
        ref: ref || null,
        upload_id: kind === 'file' ? uploadId : null,
      }),
    onSuccess: (data) => {
      setPreview(data);
      setError(null);
      setStep(2);
    },
    onError: (cause) => {
      const info = describeError(cause);
      setError({ message: info.message, hint: info.hint, code: info.code, requestId: info.requestId });
    },
  });

  const commitMutation = useMutation({
    mutationFn: async () =>
      apiPost<Skill>('/admin/skills/import/commit', { preview_token: preview?.preview_token }),
    onSuccess: (skill) => {
      toast({ title: '已导入为草稿', description: '别忘了配置依赖并发布版本。', tone: 'ok' });
      router.push(`/skills/${skill.id}`);
    },
    onError: (cause) => {
      const info = describeError(cause);
      setError({ message: info.message, hint: info.hint, code: info.code, requestId: info.requestId });
    },
  });

  const createManual = useMutation({
    mutationFn: async () =>
      apiPost<Skill>('/admin/skills', {
        name: manualName,
        title: manualTitle || manualName,
        description: manualDescription,
        category: manualCategory || null,
        source_type: 'manual',
      }),
    onSuccess: (skill) => {
      toast({ title: 'SKILL 草稿已创建', tone: 'ok' });
      router.push(`/skills/${skill.id}`);
    },
    onError: (cause) => {
      const info = describeError(cause);
      setError({ message: info.message, hint: info.hint, code: info.code, requestId: info.requestId });
    },
  });

  const hasSkillMd = preview?.files.some((file) => /(^|\/)SKILL\.md$/i.test(file.path)) ?? false;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="新建 / 导入 SKILL"
        breadcrumbs={[{ label: 'SKILL', href: '/skills' }]}
        description="导入会先生成预览（文件清单、commit、权限、警告），确认后才落库为草稿。"
        status={
          <div className="flex items-center gap-2 text-12">
            <Badge tone={step === 1 ? 'accent' : 'ok'} dot>
              1 选择来源
            </Badge>
            <ArrowRight size={12} strokeWidth={1.75} aria-hidden />
            <Badge tone={step === 2 ? 'accent' : 'neutral'} dot>
              2 预览并确认
            </Badge>
          </div>
        }
      />

      {error ? (
        <div role="alert" className="rounded border border-danger bg-[var(--sr-danger-weak)] px-3 py-2">
          <p className="text-13 text-content">{error.message}</p>
          {error.hint ? <p className="mt-1 text-12 text-content-muted">{error.hint}</p> : null}
          {error.code === 'SKILL_IMPORT_INVALID' ? (
            <p className="mt-1 text-12 text-content-muted">
              常见原因：仓库根目录缺少 <code className="sr-mono">SKILL.md</code>，或 frontmatter 缺少 name/description。
            </p>
          ) : null}
          {error.requestId ? <RequestIdCopy requestId={error.requestId} className="mt-1" /> : null}
        </div>
      ) : null}

      {step === 1 ? (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <section className="sr-card flex flex-col gap-3 p-4" aria-label="从仓库或 URL 导入">
            <h2 className="text-16 font-semibold text-content">导入来源</h2>
            <div className="flex flex-wrap gap-1" role="group" aria-label="来源类型">
              {(
                [
                  { value: 'git' as SourceKind, label: 'Git 仓库', icon: <GitBranch size={14} strokeWidth={1.75} aria-hidden /> },
                  { value: 'url' as SourceKind, label: 'URL', icon: <FileArchive size={14} strokeWidth={1.75} aria-hidden /> },
                  { value: 'file' as SourceKind, label: '上传包', icon: <Upload size={14} strokeWidth={1.75} aria-hidden /> },
                ] as const
              ).map((option) => (
                <button
                  key={option.value}
                  type="button"
                  aria-pressed={kind === option.value}
                  onClick={() => setKind(option.value)}
                  className={cn(
                    'inline-flex items-center gap-1 rounded-sm border px-2 py-1 text-13 focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]',
                    kind === option.value
                      ? 'border-accent bg-[var(--sr-accent-weak)] text-accent'
                      : 'border-border text-content-muted hover:bg-hover',
                  )}
                >
                  {option.icon}
                  {option.label}
                </button>
              ))}
            </div>

            {kind === 'file' ? (
              <FormField id="upload-id" label="上传包 file_id" hint="先通过 POST /api/v1/files 上传 zip，再填入返回的 file_id">
                <Input value={uploadId} onChange={(event) => setUploadId(event.target.value)} className="sr-mono" />
              </FormField>
            ) : (
              <>
                <FormField id="url" label="仓库 / 文档 URL" required>
                  <Input
                    value={url}
                    onChange={(event) => setUrl(event.target.value)}
                    placeholder="https://github.com/acme/skill-demo"
                  />
                </FormField>
                {kind === 'git' ? (
                  <FormField id="ref" label="分支 / tag / commit" hint="留空取默认分支 HEAD">
                    <Input value={ref} onChange={(event) => setRef(event.target.value)} className="sr-mono" />
                  </FormField>
                ) : null}
              </>
            )}

            <div className="mt-1 flex items-center gap-2">
              <Button
                variant="primary"
                size="sm"
                loading={previewMutation.isPending}
                disabled={kind === 'file' ? !uploadId : !url}
                onClick={() => previewMutation.mutate()}
              >
                生成预览
              </Button>
              <span className="text-12 text-content-muted">不会直接落库；预览后才可提交。</span>
            </div>
          </section>

          <section className="sr-card flex flex-col gap-3 p-4" aria-label="手工创建">
            <h2 className="text-16 font-semibold text-content">手工创建草稿</h2>
            <FormField id="skill-name" label="name（唯一，小写）" required>
              <Input
                value={manualName}
                onChange={(event) => setManualName(event.target.value)}
                className="sr-mono"
                placeholder="image-cover-generator"
              />
            </FormField>
            <FormField id="skill-title" label="标题">
              <Input value={manualTitle} onChange={(event) => setManualTitle(event.target.value)} />
            </FormField>
            <FormField id="skill-category" label="分类">
              <Select
                value={manualCategory || undefined}
                onValueChange={setManualCategory}
                placeholder="选择分类（可选）"
                options={[
                  { value: 'media', label: '媒体' },
                  { value: 'text', label: '文本' },
                  { value: 'ops', label: '运维' },
                  { value: 'other', label: '其它' },
                ]}
              />
            </FormField>
            <FormField id="skill-description" label="说明">
              <Textarea
                rows={4}
                value={manualDescription}
                onChange={(event) => setManualDescription(event.target.value)}
                placeholder="这个 SKILL 解决什么问题、适用于什么场景"
              />
            </FormField>
            <Button
              variant="secondary"
              size="sm"
              className="w-fit"
              loading={createManual.isPending}
              disabled={!/^[a-z0-9][a-z0-9-]*$/.test(manualName)}
              onClick={() => createManual.mutate()}
            >
              创建草稿
            </Button>
          </section>
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          {preview ? (
            <>
              <section className="sr-card p-4" aria-label="导入预览">
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="text-16 font-semibold text-content">预览</h2>
                  <Badge tone="outline" mono>
                    {preview.kind}
                  </Badge>
                  {preview.resolved_commit ? <Badge tone="info" mono>commit {preview.resolved_commit.slice(0, 10)}</Badge> : null}
                  <Badge tone={hasSkillMd ? 'ok' : 'danger'} dot>
                    {hasSkillMd ? '找到 SKILL.md' : '缺少 SKILL.md'}
                  </Badge>
                  {preview.requires_sandbox_review ? <Badge tone="warn">需沙箱复核</Badge> : null}
                  <span className="text-12 text-content-muted">preview_token {preview.preview_token.slice(0, 8)}…</span>
                </div>

                {preview.warnings.length > 0 ? (
                  <ul className="mt-3 flex flex-col gap-1 rounded border border-warn bg-[var(--sr-warn-weak)] px-3 py-2">
                    {preview.warnings.map((warning, index) => (
                      <li key={index} className="flex items-start gap-1.5 text-12 text-content">
                        <CircleAlert size={12} strokeWidth={1.75} className="mt-0.5 text-warn" aria-hidden />
                        {warning}
                      </li>
                    ))}
                  </ul>
                ) : null}

                <div className="mt-3 overflow-x-auto sr-scroll">
                  <table className="w-full text-13">
                    <caption className="px-1 py-1 text-left text-12 text-content-muted">
                      预览文件清单（{preview.files.length} 个文件）
                    </caption>
                    <thead>
                      <tr className="border-b border-border">
                        <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">路径</th>
                        <th scope="col" className="px-2 py-1.5 text-right text-12 font-medium text-content-muted">大小</th>
                        <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">类型</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-[var(--sr-border)]">
                      {preview.files.map((file) => (
                        <tr key={file.path}>
                          <td className="px-2 py-1.5 sr-mono text-12 text-content">{file.path}</td>
                          <td className="px-2 py-1.5 text-right text-content-muted">{formatBytes(file.size)}</td>
                          <td className="px-2 py-1.5 text-content-muted">
                            {file.mime ?? '—'}
                            {file.executable ? <Badge tone="warn" className="ml-1">可执行</Badge> : null}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                {preview.skill_md_preview ? (
                  <details className="mt-3">
                    <summary className="cursor-pointer text-12 text-content-muted">查看 SKILL.md 预览</summary>
                    <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-[var(--sr-surface-sunken)] p-2 text-12 sr-mono sr-scroll">
                      {preview.skill_md_preview}
                    </pre>
                  </details>
                ) : null}
              </section>

              <div className="flex items-center justify-between border-t border-border pt-3">
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => {
                    setStep(1);
                    setPreview(null);
                  }}
                  icon={<ArrowLeft size={16} strokeWidth={1.75} />}
                >
                  返回上一步
                </Button>
                <div className="flex items-center gap-2">
                  {!hasSkillMd ? (
                    <span className="text-12 text-danger">缺少 SKILL.md：需在仓库根目录提供</span>
                  ) : null}
                  <Button
                    variant="primary"
                    size="sm"
                    loading={commitMutation.isPending}
                    disabled={!hasSkillMd}
                    onClick={() => commitMutation.mutate()}
                  >
                    落地为草稿
                  </Button>
                </div>
              </div>
            </>
          ) : (
            <EmptyState
              kind="empty"
              title="没有可用的预览"
              description="回到上一步重新生成预览。"
              actionLabel="返回"
              onAction={() => setStep(1)}
            />
          )}
        </div>
      )}
    </div>
  );
}
