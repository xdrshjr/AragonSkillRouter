'use client';

import type { ReactNode } from 'react';

import { AgentDock } from '@/components/agent/agent-dock';
import { AgentProvider } from '@/components/agent/agent-context';
import { AppShell } from '@/components/shell/app-shell';

/**
 * Route group layout for every authenticated page.
 *
 * `AgentProvider` wraps the shell so the floating panel keeps its draft, active
 * run and scroll position across client-side navigation. The trailing spacer
 * guarantees the 56px dock button never sits on top of the last control of a
 * page (the dock is also raised above any sticky footer on narrow screens).
 */
export default function DashboardLayout({ children }: { children: ReactNode }) {
  return (
    <AgentProvider>
      <AppShell>
        {children}
        <div aria-hidden className="h-20 w-full" />
      </AppShell>
      <AgentDock />
    </AgentProvider>
  );
}
