import * as React from 'react';
import { cn } from './cn';

export interface SkeletonProps extends React.HTMLAttributes<HTMLDivElement> {
  width?: string | number;
  height?: string | number;
  lines?: number;
  rounded?: 'sm' | 'md' | 'full';
}

/** 骨架屏：尺寸与真实布局一致，避免加载完成后跳动。 */
export function Skeleton({ className, width, height = 16, lines = 1, rounded = 'sm', style, ...props }: SkeletonProps) {
  const radius = rounded === 'full' ? '999px' : rounded === 'md' ? 'var(--sr-radius)' : 'var(--sr-radius-sm)';
  if (lines > 1) {
    return (
      <div className={cn('flex flex-col gap-2', className)} aria-hidden {...props}>
        {Array.from({ length: lines }).map((_, index) => (
          <div
            key={index}
            className="sr-skeleton"
            style={{
              width: index === lines - 1 ? '60%' : (width ?? '100%'),
              height,
              borderRadius: radius,
            }}
          />
        ))}
      </div>
    );
  }
  return (
    <div
      aria-hidden
      className={cn('sr-skeleton', className)}
      style={{ width: width ?? '100%', height, borderRadius: radius, ...style }}
      {...props}
    />
  );
}

/** 表格骨架：与 DataTable 行高一致 */
export function TableSkeleton({ rows = 6, cols = 5 }: { rows?: number; cols?: number }) {
  return (
    <div className="w-full" aria-hidden>
      <div className="border-b border-border px-3 py-2">
        <Skeleton width="30%" height={12} />
      </div>
      {Array.from({ length: rows }).map((_, rowIndex) => (
        <div key={rowIndex} className="flex items-center gap-4 border-b border-border px-3 py-3">
          {Array.from({ length: cols }).map((__, colIndex) => (
            <Skeleton
              key={colIndex}
              width={colIndex === 0 ? '22%' : `${Math.max(8, 60 / cols)}%`}
              height={12}
            />
          ))}
        </div>
      ))}
    </div>
  );
}
