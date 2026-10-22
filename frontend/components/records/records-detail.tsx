'use client';

import { useState } from 'react';
import Link from 'next/link';
import { AlertTriangle, Download, RefreshCw, Square, Wifi } from 'lucide-react';

import { describeError, errorRequestId } from '@/lib/api';
import { formatBytes, formatCurrency, formatDateTime, formatDuration, formatPercent, statusLabel } from '@/lib/format';
import { Badge, StatusBadge } from '@/components/ui/badge';
import { Banner, InlineAlert } from '@/components/ui/banner';
import { Button } from '@/components/ui/button';
import { Card, CardBody, CardFooter, CardHeader } from '@/components/ui/card';
import { CodeBlock, prettyJson } from '@/components/ui/code-block';
import { Dialog } from '@/components/ui/dialog';
import { EmptyState } from '@/components/ui/empty-state';
import { Field } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { TimeAgo } from '@/components/ui/time-ago';
import { useToast } from '@/components/ui/toast';
import { useCancelTaskMutation, useReconcileTaskMutation, type CancelResult, type InvocationDetail, type Task } from './api';

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="text-xs uppercase tracking-wide text-muted">{label}</p>
      <div className="mt-0.5 break-words text-sm text-text">{children}</div>
    </div>
  );
}

/* ------------------------------------------------------------- invocation view -- */

