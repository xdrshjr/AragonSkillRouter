'use client';

import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from 'react';
import { Loader2 } from 'lucide-react';

import { cn } from '@/lib/utils';

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger';
export type ButtonSize = 'sm' | 'md';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Shows a spinner, sets `aria-busy` and disables the button. */
  loading?: boolean;
  iconLeft?: ReactNode;
  iconRight?: ReactNode;
  fullWidth?: boolean;
}

const VARIANTS: Record<ButtonVariant, string> = {
  primary:
    'bg-accent text-white border border-transparent hover:bg-accent-strong active:bg-accent-strong disabled:bg-accent/60',
  secondary:
    'bg-surface text-text border border-border-strong hover:bg-info-soft active:bg-info-soft disabled:text-muted',
  ghost:
    'bg-transparent text-text border border-transparent hover:bg-info-soft active:bg-info-soft disabled:text-muted',
  danger: 'bg-danger text-white border border-transparent hover:bg-danger/90 active:bg-danger/90 disabled:bg-danger/60',
};

const SIZES: Record<ButtonSize, string> = {
  sm: 'h-8 px-3 text-xs gap-1.5',
  md: 'h-10 px-4 text-sm gap-2',
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    variant = 'secondary',
    size = 'md',
    loading = false,
    iconLeft,
    iconRight,
    fullWidth = false,
    className,
    disabled,
    children,
    type = 'button',
    ...rest
  },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={cn(
        'inline-flex items-center justify-center rounded-control font-medium',
        'transition-colors duration-150',
        'disabled:cursor-not-allowed disabled:opacity-70',
        VARIANTS[variant],
        SIZES[size],
        fullWidth && 'w-full',
        className,
      )}
      {...rest}
    >
      {loading ? <Loader2 aria-hidden className="h-4 w-4 animate-spin" /> : iconLeft}
      {children}
      {!loading && iconRight}
    </button>
  );
});

export interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** Required: an icon-only control must announce itself. */
  label: string;
  size?: ButtonSize;
  variant?: ButtonVariant;
  children: ReactNode;
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, size = 'md', variant = 'ghost', className, children, type = 'button', ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      aria-label={label}
      title={label}
      className={cn(
        'inline-flex items-center justify-center rounded-control transition-colors duration-150',
        'disabled:cursor-not-allowed disabled:opacity-60',
        VARIANTS[variant],
        size === 'sm' ? 'h-8 w-8' : 'h-10 w-10',
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  );
});
