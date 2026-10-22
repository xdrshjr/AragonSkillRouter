import { cn } from '@/lib/utils';

export function Skeleton({ className }: { className?: string }) {
  return <div aria-hidden className={cn('animate-shimmer rounded-control bg-info-soft', className)} />;
}

/** Announces loading once for screen readers while the shapes carry the visual. */
export function SkeletonBlock({ rows = 3, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn('space-y-2', className)}>
      <span className="sr-visually-hidden" role="status">
        Loading…
      </span>
      {Array.from({ length: rows }).map((_, index) => (
        <Skeleton key={index} className={cn('h-4', index === rows - 1 ? 'w-2/3' : 'w-full')} />
      ))}
    </div>
  );
}

export function SkeletonTable({ rows = 5, columns = 4 }: { rows?: number; columns?: number }) {
  return (
    <div className="rounded-card border border-border bg-surface">
      <span className="sr-visually-hidden" role="status">
        Loading table…
      </span>
      <div className="flex gap-4 border-b border-border bg-info-soft px-3 py-2">
        {Array.from({ length: columns }).map((_, index) => (
          <Skeleton key={index} className="h-3 flex-1" />
        ))}
      </div>
      <div className="divide-y divide-border">
        {Array.from({ length: rows }).map((_, rowIndex) => (
          <div key={rowIndex} className="flex gap-4 px-3 py-3">
            {Array.from({ length: columns }).map((__, colIndex) => (
              <Skeleton key={colIndex} className={cn('h-4 flex-1', colIndex === 0 && 'max-w-[160px]')} />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

export function SkeletonStats({ count = 4 }: { count?: number }) {
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
      {Array.from({ length: count }).map((_, index) => (
        <div key={index} className="rounded-card border border-border bg-surface p-4">
          <Skeleton className="h-3 w-20" />
          <Skeleton className="mt-3 h-6 w-16" />
        </div>
      ))}
    </div>
  );
}
