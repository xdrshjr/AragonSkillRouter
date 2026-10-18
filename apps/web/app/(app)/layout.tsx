'use client';

import { useQuery } from '@tanstack/react-query';
import { usePathname, useRouter } from 'next/navigation';
import * as React from 'react';
import { AppShell } from '@/components/common/AppShell';
import { EmptyState } from '@/components/common/EmptyState';
import { AgentDock } from '@/components/agent/AgentDock';
import { AgentPanel } from '@/components/agent/AgentPanel';
import { AgentProvider } from '@/components/agent/useAgentSession';
import { Skeleton } from '@/components/ui/skeleton';
import { apiGet, isApiError } from '@/lib/api';
import { fetchMe } from '@/lib/auth';
import { queryKeys } from '@/lib/query';
import type { AgentApproval, Overview } from '@/lib/types';

/**
 * 应用外壳（spec §13.1）：侧栏 + 顶栏 + 浮窗 Agent 挂载点。
 * 认证与强制改密的守卫都在这里，避免每个页面重复实现。
 */
export default function AppLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname() ?? '/';

  const me = useQuery({
    queryKey: queryKeys.auth.me(),
    queryFn: fetchMe,
    retry: false,
    staleTime: 60_000,
  });

  // 未登录 → /login（带上回跳地址）
  React.useEffect(() => {
    if (me.isLoading) return;
    if (me.data === null || me.data === undefined) {
      if (me.isSuccess) {
        router.replace(`/login?next=${encodeURIComponent(pathname)}`);
      }
      return;
    }
    if (me.data.admin.must_change_password && !pathname.startsWith('/change-password')) {
      router.replace('/change-password');
    }
  }, [me.data, me.isLoading, me.isSuccess, pathname, router]);

  const overview = useQuery<Overview>({
    queryKey: queryKeys.overview(),
    queryFn: () => apiGet<Overview>('/admin/overview'),
    refetchInterval: 60_000,
    enabled: Boolean(me.data),
    staleTime: 30_000,
  });

  const approvals = useQuery<AgentApproval[]>({
    queryKey: queryKeys.agent.approvals(),
    queryFn: async () => {
      const result = await apiGet<{ items: AgentApproval[] } | AgentApproval[]>('/admin/agent/approvals');
      return Array.isArray(result) ? result : result.items;
    },
    enabled: Boolean(me.data),
    refetchInterval: 30_000,
  });

  if (me.isLoading) {
    return (
      <div className="flex min-h-screen bg-bg" aria-busy="true">
        <div className="hidden w-[240px] border-r border-border bg-surface p-3 md:block">
          <Skeleton height={20} width="60%" />
          <div className="mt-6 flex flex-col gap-2">
            {Array.from({ length: 9 }).map((_, index) => (
              <Skeleton key={index} height={28} />
            ))}
          </div>
        </div>
        <div className="flex-1 p-6">
          <Skeleton height={28} width="30%" />
          <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {Array.from({ length: 4 }).map((_, index) => (
              <Skeleton key={index} height={88} rounded="md" />
            ))}
          </div>
          <div className="mt-6">
            <Skeleton height={220} rounded="md" />
          </div>
        </div>
      </div>
    );
  }

  if (me.isError) {
    const info = isApiError(me.error)
      ? { message: me.error.message, requestId: me.error.requestId, code: me.error.code }
      : { message: '无法获取当前会话', requestId: null, code: 'UNKNOWN' };
    return (
      <main className="flex min-h-screen items-center justify-center bg-bg px-4">
        <div className="w-full max-w-[520px]">
          <EmptyState
            kind={info.code === 'NETWORK_OFFLINE' ? 'offline' : 'unavailable'}
            title={info.message}
            requestId={info.requestId}
            actionLabel="重试"
            onAction={() => void me.refetch()}
          />
        </div>
      </main>
    );
  }

  if (!me.data) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-bg px-4">
        <p className="text-13 text-content-muted" role="status">
          正在跳转到登录页…
        </p>
      </main>
    );
  }

  const pendingApprovals = (approvals.data ?? []).filter((approval) => approval.status === 'pending').length;

  return (
    <AgentProvider>
      <AppShell
        me={me.data}
        environment={me.data.env}
        engineStatus={me.data.engine.status}
        engineVersion={me.data.engine.version}
        counts={{
          drafts: overview.data?.capabilities.draft,
          failedTasks: overview.data?.failed_tasks,
          pendingApprovals,
        }}
      >
        {children}
      </AppShell>
      <AgentPanel />
      <AgentDock />
    </AgentProvider>
  );
}
