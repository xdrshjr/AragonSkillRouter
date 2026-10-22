'use client';

/**
 * SKILL dialogs: create/edit, delete-with-impact, and the three-step import flow
 * (source → preview review → permission confirmation → commit).
 *
 * The import dialog never claims anything it did not see: the file list, the
 * flagged scripts, the requested permissions and the validation issues all come
 * from `POST /api/admin/skills/import/preview`, and the commit is only enabled
 * once the preview says what it will create.
 */

import { useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import { AlertTriangle, CheckCircle2, FileWarning, Upload } from 'lucide-react';

import { describeError, errorRequestId } from '@/lib/api';
import { formatBytes } from '@/lib/format';
import { uploadFile } from '@/lib/agent';
import { Badge } from '@/components/ui/badge';
import { Banner, InlineAlert } from '@/components/ui/banner';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { CodeBlock, prettyJson } from '@/components/ui/code-block';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { Dialog } from '@/components/ui/dialog';
import { EmptyState } from '@/components/ui/empty-state';
import { Field, FormRow } from '@/components/ui/field';
import { Input, Textarea } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import { Stepper, type Step } from '@/components/ui/stepper';
import { useToast } from '@/components/ui/toast';
import { useGuardedRouter } from '@/lib/navigation-guard';
import type { Skill, SkillImportPreview } from './api';
import {
  useCreateSkillMutation,
  useDeleteSkillMutation,
  useImportCommitMutation,
  useImportPreviewMutation,
  useUpdateSkillMutation,
} from './api';

const SKILL_STATUS_OPTIONS = [
  { value: 'draft', label: 'Draft' },
  { value: 'published', label: 'Published' },
  { value: 'disabled', label: 'Disabled' },
];

/* ---------------------------------------------------------------- form dialog -- */

export function SkillFormDialog({
  open,
  onClose,
  skill,
}: {
  open: boolean;
  onClose: () => void;
  skill?: Skill | null;
}) {
  const toast = useToast();
  const router = useGuardedRouter();
  const create = useCreateSkillMutation();
  const update = useUpdateSkillMutation(skill?.id ?? '');
  const editing = Boolean(skill);

  const [name, setName] = useState('');
  const [slug, setSlug] = useState('');
  const [description, setDescription] = useState('');
  const [category, setCategory] = useState('general');
  const [status, setStatus] = useState<Skill['status']>('draft');

  useEffect(() => {
    if (!open) return;
    setName(skill?.name ?? '');
    setSlug(skill?.slug ?? '');
    setDescription(skill?.description ?? '');
    setCategory(skill?.category ?? 'general');
    setStatus(skill?.status ?? 'draft');
  }, [open, skill]);

  const pending = create.isPending || update.isPending;
  const canSubmit = name.trim().length > 0 && (editing || category.trim().length > 0);

  const submit = async () => {
    if (!canSubmit) return;
    try {
      if (editing && skill) {
        const updated = await update.mutateAsync({
          name: name.trim(),
          description,
          category: category.trim() || 'general',
          status,
        });
        toast.success({ title: 'SKILL saved', description: `${updated.name} is ready to edit in the Manifest tab.` });
        onClose();
      } else {
        const created = await create.mutateAsync({
          name: name.trim(),
          slug: slug.trim() || undefined,
          description,
          category: category.trim() || 'general',
        });
        toast.success({ title: 'SKILL created', description: 'Version 1 is a draft; add a plan before publishing.' });
        onClose();
        router.push(`/skills/${created.id}`);
      }
    } catch (cause) {
      toast.apiError(cause, editing ? 'The SKILL was not saved' : 'The SKILL was not created');
    }
  };

  const mutation = editing ? update : create;

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={editing ? `Edit ${skill?.name}` : 'New SKILL'}
      description={
        editing
          ? 'These fields describe the SKILL in the catalogue; the manifest is edited on the detail page.'
          : 'A SKILL composes capability calls into a plan an external agent can follow.'
      }
      size="md"
      dismissible={!pending}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={pending}>
            Cancel
          </Button>
          <Button variant="primary" onClick={() => void submit()} loading={pending} disabled={!canSubmit}>
            {editing ? 'Save SKILL' : 'Create SKILL'}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        {mutation.isError ? (
          <Banner tone="danger" title="The last attempt failed" requestId={errorRequestId(mutation.error)}>
            {describeError(mutation.error)}
          </Banner>
        ) : null}

        <FormRow columns={2}>
          <Field label="Name" htmlFor="skill-name" required>
            <Input
              id="skill-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="Summarise a transcript"
              maxLength={160}
            />
          </Field>
          <Field
            label="Slug"
            htmlFor="skill-slug"
            hint={editing ? 'The slug is immutable once the SKILL exists.' : 'Optional; derived from the name.'}
          >
            <Input
              id="skill-slug"
              value={slug}
              onChange={(event) => setSlug(event.target.value)}
              disabled={editing}
              placeholder="summarise-transcript"
            />
          </Field>
        </FormRow>

        <Field label="Description" htmlFor="skill-description" hint="One sentence an agent reads first.">
          <Textarea
            id="skill-description"
            value={description}
            rows={3}
            maxLength={2000}
            onChange={(event) => setDescription(event.target.value)}
          />
        </Field>

        <FormRow columns={2}>
          <Field label="Category" htmlFor="skill-category" required>
            <Input
              id="skill-category"
              value={category}
              onChange={(event) => setCategory(event.target.value)}
              placeholder="media"
              maxLength={60}
            />
          </Field>
          {editing ? (
            <Field
              label="Status"
              htmlFor="skill-status"
              hint="Publishing a version sets this automatically; disabling keeps the record."
            >
              <Select
                id="skill-status"
                options={SKILL_STATUS_OPTIONS}
                value={status}
                onChange={(event) => setStatus(event.target.value as Skill['status'])}
              />
            </Field>
          ) : null}
        </FormRow>
      </div>
    </Dialog>
  );
}

