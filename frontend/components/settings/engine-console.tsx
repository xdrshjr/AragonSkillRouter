'use client';

/**
 * `/settings/engine` — the AragonAgent engine console.
 *
 * Every action posts to the real endpoint and renders the response it got:
 *  - Check → candidate release row (or "nothing newer");
 *  - Validate → the per-check list (npm build, import smoke, adapter compat, …);
 *  - Activate → the activated release plus the sidecar health comparison;
 *  - Rollback → the previous release plus whether the running sidecar matches.
 *
 * Nothing here reports success that the backend did not return, and a failed
 * validation explicitly says the current release is unchanged.
 */

import { useEffect, useMemo, useState } from 'react';
import { AlertTriangle, CheckCircle2, ChevronDown, Clock3, RefreshCw, RotateCcw, ShieldCheck, Upload } from 'lucide-react';

import { describeError, errorRequestId } from '@/lib/api';
import { formatDateTime, statusLabel } from '@/lib/format';
import { Badge, StatusBadge } from '@/components/ui/badge';
import { Banner, InlineAlert } from '@/components/ui/banner';
import { Button } from '@/components/ui/button';
import { Card, CardBody, CardFooter, CardHeader } from '@/components/ui/card';
import { CodeBlock, prettyJson } from '@/components/ui/code-block';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { EmptyState } from '@/components/ui/empty-state';
import { SkeletonTable } from '@/components/ui/skeleton';
import { Table, TBody, TD, TH, THead, TR } from '@/components/ui/table';
import { TimeAgo } from '@/components/ui/time-ago';
import { useToast } from '@/components/ui/toast';
import {
  useAgentToolsQuery,
  useEngineActivateMutation,
  useEngineCheckMutation,
  useEngineHealthQuery,
  useEngineQuery,
  useEngineRollbackMutation,
  useEngineValidateMutation,
  type EngineCheck,
  type EngineCheckResult,
  type EngineRelease,
} from './api';

function useElapsed(active: boolean): number {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    if (!active) {
      setSeconds(0);
      return;
    }
    const started = Date.now();
    const timer = window.setInterval(() => setSeconds(Math.round((Date.now() - started) / 1000)), 500);
    return () => window.clearInterval(timer);
  }, [active]);
  return seconds;
}

function versionTuple(value: string | null | undefined): [number, number, number] | null {
  if (!value) return null;
  const match = /^(\d+)\.(\d+)\.(\d+)/.exec(value.trim());
  if (!match) return null;
  return [Number(match[1]), Number(match[2]), Number(match[3])];
}

function compare(a: [number, number, number], b: [number, number, number]): number {
  for (let index = 0; index < 3; index += 1) {
    if (a[index] !== b[index]) return a[index] - b[index];
  }
  return 0;
}

function CheckList({ checks }: { checks: EngineCheck[] }) {
  if (checks.length === 0) return <p className="text-xs text-muted">No check was recorded for this release.</p>;
  return (
    <ul className="space-y-1">
      {checks.map((check) => (
        <li key={check.name} className="flex items-start justify-between gap-2 rounded-control border border-border px-2 py-1">
          <div className="min-w-0">
            <p className="font-mono text-[11px] text-text">{check.name}</p>
            <p className="break-words text-[11px] text-muted">{check.detail}</p>
          </div>
          <Badge tone={check.status === 'passed' ? 'success' : check.status === 'skipped' ? 'warning' : 'danger'} dot={false}>
            {check.status}
          </Badge>
        </li>
      ))}
    </ul>
  );
}

function ReleaseSummary({ release, label }: { release: EngineRelease; label: string }) {
  return (
    <div className="space-y-2">
      <p className="flex flex-wrap items-center gap-2 text-sm">
        <StatusBadge status={release.status} />
        <span className="font-mono text-xs">{release.ref}</span>
        <Badge tone="neutral" dot={false}>
          {label}
        </Badge>
      </p>
      <dl className="grid gap-2 text-xs sm:grid-cols-2 lg:grid-cols-3">
        <div>
          <dt className="text-muted">version</dt>
          <dd className="font-mono">{release.npm_version ?? release.version ?? '—'}</dd>
        </div>
        <div>
          <dt className="text-muted">commit</dt>
          <dd className="font-mono">{release.commit_sha ? release.commit_sha.slice(0, 12) : '—'}</dd>
        </div>
        <div>
          <dt className="text-muted">adapter</dt>
          <dd className="font-mono">{release.adapter_version || '—'}</dd>
        </div>
        <div>
          <dt className="text-muted">channel</dt>
          <dd>{release.channel}</dd>
        </div>
        <div>
          <dt className="text-muted">verified</dt>
          <dd>{release.verified_at ? formatDateTime(release.verified_at) : 'not verified'}</dd>
        </div>
        <div>
          <dt className="text-muted">activated</dt>
          <dd>{release.activated_at ? formatDateTime(release.activated_at) : 'not activated'}</dd>
        </div>
      </dl>
      {release.notes ? <p className="text-xs text-muted">{release.notes}</p> : null}
      <details className="rounded-control border border-border p-2">
        <summary className="flex cursor-pointer items-center gap-1 text-xs text-text">
          <ChevronDown aria-hidden className="h-3 w-3" /> {release.checks.length} validation check(s)
        </summary>
        <div className="mt-2">
          <CheckList checks={release.checks} />
        </div>
      </details>
    </div>
  );
}

