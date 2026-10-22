'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useParams, useSearchParams } from 'next/navigation';
import { Download, Pencil, Power, Save, Trash2 } from 'lucide-react';

import { api, describeError, errorRequestId, isApiError } from '@/lib/api';
import { jsonEqual, parseJsonObject } from '@/lib/json';
import { formatDateTime } from '@/lib/format';
import { Badge, StatusBadge } from '@/components/ui/badge';
import { Banner } from '@/components/ui/banner';
import { Button } from '@/components/ui/button';
import { Card, CardHeader } from '@/components/ui/card';
import { PageHeader } from '@/components/ui/page-header';
import { SkeletonBlock } from '@/components/ui/skeleton';
import { TabPanel, Tabs } from '@/components/ui/tabs';
import { useToast } from '@/components/ui/toast';
import { SkillDeleteDialog, SkillFormDialog } from '@/components/skills/skill-dialogs';
import {
  skillEndpoints,
  usePublishSkillVersionMutation,
  useSkillQuery,
  useTestSkillMutation,
  useUpdateSkillMutation,
  useUpdateVersionMutation,
  useCreateVersionMutation,
  useValidateSkillVersionMutation,
  type SkillIssue,
  type SkillManifest,
  type SkillTestResult,
} from '@/components/skills/api';
import {
  DependenciesTab,
  ExportTab,
  ManifestTab,
  PermissionsTab,
  PlanTab,
  TestTab,
  VersionsTab,
  editorFromVersion,
  manifestFromEditor,
  type EditorState,
} from '@/components/skills/skill-tabs';

type TabId = 'manifest' | 'plan' | 'dependencies' | 'permissions' | 'versions' | 'test' | 'export';

const TAB_IDS: TabId[] = ['manifest', 'plan', 'dependencies', 'permissions', 'versions', 'test', 'export'];

interface ExportPayload {
  format: string;
  filename: string;
  content_type: string;
  content: string;
  sha256: string;
  includes_credentials: boolean;
  notes: string[];
}

