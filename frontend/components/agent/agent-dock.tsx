'use client';

/**
 * The floating agent panel dock.
 *
 * Layout contract:
 *  - the whole panel is a fixed-height column: the title bar, the session tab strip
 *    and the status/run strip never move;
 *  - only the transcript scrolls;
 *  - the composer lives in the drawer's footer slot, so it is always visible at the
 *    bottom and never scrolls away — which is the whole point of a chat panel.
 *
 * Accessibility contract (CONTRACT §9):
 *  - the trigger is a 56px accent button, focusable and labelled, on every dashboard
 *    page; it sits above a sticky footer on narrow screens and never covers a page's
 *    primary action;
 *  - the drawer is 440px wide on ≥768px and a full-screen sheet below that; Esc
 *    closes it, focus is trapped inside while it is open and returns to the button
 *    when it closes (`useFocusTrap` in `components/ui/drawer`).
 */

import { useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import { Clock3, RotateCcw, Sparkles, WifiOff } from 'lucide-react';

import { describeError, errorRequestId } from '@/lib/api';
import { formatDateTimeShort, formatDuration, statusLabel } from '@/lib/format';
import { Badge, StatusBadge } from '@/components/ui/badge';
import { Banner } from '@/components/ui/banner';
import { Button } from '@/components/ui/button';
import { Drawer } from '@/components/ui/drawer';
import { SkeletonBlock } from '@/components/ui/skeleton';
import { Tooltip } from '@/components/ui/tooltip';
import { cn } from '@/lib/utils';
import { AgentComposer, AgentTimeline } from './agent-chat';
import { SessionTabs } from './session-tabs';
import { useAgent } from './agent-context';

/* --------------------------------------------------------------- health gate -- */

function HealthBanner() {
  const { health, healthError, healthLoading, refreshHealth } = useAgent();

  if (healthLoading) {
    return <SkeletonBlock rows={1} />;
  }
  if (healthError) {
    return (
      <Banner
        tone="warning"
        title="Could not read the agent engine health"
        requestId={errorRequestId(healthError)}
        primaryAction={{ label: 'Retry', onClick: refreshHealth, variant: 'secondary' }}
      >
        {describeError(healthError)}
      </Banner>
    );
  }
  if (!health) return null;

  const engineVersion = health.sidecar.engine?.version;
  const sidecarOk = health.sidecar.ok === true;
  const modelConfigured = health.platform.model.configured === true;
  if (sidecarOk && modelConfigured) return null;

  const title = !modelConfigured
    ? 'No model is configured for the agent'
    : !sidecarOk
      ? engineVersion
        ? 'The agent engine is not responding'
        : 'The agent engine is not installed'
      : 'The agent is not fully configured';

  return (
    <Banner tone="warning" title={title}>
      <p>
        {!sidecarOk
          ? engineVersion
            ? `The sidecar did not answer a health check (reported engine ${engineVersion}).`
            : 'The sidecar reported no engine package, so agent runs cannot start.'
          : `The model is not configured (${health.platform.model.error?.message ?? 'no provider/model pair resolves'}).`}
      </p>
      <div className="mt-1.5 flex flex-wrap gap-1.5">
        <Link href="/settings">
          <Button size="sm" variant="secondary">
            Set the model
          </Button>
        </Link>
        <Link href="/providers">
          <Button size="sm" variant="secondary">
            Providers &amp; credentials
          </Button>
        </Link>
        <Button size="sm" variant="ghost" onClick={refreshHealth}>
          Re-check health
        </Button>
      </div>
    </Banner>
  );
}

/* ----------------------------------------------------------------- run strip -- */

const CONNECTION_LABEL: Record<string, string> = {
  idle: 'Idle',
  connecting: 'Connecting to the event stream',
  open: 'Streaming',
  reconnecting: 'Reconnecting…',
  closed: 'Stream closed',
  error: 'Stream error',
};

function RunStrip() {
  const { run, connection, stopping, stop } = useAgent();
  const [now, setNow] = useState(() => Date.now());

  const running = Boolean(run.id) && (run.status === 'running' || run.status === 'queued');
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);

  if (!run.id && !run.status) return null;

  const elapsedMs = run.startedAt ? now - new Date(run.startedAt).getTime() : null;
  const reconnecting = connection === 'reconnecting';

  return (
    <div
      className={cn(
        'flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-border px-3 py-1.5 text-xs',
        running ? 'bg-accent-soft' : 'bg-info-soft/60',
      )}
    >
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        {run.status ? <StatusBadge status={run.status} /> : null}
        <span className="inline-flex items-center gap-1 text-text">
          <Clock3 aria-hidden className="h-3 w-3 text-muted" />
          <span className="tabular-nums">{running ? formatDuration(elapsedMs) : run.step || 'Idle'}</span>
        </span>
        {running ? <span className="truncate text-muted">{run.step}</span> : null}
        {reconnecting ? (
          <Badge tone="warning" dot={false}>
            <WifiOff aria-hidden className="h-3 w-3" /> Reconnecting
          </Badge>
        ) : (
          <span className="hidden text-[11px] text-muted sm:inline">{CONNECTION_LABEL[connection] ?? connection}</span>
        )}
      </div>
      {running ? (
        <Tooltip content="Stop and report what stopped, and which tool calls may still complete upstream">
          <Button size="sm" variant="danger" onClick={() => void stop()} loading={stopping}>
            Stop
          </Button>
        </Tooltip>
      ) : null}
    </div>
  );
}