interface MatrixRow {
  feature: string;
  state: 'available' | 'needs_adapter' | 'permission_limited';
  reason: string;
}

export function EngineConsole() {
  const toast = useToast();
  const engine = useEngineQuery();
  const health = useEngineHealthQuery();
  const tools = useAgentToolsQuery();
  const check = useEngineCheckMutation();
  const validate = useEngineValidateMutation();
  const activate = useEngineActivateMutation();
  const rollback = useEngineRollbackMutation();

  const [lastResult, setLastResult] = useState<{ action: string; result: EngineCheckResult } | null>(null);
  const [lastError, setLastError] = useState<{ action: string; error: unknown } | null>(null);
  const [rollbackOpen, setRollbackOpen] = useState(false);

  const busy = check.isPending || validate.isPending || activate.isPending || rollback.isPending;
  const elapsed = useElapsed(busy);

  const overview = engine.data;
  const matrix = overview?.capability_matrix;

  const matrixRows = useMemo<MatrixRow[]>(() => {
    if (!matrix) return [];
    const rows: MatrixRow[] = [];
    const runningVersion = health.data?.sidecar.engine?.version ?? overview?.current?.npm_version ?? null;
    const min = versionTuple(matrix.supported_core.min);
    const maxExclusive = versionTuple(matrix.supported_core.max_exclusive);
    const running = versionTuple(runningVersion);

    if (!running) {
      rows.push({
        feature: 'Engine core',
        state: 'needs_adapter',
        reason: `No engine version was reported by the sidecar (adapter ${matrix.adapter_version} supports ${matrix.supported_core.min} – <${matrix.supported_core.max_exclusive}).`,
      });
    } else if (min && maxExclusive && (compare(running, min) < 0 || compare(running, maxExclusive) >= 0)) {
      rows.push({
        feature: `Engine core ${runningVersion}`,
        state: 'needs_adapter',
        reason: `Outside the adapter's supported range ${matrix.supported_core.min} – <${matrix.supported_core.max_exclusive}. Install an engine in range or upgrade the adapter.`,
      });
    } else {
      rows.push({
        feature: `Engine core ${runningVersion ?? ''}`.trim(),
        state: 'available',
        reason: `Inside the supported range ${matrix.supported_core.min} – <${matrix.supported_core.max_exclusive}.`,
      });
    }

    rows.push({
      feature: `Adapter ${matrix.adapter_version}`,
      state: 'available',
      reason: `Sidecar protocol ${matrix.sidecar_protocol}, ${matrix.endpoints.length} endpoints, ${matrix.event_union.length} event types.`,
    });

    const policy = tools.data?.policy;
    const mutationCount = tools.data?.mutating.length ?? 0;
    rows.push({
      feature: `Platform tools (${matrix.platform_tools})`,
      state: policy && !policy.enabled ? 'permission_limited' : 'available',
      reason: policy
        ? policy.enabled
          ? `Auto-execute is on: ${policy.max_calls} call(s) / ${policy.max_minutes} min / ${policy.max_cost_usd} USD per run.`
          : `Auto-execute is off, so the ${mutationCount} mutating tool(s) need explicit confirmation in the panel.`
        : 'The tool ceiling could not be read from /api/admin/agent/tools.',
    });

    for (const [status, count] of Object.entries(matrix.capabilities_by_status)) {
      const state: MatrixRow['state'] = status === 'published' ? 'available' : 'permission_limited';
      rows.push({
        feature: `Capabilities · ${statusLabel(status)}`,
        state,
        reason:
          status === 'published'
            ? `${count} capability(ies) are published, so the agent can read and call them.`
            : `${count} capability(ies) are ${status}, so they are invisible to an external token and cannot be called.`,
      });
    }

    if (overview?.current && overview.aligned.core_version && runningVersion !== overview.aligned.core_version) {
      rows.push({
        feature: 'Aligned core version',
        state: 'needs_adapter',
        reason: `This build expects core ${overview.aligned.core_version}; the running sidecar reports ${runningVersion ?? 'nothing'}.`,
      });
    }

    return rows;
  }, [matrix, health.data, overview, tools.data]);

  const runAction = async (action: string, run: () => Promise<EngineCheckResult>) => {
    setLastError(null);
    try {
      const result = await run();
      setLastResult({ action, result });
      toast.success({ title: `${action} finished`, description: summarise(result) });
    } catch (cause) {
      setLastError({ action, error: cause });
      setLastResult(null);
      toast.apiError(cause, `${action} failed`);
    }
  };

  if (engine.isLoading) {
    return <SkeletonTable rows={5} columns={4} />;
  }

  if (engine.isError) {
    return (
      <Banner
        tone="danger"
        title="Could not load the engine state"
        requestId={errorRequestId(engine.error)}
        primaryAction={{ label: 'Retry', onClick: () => void engine.refetch(), loading: engine.isFetching, variant: 'primary' }}
      >
        {describeError(engine.error)}
      </Banner>
    );
  }

  const healthOk = health.data?.sidecar.ok === true;
  const modelConfigured = health.data?.platform.model.configured === true;

  return (
    <div className="space-y-4">
      <Banner tone="info" title="This console drives a real engine">
        Check hits the network, Validate builds the candidate in isolation, Activate switches the running engine and
        rolls back automatically if its health check fails. A failed validation always leaves the current release in
        place.
      </Banner>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader
            title="Current engine"
            description="The release row marked active, plus what the sidecar reports right now."
            actions={
              <Badge tone={healthOk ? 'success' : 'warning'} dot={false}>
                {healthOk ? 'sidecar ok' : 'sidecar not ok'}
              </Badge>
            }
          />
          <CardBody className="space-y-3">
            {overview?.current ? (
              <ReleaseSummary release={overview.current} label="active" />
            ) : (
              <EmptyState
                title="No active release recorded"
                description="Run Check to detect an upstream ref; activation records the release row."
              />
            )}
            <dl className="grid gap-2 text-xs sm:grid-cols-2">
              <div>
                <dt className="text-muted">sidecar version</dt>
                <dd className="font-mono">{health.data?.sidecar.engine?.version ?? '—'}</dd>
              </div>
              <div>
                <dt className="text-muted">sidecar commit</dt>
                <dd className="font-mono">{health.data?.sidecar.engine?.commit?.slice(0, 12) ?? '—'}</dd>
              </div>
              <div>
                <dt className="text-muted">package</dt>
                <dd className="font-mono">{overview?.npm.core ?? '—'}</dd>
              </div>
              <div>
                <dt className="text-muted">model configured</dt>
                <dd>{modelConfigured ? 'yes' : 'no'}</dd>
              </div>
              <div>
                <dt className="text-muted">sidecar URL</dt>
                <dd className="break-all font-mono">{overview?.sidecar.url ?? '—'}</dd>
              </div>
              <div>
                <dt className="text-muted">aligned commit</dt>
                <dd className="font-mono">{overview?.aligned.commit?.slice(0, 12) ?? '—'}</dd>
              </div>
            </dl>
            {health.isError ? (
              <InlineAlert tone="warning">
                Health could not be read: {describeError(health.error)}
              </InlineAlert>
            ) : null}
          </CardBody>
        </Card>

        <Card>
          <CardHeader
            title="Candidate"
            description="Detected refs wait here until they are validated and activated."
            actions={
              <Button
                size="sm"
                variant="secondary"
                onClick={() => void runAction('Check', () => check.mutateAsync())}
                loading={check.isPending}
                iconLeft={<RefreshCw aria-hidden className="h-3.5 w-3.5" />}
              >
                Check for updates
              </Button>
            }
          />
          <CardBody className="space-y-3">
            {overview?.candidate ? (
              <ReleaseSummary release={overview.candidate} label="candidate" />
            ) : (
              <p className="text-xs text-muted">
                No candidate release is recorded. Check reads the upstream ref and stores a candidate row when something
                newer exists.
              </p>
            )}
            <div className="flex flex-wrap gap-2">
              <Button
                size="sm"
                variant="primary"
                disabled={!overview?.candidate || busy}
                onClick={() =>
                  overview?.candidate
                    ? void runAction('Validate', () => validate.mutateAsync(overview.candidate!.id))
                    : undefined
                }
                loading={validate.isPending}
                iconLeft={<ShieldCheck aria-hidden className="h-3.5 w-3.5" />}
              >
                Validate
              </Button>
              <Button
                size="sm"
                variant="secondary"
                disabled={!overview?.candidate || busy}
                onClick={() =>
                  overview?.candidate
                    ? void runAction('Activate', () => activate.mutateAsync(overview.candidate!.id))
                    : undefined
                }
                loading={activate.isPending}
                iconLeft={<Upload aria-hidden className="h-3.5 w-3.5" />}
              >
                Activate
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={busy || !overview?.current?.previous_release_id}
                onClick={() => setRollbackOpen(true)}
                loading={rollback.isPending}
                iconLeft={<RotateCcw aria-hidden className="h-3.5 w-3.5" />}
              >
                Roll back
              </Button>
            </div>
            {busy ? (
              <p className="flex items-center gap-1.5 text-xs text-accent-strong" role="status">
                <Clock3 aria-hidden className="h-3.5 w-3.5 animate-pulse" />
                {elapsed}s elapsed — the request is still running. An isolated npm build can take minutes.
              </p>
            ) : null}
          </CardBody>
          <CardFooter>
            <p className="text-[11px] text-muted">
              Activation performs a health check and rolls back automatically when it fails.
            </p>
          </CardFooter>
        </Card>
      </div>

      {lastError ? (
        <Banner tone="danger" title={`${lastError.action} failed`} requestId={errorRequestId(lastError.error)}>
          {describeError(lastError.error)}
        </Banner>
      ) : null}

      {lastResult ? (
        <Card>
          <CardHeader
            title={`${lastResult.action} result`}
            description="Exactly what the endpoint returned."
            actions={
              <Button size="sm" variant="ghost" onClick={() => setLastResult(null)}>
                Dismiss
              </Button>
            }
          />
          <CardBody className="space-y-2">
            {lastResult.result.status ? (
              <p className="flex flex-wrap items-center gap-2 text-sm">
                <StatusBadge status={lastResult.result.status} />
                {lastResult.result.note ? <span className="text-xs text-muted">{lastResult.result.note}</span> : null}
              </p>
            ) : null}
            {lastResult.result.warning ? (
              <Banner tone="warning" title="The backend reported a caveat">
                {lastResult.result.warning}
              </Banner>
            ) : null}
            {lastResult.result.sidecar ? (
              <p className="text-xs text-muted">
                sidecar reports {lastResult.result.sidecar.running_version ?? 'nothing'} ·{' '}
                {lastResult.result.sidecar.matches_release ? 'matches the release' : 'does NOT match the release'}
              </p>
            ) : null}
            {lastResult.result.checks ? <CheckList checks={lastResult.result.checks} /> : null}
            {lastResult.result.next_step ? (
              <p className="text-xs text-muted">Next: {lastResult.result.next_step}</p>
            ) : null}
            <details className="rounded-control border border-border p-2">
              <summary className="cursor-pointer text-xs text-text">Raw response</summary>
              <div className="mt-2">
                <CodeBlock code={prettyJson(lastResult.result)} label="response" maxHeightClassName="max-h-64" />
              </div>
            </details>
          </CardBody>
        </Card>
      ) : null}

      <Card>
        <CardHeader
          title="Capability matrix"
          description="What this build + adapter can actually do, with the reason for every limitation."
        />
        <CardBody className="p-0">
          {matrixRows.length === 0 ? (
            <EmptyState title="No matrix" description="The engine endpoint returned no capability matrix." />
          ) : (
            <Table caption="Engine capability matrix">
              <THead sticky={false}>
                <TR>
                  <TH>Feature</TH>
                  <TH>State</TH>
                  <TH>Reason</TH>
                </TR>
              </THead>
              <TBody>
                {matrixRows.map((row) => (
                  <TR key={`${row.feature}-${row.state}`}>
                    <TD className="text-xs">{row.feature}</TD>
                    <TD>
                      <Badge
                        tone={row.state === 'available' ? 'success' : row.state === 'needs_adapter' ? 'warning' : 'neutral'}
                        dot={false}
                      >
                        {row.state.replace('_', ' ')}
                      </Badge>
                    </TD>
                    <TD className="text-xs text-muted">{row.reason}</TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          )}
        </CardBody>
        <CardFooter>
          <p className="text-[11px] text-muted">
            Event union: {matrix?.event_union.join(', ') ?? '—'} · CLI min {matrix?.cli_min ?? '—'}
          </p>
        </CardFooter>
      </Card>

      <Card>
        <CardHeader title="Release history" description="Every detected, validated, activated and retired release." />
        <CardBody className="p-0">
          {(overview?.history.length ?? 0) === 0 ? (
            <EmptyState title="No release recorded" description="Check for updates to detect the first candidate." />
          ) : (
            <Table caption="Engine release history">
              <THead sticky={false}>
                <TR>
                  <TH>Ref</TH>
                  <TH>Version</TH>
                  <TH>Status</TH>
                  <TH>Checks</TH>
                  <TH>Detected</TH>
                  <TH>Activated</TH>
                  <TH>Details</TH>
                </TR>
              </THead>
              <TBody>
                {overview?.history.map((release) => {
                  const failed = release.checks.filter((item) => item.status === 'failed').length;
                  const passed = release.checks.filter((item) => item.status === 'passed').length;
                  return (
                    <TR key={release.id}>
                      <TD className="font-mono text-[11px]">{release.ref}</TD>
                      <TD className="font-mono text-[11px]">{release.npm_version ?? release.version ?? '—'}</TD>
                      <TD>
                        <StatusBadge status={release.status} />
                      </TD>
                      <TD className="text-xs">
                        {release.checks.length === 0 ? (
                          <span className="text-muted">none</span>
                        ) : (
                          <span className="flex items-center gap-1.5">
                            <span className="inline-flex items-center gap-1 text-success">
                              <CheckCircle2 aria-hidden className="h-3 w-3" />
                              {passed}
                            </span>
                            {failed > 0 ? (
                              <span className="inline-flex items-center gap-1 text-danger">
                                <AlertTriangle aria-hidden className="h-3 w-3" />
                                {failed}
                              </span>
                            ) : null}
                          </span>
                        )}
                      </TD>
                      <TD className="text-xs text-muted">
                        <TimeAgo value={release.created_at} />
                      </TD>
                      <TD className="text-xs text-muted">
                        {release.activated_at ? formatDateTime(release.activated_at) : '—'}
                      </TD>
                      <TD>
                        <details className="text-xs">
                          <summary className="cursor-pointer text-muted">checks</summary>
                          <div className="mt-1 w-[320px]">
                            <CheckList checks={release.checks} />
                          </div>
                        </details>
                      </TD>
                    </TR>
                  );
                })}
              </TBody>
            </Table>
          )}
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="Current policy" description="Read from the engine settings, not from this page." />
        <CardBody className="text-sm">
          <p>
            Upgrade policy: <strong>{overview?.policy.upgrade_policy ?? '—'}</strong> · channel{' '}
            <strong>{overview?.policy.upgrade_channel ?? '—'}</strong> · tracked branch{' '}
            <strong>{overview?.policy.track_branch ?? '—'}</strong>.
          </p>
          <p className="mt-1 text-xs text-muted">
            {overview?.policy.upgrade_policy === 'auto'
              ? 'A detected release can be activated without a second click once validation passes — but a failing check still stops it.'
              : 'Nothing is installed automatically: a detected candidate waits until you validate and activate it here.'}{' '}
            A validation failure never replaces the active release, and an activation whose health check fails rolls back.
          </p>
        </CardBody>
        <CardFooter>
          <p className="text-[11px] text-muted">
            Aligned core {overview?.aligned.core_version ?? '—'} · aligned CLI {overview?.aligned.cli_version ?? '—'} ·
            history rows {overview?.history_total ?? 0}
          </p>
        </CardFooter>
      </Card>

      <ConfirmDialog
        open={rollbackOpen}
        onCancel={() => setRollbackOpen(false)}
        onConfirm={() => {
          setRollbackOpen(false);
          void runAction('Rollback', () => rollback.mutateAsync());
        }}
        title="Roll back to the previous release?"
        description="The current release is marked rolled_back and its predecessor becomes active. The sidecar must be reinstalled to match."
        confirmLabel="Roll back"
        tone="danger"
        loading={rollback.isPending}
        impact={
          <div className="space-y-1">
            <p>
              From {overview?.current?.ref ?? '—'} to the recorded predecessor (
              {overview?.current?.previous_release_id ?? 'none'}).
            </p>
            <p>The endpoint compares the running sidecar version with the release and reports any mismatch.</p>
          </div>
        }
      />
    </div>
  );
}

function summarise(result: EngineCheckResult): string {
  if (result.warning) return result.warning;
  if (result.release?.status) return `status ${result.release.status}`;
  if (result.status) return `status ${result.status}`;
  if (result.candidate) return `candidate ${result.candidate.ref}`;
  return result.note ?? 'See the result panel for the exact answer.';
}
