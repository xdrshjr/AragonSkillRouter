'use client';

import { Slot } from '@radix-ui/react-slot';
import * as React from 'react';
import { cn } from './cn';
import { Spinner } from './spinner';

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'link';
export type ButtonSize = 'sm' | 'md' | 'icon';

const base =
  'inline-flex items-center justify-center gap-2 whitespace-nowrap rounded font-medium ' +
  'transition-colors duration-150 ease-sr disabled:cursor-not-allowed disabled:opacity-55 ' +
  'focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]';

const variants: Record<ButtonVariant, string> = {
  primary:
    'bg-accent text-white border border-transparent hover:bg-[color-mix(in_srgb,var(--sr-accent)_88%,black)]',
  secondary:
    'bg-surface text-content border border-border hover:bg-hover hover:border-border-strong',
  ghost: 'bg-transparent text-content-muted border border-transparent hover:bg-hover hover:text-content',
  danger: 'bg-danger text-white border border-transparent hover:bg-[color-mix(in_srgb,var(--sr-danger)_88%,black)]',
  link: 'bg-transparent text-accent border border-transparent underline-offset-2 hover:underline p-0 h-auto',
};

const sizes: Record<ButtonSize, string> = {
  sm: 'h-8 px-3 text-13',
  md: 'h-9 px-4 text-14',
  icon: 'h-9 w-9 p-0',
};

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
  asChild?: boolean;
  icon?: React.ReactNode;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { className, variant = 'secondary', size = 'md', loading = false, asChild = false, icon, children, disabled, type, ...props },
  ref,
) {
  const Comp = asChild ? Slot : 'button';
  const isDisabled = disabled || loading;
  return (
    <Comp
      ref={ref}
      type={asChild ? undefined : (type ?? 'button')}
      className={cn(base, variants[variant], sizes[size], className)}
      aria-busy={loading || undefined}
      disabled={asChild ? undefined : isDisabled}
      {...props}
    >
      {loading ? <Spinner size={14} /> : icon}
      {children}
    </Comp>
  );
});
