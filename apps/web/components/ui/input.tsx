'use client';

import * as React from 'react';
import { cn } from './cn';

const fieldBase =
  'w-full rounded border border-border bg-surface px-3 text-14 text-content ' +
  'placeholder:text-content-muted transition-colors duration-150 ease-sr ' +
  'hover:border-border-strong focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)] ' +
  'disabled:cursor-not-allowed disabled:bg-[var(--sr-surface-sunken)] disabled:text-content-muted ' +
  'aria-[invalid=true]:border-danger';

export interface InputProps extends Omit<React.InputHTMLAttributes<HTMLInputElement>, 'size'> {
  invalid?: boolean;
  leading?: React.ReactNode;
  trailing?: React.ReactNode;
  inputSize?: 'sm' | 'md';
}

export const Input = React.forwardRef<HTMLInputElement, InputProps>(function Input(
  { className, invalid, leading, trailing, inputSize = 'md', ...props },
  ref,
) {
  const height = inputSize === 'sm' ? 'h-8' : 'h-9';
  if (!leading && !trailing) {
    return (
      <input
        ref={ref}
        aria-invalid={invalid || undefined}
        className={cn(fieldBase, height, className)}
        {...props}
      />
    );
  }
  return (
    <span
      className={cn(
        'flex items-center gap-2 rounded border border-border bg-surface px-3',
        'transition-colors duration-150 ease-sr hover:border-border-strong',
        'focus-within:shadow-[var(--sr-focus-ring)]',
        invalid && 'border-danger',
        height,
        className,
      )}
    >
      {leading ? <span className="text-content-muted">{leading}</span> : null}
      <input
        ref={ref}
        aria-invalid={invalid || undefined}
        className="min-w-0 flex-1 border-0 bg-transparent p-0 text-14 text-content outline-none placeholder:text-content-muted"
        {...props}
      />
      {trailing ? <span className="text-content-muted">{trailing}</span> : null}
    </span>
  );
});
