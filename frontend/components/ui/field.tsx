'use client';

import { forwardRef, useId, type HTMLAttributes, type ReactNode } from 'react';
import { AlertCircle } from 'lucide-react';

import { cn } from '@/lib/utils';

export interface FieldProps extends Omit<HTMLAttributes<HTMLDivElement>, 'children'> {
  label: ReactNode;
  /** Rendered under the control when there is no error. */
  hint?: ReactNode;
  /** Inline validation error; replaces the hint and sets aria-invalid on the control. */
  error?: string | null;
  required?: boolean;
  htmlFor?: string;
  /** Extra element on the right of the label row (e.g. a "write-only" badge). */
  labelAccessory?: ReactNode;
  children: ReactNode;
}

/**
 * Label + hint + inline error wrapper. `errorId`/`hintId` are deterministic from
 * `htmlFor` so callers can point `aria-describedby` at them via `fieldAria()`.
 */
export function fieldIds(htmlFor: string, hasHint: boolean, hasError: boolean) {
  return {
    hintId: hasHint ? `${htmlFor}-hint` : undefined,
    errorId: hasError ? `${htmlFor}-error` : undefined,
    describedBy: [hasHint ? `${htmlFor}-hint` : null, hasError ? `${htmlFor}-error` : null].filter(Boolean).join(' ') || undefined,
  };
}

export const Field = forwardRef<HTMLDivElement, FieldProps>(function Field(
  { label, hint, error, required, htmlFor, labelAccessory, children, className, ...rest },
  ref,
) {
  const generatedId = useId();
  const target = htmlFor ?? generatedId;
  const { hintId, errorId } = fieldIds(target, Boolean(hint), Boolean(error));
  return (
    <div ref={ref} className={cn('flex flex-col gap-1.5', className)} {...rest}>
      <div className="flex items-center justify-between gap-2">
        <label htmlFor={target} className="text-xs font-medium text-text">
          {label}
          {required ? (
            <span aria-hidden className="ml-0.5 text-danger">
              *
            </span>
          ) : null}
          {required ? <span className="sr-visually-hidden"> (required)</span> : null}
        </label>
        {labelAccessory}
      </div>
      {children}
      {error ? (
        <p id={errorId} role="alert" className="flex items-start gap-1 text-xs text-danger">
          <AlertCircle aria-hidden className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>{error}</span>
        </p>
      ) : hint ? (
        <p id={hintId} className="text-xs text-muted">
          {hint}
        </p>
      ) : null}
    </div>
  );
});

export interface FormRowProps extends HTMLAttributes<HTMLDivElement> {
  columns?: 1 | 2 | 3;
}

/** Grid helper so form fields line up on the 8px grid. */
export function FormRow({ columns = 2, className, children, ...rest }: FormRowProps) {
  return (
    <div
      className={cn(
        'grid gap-4',
        columns === 1 && 'grid-cols-1',
        columns === 2 && 'grid-cols-1 sm:grid-cols-2',
        columns === 3 && 'grid-cols-1 sm:grid-cols-3',
        className,
      )}
      {...rest}
    >
      {children}
    </div>
  );
}

export function FormSection({
  title,
  description,
  children,
  className,
}: {
  title: string;
  description?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={cn('flex flex-col gap-4 border-t border-border pt-4 first:border-t-0 first:pt-0', className)}>
      <div>
        <h3 className="text-sm font-semibold text-text">{title}</h3>
        {description ? <p className="mt-0.5 text-xs text-muted">{description}</p> : null}
      </div>
      {children}
    </section>
  );
}
