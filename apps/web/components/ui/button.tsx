'use client';

import { Slot, Slottable } from '@radix-ui/react-slot';
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
  // asChild 分支：Radix Slot 1.5 只接受两种形态 ——
  //   1) 恰好一个元素子节点；
  //   2) 用 <Slottable> 包住原始子节点，再配一个兄弟节点（Spinner / icon）。
  // 原来无条件写 `{loading ? <Spinner/> : icon}{children}`：asChild 下子节点是数组，
  // React.Children.count 把缺失的 undefined 也算成一个，Slot 直接抛
  // 「Slot failed to slot onto its children」，整页 client-side exception。
  // **注意**：不能用 <>…</> 包起来再给 Slot —— Fragment 本身会被 React.Children.count
  // 计为 1 且 isValidElement，Slot 会把 type/disabled 等 props 灌进 Fragment 并报
  // 「Invalid prop `type` supplied to React.Fragment」。必须保持两个并列兄弟节点。
  const decoration = loading ? <Spinner size={14} /> : icon;
  const commonProps = {
    ref,
    className: cn(base, variants[variant], sizes[size], className),
    'aria-busy': loading || undefined,
    ...props,
  } as const;

  if (asChild) {
    return (
      <Comp {...commonProps}>
        {decoration}
        <Slottable>{children}</Slottable>
      </Comp>
    );
  }
  return (
    <Comp {...commonProps} type={type ?? 'button'} disabled={isDisabled}>
      {decoration}
      {children}
    </Comp>
  );
});
