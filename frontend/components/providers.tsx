'use client';

import type { ReactNode } from 'react';

import { SessionProvider } from '@/lib/auth';
import { NavigationGuardProvider } from '@/lib/navigation-guard';
import { QueryProvider } from '@/lib/query-provider';
import { ToastProvider } from '@/components/ui/toast';

/**
 * Single client boundary for the whole app: data cache, toast queue, session
 * context and the in-app unsaved-changes guard.
 */
export function AppProviders({ children }: { children: ReactNode }) {
  return (
    <QueryProvider>
      <ToastProvider>
        <SessionProvider>
          <NavigationGuardProvider>{children}</NavigationGuardProvider>
        </SessionProvider>
      </ToastProvider>
    </QueryProvider>
  );
}
