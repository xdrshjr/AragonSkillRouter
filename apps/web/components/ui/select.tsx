'use client';

import * as SelectPrimitive from '@radix-ui/react-select';
import { Check, ChevronDown } from 'lucide-react';
import * as React from 'react';
import { cn } from './cn';

export interface SelectOption {
  value: string;
  label: string;
  disabled?: boolean;
  hint?: string;
}

export interface SelectProps {
  id?: string;
  name?: string;
  value: string | undefined;
  onValueChange: (value: string) => void;
  options: SelectOption[];
  placeholder?: string;
  disabled?: boolean;
  invalid?: boolean;
  className?: string;
  'aria-label'?: string;
  'aria-describedby'?: string;
  size?: 'sm' | 'md';
}

export function Select({
  id,
  name,
  value,
  onValueChange,
  options,
  placeholder = '请选择',
  disabled,
  invalid,
  className,
  size = 'md',
  ...aria
}: SelectProps) {
  return (
    <SelectPrimitive.Root value={value} onValueChange={onValueChange} disabled={disabled} name={name}>
      <SelectPrimitive.Trigger
        id={id}
        aria-label={aria['aria-label']}
        aria-describedby={aria['aria-describedby']}
        aria-invalid={invalid || undefined}
        className={cn(
          'flex w-full items-center justify-between gap-2 rounded border border-border bg-surface px-3 text-14 text-content',
          'transition-colors duration-150 ease-sr hover:border-border-strong',
          'focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]',
          'data-[placeholder]:text-content-muted disabled:cursor-not-allowed disabled:opacity-55',
          invalid && 'border-danger',
          size === 'sm' ? 'h-8 text-13' : 'h-9',
          className,
        )}
      >
        <SelectPrimitive.Value placeholder={placeholder} />
        <SelectPrimitive.Icon>
          <ChevronDown size={16} strokeWidth={1.75} aria-hidden />
        </SelectPrimitive.Icon>
      </SelectPrimitive.Trigger>
      <SelectPrimitive.Portal>
        <SelectPrimitive.Content
          position="popper"
          sideOffset={4}
          className="z-[var(--sr-z,50)] max-h-72 min-w-[var(--radix-select-trigger-width)] overflow-hidden rounded border border-border bg-surface"
        >
          <SelectPrimitive.Viewport className="p-1">
            {options.map((option) => (
              <SelectPrimitive.Item
                key={option.value}
                value={option.value}
                disabled={option.disabled}
                className={cn(
                  'flex cursor-default select-none items-center justify-between gap-3 rounded-sm px-2 py-1.5 text-14 text-content outline-none',
                  'data-[highlighted]:bg-hover data-[state=checked]:text-accent',
                  'data-[disabled]:pointer-events-none data-[disabled]:opacity-50',
                )}
              >
                <SelectPrimitive.ItemText>{option.label}</SelectPrimitive.ItemText>
                {option.hint ? (
                  <span className="text-12 text-content-muted">{option.hint}</span>
                ) : null}
                <SelectPrimitive.ItemIndicator>
                  <Check size={16} strokeWidth={1.75} aria-hidden />
                </SelectPrimitive.ItemIndicator>
              </SelectPrimitive.Item>
            ))}
            {options.length === 0 ? (
              <div className="px-2 py-1.5 text-13 text-content-muted">无可选项</div>
            ) : null}
          </SelectPrimitive.Viewport>
        </SelectPrimitive.Content>
      </SelectPrimitive.Portal>
    </SelectPrimitive.Root>
  );
}
