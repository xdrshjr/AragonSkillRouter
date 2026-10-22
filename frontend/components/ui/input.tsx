'use client';

import { forwardRef, type InputHTMLAttributes, type TextareaHTMLAttributes } from 'react';

import { cn } from '@/lib/utils';

const BASE =
  'w-full rounded-control border border-border-strong bg-surface px-3 text-sm text-text placeholder:text-muted ' +
  'transition-colors duration-150 hover:border-muted/60 ' +
  'disabled:cursor-not-allowed disabled:bg-info-soft disabled:text-muted ' +
  'aria-[invalid=true]:border-danger';

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  invalid?: boolean;
  /** Optional leading icon slot (kept out of the tab order). */
  iconLeft?: React.ReactNode;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { className, invalid, iconLeft, ...rest },
  ref,
) {
  const input = (
    <input
      ref={ref}
      aria-invalid={invalid || undefined}
      className={cn(BASE, 'h-10', iconLeft && 'pl-9', className)}
      {...rest}
    />
  );
  if (!iconLeft) return input;
  return (
    <div className="relative">
      <span aria-hidden className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted">
        {iconLeft}
      </span>
      {input}
    </div>
  );
});

export interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  invalid?: boolean;
  monospace?: boolean;
}

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea(
  { className, invalid, monospace, rows = 3, ...rest },
  ref,
) {
  return (
    <textarea
      ref={ref}
      rows={rows}
      aria-invalid={invalid || undefined}
      className={cn(BASE, 'py-2 leading-5', monospace && 'font-mono text-xs leading-5', className)}
      {...rest}
    />
  );
});
