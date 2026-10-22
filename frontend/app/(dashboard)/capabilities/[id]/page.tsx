'use client';

import { Suspense, useCallback, useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import { useParams, useSearchParams } from 'next/navigation';
import {
  CheckCircle2,
  FlaskConical,
  RotateCcw,
  Save,
  ShieldCheck,
  UploadCloud,
  XCircle,
} from 'lucide-react';

import { describeError, errorActions, errorRequestId, isApiError } from '@/lib/api';
import {
  useCapabilityQuery,
  usePublishVersionMutation,
  useTestsQuery,
  useUpdateCapabilityMutation,
  useUpdateVersionMutation,
  useValidateVersionMutation,
  useVersionQuery,
  useVersionsQuery,
} from '@/lib/queries';
import { setConfigPath } from '@/lib/config';
import { jsonEqual, parseJsonObject } from '@/lib/json';
import { useRegisterUnsavedChanges } from '@/lib/navigation-guard';
import type { CapabilityConfig, CapabilityIssue, VersionStatus } from '@/lib/types';
import { formatDateTime, formatDuration, statusLabel } from '@/lib/format';
import {
  AdvancedPanel,
  ExamplesPanel,
  ErrorsPanel,
  InputSchemaPanel,
  LimitsPanel,
  ModesPanel,
  OutputPanel,
  RequestPanel,
} from '@/components/capabilities/config-editor';
import { RoutesPanel } from '@/components/capabilities/routes-panel';
import { StateChips } from '@/components/capabilities/state-chips';
import { TestRunner } from '@/components/capabilities/test-runner';
import { VersionsPanel } from '@/components/capabilities/versions-panel';
import { Badge, StatusBadge } from '@/components/ui/badge';
import { Banner, InlineAlert } from '@/components/ui/banner';
import { Button } from '@/components/ui/button';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { EmptyState } from '@/components/ui/empty-state';
import { Field } from '@/components/ui/field';
import { Input, Textarea } from '@/components/ui/input';
import { JsonEditor } from '@/components/ui/json-editor';
import { PageHeader } from '@/components/ui/page-header';
import { SkeletonBlock, SkeletonTable } from '@/components/ui/skeleton';
import { Table, TBody, TD, TH, THead, TR } from '@/components/ui/table';
import { Tabs, TabPanel } from '@/components/ui/tabs';
import { TimeAgo } from '@/components/ui/time-ago';
import { useToast } from '@/components/ui/toast';

type TabId =
  | 'overview'
  | 'request'
  | 'input'
  | 'output'
  | 'modes'
  | 'errors'
  | 'limits'
  | 'examples'
  | 'versions'
  | 'tests'
  | 'routes';

const TAB_IDS: TabId[] = [
  'overview',
  'request',
  'input',
  'output',
  'modes',
  'errors',
  'limits',
  'examples',
  'versions',
  'tests',
  'routes',
];

/** Which tab owns a config field path, so a validation issue can link to it. */
function tabForField(field: string): TabId {
  if (field.startsWith('request')) return 'request';
  if (field.startsWith('input_schema')) return 'input';
  if (field.startsWith('response') || field.startsWith('output_schema')) return 'output';
  if (field.startsWith('async') || field.startsWith('stream') || field.startsWith('modes')) return 'modes';
  if (field.startsWith('errors')) return 'errors';
  if (field.startsWith('limits') || field.startsWith('pricing') || field.startsWith('retry')) return 'limits';
  if (field.startsWith('examples')) return 'examples';
  return 'overview';
}

function IssueList({ issues, onJump }: { issues: CapabilityIssue[]; onJump: (tab: TabId) => void }) {
  if (issues.length === 0) {
    return <InlineAlert tone="success">Validation reported no issues.</InlineAlert>;
  }
  return (
    <ul className="space-y-1.5">
      {issues.map((issue) => (
        <li
          key={`${issue.field}-${issue.message}`}
          className="flex flex-wrap items-center justify-between gap-2 rounded-control border border-border px-2 py-1.5"
        >
          <span className="flex min-w-0 items-start gap-2">
            <Badge tone={issue.severity === 'error' ? 'danger' : 'warning'}>{statusLabel(issue.severity)}</Badge>
            <span className="min-w-0 text-xs">
              <span className="font-mono text-muted">{issue.field || 'config'}</span> — {issue.message}
            </span>
          </span>
          <Button size="sm" variant="ghost" onClick={() => onJump(tabForField(issue.field))}>
            Go to field
          </Button>
        </li>
      ))}
    </ul>
  );
}

function CapabilityWorkbench() {
  const params = useParams<{ id: string }>();
  const capabilityId = params?.id;
  const searchParams = useSearchParams();
  const toast = useToast();

  const capabilityQuery = useCapabilityQuery(capabilityId);
  const versionsQuery = useVersionsQuery(capabilityId);

  const capability = capabilityQuery.data;
  const versions = useMemo(
    () => versionsQuery.data?.items ?? capability?.versions ?? [],
    [versionsQuery.data?.items, capability?.versions],
  );

  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(null);
  const [tab, setTab] = useState<TabId>(() => {
    const requested = searchParams.get('tab');
    return (TAB_IDS as string[]).includes(requested ?? '') ? (requested as TabId) : 'overview';
  });

  useEffect(() => {
    if (selectedVersionId) return;
    const preferred =
      capability?.current_version_id ??
      versions.find((version) => version.status === 'published')?.id ??
      versions[0]?.id ??
      null;
    if (preferred) setSelectedVersionId(preferred);
  }, [capability?.current_version_id, versions, selectedVersionId]);

  const versionQuery = useVersionQuery(capabilityId, selectedVersionId ?? undefined);
  const version = versionQuery.data;

  const serverConfig: CapabilityConfig = useMemo(
    () => (version?.raw_config ?? version?.config ?? {}) as CapabilityConfig,
    [version?.raw_config, version?.config],
  );

  const [baseline, setBaseline] = useState<CapabilityConfig>(serverConfig);
  const [draft, setDraft] = useState<CapabilityConfig>(serverConfig);
  const [configText, setConfigText] = useState<string>('');
  const [advancedError, setAdvancedError] = useState<string | null>(null);
  const [validation, setValidation] = useState<{ issues: CapabilityIssue[]; blocking: boolean } | null>(null);
  const [testOpen, setTestOpen] = useState(false);
  const [detailsError, setDetailsError] = useState<string | null>(null);

  useEffect(() => {
    setBaseline(serverConfig);
    setDraft(serverConfig);
    setConfigText(JSON.stringify(serverConfig, null, 2));
    setAdvancedError(null);
    setValidation(null);
    // Reset when the selected version (not every refetch) changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version?.id]);

  const dirty = !jsonEqual(draft, baseline);
  const editable = Boolean(version?.editable);
  const versionStatus: VersionStatus | null = version?.status ?? null;

  const updateVersion = useUpdateVersionMutation(capabilityId ?? '', selectedVersionId ?? '');
  const validateVersion = useValidateVersionMutation(capabilityId ?? '', selectedVersionId ?? '');
  const publishVersion = usePublishVersionMutation(capabilityId ?? '', selectedVersionId ?? '');
  const updateCapability = useUpdateCapabilityMutation(capabilityId ?? '');
  const tests = useTestsQuery(capabilityId);

  const saveDraft = useCallback(async (): Promise<boolean> => {
    if (!dirty) return true;
    try {
      await updateVersion.mutateAsync({ config: draft });
      setBaseline(draft);
      toast.success({ title: `Draft v${version?.version ?? ''} saved`, description: 'Validation and tests use the saved config.' });
      setValidation(null);
      return true;
    } catch (error) {
      toast.apiError(error, 'Draft was not saved');
      return false;
    }
  }, [dirty, draft, updateVersion, toast, version?.version]);

  useRegisterUnsavedChanges({
    what: capability ? `${capability.slug} v${version?.version ?? ''}` : 'this capability',
    isDirty: () => dirty,
    save: saveDraft,
  });

  const setPath = (path: string, value: unknown) => {
    setDraft((current) => {
      const next = setConfigPath(current as Record<string, unknown>, path, value) as CapabilityConfig;
      setConfigText(JSON.stringify(next, null, 2));
      return next;
    });
  };

  const replaceConfig = (next: CapabilityConfig) => {
    setDraft(next);
    setConfigText(JSON.stringify(next, null, 2));
  };

  const onAdvancedChange = (text: string) => {
    setConfigText(text);
    const parsed = parseJsonObject(text);
    if (!parsed.ok) {
      setAdvancedError(parsed.error);
      return;
    }
    setAdvancedError(null);
    setDraft(parsed.value as CapabilityConfig);
  };

  const runValidate = async () => {
    if (dirty) {
      toast.warning({
        title: 'Save the draft first',
        description: 'Validation runs against the stored version, not the unsaved editor state.',
      });
      return;
    }
    try {
      const result = await validateVersion.mutateAsync();
      setValidation({ issues: result.issues, blocking: result.blocking });
      if (result.blocking) {
        toast.error({ title: `${result.errors} blocking issue(s)`, description: 'Publishing and tests need a clean config.' });
      } else {
        toast.success({ title: 'Validation clean', description: `${result.warnings} warning(s), no blockers.` });
      }
    } catch (error) {
      toast.apiError(error, 'Validation failed');
    }
  };

  const runPublish = async () => {
    if (dirty) {
      toast.warning({ title: 'Save the draft first', description: 'Publishing publishes the stored version.' });
      return;
    }
    try {
      await publishVersion.mutateAsync();
      toast.success({ title: `Published v${version?.version ?? ''}`, description: 'The version is immutable and routable now.' });
    } catch (error) {
      // 409 test_evidence_missing is the common case and must stay explicit.
      toast.apiError(error, 'Publishing was refused');
    }
  };

  if (capabilityQuery.isLoading) {
    return (
      <div className="space-y-4">
        <PageHeader title="Capability" backHref="/capabilities" backLabel="Capabilities" />
        <SkeletonBlock rows={3} />
        <SkeletonTable rows={4} columns={4} />
      </div>
    );
  }

  if (capabilityQuery.isError || !capability) {
    return (
      <div className="space-y-4">
        <PageHeader title="Capability" backHref="/capabilities" backLabel="Capabilities" />
        <Banner
          tone="danger"
          title="Could not load this capability"
          requestId={errorRequestId(capabilityQuery.error)}
          primaryAction={{
            label: 'Retry',
            onClick: () => void capabilityQuery.refetch(),
            loading: capabilityQuery.isFetching,
            variant: 'primary',
          }}
        >
          {describeError(capabilityQuery.error)}
        </Banner>
        <Link href="/capabilities" className="text-xs text-accent hover:underline">
          Back to the capability list
        </Link>
      </div>
    );
  }

  const publishBlocked = capability.last_test_status !== 'passed';
  const publishError = publishVersion.error;
  const publishBlockedByServer = isApiError(publishError) && publishError.code === 'test_evidence_missing';

  const panelProps = { config: draft, set: setPath, replace: replaceConfig, issues: validation?.issues ?? capability.issues ?? [], disabled: !editable };

  const tabs = [
    { id: 'overview' as TabId, label: 'Overview' },
    { id: 'request' as TabId, label: 'Request' },
    { id: 'input' as TabId, label: 'Input schema' },
    { id: 'output' as TabId, label: 'Output & artifacts' },
    { id: 'modes' as TabId, label: 'Modes' },
    { id: 'errors' as TabId, label: 'Errors' },
    { id: 'limits' as TabId, label: 'Limits & pricing' },
    { id: 'examples' as TabId, label: 'Examples' },
    { id: 'versions' as TabId, label: 'Versions', count: versions.length },
    { id: 'tests' as TabId, label: 'Tests', count: tests.data?.total },
    { id: 'routes' as TabId, label: 'Routes', count: capability.routes?.length },
  ];

  return (
    <div className="space-y-5">
      <PageHeader
        title={capability.slug}
        backHref="/capabilities"
        backLabel="Capabilities"
        description={capability.description || 'No description yet — agents see an empty description.'}
        meta={
          <>
            <StatusBadge status={capability.status} />
            <Badge tone="info" dot={false}>
              {statusLabel(capability.type)}
            </Badge>
            {capability.provider_name ? <span>provider {capability.provider_name}</span> : <span>no provider</span>}
            {capability.credential_name ? <span>credential {capability.credential_name}</span> : <span>no credential</span>}
            <span>updated <TimeAgo value={capability.updated_at} /></span>
            {capability.needs_live_agent ? <Badge tone="warning">needs live agent</Badge> : null}
          </>
        }
        actions={
          <>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => void runValidate()}
              loading={validateVersion.isPending}
              disabled={!version || !editable || dirty}
              iconLeft={<ShieldCheck aria-hidden className="h-3.5 w-3.5" />}
            >
              Validate
            </Button>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => setTestOpen(true)}
              disabled={!version || dirty}
              iconLeft={<FlaskConical aria-hidden className="h-3.5 w-3.5" />}
            >
              Test
            </Button>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => void runPublish()}
              loading={publishVersion.isPending}
              disabled={!version || dirty || versionStatus === 'published'}
              iconLeft={<UploadCloud aria-hidden className="h-3.5 w-3.5" />}
            >
              Publish
            </Button>
            <Button
              variant="primary"
              size="sm"
              onClick={() => void saveDraft()}
              loading={updateVersion.isPending}
              disabled={!dirty || !editable}
              iconLeft={<Save aria-hidden className="h-3.5 w-3.5" />}
            >
              Save draft
            </Button>
          </>
        }
      />

      <StateChips verification={capability.verification} />

      {!editable && version ? (
        <Banner tone="info" title={`Version ${version.version} is ${statusLabel(version.status)} and immutable`}>
          Branch a new draft from the Versions tab to change the configuration. Rollback republishes an old config.
        </Banner>
      ) : null}

      {dirty ? (
        <Banner
          tone="warning"
          title="Unsaved changes"
          primaryAction={{ label: 'Save draft', onClick: () => void saveDraft(), loading: updateVersion.isPending, variant: 'primary' }}
        >
          Validate, test and publish all run against the stored version — save first so those results describe what you
          see.
        </Banner>
      ) : null}

      {publishBlocked ? (
        <Banner
          tone="warning"
          title="Publishing is blocked: there is no passing test for this version"
          actions={[
            'Run a real test from the Test button (it records the evidence).',
            'Fix the request/credential if the test failed.',
            'Only a successful real call unlocks publishing (409 test_evidence_missing otherwise).',
          ]}
        >
          The API refuses with <code>409 test_evidence_missing</code> and lists the last test status. {publishBlockedByServer ? 'That refusal has been recorded — see the notification for the request id.' : ''}
        </Banner>
      ) : null}

      {publishError && !publishBlockedByServer ? (
        <Banner
          tone="danger"
          title="Publishing failed"
          actions={errorActions(publishError)}
          requestId={errorRequestId(publishError)}
        >
          {describeError(publishError)}
        </Banner>
      ) : null}

      {publishBlockedByServer ? (
        <Banner tone="danger" title="The server refused the publish" actions={errorActions(publishError)} requestId={errorRequestId(publishError)}>
          {describeError(publishError)}
        </Banner>
      ) : null}

      {validation ? (
        <Banner
          tone={validation.blocking ? 'danger' : 'success'}
          title={validation.blocking ? 'Validation found blocking issues' : 'Validation is clean'}
        >
          {validation.issues.length} issue(s). Issues are linked to the field that owns them.
        </Banner>
      ) : null}

      <Tabs
        tabs={tabs}
        value={tab}
        onChange={(id) => setTab(id as TabId)}
        label="Capability sections"
        panelIdPrefix="capability"
      />

      <TabPanel id="overview" active={tab === 'overview'} panelIdPrefix="capability">
        <div className="space-y-4">
          <Card>
            <CardHeader title="Details" description="Capability-level fields (PATCH /api/admin/capabilities/{id})." />
            <CardBody>
              <form
                className="space-y-3"
                onSubmit={(event) => {
                  event.preventDefault();
                  const data = new FormData(event.currentTarget);
                  setDetailsError(null);
                  updateCapability
                    .mutateAsync({
                      name: String(data.get('name') ?? '').trim(),
                      description: String(data.get('description') ?? '').trim(),
                      provider_id: String(data.get('provider_id') ?? '') || null,
                      credential_id: String(data.get('credential_id') ?? '') || null,
                      needs_live_agent: data.get('needs_live_agent') === 'on',
                    })
                    .then(() => toast.success({ title: 'Capability details saved' }))
                    .catch((error: unknown) => {
                      setDetailsError(describeError(error));
                      toast.apiError(error, 'Details were not saved');
                    });
                }}
              >
                <div className="grid gap-3 sm:grid-cols-2">
                  <Field label="Name" htmlFor="capability-detail-name">
                    <Input id="capability-detail-name" name="name" defaultValue={capability.name} required />
                  </Field>
                  <Field label="Provider" htmlFor="capability-detail-provider" hint="Leave empty to keep the current provider.">
                    <Input
                      id="capability-detail-provider"
                      name="provider_id"
                      defaultValue={capability.provider_id ?? ''}
                      placeholder="prov_…"
                    />
                  </Field>
                </div>
                <Field label="Credential" htmlFor="capability-detail-credential" hint="Credential id (cred_…), not the secret.">
                  <Input
                    id="capability-detail-credential"
                    name="credential_id"
                    defaultValue={capability.credential_id ?? ''}
                    placeholder="cred_…"
                  />
                </Field>
                <Field label="Description" htmlFor="capability-detail-description">
                  <Textarea id="capability-detail-description" name="description" rows={3} defaultValue={capability.description} />
                </Field>
                <div className="flex items-center justify-between gap-2 rounded-control border border-border px-2 py-1.5">
                  <span className="text-xs text-muted">Needs live agent reasoning</span>
                  <Badge tone={capability.needs_live_agent ? 'warning' : 'neutral'}>
                    {capability.needs_live_agent ? 'yes — agent engine required' : 'no'}
                  </Badge>
                </div>
                <div className="flex items-center justify-between gap-2">
                  {detailsError ? (
                    <InlineAlert tone="danger">{detailsError}</InlineAlert>
                  ) : (
                    <span className="text-xs text-muted">
                      Editing details is independent from the version config.
                    </span>
                  )}
                  <Button type="submit" size="sm" variant="secondary" loading={updateCapability.isPending}>
                    Save details
                  </Button>
                </div>
              </form>
            </CardBody>
          </Card>

          <Card>
            <CardHeader
              title="Readiness checks"
              description="Why the capability is (or is not) callable right now — database facts only, no probing."
            />
            <CardBody className="grid gap-2 sm:grid-cols-2">
              {[
                { label: 'Published version', ok: capability.verification.checks.published, detail: capability.verification.published_version ? `v${capability.verification.published_version}` : 'none' },
                { label: 'Credential', ok: !capability.verification.checks.credential_required || capability.verification.checks.credential_active, detail: capability.verification.checks.credential_required ? (capability.verification.checks.credential_active ? 'active' : 'missing or inactive') : 'not required' },
                { label: 'Provider', ok: capability.verification.checks.provider_active, detail: capability.provider_name ?? 'none' },
                { label: 'Route', ok: capability.verification.checks.route_usable, detail: capability.verification.checks.route_detail },
              ].map((check) => (
                <div key={check.label} className="flex items-start gap-2 rounded-control border border-border p-2">
                  {check.ok ? (
                    <CheckCircle2 aria-hidden className="mt-0.5 h-4 w-4 shrink-0 text-success" />
                  ) : (
                    <XCircle aria-hidden className="mt-0.5 h-4 w-4 shrink-0 text-danger" />
                  )}
                  <div className="min-w-0">
                    <p className="text-xs font-medium text-text">{check.label}</p>
                    <p className="break-words text-[11px] text-muted">{check.detail}</p>
                  </div>
                </div>
              ))}
            </CardBody>
          </Card>

          <Card>
            <CardHeader
              title="Configuration issues"
              description="Issues from the last validation run, or from the stored config when this page loaded."
              actions={
                <Button size="sm" variant="secondary" onClick={() => void runValidate()} loading={validateVersion.isPending} disabled={!editable || dirty}>
                  Re-validate
                </Button>
              }
            />
            <CardBody>
              <IssueList issues={validation?.issues ?? capability.issues ?? []} onJump={(next) => setTab(next)} />
            </CardBody>
          </Card>

          <Card>
            <CardHeader title="Model and advanced keys" description="Values that are not part of the tabbed forms." />
            <CardBody>
              <AdvancedPanel {...panelProps} />
            </CardBody>
          </Card>

          <Card>
            <CardHeader title="Version in the editor" description="Switch versions from the Versions tab." />
            <CardBody className="space-y-2 text-xs">
              {version ? (
                <>
                  <p>
                    v{version.version} · <StatusBadge status={version.status} /> · checksum{' '}
                    <span className="font-mono">{version.checksum.slice(0, 12)}…</span>
                  </p>
                  <p className="text-muted">
                    created {formatDateTime(version.created_at)} · last test {statusLabel(version.last_test_status ?? 'unknown')}
                  </p>
                </>
              ) : (
                <p className="text-muted">No version selected.</p>
              )}
              {versionQuery.isError ? (
                <Banner tone="danger" title="Could not load this version" requestId={errorRequestId(versionQuery.error)}>
                  {describeError(versionQuery.error)}
                </Banner>
              ) : null}
            </CardBody>
          </Card>
        </div>
      </TabPanel>

      <TabPanel id="request" active={tab === 'request'} panelIdPrefix="capability">
        <RequestPanel {...panelProps} />
      </TabPanel>
      <TabPanel id="input" active={tab === 'input'} panelIdPrefix="capability">
        <InputSchemaPanel {...panelProps} />
      </TabPanel>
      <TabPanel id="output" active={tab === 'output'} panelIdPrefix="capability">
        <OutputPanel {...panelProps} />
      </TabPanel>
      <TabPanel id="modes" active={tab === 'modes'} panelIdPrefix="capability">
        <ModesPanel {...panelProps} />
      </TabPanel>
      <TabPanel id="errors" active={tab === 'errors'} panelIdPrefix="capability">
        <ErrorsPanel {...panelProps} />
      </TabPanel>
      <TabPanel id="limits" active={tab === 'limits'} panelIdPrefix="capability">
        <LimitsPanel {...panelProps} />
      </TabPanel>
      <TabPanel id="examples" active={tab === 'examples'} panelIdPrefix="capability">
        <ExamplesPanel {...panelProps} />
      </TabPanel>

      <TabPanel id="versions" active={tab === 'versions'} panelIdPrefix="capability">
        <VersionsPanel
          capabilityId={capability.id}
          versions={versions}
          selectedVersionId={selectedVersionId}
          onSelectVersion={(id) => setSelectedVersionId(id)}
          currentVersionId={capability.current_version_id}
          publishedVersionId={capability.published_version_id}
          onChanged={() => void capabilityQuery.refetch()}
        />
      </TabPanel>

      <TabPanel id="tests" active={tab === 'tests'} panelIdPrefix="capability">
        <Card>
          <CardHeader
            title="Test history"
            description="Every row is a real call. The newest passing test for a version is the publish evidence."
            actions={
              <Button
                size="sm"
                variant="primary"
                onClick={() => setTestOpen(true)}
                disabled={!version || dirty}
                iconLeft={<FlaskConical aria-hidden className="h-3.5 w-3.5" />}
              >
                Run test
              </Button>
            }
          />
          <CardBody className="p-0">
            {tests.isError ? (
              <div className="p-3">
                <Banner tone="danger" title="Could not load test history" requestId={errorRequestId(tests.error)}>
                  {describeError(tests.error)}
                </Banner>
              </div>
            ) : tests.isLoading ? (
              <div className="p-3">
                <SkeletonTable rows={3} columns={5} />
              </div>
            ) : (tests.data?.items.length ?? 0) === 0 ? (
              <EmptyState
                icon={<FlaskConical aria-hidden className="h-5 w-5" />}
                title="No test recorded"
                description="A test makes a real upstream call and stores redacted request/response evidence."
                action={
                  <Button size="sm" variant="primary" onClick={() => setTestOpen(true)} disabled={!version || dirty}>
                    Run the first test
                  </Button>
                }
              />
            ) : (
              <Table caption="Test history">
                <THead sticky={false}>
                  <TR>
                    <TH>Test</TH>
                    <TH>Version</TH>
                    <TH>Status</TH>
                    <TH align="right">HTTP</TH>
                    <TH align="right">Latency</TH>
                    <TH>Error</TH>
                    <TH>When</TH>
                  </TR>
                </THead>
                <TBody>
                  {tests.data?.items.map((test) => (
                    <TR key={test.id}>
                      <TD className="font-mono text-[11px]">{test.id}</TD>
                      <TD>v{test.version ?? '—'}</TD>
                      <TD>
                        <StatusBadge status={test.status} />
                      </TD>
                      <TD align="right" className="tabular-nums">
                        {test.http_status ?? '—'}
                      </TD>
                      <TD align="right" className="tabular-nums">
                        {formatDuration(test.latency_ms)}
                      </TD>
                      <TD className="max-w-[220px]">
                        <span className="sr-truncate text-xs text-muted">
                          {test.error ? JSON.stringify(test.error).slice(0, 120) : '—'}
                        </span>
                      </TD>
                      <TD>
                        <TimeAgo value={test.created_at} />
                      </TD>
                    </TR>
                  ))}
                </TBody>
              </Table>
            )}
          </CardBody>
        </Card>
      </TabPanel>

      <TabPanel id="routes" active={tab === 'routes'} panelIdPrefix="capability">
        <RoutesPanel capabilityId={capability.id} versions={versions} />
      </TabPanel>

      <Card>
        <CardHeader
          title="Advanced JSON (whole version config)"
          description="Two-way: the structured fields above write here, and a valid object here replaces the draft."
          actions={
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                setDraft(baseline);
                setConfigText(JSON.stringify(baseline, null, 2));
                setAdvancedError(null);
                toast.info({ title: 'Draft reverted to the saved config' });
              }}
              disabled={!dirty}
              iconLeft={<RotateCcw aria-hidden className="h-3.5 w-3.5" />}
            >
              Revert
            </Button>
          }
        />
        <CardBody>
          <JsonEditor
            label="Version config (JSON)"
            hint="Use this when a nested shape is not covered by the structured fields."
            value={configText}
            onChange={onAdvancedChange}
            rows={18}
            requireObject
            externalError={advancedError}
            disabled={!editable}
          />
        </CardBody>
      </Card>

      {version ? (
        <TestRunner
          open={testOpen}
          onClose={() => setTestOpen(false)}
          capabilityId={capability.id}
          versionId={version.id}
          versionNumber={version.version}
          examples={draft.examples}
          modes={[
            ...(draft.modes?.sync ? (['sync'] as const) : []),
            ...(draft.modes?.async ? (['async'] as const) : []),
            ...(draft.modes?.stream ? (['stream'] as const) : []),
          ]}
          initialInput={JSON.stringify(draft.examples?.[0]?.input ?? {}, null, 2)}
        />
      ) : null}
    </div>
  );
}

export default function CapabilityDetailPage() {
  return (
    <Suspense
      fallback={
        <div className="space-y-4">
          <PageHeader title="Capability" backHref="/capabilities" backLabel="Capabilities" />
          <SkeletonBlock rows={3} />
        </div>
      }
    >
      <CapabilityWorkbench />
    </Suspense>
  );
}
