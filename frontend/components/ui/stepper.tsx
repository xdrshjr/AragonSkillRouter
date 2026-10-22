import type { ReactNode } from 'react';
import Link from 'next/link';
import { Check } from 'lucide-react';

import { cn } from '@/lib/utils';

export type StepStatus = 'complete' | 'current' | 'upcoming' | 'blocked';

export interface Step {
  id: string;
  label: string;
  hint?: ReactNode;
  status: StepStatus;
  href?: string;
}

export interface StepperProps {
  steps: Step[];
  label: string;
  className?: string;
  orientation?: 'vertical' | 'horizontal';
}

const DOT: Record<StepStatus, string> = {
  complete: 'border-success bg-success text-white',
  current: 'border-accent bg-accent-soft text-accent-strong',
  upcoming: 'border-border-strong bg-surface text-muted',
  blocked: 'border-danger bg-danger-soft text-danger',
};

/** Ordered progress list (onboarding checklist, publish pipeline). */
export function Stepper({ steps, label, className, orientation = 'vertical' }: StepperProps) {
  return (
    <ol
      aria-label={label}
      className={cn(
        orientation === 'vertical' ? 'flex flex-col gap-0' : 'flex flex-wrap items-start gap-4',
        className,
      )}
    >
      {steps.map((step, index) => (
        <li key={step.id} className={cn('flex gap-3', orientation === 'vertical' ? 'items-start' : 'items-center')}>
          <div className="flex flex-col items-center">
            <span
              aria-hidden
              className={cn(
                'flex h-6 w-6 shrink-0 items-center justify-center rounded-pill border text-xs font-medium',
                DOT[step.status],
              )}
            >
              {step.status === 'complete' ? <Check className="h-3.5 w-3.5" /> : index + 1}
            </span>
            {orientation === 'vertical' && index < steps.length - 1 ? (
              <span aria-hidden className="my-1 h-full min-h-4 w-px flex-1 bg-border" />
            ) : null}
          </div>
          <div className={cn('pb-4', orientation === 'horizontal' && 'pb-0')}>
            {step.href ? (
              <Link href={step.href} className="text-sm font-medium text-text hover:text-accent-strong">
                {step.label}
              </Link>
            ) : (
              <span className="text-sm font-medium text-text">{step.label}</span>
            )}
            <span className="sr-visually-hidden">
              {' '}
              — {step.status === 'complete' ? 'done' : step.status === 'current' ? 'current step' : step.status}
            </span>
            {step.hint ? <p className="mt-0.5 text-xs text-muted">{step.hint}</p> : null}
          </div>
        </li>
      ))}
    </ol>
  );
}
