'use client';

import { forwardRef, useId, type InputHTMLAttributes, type ReactNode } from 'react';

import { cn } from '@/lib/utils';

export interface CheckboxProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'type'> {
  label: ReactNode;
  hint?: string;
}

export const Checkbox = forwardRef<HTMLInputElement, CheckboxProps>(function Checkbox(
  { label, hint, className, id, ...rest },
  ref,
) {
  const generatedId = useId();
  const inputId = id ?? generatedId;
  const hintId = hint ? `${inputId}-hint` : undefined;
  return (
    <div className={cn('flex items-start gap-2', className)}>
      <input
        ref={ref}
        id={inputId}
        type="checkbox"
        aria-describedby={hintId}
        className="mt-0.5 h-4 w-4 shrink-0 cursor-pointer rounded border-border-strong text-accent accent-accent disabled:cursor-not-allowed"
        {...rest}
      />
      <label htmlFor={inputId} className="cursor-pointer select-none text-sm text-text">
        {label}
        {hint ? (
          <span id={hintId} className="mt-0.5 block text-xs text-muted">
            {hint}
          </span>
        ) : null}
      </label>
    </div>
  );
});

export interface SwitchProps {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  label: ReactNode;
  hint?: string;
  disabled?: boolean;
  id?: string;
  className?: string;
}

/** Accessible switch built on a native button with `role="switch"`. */
export function Switch({ checked, onCheckedChange, label, hint, disabled, id, className }: SwitchProps) {
  const generatedId = useId();
  const buttonId = id ?? generatedId;
  const hintId = hint ? `${buttonId}-hint` : undefined;
  return (
    <div className={cn('flex items-start gap-3', className)}>
      <button
        id={buttonId}
        type="button"
        role="switch"
        aria-checked={checked}
        aria-describedby={hintId}
        disabled={disabled}
        onClick={() => onCheckedChange(!checked)}
        className={cn(
          'relative mt-0.5 inline-flex h-5 w-9 shrink-0 items-center rounded-pill border transition-colors duration-150',
          checked ? 'border-accent bg-accent' : 'border-border-strong bg-surface',
          disabled && 'cursor-not-allowed opacity-60',
        )}
      >
        <span
          aria-hidden
          className={cn(
            'inline-block h-3.5 w-3.5 transform rounded-pill bg-white shadow-sm transition-transform duration-150',
            checked ? 'translate-x-[18px]' : 'translate-x-[3px]',
            !checked && 'border border-border-strong',
          )}
        />
      </button>
      <div className="text-sm">
        <label htmlFor={buttonId} className="cursor-pointer select-none font-medium text-text">
          {label}
        </label>
        {hint ? (
          <span id={hintId} className="mt-0.5 block text-xs text-muted">
            {hint}
          </span>
        ) : null}
      </div>
    </div>
  );
}
