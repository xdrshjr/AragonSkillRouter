'use client';

import * as React from 'react';
import { cn } from './cn';

export interface TextareaProps extends React.TextareaHTMLAttributes<HTMLTextAreaElement> {
  invalid?: boolean;
}

export const Textarea = React.forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea(
  { className, invalid, rows = 4, ...props },
  ref,
) {
  return (
    <textarea
      ref={ref}
      rows={rows}
      aria-invalid={invalid || undefined}
      className={cn(
        'w-full resize-y rounded border border-border bg-surface px-3 py-2 text-14 text-content',
        'placeholder:text-content-muted transition-colors duration-150 ease-sr',
        'hover:border-border-strong focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]',
        'disabled:cursor-not-allowed disabled:bg-[var(--sr-surface-sunken)]',
        'aria-[invalid=true]:border-danger',
        className,
      )}
      {...props}
    />
  );
});
