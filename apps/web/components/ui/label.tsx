'use client';

import * as LabelPrimitive from '@radix-ui/react-label';
import * as React from 'react';
import { cn } from './cn';

export interface LabelProps extends React.LabelHTMLAttributes<HTMLLabelElement> {
  required?: boolean;
  hint?: string;
}

export function Label({ className, required, hint, children, ...props }: LabelProps) {
  return (
    <LabelPrimitive.Root
      className={cn('flex items-center gap-1 text-13 font-medium text-content', className)}
      {...props}
    >
      {children}
      {required ? (
        <span className="text-danger" aria-hidden>
          *
        </span>
      ) : null}
      {required ? <span className="sr-only">必填</span> : null}
      {hint ? <span className="text-12 font-normal text-content-muted">{hint}</span> : null}
    </LabelPrimitive.Root>
  );
}
