'use client';

import { CheckCircle2, CircleDashed, FlaskConical, Radio, UploadCloud } from 'lucide-react';

import type { VerificationSummary } from '@/lib/types';
import { cn } from '@/lib/utils';
import { statusLabel } from '@/lib/format';
import { TimeAgo } from '@/components/ui/time-ago';
import { Tooltip } from '@/components/ui/tooltip';

interface ChipProps {
  icon: React.ReactNode;
  label: string;
  value: string;
  detail: React.ReactNode;
  tone: 'complete' | 'pending' | 'blocked';
  tooltip: string;
}

const TONE: Record<ChipProps['tone'], string> = {
  complete: 'border-success/30 bg-success-soft',
  pending: 'border-warning/30 bg-warning-soft',
  blocked: 'border-border-strong bg-info-soft',
};

const ICON_TONE: Record<ChipProps['tone'], string> = {
  complete: 'text-success',
  pending: 'text-warning',
  blocked: 'text-muted',
};

function Chip({ icon, label, value, detail, tone, tooltip }: ChipProps) {
  return (
    <Tooltip content={tooltip}>
      <div className={cn('flex w-full items-start gap-2 rounded-control border p-2 text-left', TONE[tone])}>
        <span className={cn('mt-0.5 shrink-0', ICON_TONE[tone])}>{icon}</span>
        <div className="min-w-0">
          <p className="text-[11px] uppercase tracking-wide text-muted">{label}</p>
          <p className="truncate text-xs font-medium text-text">{value}</p>
          <p className="truncate text-[11px] text-muted">{detail}</p>
        </div>
      </div>
    </Tooltip>
  );
}

export interface StateChipsProps {
  verification: VerificationSummary;
  className?: string;
}

/**
 * CONTRACT §4: saved / test passed / published / callable are four different
 * facts and are never merged into one "ready" badge.
 */
export function StateChips({ verification, className }: StateChipsProps) {
  const saved = verification.saved;
  const testPassed = verification.test_passed;
  const published = verification.published_version !== null;
  const callable = verification.callable;

  return (
    <div className={cn('grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-4', className)}>
      <Chip
        icon={saved ? <CheckCircle2 aria-hidden className="h-4 w-4" /> : <CircleDashed aria-hidden className="h-4 w-4" />}
        tone={saved ? 'complete' : 'blocked'}
        label="Saved"
        value={saved ? `Version ${verification.saved_version}` : 'No version'}
        detail={saved ? statusLabel(verification.saved_version_status) : 'Config not persisted'}
        tooltip="Saved means the configuration was persisted by the API. It says nothing about whether it works."
      />
      <Chip
        icon={testPassed ? <CheckCircle2 aria-hidden className="h-4 w-4" /> : <FlaskConical aria-hidden className="h-4 w-4" />}
        tone={testPassed ? 'complete' : 'pending'}
        label="Test passed"
        value={testPassed ? 'Real call succeeded' : verification.last_test_status ? statusLabel(verification.last_test_status) : 'Never tested'}
        detail={
          verification.last_test ? (
            <>
              <TimeAgo value={verification.last_test.created_at} />
            </>
          ) : (
            'Run a test to record evidence'
          )
        }
        tooltip="Test passed means a real upstream call succeeded and the evidence row is stored. It is required before publishing."
      />
      <Chip
        icon={published ? <CheckCircle2 aria-hidden className="h-4 w-4" /> : <UploadCloud aria-hidden className="h-4 w-4" />}
        tone={published ? 'complete' : 'blocked'}
        label="Published"
        value={published ? `Version ${verification.published_version}` : 'Not published'}
        detail={verification.published_at ? <TimeAgo value={verification.published_at} /> : 'No immutable version is live'}
        tooltip="Published means an immutable version is live and can be routed to."
      />
      <Chip
        icon={callable ? <Radio aria-hidden className="h-4 w-4" /> : <CircleDashed aria-hidden className="h-4 w-4" />}
        tone={callable ? 'complete' : 'blocked'}
        label="Callable"
        value={callable ? 'External calls allowed' : 'Not callable'}
        detail={verification.callable_reason}
        tooltip="Callable means a published version, an active credential and a usable route all line up right now."
      />
    </div>
  );
}
