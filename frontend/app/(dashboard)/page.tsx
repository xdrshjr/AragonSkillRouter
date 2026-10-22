'use client';

import { useMemo, useState } from 'react';
import Link from 'next/link';
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  Boxes,
  Clock,
  Gauge,
  KeyRound,
  RefreshCw,
  Server,
  ShieldAlert,
  Timer,
} from 'lucide-react';

import { useOverviewQuery } from '@/lib/queries';
import { ONBOARDING_LABELS, useSession } from '@/lib/auth';
import { describeError, errorRequestId } from '@/lib/api';
import {
  formatDateTime,
  formatDuration,
  formatNumber,
  formatPercent,
  pluralize,
  severityTone,
  statusLabel,
} from '@/lib/format';
import type { Invocation, OnboardingSteps, Problem } from '@/lib/types';
import { Badge, StatusBadge } from '@/components/ui/badge';
import { Banner } from '@/components/ui/banner';
import { Button } from '@/components/ui/button';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { EmptyState } from '@/components/ui/empty-state';
import { PageHeader, SectionHeading } from '@/components/ui/page-header';
import { ConnectGuide } from '@/components/overview/connect-guide';
import { SkeletonStats, SkeletonTable } from '@/components/ui/skeleton';
import { Stat, StatGrid } from '@/components/ui/stat';
import { Stepper, type Step } from '@/components/ui/stepper';
import { Table, TBody, TD, TH, THead, TR, type SortState } from '@/components/ui/table';
import { TimeAgo } from '@/components/ui/time-ago';

function BuildSteps({ steps, agentConfigured }: { steps: OnboardingSteps; agentConfigured: boolean }) {
  const items: Step[] = ONBOARDING_LABELS.map((entry) => {
    const done =
      entry.key === 'agent_model_configured'
        ? Boolean(steps.agent_model_configured ?? agentConfigured)
        : Boolean(steps[entry.key]);
    return {
      id: entry.key,
      label: entry.label,
      hint: entry.hint,
      status: done ? 'complete' : 'current',
      href: entry.href,
    };
  });
  const passwordDone = Boolean(steps.password_changed);
  items.unshift({
    id: 'password_changed',
    label: 'Bootstrap password replaced',
    hint: 'Required before business endpoints accept requests.',
    status: passwordDone ? 'complete' : 'blocked',
    href: '/login',
  });
  return <Stepper steps={items} label="Onboarding checklist" />;
}

function ProblemsList({ problems, total }: { problems: Problem[]; total: number }) {
  if (problems.length === 0) {
    return (
      <EmptyState
        title="No open problems"
        description="Credentials, routes, tasks and tests are all reporting healthy."
      />
    );
  }
  return (
    <div className="flex flex-col gap-2">
      <ul className="flex flex-col gap-2">
        {problems.map((problem) => (
          <li
            key={problem.id}
            className="rounded-control border border-border bg-surface p-3 transition-colors duration-150 hover:border-border-strong"
          >
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div className="flex min-w-0 items-start gap-2">
                <Badge tone={severityTone(problem.severity)}>{statusLabel(problem.severity)}</Badge>
                <div className="min-w-0">
                  <p className="text-sm font-medium text-text">{problem.title}</p>
                  <p className="mt-0.5 text-xs text-muted">{problem.detail}</p>
                </div>
              </div>
              <Link
                href={problem.href}
                className="inline-flex shrink-0 items-center gap-1 text-xs font-medium text-accent hover:underline"
              >
                {problem.action}
                <ArrowRight aria-hidden className="h-3 w-3" />
              </Link>
            </div>
          </li>
        ))}
      </ul>
      {total > problems.length ? (
        <p className="text-xs text-muted">
          Showing {problems.length} of {total} problems. Fix the top ones first — they are ordered by severity.
        </p>
      ) : null}
    </div>
  );
}

const INVOCATION_SORT_FIELD: Record<string, (row: Invocation) => string | number> = {
  created_at: (row) => row.created_at,
  capability: (row) => row.capability?.slug ?? '',
  status: (row) => row.status,
  latency: (row) => row.latency_ms ?? -1,
  mode: (row) => row.mode ?? '',
};