/* -------------------------------------------------------------- delete dialog -- */

export function SkillDeleteDialog({
  open,
  onClose,
  skill,
}: {
  open: boolean;
  onClose: () => void;
  skill: Skill;
}) {
  const toast = useToast();
  const router = useGuardedRouter();
  const remove = useDeleteSkillMutation(skill.id);
  const impact = skill.dependency_impact;

  const run = async () => {
    try {
      await remove.mutateAsync();
      toast.success({ title: 'SKILL deleted', description: skill.name });
      onClose();
      router.push('/skills');
    } catch (cause) {
      toast.apiError(cause, 'The SKILL was not deleted');
    }
  };

  return (
    <ConfirmDialog
      open={open}
      onCancel={onClose}
      onConfirm={() => void run()}
      title={`Delete SKILL ${skill.name}?`}
      description="Deleting removes the SKILL and its versions. Published capability versions are not touched."
      confirmLabel="Delete SKILL"
      loading={remove.isPending}
      impact={
        <div className="space-y-1">
          <p className="font-medium text-text">Impact</p>
          <p>
            {skill.version_count} version(s) · status {skill.status} ·{' '}
            {skill.requires_live_agent ? 'contains agent steps' : 'capability/transform steps only'}
          </p>
          {impact && impact.impact.length > 0 ? (
            <ul className="list-disc pl-4">
              {impact.impact.map((entry) => (
                <li key={entry.capability}>
                  {entry.capability}
                  {entry.resolvable ? ' resolves' : ' does not resolve to a published version'}
                  {entry.used_by_published_skills.length > 0
                    ? ` · also used by ${entry.used_by_published_skills.map((item) => item.name).join(', ')}`
                    : ''}
                </li>
              ))}
            </ul>
          ) : (
            <p>No capability dependencies are declared.</p>
          )}
        </div>
      }
    />
  );
}

/* ----------------------------------------------------------------- import flow -- */

type ImportStep = 'source' | 'review' | 'done';

