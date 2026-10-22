'use client';

import { useParams } from 'next/navigation';
import { RefreshCw } from 'lucide-react';

import { describeError, errorRequestId } from '@/lib/api';
import { Banner } from '@/components/ui/banner';
import { PageHeader } from '@/components/ui/page-header';
import { SkeletonBlock } from '@/components/ui/skeleton';
import { InvocationDetailView, TaskDetailView } from '@/components/records/records-detail';
import { useRecordResolver } from '@/components/records/api';

export default function RecordDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params?.id;
  const resolved = useRecordResolver(id);

  if (resolved.isLoading) {
    return (
      <div className="space-y-4">
        <PageHeader title="Record" backHref="/records" backLabel="Records" />
        <SkeletonBlock rows={5} />
      </div>
    );
  }

  if (resolved.isError || !resolved.data) {
    return (
      <div className="space-y-4">
        <PageHeader title="Record" backHref="/records" backLabel="Records" />
        <Banner
          tone="danger"
          title="Could not load this record"
          requestId={errorRequestId(resolved.error)}
          primaryAction={{
            label: 'Retry',
            onClick: () => void resolved.refetch(),
            loading: resolved.isFetching,
            variant: 'primary',
          }}
        >
          {describeError(resolved.error)} The page first asks <code>/api/admin/invocations/&#123;id&#125;</code> (which
          also accepts a request id) and falls back to <code>/api/admin/tasks/&#123;id&#125;</code> only on a 404.
        </Banner>
        <p className="inline-flex items-center gap-1 text-xs text-muted">
          <RefreshCw aria-hidden className="h-3 w-3" /> The id is looked up in both record tables.
        </p>
      </div>
    );
  }

  if (resolved.data.kind === 'invocation') {
    const invocation = resolved.data.invocation;
    return (
      <div className="space-y-4">
        <PageHeader
          title={`Invocation ${invocation.request_id}`}
          backHref="/records"
          backLabel="Records"
          description="A single capability call: routing, usage, cost and the redacted bodies."
        />
        <InvocationDetailView invocation={invocation} />
      </div>
    );
  }

  const task = resolved.data.task;
  return (
    <div className="space-y-4">
      <PageHeader
        title={`Task ${task.id}`}
        backHref="/records?tab=tasks"
        backLabel="Records"
        description="An async capability job. Cancel and reconcile answer with what the provider actually did."
      />
      <TaskDetailView task={task} />
    </div>
  );
}
