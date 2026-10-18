'use client';

import { cn } from './cn';

/** 加载指示（不阻塞布局，尺寸与文本基线对齐） */
export function Spinner({
  size = 16,
  className,
  label = '加载中',
}: {
  size?: number;
  className?: string;
  label?: string;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      className={cn('animate-spin', className)}
      role="status"
      aria-label={label}
    >
      <path d="M12 3a9 9 0 1 0 9 9" />
    </svg>
  );
}
