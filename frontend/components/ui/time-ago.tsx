'use client';

import { useEffect, useState } from 'react';

import { formatDateTime, formatRelativeTime } from '@/lib/format';

export interface TimeAgoProps {
  value: string | null | undefined;
  /** Rendered when the value is missing. */
  fallback?: string;
  className?: string;
}

/**
 * Relative time computed after mount only: the server render and the first
 * client paint both use the absolute UTC timestamp, so hydration is stable while
 * the reader still gets "4 minutes ago".
 */
export function TimeAgo({ value, fallback = '—', className }: TimeAgoProps) {
  const [relative, setRelative] = useState<string | null>(null);

  useEffect(() => {
    setRelative(formatRelativeTime(value));
    if (!value) return;
    const timer = window.setInterval(() => setRelative(formatRelativeTime(value)), 30_000);
    return () => window.clearInterval(timer);
  }, [value]);

  if (!value) return <span className={className}>{fallback}</span>;
  return (
    <time dateTime={value} title={formatDateTime(value)} className={className}>
      {relative ?? formatDateTime(value)}
    </time>
  );
}