export function InvocationDetailView({ invocation }: { invocation: InvocationDetail }) {
  const cost = invocation.cost;
  const estimated = cost?.estimated_usd ?? null;
  const actual = cost?.actual_usd ?? null;

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader
          title="Invocation"
          description="One row per call attempt. Previews are redacted at rest by the platform."
          actions={<StatusBadge status={invocation.status} />}
        />
        <CardBody className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          <Row label="Request id">
            <span className="select-all font-mono text-xs">{invocation.request_id}</span>
          </Row>
          <Row label="Invocation id">
            <span className="select-all font-mono text-xs">{invocation.id}</span>
          </Row>
          <Row label="Caller">
            {invocation.agent?.name ?? '—'}
            <span className="ml-1 text-xs text-muted">({invocation.caller_type ?? 'unknown'})</span>
          </Row>
          <Row label="Surface">
            <Badge tone="neutral" dot={false}>
              {statusLabel(invocation.surface)}
            </Badge>
          </Row>
          <Row label="Capability">
            <span className="font-mono text-xs">{invocation.capability?.slug ?? '—'}</span>
            {invocation.version ? <span className="ml-1 text-xs text-muted">v{invocation.version}</span> : null}
          </Row>
          <Row label="Provider">{invocation.provider?.name ?? '—'}</Row>
          <Row label="Mode">{invocation.mode ? statusLabel(invocation.mode) : '—'}</Row>
          <Row label="HTTP status">{invocation.http_status ?? '—'}</Row>
          <Row label="Latency">{formatDuration(invocation.latency_ms)}</Row>
          <Row label="Retries">{invocation.retries}</Row>
          <Row label="Started">{formatDateTime(invocation.created_at)}</Row>
          <Row label="Finished">
            {invocation.finished_at ? <TimeAgo value={invocation.finished_at} /> : 'still running'}
          </Row>
          <Row label="Task">
            {invocation.task_id ? (
              <Link href={`/records/${invocation.task_id}`} className="font-mono text-xs hover:underline">
                {invocation.task_id}
              </Link>
            ) : (
              '—'
            )}
          </Row>
          <Row label="Idempotency key">
            <span className="font-mono text-xs">{invocation.idempotency_key ?? '—'}</span>
          </Row>
          <Row label="Route reason">{invocation.route_reason ?? '—'}</Row>
        </CardBody>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader title="Usage" description="Token counters as reported by the provider." />
          <CardBody>
            <CodeBlock code={prettyJson(invocation.usage)} label="usage" maxHeightClassName="max-h-40" copyable={false} />
          </CardBody>
        </Card>
        <Card>
          <CardHeader title="Cost" description="Estimated and actual are different numbers and are labelled as such." />
          <CardBody className="space-y-2 text-sm">
            <p className="flex items-center justify-between">
              <span className="text-muted">Estimated (from pricing)</span>
              <span className="tabular-nums">{estimated === null ? '—' : formatCurrency(estimated, cost?.currency ?? 'USD')}</span>
            </p>
            <p className="flex items-center justify-between">
              <span className="text-muted">Actual (provider-reported)</span>
              <span className="tabular-nums">{actual === null ? 'not reported' : formatCurrency(actual, cost?.currency ?? 'USD')}</span>
            </p>
            {estimated !== null && actual !== null && estimated > 0 ? (
              <p className="text-xs text-muted">
                Difference: {formatPercent((actual - estimated) / estimated, 1)} against the estimate.
              </p>
            ) : null}
          </CardBody>
        </Card>
      </div>

      {invocation.error ? (
        <Card>
          <CardHeader title="Error" description="The error the caller received." />
          <CardBody className="space-y-2">
            <Banner tone="danger" title={`${invocation.error.code}`}>
              {invocation.error.message}
            </Banner>
            {invocation.error.action ? (
              <InlineAlert tone="info">Next step: {invocation.error.action}</InlineAlert>
            ) : (
              <p className="text-xs text-muted">The envelope carried no action for this error.</p>
            )}
          </CardBody>
          <CardFooter>
            <Link href={`/records?status=${encodeURIComponent(invocation.error.code)}`} className="text-xs hover:underline">
              Look for the same code in other records
            </Link>
            <Link
              href={`/capabilities?search=${encodeURIComponent(invocation.capability?.slug ?? '')}`}
              className="text-xs hover:underline"
            >
              Open the capability
            </Link>
          </CardFooter>
        </Card>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader title="Request preview (redacted)" />
          <CardBody>
            <CodeBlock
              code={prettyJson(invocation.request_preview)}
              label="request"
              maxHeightClassName="max-h-64"
              copyable={false}
              notice="Stored only when the logging policy records bodies; secret values are redacted at rest."
              emptyLabel="No request body was stored for this call."
            />
          </CardBody>
        </Card>
        <Card>
          <CardHeader title="Response preview (redacted)" />
          <CardBody>
            <CodeBlock
              code={prettyJson(invocation.response_preview)}
              label="response"
              maxHeightClassName="max-h-64"
              copyable={false}
              notice="Redacted at rest; the full body is never echoed to the browser."
              emptyLabel="No response body was stored for this call."
            />
          </CardBody>
        </Card>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------- task view -- */

function CancelDialog({ task, onClose }: { task: Task; onClose: () => void }) {
  const toast = useToast();
  const cancel = useCancelTaskMutation(task.id);
  const [reason, setReason] = useState('');
  const [result, setResult] = useState<CancelResult | null>(null);

  const run = async () => {
    try {
      const outcome = await cancel.mutateAsync({ reason: reason.trim() || undefined });
      setResult(outcome);
      if (outcome.canceled) {
        toast.success({ title: 'Cancel confirmed by the provider', description: outcome.message });
      } else if (!outcome.supported) {
        toast.warning({ title: 'The provider has no cancel endpoint', description: outcome.message });
      } else {
        toast.warning({ title: 'Cancellation requested', description: outcome.message });
      }
    } catch (cause) {
      toast.apiError(cause, 'The cancel request failed');
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={`Cancel task ${task.id}?`}
      description="Cancel is only truthful when the provider confirms it. The result below is the backend's own answer."
      size="md"
      dismissible={!cancel.isPending}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={cancel.isPending}>
            Close
          </Button>
          <Button
            variant="danger"
            onClick={() => void run()}
            loading={cancel.isPending}
            disabled={Boolean(result)}
            iconLeft={<Square aria-hidden className="h-3.5 w-3.5" />}
          >
            Request cancel
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <Field label="Reason (optional)" htmlFor="cancel-reason">
          <Input
            id="cancel-reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="User changed the brief."
            maxLength={500}
            disabled={Boolean(result)}
          />
        </Field>

        {cancel.isError ? (
          <Banner tone="danger" title="The cancel request failed" requestId={errorRequestId(cancel.error)}>
            {describeError(cancel.error)}
          </Banner>
        ) : null}

        {result ? (
          <div className="space-y-2">
            <Banner
              tone={result.canceled ? 'success' : result.supported ? 'warning' : 'danger'}
              title={
                result.canceled
                  ? 'Cancelled — the provider confirmed it'
                  : result.supported
                    ? result.cancel_requested
                      ? 'Cancel requested — not yet cancelled upstream'
                      : 'The provider rejected the cancel request'
                    : 'Unsupported — nothing was cancelled upstream'
              }
            >
              {result.message}
            </Banner>
            <dl className="grid grid-cols-2 gap-2 text-xs">
              <div>
                <dt className="text-muted">supported</dt>
                <dd className="font-mono">{String(result.supported)}</dd>
              </div>
              <div>
                <dt className="text-muted">canceled</dt>
                <dd className="font-mono">{String(result.canceled)}</dd>
              </div>
              <div>
                <dt className="text-muted">cancel_requested</dt>
                <dd className="font-mono">{String(result.cancel_requested)}</dd>
              </div>
              <div>
                <dt className="text-muted">upstream_may_continue</dt>
                <dd className="font-mono">{String(result.upstream_may_continue)}</dd>
              </div>
            </dl>
            {result.upstream_may_continue ? (
              <InlineAlert tone="warning">
                The upstream job may still complete and be billed. Use Reconcile to pick up its real outcome.
              </InlineAlert>
            ) : null}
            <p className="text-xs text-muted">
              New task status: <StatusBadge status={result.task?.status ?? result.status} />
            </p>
          </div>
        ) : (
          <p className="text-xs text-muted">
            The backend answers with <code>supported</code>, <code>canceled</code>, <code>cancel_requested</code> and{' '}
            <code>upstream_may_continue</code> separately — this dialog shows all four.
          </p>
        )}
      </div>
    </Dialog>
  );
}

export function TaskDetailView({ task }: { task: Task }) {
  const toast = useToast();
  const reconcile = useReconcileTaskMutation(task.id);
  const [cancelOpen, setCancelOpen] = useState(false);
  const terminal = task.status === 'succeeded' || task.status === 'failed' || task.status === 'cancelled';

  const runReconcile = async () => {
    try {
      const updated = await reconcile.mutateAsync();
      toast.success({
        title: 'Reconciled with the upstream job',
        description: `The task is now ${statusLabel(updated.status)}${updated.progress_note ? ` — ${updated.progress_note}` : ''}.`,
      });
    } catch (cause) {
      toast.apiError(cause, 'Reconciliation failed');
    }
  };

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader
          title="Async task"
          description="The worker polls the provider; progress comes from the upstream job."
          actions={
            <>
              <Button
                size="sm"
                variant="secondary"
                onClick={() => void runReconcile()}
                loading={reconcile.isPending}
                iconLeft={<RefreshCw aria-hidden className="h-3.5 w-3.5" />}
              >
                Reconcile
              </Button>
              <Button
                size="sm"
                variant="danger"
                onClick={() => setCancelOpen(true)}
                disabled={terminal}
                iconLeft={<Square aria-hidden className="h-3.5 w-3.5" />}
              >
                Cancel
              </Button>
            </>
          }
        />
        <CardBody className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <Row label="Task id">
              <span className="select-all font-mono text-xs">{task.id}</span>
            </Row>
            <Row label="Request id">
              <span className="select-all font-mono text-xs">{task.request_id ?? '—'}</span>
            </Row>
            <Row label="Status">
              <StatusBadge status={task.status} />
            </Row>
            <Row label="Capability">
              <span className="font-mono text-xs">{task.capability?.slug ?? '—'}</span>
            </Row>
            <Row label="Provider">{task.provider?.name ?? '—'}</Row>
            <Row label="Agent">{task.agent?.name ?? '—'}</Row>
            <Row label="Upstream job id">
              <span className="font-mono text-xs">{task.upstream_task_id ?? '—'}</span>
            </Row>
            <Row label="Attempts">
              {task.attempts} / {task.max_attempts || '—'}
            </Row>
            <Row label="Cost">
              {task.cost?.actual_usd !== null && task.cost?.actual_usd !== undefined
                ? `${formatCurrency(task.cost.actual_usd, task.cost.currency)} actual`
                : 'not reported'}
              {task.budget_usd !== null ? ` · budget ${formatCurrency(task.budget_usd)}` : ''}
            </Row>
            <Row label="Created">{formatDateTime(task.created_at)}</Row>
            <Row label="Updated">{task.updated_at ? <TimeAgo value={task.updated_at} /> : '—'}</Row>
            <Row label="Finished">{task.finished_at ? <TimeAgo value={task.finished_at} /> : 'not finished'}</Row>
          </div>

          <div>
            <p className="mb-1 flex items-center justify-between text-xs text-muted">
              <span>Progress</span>
              <span className="tabular-nums">{task.progress}%</span>
            </p>
            <div
              role="progressbar"
              aria-valuenow={task.progress}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-label="Task progress"
              className="h-2 w-full overflow-hidden rounded-pill bg-info-soft"
            >
              <div className="h-full bg-accent" style={{ width: `${Math.max(0, Math.min(100, task.progress))}%` }} />
            </div>
            {task.progress_note ? <p className="mt-1 text-xs text-text">{task.progress_note}</p> : null}
          </div>

          {task.error ? (
            <Banner tone="danger" title={task.error.code ?? 'Task error'}>
              {task.error.message ?? 'The task failed without a message.'}
            </Banner>
          ) : null}
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="Timeline" description="Task fields plus the invocation rows opened for this task." />
        <CardBody className="p-0">
          {(task.events?.length ?? 0) === 0 ? (
            <EmptyState title="No timeline rows" description="The task has not produced an event yet." />
          ) : (
            <ol className="divide-y divide-border">
              {task.events?.map((event, index) => (
                <li key={`${event.type}-${index}`} className="flex flex-wrap items-start justify-between gap-2 px-4 py-2">
                  <div className="min-w-0">
                    <p className="text-sm text-text">{event.title}</p>
                    {event.detail ? <p className="mt-0.5 break-words text-[11px] text-muted">{event.detail}</p> : null}
                    <p className="text-[11px] text-muted">
                      <span className="font-mono">{event.type}</span> · source {event.source}
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <StatusBadge status={event.status} />
                    <span className="text-[11px] text-muted">{formatDateTime(event.at)}</span>
                  </div>
                </li>
              ))}
            </ol>
          )}
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="Artifacts" description="Files this task produced. Downloads are mediated by the platform." />
        <CardBody className="p-0">
          {task.artifacts.length === 0 ? (
            <EmptyState title="No artifact" description="The task has not produced a file (yet)." />
          ) : (
            <ul className="divide-y divide-border">
              {task.artifacts.map((artifact) => {
                const image = (artifact.content_type ?? '').startsWith('image/');
                return (
                  <li key={artifact.id} className="flex items-center justify-between gap-3 px-4 py-2">
                    <div className="flex min-w-0 items-center gap-2">
                      {image ? (
                        <span
                          aria-hidden
                          className="h-10 w-10 shrink-0 rounded border border-border bg-info-soft bg-cover bg-center"
                          style={{ backgroundImage: `url(${artifact.url})` }}
                        />
                      ) : null}
                      <div className="min-w-0">
                        <p className="truncate text-sm text-text">{artifact.filename ?? artifact.id}</p>
                        <p className="text-[11px] text-muted">
                          {artifact.content_type ?? 'unknown type'} · {formatBytes(artifact.size)}
                        </p>
                      </div>
                    </div>
                    <a
                      href={artifact.url}
                      className="inline-flex shrink-0 items-center gap-1 text-xs hover:underline"
                      download={artifact.filename ?? undefined}
                    >
                      <Download aria-hidden className="h-3.5 w-3.5" />
                      Download
                    </a>
                  </li>
                );
              })}
            </ul>
          )}
        </CardBody>
        <CardFooter>
          <span className="inline-flex items-center gap-1 text-[11px] text-muted">
            <Wifi aria-hidden className="h-3 w-3" />
            Artifact URLs are scope-checked server-side; this page never receives a storage path.
          </span>
        </CardFooter>
      </Card>

      {task.result !== null && task.result !== undefined ? (
        <Card>
          <CardHeader title="Result" description="What the provider returned for this task." />
          <CardBody>
            <CodeBlock code={prettyJson(task.result)} label="result" maxHeightClassName="max-h-64" />
          </CardBody>
        </Card>
      ) : null}

      {task.status === 'uncertain' ? (
        <Banner tone="warning" title="The real outcome is unknown">
          <span className="flex items-start gap-1">
            <AlertTriangle aria-hidden className="mt-0.5 h-3 w-3 shrink-0" />
            The provider did not confirm a final state. Reconcile to re-query it; the platform does not report
            &quot;failed&quot; for a job that may still be running.
          </span>
        </Banner>
      ) : null}

      {cancelOpen ? <CancelDialog task={task} onClose={() => setCancelOpen(false)} /> : null}
    </div>
  );
}
