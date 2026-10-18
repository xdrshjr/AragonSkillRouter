'use client';

import * as SwitchPrimitive from '@radix-ui/react-switch';
import * as React from 'react';
import { cn } from './cn';

export interface SwitchProps {
  id?: string;
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  disabled?: boolean;
  label: string;
  /** 说明停用/启用后会发生什么（读屏可见） */
  description?: string;
  className?: string;
}

export function Switch({ id, checked, onCheckedChange, disabled, label, description, className }: SwitchProps) {
  const describedBy = description ? `${id ?? 'switch'}-desc` : undefined;
  return (
    <div className={cn('flex items-start gap-2', className)}>
      <SwitchPrimitive.Root
        id={id}
        checked={checked}
        onCheckedChange={onCheckedChange}
        disabled={disabled}
        aria-label={label}
        aria-describedby={describedBy}
        className={cn(
          'relative h-5 w-9 shrink-0 rounded-full border border-border transition-colors duration-150 ease-sr',
          'data-[state=checked]:border-accent data-[state=checked]:bg-accent data-[state=unchecked]:bg-[var(--sr-surface-sunken)]',
          'focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)] disabled:opacity-55',
        )}
      >
        <SwitchPrimitive.Thumb
          className={cn(
            'block h-3.5 w-3.5 translate-x-0.5 rounded-full bg-surface transition-transform duration-150 ease-sr',
            'data-[state=checked]:translate-x-[18px]',
            'border border-border data-[state=checked]:border-transparent',
          )}
        />
      </SwitchPrimitive.Root>
      {description ? (
        <p id={describedBy} className="text-12 text-content-muted">
          {description}
        </p>
      ) : null}
    </div>
  );
}