export function ImportSkillDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const toast = useToast();
  const router = useGuardedRouter();
  const preview = useImportPreviewMutation();
  const commit = useImportCommitMutation();

  const [step, setStep] = useState<ImportStep>('source');
  const [sourceType, setSourceType] = useState<'git' | 'web' | 'file'>('git');
  const [uri, setUri] = useState('');
  const [fileId, setFileId] = useState<string | null>(null);
  const [fileName, setFileName] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [staged, setStaged] = useState<SkillImportPreview | null>(null);
  const [createdSkill, setCreatedSkill] = useState<Skill | null>(null);

  useEffect(() => {
    if (!open) return;
    setStep('source');
    setSourceType('git');
    setUri('');
    setFileId(null);
    setFileName(null);
    setConfirmed(false);
    setStaged(null);
    setCreatedSkill(null);
    setUploadError(null);
  }, [open]);

  const steps = useMemo<Step[]>(
    () => [
      { id: 'source', label: 'Source', status: step === 'source' ? 'current' : 'complete', hint: 'Git URL, web URL or an uploaded bundle.' },
      {
        id: 'review',
        label: 'Preview & permissions',
        status: step === 'review' ? 'current' : step === 'done' ? 'complete' : 'upcoming',
        hint: 'File list, flagged scripts, requested permissions, validation.',
      },
      {
        id: 'commit',
        label: 'Import',
        status: step === 'done' ? 'complete' : 'upcoming',
        hint: 'Creates a draft version; nothing is published.',
      },
    ],
    [step],
  );

  const upload = async (file: File) => {
    setUploading(true);
    setUploadError(null);
    try {
      const uploaded = await uploadFile(file, 'upload');
      setFileId(uploaded.id);
      setFileName(uploaded.filename);
      toast.success({ title: 'Bundle uploaded', description: `${uploaded.filename} (${formatBytes(uploaded.size)})` });
    } catch (cause) {
      setUploadError(describeError(cause));
      toast.apiError(cause, 'The upload failed');
    } finally {
      setUploading(false);
    }
  };

  const runPreview = async () => {
    try {
      const result = await preview.mutateAsync({
        source_type: sourceType,
        uri: sourceType === 'file' ? undefined : uri.trim(),
        file_id: sourceType === 'file' ? (fileId ?? undefined) : undefined,
      });
      setStaged(result);
      setConfirmed(false);
      setStep('review');
    } catch (cause) {
      toast.apiError(cause, 'The preview could not be staged');
    }
  };

  const runCommit = async () => {
    if (!staged) return;
    try {
      const result = await commit.mutateAsync({
        preview_id: staged.preview_id,
        confirm_permissions: confirmed,
      });
      setCreatedSkill(result.skill);
      setStep('done');
      toast.success({
        title: 'Imported as a draft',
        description: `${result.skill.name} — publish it from the Versions tab when it is ready.`,
      });
    } catch (cause) {
      toast.apiError(cause, 'The import was rejected');
    }
  };

  const canPreview =
    sourceType === 'file' ? Boolean(fileId) : uri.trim().length > 0;

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Import a SKILL"
      description="Scripts and install hooks found in a bundle are listed for review and are never executed."
      size="xl"
      dismissible={!preview.isPending && !commit.isPending && !uploading}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={preview.isPending || commit.isPending}>
            {step === 'done' ? 'Close' : 'Cancel'}
          </Button>
          {step === 'source' ? (
            <Button variant="primary" onClick={() => void runPreview()} loading={preview.isPending} disabled={!canPreview}>
              Preview import
            </Button>
          ) : null}
          {step === 'review' && staged ? (
            <Button
              variant="primary"
              onClick={() => void runCommit()}
              loading={commit.isPending}
              disabled={staged.validation.blocking || (staged.requires_permissions_confirmation && !confirmed)}
            >
              Import as draft
            </Button>
          ) : null}
          {step === 'done' && createdSkill ? (
            <Button
              variant="primary"
              onClick={() => {
                onClose();
                router.push(`/skills/${createdSkill.id}`);
              }}
            >
              Open the SKILL
            </Button>
          ) : null}
        </>
      }
    >
      <div className="space-y-4">
        <Stepper steps={steps} label="Import steps" orientation="horizontal" />

        {step === 'source' ? (
          <div className="space-y-3">
            <Field label="Source type" htmlFor="import-source">
              <Select
                id="import-source"
                value={sourceType}
                onChange={(event) => setSourceType(event.target.value as 'git' | 'web' | 'file')}
                options={[
                  { value: 'git', label: 'Git repository URL' },
                  { value: 'web', label: 'Web URL (raw SKILL.md or archive)' },
                  { value: 'file', label: 'Uploaded bundle (zip / tar / folder archive)' },
                ]}
              />
            </Field>

            {sourceType === 'file' ? (
              <Field
                label="Bundle file"
                htmlFor="import-file"
                hint="Uploaded to POST /api/admin/files first; the preview then reads it by id."
                error={uploadError}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <input
                    id="import-file"
                    type="file"
                    accept=".zip,.tar,.gz,.tgz,.md,.json,.txt"
                    onChange={(event) => {
                      const file = event.target.files?.[0];
                      if (file) void upload(file);
                      event.target.value = '';
                    }}
                    className="text-xs"
                  />
                  {uploading ? <span className="text-xs text-muted">Uploading…</span> : null}
                  {fileId ? (
                    <Badge tone="success" dot={false}>
                      {fileName ?? fileId}
                    </Badge>
                  ) : null}
                </div>
              </Field>
            ) : (
              <Field
                label={sourceType === 'git' ? 'Repository URL' : 'Web URL'}
                htmlFor="import-uri"
                required
                hint={
                  sourceType === 'git'
                    ? 'Public repository; the resolved commit is recorded on the SKILL.'
                    : 'Same-origin links are followed only when the preview allows it.'
                }
              >
                <Input
                  id="import-uri"
                  value={uri}
                  onChange={(event) => setUri(event.target.value)}
                  placeholder={
                    sourceType === 'git' ? 'https://github.com/acme/skill-summarise' : 'https://example.com/SKILL.md'
                  }
                />
              </Field>
            )}

            {preview.isError ? (
              <Banner tone="danger" title="The preview failed" requestId={errorRequestId(preview.error)}>
                {describeError(preview.error)}
              </Banner>
            ) : null}

            <p className="rounded-control border border-border bg-info-soft px-2 py-1.5 text-xs text-muted">
              Nothing is created during the preview. Scripts, shebang files and install hooks are listed for review and
              are never executed by Skill Router.
            </p>
          </div>
        ) : null}

        {step === 'review' && staged ? (
          <div className="space-y-3">
            <Banner tone="info" title={`Staged ${staged.source_type} import`}>
              {staged.source_uri} · {staged.limits.file_count} file(s) · {formatBytes(staged.limits.total_bytes)}
              {staged.limits.truncated ? ' · the bundle was truncated at the configured limit' : ''}
              {staged.detected_skill_document ? ` · SKILL document: ${staged.detected_skill_document}` : ' · no SKILL.md detected'}
            </Banner>

            <div className="rounded-card border border-border">
              <div className="flex items-center justify-between gap-2 border-b border-border px-3 py-2">
                <p className="text-xs font-medium text-text">Files</p>
                <Badge tone={staged.requires_review.length > 0 ? 'warning' : 'neutral'} dot={false}>
                  {staged.requires_review.length} flagged for review
                </Badge>
              </div>
              <div className="max-h-48 overflow-y-auto">
                {staged.files.length === 0 ? (
                  <EmptyState title="The bundle had no files" description="Nothing can be imported from an empty bundle." />
                ) : (
                  <ul className="divide-y divide-border text-xs">
                    {staged.files.map((file) => (
                      <li key={file.path} className="flex items-start justify-between gap-2 px-3 py-1.5">
                        <span className="min-w-0">
                          <span className="block truncate font-mono text-[11px] text-text">{file.path}</span>
                          {file.review_reason ? (
                            <span className="mt-0.5 flex items-start gap-1 text-[11px] text-warning">
                              <FileWarning aria-hidden className="mt-0.5 h-3 w-3 shrink-0" />
                              {file.review_reason}
                            </span>
                          ) : null}
                        </span>
                        <span className="flex shrink-0 items-center gap-1.5">
                          <span className="text-[11px] text-muted">{formatBytes(file.size)}</span>
                          <Badge tone={file.requires_review ? 'warning' : 'neutral'} dot={false}>
                            {file.kind}
                          </Badge>
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>

            <div className="rounded-card border border-border px-3 py-2">
              <p className="text-xs font-medium text-text">Permissions requested by the bundle</p>
              {staged.permissions.length === 0 ? (
                <p className="mt-1 text-xs text-muted">The bundle requests no network, file or credential access.</p>
              ) : (
                <ul className="mt-1 flex flex-wrap gap-1.5">
                  {staged.permissions.map((permission) => (
                    <li key={permission}>
                      <Badge tone="warning" dot={false}>
                        {permission}
                      </Badge>
                    </li>
                  ))}
                </ul>
              )}
              {staged.requires_permissions_confirmation ? (
                <div className="mt-2">
                  <Checkbox
                    checked={confirmed}
                    onChange={(event) => setConfirmed(event.target.checked)}
                    label="I reviewed the requested permissions and the flagged files"
                    hint="The API refuses the import with 409 permissions_not_confirmed until this is checked."
                  />
                </div>
              ) : null}
            </div>

            <div className="rounded-card border border-border px-3 py-2">
              <p className="text-xs font-medium text-text">Validation</p>
              {staged.validation.issues.length === 0 ? (
                <p className="mt-1 inline-flex items-center gap-1 text-xs text-success">
                  <CheckCircle2 aria-hidden className="h-3 w-3" /> No structural issues found.
                </p>
              ) : (
                <ul className="mt-1 space-y-1">
                  {staged.validation.issues.map((issue) => (
                    <li key={`${issue.field}-${issue.message}`}>
                      <InlineAlert tone={issue.severity === 'error' ? 'danger' : 'warning'}>
                        <span className="font-mono">{issue.field}</span> — {issue.message}
                      </InlineAlert>
                    </li>
                  ))}
                </ul>
              )}
              {staged.validation.blocking ? (
                <p className="mt-1 text-xs text-danger">
                  Blocking issues must be fixed in the bundle; the commit is refused until then.
                </p>
              ) : null}
            </div>

            <CodeBlock
              code={prettyJson(staged.proposed_manifest)}
              label="proposed manifest (draft)"
              maxHeightClassName="max-h-56"
              notice="Secrets are never part of a SKILL bundle; credentials are referenced by id only."
            />

            <p className="text-[11px] text-muted">
              scripts_executed: {String(staged.scripts_executed)} · {staged.scripts_policy}
            </p>

            {commit.isError ? (
              <Banner tone="danger" title="The import was rejected" requestId={errorRequestId(commit.error)}>
                {describeError(commit.error)}
              </Banner>
            ) : null}
          </div>
        ) : null}

        {step === 'done' && createdSkill ? (
          <div className="space-y-2">
            <Banner tone="success" title={`${createdSkill.name} imported as a draft`}>
              It has one draft version. Review the plan, run the Test tab, then publish from Versions.
            </Banner>
            <p className="text-xs text-muted">
              <Link href={`/skills/${createdSkill.id}`} className="hover:underline">
                /skills/{createdSkill.slug}
              </Link>
            </p>
            <div className="flex items-center gap-1.5 text-xs text-muted">
              <Upload aria-hidden className="h-3 w-3" />
              No script from the bundle was executed during import.
            </div>
          </div>
        ) : null}

        {preview.isError && step === 'review' ? (
          <Banner tone="warning" title="Preview error">
            <AlertTriangle aria-hidden className="h-3 w-3" /> {describeError(preview.error)}
          </Banner>
        ) : null}
      </div>
    </Dialog>
  );
}
