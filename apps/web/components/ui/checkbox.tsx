'use client';

import * as CheckboxPrimitive from '@radix-ui/react-checkbox';
import { Check, Minus } from 'lucide-react';
import * as React from 'react';
import { cn } from './cn';

export interface CheckboxProps {
  id?: string;
  checked: boolean | 'indeterminate';
  onCheckedChange: (checked: boolean | 'indeterminate') => void;
  disabled?: boolean;
  label: string;
  hideLabel?: boolean;
  description?: string;
  className?: string;
}

export function Checkbox({
  id,
  checked,
  onCheckedChange,
  disabled,
  label,
  hideLabel,
  description,
  className,
}: CheckboxProps) {
  return (
    <div className={cn('flex items-start gap-2', className)}>
      <CheckboxPrimitive.Root
        id={id}
        checked={checked}
        onCheckedChange={onCheckedChange}
        disabled={disabled}
        aria-label={hideLabel ? label : undefined}
        className={cn(
          'mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-sm border border-border-strong bg-surface',
          'transition-colors duration-150 ease-sr hover:border-accent',
          'data-[state=checked]:border-accent data-[state=checked]:bg-accent data-[state=checked]:text-white',
          'data-[state=indeterminate]:border-accent data-[state=indeterminate]:bg-accent data-[state=indeterminate]:text-white',
          'focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)] disabled:opacity-55',
        )}
      >
        <CheckboxPrimitive.Indicator>
          {checked === 'indeterminate' ? (
            <Minus size={12} strokeWidth={2.5} aria-hidden />
          ) : (
            <Check size={12} strokeWidth={2.5} aria-hidden />
          )}
        </CheckboxPrimitive.Indicator>
      </CheckboxPrimitive.Root>
      {!hideLabel ? (
        <div className="min-w-0">
          <label htmlFor={id} className="text-13 text-content">
            {label}
          </label>
          {description ? <p className="text-12 text-content-muted">{description}</p> : null}
        </div>
      ) : null}
    </div>
  );
}
