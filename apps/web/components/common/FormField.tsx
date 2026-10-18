'use client';

import * as React from 'react';
import { Label } from '@/components/ui/label';
import { cn } from '@/components/ui/cn';

/**
 * 表单字段容器：label + 必填标记 + 就地错误 + 描述。
 * 与 react-hook-form 配合使用时，把 `error` 传 message 字符串。
 */
export interface FormFieldProps {
  id: string;
  label: string;
  required?: boolean;
  hint?: string;
  description?: string;
  error?: string;
  /** 由控件渲染 */
  children: React.ReactNode;
  className?: string;
  /** 右侧附加（如「生成」按钮） */
  aside?: React.ReactNode;
}

export function FormField({
  id,
  label,
  required,
  hint,
  description,
  error,
  children,
  className,
  aside,
}: FormFieldProps) {
  return (
    <div className={cn('flex flex-col gap-1.5', className)}>
      <div className="flex items-center justify-between gap-2">
        <Label htmlFor={id} required={required} hint={hint}>
          {label}
        </Label>
        {aside}
      </div>
      {description ? (
        <p id={`${id}-desc`} className="text-12 text-content-muted">
          {description}
        </p>
      ) : null}
      {children}
      <p
        id={`${id}-error`}
        role={error ? 'alert' : undefined}
        aria-live={error ? 'polite' : undefined}
        className={cn('min-h-[16px] text-12', error ? 'text-danger' : 'text-transparent')}
      >
        {error ?? '占位'}
      </p>
    </div>
  );
}