export default function SkillDetailPage() {
  const params = useParams<{ id: string }>();
  const skillId = params?.id;
  const searchParams = useSearchParams();
  const toast = useToast();

  const skillQuery = useSkillQuery(skillId);
  const skill = skillQuery.data;

  const [tab, setTab] = useState<TabId>(() => {
    const requested = searchParams.get('tab');
    return (TAB_IDS as string[]).includes(requested ?? '') ? (requested as TabId) : 'manifest';
  });
  const [versionId, setVersionId] = useState<string | null>(null);
  const [editor, setEditor] = useState<EditorState | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [validation, setValidation] = useState<{ versionId: string; issues: SkillIssue[]; blocking: boolean } | null>(null);
  const [testInput, setTestInput] = useState('{\n  \n}');
  const [testVersionId, setTestVersionId] = useState('');
  const [testResult, setTestResult] = useState<SkillTestResult | null>(null);
  const [testError, setTestError] = useState<string | null>(null);
  const [testErrorRequestId, setTestErrorRequestId] = useState<string | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState<'skill' | 'json' | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);

  const versions = useMemo(() => skill?.versions ?? [], [skill?.versions]);
  const activeVersion = useMemo(
    () => versions.find((item) => item.id === (versionId ?? skill?.current_version_id)) ?? skill?.current_version ?? null,
    [versions, versionId, skill?.current_version_id, skill?.current_version],
  );

  const updateSkill = useUpdateSkillMutation(skillId ?? '');
  const updateVersion = useUpdateVersionMutation(skillId ?? '');
  const createVersion = useCreateVersionMutation(skillId ?? '');
  const validateVersion = useValidateSkillVersionMutation(skillId ?? '');
  const publishVersion = usePublishSkillVersionMutation(skillId ?? '');
  const testSkill = useTestSkillMutation(skillId ?? '');

  // Reset the editor whenever the selected version changes (or the skill reloads).
  useEffect(() => {
    if (!skill || !activeVersion) {
      setEditor(null);
      return;
    }
    setEditor(editorFromVersion(skill, activeVersion));
    setSaveError(null);
    setValidation(null);
    setTestResult(null);
    setTestError(null);
    setTestInput(JSON.stringify(activeVersion.manifest?.examples?.[0]?.input ?? {}, null, 2));
    setTestVersionId(activeVersion.id);
  }, [skill, activeVersion]);

  const baseManifest: SkillManifest | null = activeVersion?.manifest ?? null;
  const build = useMemo(
    () => (editor && baseManifest ? manifestFromEditor(baseManifest, editor) : null),
    [editor, baseManifest],
  );
  const dirty = Boolean(build?.ok && baseManifest && !jsonEqual(build.manifest, baseManifest));
  const immutable = Boolean(activeVersion?.immutable);
  const editorDisabled = immutable || !editor;
  const issues =
    validation && validation.versionId === activeVersion?.id
      ? validation.issues
      : (activeVersion?.validation?.issues ?? []);

  const patchEditor = useCallback((patch: Partial<EditorState>) => {
    setEditor((current) => (current ? { ...current, ...patch } : current));
  }, []);

  const save = async () => {
    if (!build || !activeVersion) return;
    if (!build.ok) {
      setSaveError(build.error);
      return;
    }
    setSaveError(null);
    try {
      if (immutable) {
        const created = await createVersion.mutateAsync({ manifest: build.manifest });
        setVersionId(created.id);
        toast.success({
          title: `Saved as version ${created.version}`,
          description: 'The new version is a draft; validate and test it before publishing.',
        });
      } else {
        await updateVersion.mutateAsync({ versionId: activeVersion.id, manifest: build.manifest });
        toast.success({ title: 'Draft saved', description: `Version ${activeVersion.version} now stores the edited manifest.` });
      }
      await skillQuery.refetch();
    } catch (cause) {
      setSaveError(describeError(cause));
      toast.apiError(cause, 'The manifest was not saved');
    }
  };

  const runValidate = async (targetVersionId: string) => {
    try {
      const result = await validateVersion.mutateAsync(targetVersionId);
      setValidation({ versionId: targetVersionId, issues: result.issues, blocking: result.blocking });
      if (result.blocking) {
        toast.warning({ title: 'Validation found blocking issues', description: `${result.issues.length} issue(s).` });
      } else {
        toast.success({ title: 'Validation passed', description: `${result.issues.length} issue(s), none blocking.` });
      }
    } catch (cause) {
      toast.apiError(cause, 'Validation could not run');
    }
  };

  const runPublish = async (targetVersionId: string) => {
    try {
      await publishVersion.mutateAsync(targetVersionId);
      toast.success({
        title: 'SKILL version published',
        description: 'Published means the version is frozen and live; it is not the same as "test passed".',
      });
      await skillQuery.refetch();
    } catch (cause) {
      toast.apiError(cause, 'Publishing was refused');
    }
  };

  const runTest = async () => {
    const parsed = parseJsonObject(testInput);
    if (!parsed.ok) {
      setTestError(`Input JSON: ${parsed.error}`);
      return;
    }
    setTestError(null);
    setTestErrorRequestId(null);
    try {
      const result = await testSkill.mutateAsync({
        version_id: testVersionId || undefined,
        input: parsed.value,
      });
      setTestResult(result);
      toast.success({ title: `Run ${result.status}`, description: `${result.steps.length} step(s) reported.` });
    } catch (cause) {
      setTestResult(null);
      setTestError(describeError(cause));
      setTestErrorRequestId(errorRequestId(cause));
      if (isApiError(cause) && cause.code === 'engine_required') {
        toast.warning({
          title: 'This plan needs the AragonAgent engine',
          description: 'A step of kind "agent" cannot run inside Skill Router; the API returned engine_required.',
        });
      } else {
        toast.apiError(cause, 'The test run failed');
      }
    }
  };

  const download = async (format: 'skill' | 'json') => {
    if (!skillId) return;
    setDownloading(format);
    setExportError(null);
    try {
      const payload = await api.get<ExportPayload>(skillEndpoints.export(skillId, format));
      const blob = new Blob([payload.content], { type: payload.content_type || 'text/plain' });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = payload.filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      toast.success({
        title: `Exported ${payload.filename}`,
        description: payload.includes_credentials ? 'Warning: the bundle claims to include credentials.' : 'No credential material is included.',
      });
    } catch (cause) {
      setExportError(describeError(cause));
      toast.apiError(cause, 'The export failed');
    } finally {
      setDownloading(null);
    }
  };

  const toggleStatus = async () => {
    if (!skill) return;
    const next = skill.status === 'disabled' ? 'draft' : 'disabled';
    try {
      await updateSkill.mutateAsync({ status: next });
      toast.success({
        title: next === 'disabled' ? 'SKILL disabled' : 'SKILL re-enabled as draft',
        description: next === 'disabled' ? 'The SKILL is no longer offered to agents.' : 'Publish a version to make it live again.',
      });
    } catch (cause) {
      toast.apiError(cause, 'The status was not changed');
    }
  };

  if (skillQuery.isLoading) {
    return (
      <div className="space-y-4">
        <PageHeader title="SKILL" backHref="/skills" backLabel="SKILLs" />
        <SkeletonBlock rows={5} />
      </div>
    );
  }

  if (skillQuery.isError || !skill) {
    return (
      <div className="space-y-4">
        <PageHeader title="SKILL" backHref="/skills" backLabel="SKILLs" />
        <Banner
          tone="danger"
          title="Could not load this SKILL"
          requestId={errorRequestId(skillQuery.error)}
          primaryAction={{
            label: 'Retry',
            onClick: () => void skillQuery.refetch(),
            loading: skillQuery.isFetching,
            variant: 'primary',
          }}
        >
          {describeError(skillQuery.error)}
        </Banner>
      </div>
    );
  }

  const tabs = [
    { id: 'manifest' as TabId, label: 'Manifest' },
    { id: 'plan' as TabId, label: 'Plan', count: activeVersion?.step_count },
    { id: 'dependencies' as TabId, label: 'Dependencies', count: (editor?.dependencies ?? []).length },
    { id: 'permissions' as TabId, label: 'Permissions' },
    { id: 'versions' as TabId, label: 'Versions', count: versions.length },
    { id: 'test' as TabId, label: 'Test' },
    { id: 'export' as TabId, label: 'Export' },
  ];

  const editableTab = tab === 'manifest' || tab === 'plan' || tab === 'dependencies' || tab === 'permissions';

  return (
    <div className="space-y-4">
      <PageHeader
        title={skill.name}
        backHref="/skills"
        backLabel="SKILLs"
        description={skill.description || 'No description yet.'}
        meta={
          <>
            <StatusBadge status={skill.status} />
            <span className="font-mono text-[11px]">{skill.slug}</span>
            <Badge tone="neutral" dot={false}>
              {skill.category || 'uncategorised'}
            </Badge>
            {activeVersion ? (
              <span className="text-xs">
                v{activeVersion.version} · {activeVersion.status}
              </span>
            ) : null}
            {skill.requires_live_agent ? (
              <Badge tone="warning" dot={false}>
                contains agent steps
              </Badge>
            ) : null}
            {skill.source.type ? (
              <span className="text-xs text-muted">
                imported from {skill.source.type}: {skill.source.ref ?? '—'}
              </span>
            ) : null}
          </>
        }
        actions={
          <>
            <Button
              size="sm"
              variant="secondary"
              onClick={() => void toggleStatus()}
              loading={updateSkill.isPending}
              iconLeft={<Power aria-hidden className="h-3.5 w-3.5" />}
            >
              {skill.status === 'disabled' ? 'Enable' : 'Disable'}
            </Button>
            <Button
              size="sm"
              variant="secondary"
              onClick={() => setEditOpen(true)}
              iconLeft={<Pencil aria-hidden className="h-3.5 w-3.5" />}
            >
              Edit
            </Button>
            <Button
              size="sm"
              variant="secondary"
              onClick={() => void download('skill')}
              loading={downloading === 'skill'}
              iconLeft={<Download aria-hidden className="h-3.5 w-3.5" />}
            >
              Export
            </Button>
            <Button
              size="sm"
              variant="danger"
              onClick={() => setDeleteOpen(true)}
              iconLeft={<Trash2 aria-hidden className="h-3.5 w-3.5" />}
            >
              Delete
            </Button>
          </>
        }
      />

      <Card>
        <CardHeader
          title="Editing version"
          description="Pick the version you are looking at; published versions are immutable, so saving creates a draft."
          actions={
            <div className="flex flex-wrap items-center gap-2">
              {versions.length > 1 ? (
                <select
                  aria-label="Version to view"
                  value={activeVersion?.id ?? ''}
                  onChange={(event) => setVersionId(event.target.value)}
                  className="h-8 rounded-control border border-border-strong bg-surface px-2 text-xs"
                >
                  {versions.map((version) => (
                    <option key={version.id} value={version.id}>
                      v{version.version} ({version.status})
                    </option>
                  ))}
                </select>
              ) : null}
              {editableTab ? (
                <>
                  {dirty ? (
                    <Badge tone="warning" dot={false}>
                      Unsaved changes
                    </Badge>
                  ) : (
                    <Badge tone="neutral" dot={false}>
                      Saved state
                    </Badge>
                  )}
                  <Button
                    size="sm"
                    variant="primary"
                    onClick={() => void save()}
                    loading={updateVersion.isPending || createVersion.isPending}
                    disabled={!dirty || editorDisabled}
                    iconLeft={<Save aria-hidden className="h-3.5 w-3.5" />}
                  >
                    {immutable ? 'Save as new version' : 'Save draft'}
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={!dirty || !skill || !activeVersion}
                    onClick={() => skill && activeVersion && setEditor(editorFromVersion(skill, activeVersion))}
                  >
                    Discard
                  </Button>
                </>
              ) : null}
            </div>
          }
        />
      </Card>

      {immutable ? (
        <Banner tone="info" title="This version is published and immutable">
          Editing it creates a new draft version. Publishing freezes a version but does not test it: the Test tab runs a
          real call.
        </Banner>
      ) : null}

      {saveError ? <Banner tone="danger" title="Nothing was saved">{saveError}</Banner> : null}
      {updateVersion.isError ? (
        <Banner tone="danger" title="The draft was not saved" requestId={errorRequestId(updateVersion.error)}>
          {describeError(updateVersion.error)}
        </Banner>
      ) : null}
      {createVersion.isError ? (
        <Banner tone="danger" title="The new version was not created" requestId={errorRequestId(createVersion.error)}>
          {describeError(createVersion.error)}
        </Banner>
      ) : null}
      {build && !build.ok ? <Banner tone="warning" title={build.error}>{build.error}</Banner> : null}

      <Tabs tabs={tabs} value={tab} onChange={(id) => setTab(id as TabId)} label="SKILL sections" panelIdPrefix="skill" />

      <TabPanel id="manifest" active={tab === 'manifest'} panelIdPrefix="skill">
        {editor ? (
          <ManifestTab
            state={editor}
            onChange={patchEditor}
            disabled={editorDisabled}
            issues={issues}
            readOnlyReason={immutable ? 'Published versions are immutable; save to create a draft version.' : null}
          />
        ) : (
          <Banner tone="info" title="This SKILL has no version yet">
            Save the editor once to create version 1.
          </Banner>
        )}
      </TabPanel>

      <TabPanel id="plan" active={tab === 'plan'} panelIdPrefix="skill">
        {editor ? (
          <PlanTab state={editor} onChange={patchEditor} disabled={editorDisabled} issues={issues} />
        ) : null}
      </TabPanel>

      <TabPanel id="dependencies" active={tab === 'dependencies'} panelIdPrefix="skill">
        {editor ? (
          <DependenciesTab
            state={editor}
            onChange={patchEditor}
            disabled={editorDisabled}
            impact={skill.dependency_impact}
            impactLoading={skillQuery.isFetching}
            impactError={null}
            onReloadImpact={() => void skillQuery.refetch()}
          />
        ) : null}
      </TabPanel>

      <TabPanel id="permissions" active={tab === 'permissions'} panelIdPrefix="skill">
        {editor ? (
          <PermissionsTab state={editor} onChange={patchEditor} disabled={editorDisabled} issues={issues} />
        ) : null}
      </TabPanel>

      <TabPanel id="versions" active={tab === 'versions'} panelIdPrefix="skill">
        <VersionsTab
          skill={skill}
          onPublish={(id) => void runPublish(id)}
          publishingId={publishVersion.isPending ? (publishVersion.variables ?? null) : null}
          onValidate={(id) => void runValidate(id)}
          validatingId={validateVersion.isPending ? (validateVersion.variables ?? null) : null}
          validation={validation}
          validationError={validateVersion.isError ? describeError(validateVersion.error) : null}
        />
      </TabPanel>

      <TabPanel id="test" active={tab === 'test'} panelIdPrefix="skill">
        <TestTab
          versions={versions}
          selectedVersionId={testVersionId}
          onSelectVersion={setTestVersionId}
          inputText={testInput}
          onInputChange={setTestInput}
          onRun={() => void runTest()}
          running={testSkill.isPending}
          result={testResult}
          error={testError}
          errorRequestId={testErrorRequestId}
        />
      </TabPanel>

      <TabPanel id="export" active={tab === 'export'} panelIdPrefix="skill">
        <ExportTab skill={skill} onDownload={(format) => void download(format)} downloading={downloading} error={exportError} />
      </TabPanel>

      <p className="text-[11px] text-muted">
        created {formatDateTime(skill.created_at)} · {skill.version_count} version(s) ·{' '}
        {skill.latest_published_version ? `latest published v${skill.latest_published_version.version}` : 'never published'}
      </p>

      <SkillFormDialog open={editOpen} onClose={() => setEditOpen(false)} skill={skill} />
      <SkillDeleteDialog open={deleteOpen} onClose={() => setDeleteOpen(false)} skill={skill} />
    </div>
  );
}