/* ---------------------------------------------------------------------- dock -- */

export function AgentDock() {
  const {
    open,
    setOpen,
    toggle,
    sessionId,
    session,
    sessionLoading,
    sessionError,
    awaitingInput,
    retry,
    lastFailedPrompt,
    connection,
  } = useAgent();

  const title = useMemo(() => session?.title ?? 'Agent', [session]);

  return (
    <>
      <button
        type="button"
        aria-label="Open the agent panel"
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-controls="agent-panel"
        onClick={toggle}
        className={cn(
          'fixed right-6 z-40 flex h-14 w-14 items-center justify-center rounded-pill bg-accent text-white',
          'bottom-20 shadow-overlay transition-colors duration-150 hover:bg-accent-strong active:bg-accent-strong',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-strong focus-visible:ring-offset-2',
          'md:bottom-6',
        )}
      >
        <Sparkles aria-hidden className="h-6 w-6" />
        {connection === 'reconnecting' ? (
          <span
            aria-hidden
            className="absolute -right-0.5 -top-0.5 h-3 w-3 rounded-pill border-2 border-surface bg-warning"
          />
        ) : null}
        {awaitingInput ? (
          <span
            aria-hidden
            className="absolute -left-0.5 -top-0.5 h-3 w-3 rounded-pill border-2 border-surface bg-warning"
          />
        ) : null}
        <span className="sr-visually-hidden">
          {awaitingInput ? 'The agent is waiting for your reply.' : 'Agent panel'}
        </span>
      </button>

      <Drawer
        open={open}
        onClose={() => setOpen(false)}
        title={title}
        description={sessionId ? undefined : 'A new session starts with your first message'}
        id="agent-panel"
        widthClassName="w-full md:w-[440px] md:max-w-[440px]"
        contentClassName="flex min-h-0 flex-1 flex-col overflow-hidden p-0"
        footer={<AgentComposer />}
      >
        <div
          id="agent-tabpanel"
          role="tabpanel"
          aria-labelledby={sessionId ? `agent-tab-${sessionId}` : undefined}
          className="flex min-h-0 flex-1 flex-col"
        >
          <SessionTabs />

          <div className="shrink-0 space-y-2 px-3 pt-2 pb-2 empty:hidden">
            <HealthBanner />
            {sessionError ? (
              <Banner tone="danger" title="Could not load this session" requestId={errorRequestId(sessionError)}>
                {describeError(sessionError)}
              </Banner>
            ) : null}
            {awaitingInput ? (
              <p className="rounded-control border border-warning/30 bg-warning-soft px-2 py-1 text-[11px] text-text">
                The agent is waiting for you.
              </p>
            ) : null}
            {session?.status ? (
              <p className="text-[11px] text-muted">
                Session state: {statusLabel(session.status)} · created {formatDateTimeShort(session.created_at)}
              </p>
            ) : null}
          </div>

          <RunStrip />

          {sessionLoading ? (
            <div className="min-h-0 flex-1 overflow-hidden p-3">
              <SkeletonBlock rows={5} />
            </div>
          ) : (
            <AgentTimeline onRetry={lastFailedPrompt ? () => void retry() : undefined} />
          )}

          {lastFailedPrompt ? (
            <div className="flex shrink-0 items-center justify-between gap-2 border-t border-border px-3 py-1">
              <span className="truncate text-[11px] text-muted">The last run failed.</span>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => void retry()}
                iconLeft={<RotateCcw aria-hidden className="h-3.5 w-3.5" />}
              >
                Retry
              </Button>
            </div>
          ) : null}
        </div>
      </Drawer>
    </>
  );
}