function RecentInvocations({ rows }: { rows: Invocation[] }) {
  const [sort] = useState<SortState>({ key: 'created_at', direction: 'desc' });

  const sorted = useMemo(() => {
    const getter = INVOCATION_SORT_FIELD[sort.key] ?? INVOCATION_SORT_FIELD.created_at;
    const copy = [...rows];
    copy.sort((a, b) => {
      const left = getter(a);
      const right = getter(b);
      if (left === right) return 0;
      const result = left < right ? -1 : 1;
      return sort.direction === 'asc' ? result : -result;
    });
    return copy;
  }, [rows, sort]);

  if (rows.length === 0) {
    return (
      <EmptyState
        title="No invocations recorded yet"
        description="Calls appear here as soon as an agent or a manual test reaches a capability."
        action={
          <Link href="/capabilities">
            <Button variant="primary" size="sm">
              Create a capability
            </Button>
          </Link>
        }
      />
    );
  }

  return (
    <Table caption="Recent invocations" maxHeightClassName="max-h-[420px]">
      <THead>
        <TR>
          <TH>Capability</TH>
          <TH>Mode</TH>
          <TH>Status</TH>
          <TH align="right">Latency</TH>
          <TH>Provider</TH>
          <TH>When</TH>
        </TR>
      </THead>
      <TBody>
        {sorted.map((row) => (
          <TR key={row.id}>
            <TD className="max-w-[240px]">
              <span className="sr-truncate font-medium">{row.capability?.slug ?? '—'}</span>
              <span className="sr-truncate block text-[11px] text-muted">{row.surface}</span>
            </TD>
            <TD>
              <Badge tone="info" dot={false}>
                {statusLabel(row.mode)}
              </Badge>
            </TD>
            <TD>
              <StatusBadge status={row.status} />
            </TD>
            <TD align="right" className="tabular-nums">
              {formatDuration(row.latency_ms)}
            </TD>
            <TD className="max-w-[160px]">
              <span className="sr-truncate">{row.provider?.name ?? '—'}</span>
            </TD>
            <TD>
              <TimeAgo value={row.created_at} />
            </TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

export default function OverviewPage() {
  const { user } = useSession();
  const overview = useOverviewQuery();

  if (overview.isLoading) {
    return (
      <div className="space-y-4">
        <PageHeader title="Overview" description="Loading the current router state…" />
        <SkeletonStats />
        <SkeletonTable rows={4} columns={5} />
      </div>
    );
  }

  if (overview.isError || !overview.data) {
    return (
      <div className="space-y-4">
        <PageHeader title="Overview" description="Dashboard payload from /api/admin/overview." />
        <Banner
          tone="danger"
          title="Could not load the overview"
          requestId={errorRequestId(overview.error)}
          actions={['Retry the request.', 'Check that the backend is running and reachable through /api.']}
          primaryAction={{
            label: 'Retry',
            onClick: () => void overview.refetch(),
            loading: overview.isFetching,
            variant: 'primary',
          }}
        >
          {describeError(overview.error)}
        </Banner>
      </div>
    );
  }

  const data = overview.data;
  const { counts, health, onboarding } = data;
  const firstRun = counts.capabilities.total === 0 && data.recent_invocations.length === 0;

  return (
    <div className="space-y-6">
      <PageHeader
        title={`Welcome back${user ? `, ${user.username}` : ''}`}
        description="Live counts, 24h health, actionable problems and the first-run checklist."
        meta={
          <>
            <span>generated {formatDateTime(data.generated_at)}</span>
            <span aria-hidden>·</span>
            <span>
              engine {health.engine.version ? `${health.engine.version} (${health.engine.status})` : health.engine.status}
            </span>
          </>
        }
        actions={
          <>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => void overview.refetch()}
              loading={overview.isFetching}
              iconLeft={<RefreshCw aria-hidden className="h-3.5 w-3.5" />}
            >
              Refresh
            </Button>
            <Link href="/capabilities">
              <Button variant="primary" size="sm" iconLeft={<Boxes aria-hidden className="h-3.5 w-3.5" />}>
                New capability
              </Button>
            </Link>
          </>
        }
      />

      {firstRun ? (
        <Banner
          tone="info"
          title="First run: nothing configured yet"
          primaryAction={{ label: 'Open capabilities', onClick: () => window.location.assign('/capabilities'), variant: 'primary' }}
        >
          Start with a provider and a credential, then create one capability, validate it, run a real test and publish
          it. Agent tokens come after that.
        </Banner>
      ) : null}

      {health.failed_tasks_24h > 0 ? (
        <Banner
          tone="warning"
          title={`${health.failed_tasks_24h} ${pluralize(health.failed_tasks_24h, 'async task')} failed in the last 24h`}
          primaryAction={{ label: 'Open records', onClick: () => window.location.assign('/records'), variant: 'secondary' }}
        >
          Failed and uncertain tasks are listed under Records; reconcile an uncertain task before retrying so a paid
          upstream call is not repeated.
        </Banner>
      ) : null}

      <StatGrid>
        <Stat
          label="Capabilities"
          value={formatNumber(counts.capabilities.total)}
          hint={`${counts.capabilities.published} published · ${counts.capabilities.draft} draft`}
          icon={<Boxes aria-hidden className="h-4 w-4" />}
        />
        <Stat
          label="Providers"
          value={formatNumber(counts.providers.total)}
          hint={`${counts.providers.active} active`}
          icon={<Server aria-hidden className="h-4 w-4" />}
        />
        <Stat
          label="Credentials"
          value={formatNumber(counts.credentials.total)}
          hint={
            counts.credentials.invalid > 0
              ? `${counts.credentials.invalid} invalid · ${counts.credentials.expiring_soon} expiring`
              : `${counts.credentials.active} active · ${counts.credentials.expiring_soon} expiring soon`
          }
          tone={counts.credentials.invalid > 0 ? 'danger' : 'neutral'}
          icon={<KeyRound aria-hidden className="h-4 w-4" />}
        />
        <Stat
          label="Calls (24h)"
          value={formatNumber(health.calls_24h)}
          hint={`success ${formatPercent(health.success_rate_24h)}`}
          tone={health.success_rate_24h !== null && health.success_rate_24h < 0.9 ? 'warning' : 'neutral'}
          icon={<Activity aria-hidden className="h-4 w-4" />}
        />
      </StatGrid>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader
            title="Problems that need a decision"
            description="Derived from real rows: invalid credentials, unhealthy routes, failing tests, stuck tasks."
            actions={
              <Link href="/capabilities" className="text-xs text-accent hover:underline">
                Capabilities
              </Link>
            }
          />
          <CardBody>
            <ProblemsList problems={data.problems} total={data.problems_total} />
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="First-run checklist" description="Each step maps to a real row in the database." />
          <CardBody>
            <BuildSteps steps={onboarding.steps} agentConfigured={onboarding.agent_configured} />
            <p className="mt-2 text-[11px] text-muted">
              Agent configured: {onboarding.agent_configured ? 'yes' : 'no'} · published capabilities:{' '}
              {onboarding.published_capabilities} · active tokens: {onboarding.active_agent_tokens}
            </p>
          </CardBody>
        </Card>
      </div>

      <ConnectGuide />

      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader
            title="Health (24h)"
            description="Window is fixed by the backend; empty windows report an em dash instead of 0%."
          />
          <CardBody className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            <div>
              <p className="text-xs uppercase tracking-wide text-muted">Success rate</p>
              <p className="mt-1 flex items-center gap-1 text-lg font-semibold tabular-nums">
                <Gauge aria-hidden className="h-4 w-4 text-muted" />
                {formatPercent(health.success_rate_24h)}
              </p>
            </div>
            <div>
              <p className="text-xs uppercase tracking-wide text-muted">Avg latency</p>
              <p className="mt-1 flex items-center gap-1 text-lg font-semibold tabular-nums">
                <Timer aria-hidden className="h-4 w-4 text-muted" />
                {formatDuration(health.avg_latency_ms_24h)}
              </p>
            </div>
            <div>
              <p className="text-xs uppercase tracking-wide text-muted">Failed calls</p>
              <p className="mt-1 text-lg font-semibold tabular-nums">
                {formatNumber(health.failed_calls_24h ?? 0)}
              </p>
            </div>
            <div>
              <p className="text-xs uppercase tracking-wide text-muted">Failed tasks</p>
              <p className="mt-1 flex items-center gap-1 text-lg font-semibold tabular-nums">
                <AlertTriangle aria-hidden className="h-4 w-4 text-muted" />
                {formatNumber(health.failed_tasks_24h)}
              </p>
            </div>
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="Task queue" description="Async work waiting, running or stuck." />
          <CardBody className="space-y-2 text-sm">
            <div className="flex items-center justify-between">
              <span className="text-muted">Queued</span>
              <span className="tabular-nums">{counts.tasks.queued}</span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-muted">Running</span>
              <span className="tabular-nums">{counts.tasks.running}</span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-muted">Failed</span>
              <span className="tabular-nums text-danger">{counts.tasks.failed}</span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-muted">Uncertain</span>
              <span className="tabular-nums text-warning">{counts.tasks.uncertain}</span>
            </div>
            <div className="flex items-center justify-between border-t border-border pt-2">
              <span className="flex items-center gap-1 text-muted">
                <Clock aria-hidden className="h-3.5 w-3.5" /> Skills / agents
              </span>
              <span className="text-xs text-muted">
                {counts.skills.total} skills · {counts.agents.total} agents
              </span>
            </div>
          </CardBody>
        </Card>
      </div>

      <section className="space-y-3">
        <SectionHeading title="Recent invocations" description="Newest 8 records across every surface." />
        <RecentInvocations rows={data.recent_invocations} />
      </section>

      {counts.credentials.invalid > 0 ? (
        <Banner
          tone="danger"
          title={`${counts.credentials.invalid} credential(s) were rejected by their provider`}
          primaryAction={{ label: 'Open providers', onClick: () => window.location.assign('/providers'), variant: 'primary' }}
        >
          Calls routed through an invalid credential cannot succeed. Verify or rotate the secret.
        </Banner>
      ) : null}

      {!onboarding.agent_configured ? (
        <p className="flex items-center gap-1 text-xs text-muted">
          <ShieldAlert aria-hidden className="h-3.5 w-3.5" />
          No agent model is configured yet — agent steps in SKILLs will stay unavailable until Settings is completed.
        </p>
      ) : null}
    </div>
  );
}
