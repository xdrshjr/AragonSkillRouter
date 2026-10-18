'use client';

import { QueryClientProvider } from '@tanstack/react-query';
import * as React from 'react';
import { ToastProvider } from '@/components/ui/toast';
import { TooltipProvider } from '@/components/ui/tooltip';
import { makeQueryClient } from '@/lib/query';

/**
 * 全局 Provider：TanStack Query + Toast + Tooltip。
 * Agent 会话 Provider 在 (app)/layout 内（需要登录态，且要跨页面保持）。
 */
export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = React.useState(() => makeQueryClient());
  return (
    <QueryClientProvider client={client}>
      <TooltipProvider delayDuration={200}>
        <ToastProvider>{children}</ToastProvider>
      </TooltipProvider>
    </QueryClientProvider>
  );
}
