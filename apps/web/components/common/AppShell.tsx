'use client';

import * as DropdownMenu from '@radix-ui/react-dropdown-menu';
import {
  Activity,
  Bot,
  ChevronLeft,
  ChevronRight,
  CircleUser,
  ClipboardList,
  Cpu,
  KeyRound,
  KeySquare,
  LayoutDashboard,
  ListChecks,
  LogOut,
  Menu,
  Moon,
  Plug,
  Search,
  Settings,
  Sparkles,
  WifiOff,
  Wrench,
  X,
} from 'lucide-react';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import * as React from 'react';
import { Badge } from '@/components/ui/badge';
import { cn } from '@/components/ui/cn';
import { Dialog, DialogContent } from '@/components/ui/dialog';
import { Tooltip } from '@/components/ui/tooltip';
import { useToast } from '@/components/ui/toast';
import { logout } from '@/lib/auth';
import { MAINTENANCE_EVENT, OFFLINE_EVENT, fetchHealthz, isApiError } from '@/lib/api';
import type { EngineState, MeResponse } from '@/lib/types';

export interface NavCounts {
  drafts?: number;
  failedTasks?: number;
  pendingApprovals?: number;
}

interface NavItem {
  href: string;
  label: string;
  icon: React.ReactNode;
  countKey?: keyof NavCounts;
  /** 命中路径前缀 */
  match: (pathname: string) => boolean;
}

const ICON = { size: 24, strokeWidth: 1.75, 'aria-hidden': true } as const;

const NAV_ITEMS: NavItem[] = [
  { href: '/', label: '概览', icon: <LayoutDashboard {...ICON} />, match: (p) => p === '/' },
  {
    href: '/providers',
    label: '提供商',
    icon: <Plug {...ICON} />,
    match: (p) => p.startsWith('/providers'),
  },
  {
    // 顶层扁平列表，紧跟「提供商」（spec §A.3.11：本轮不引入导航分组）
    href: '/credentials',
    label: '凭证',
    icon: <KeySquare {...ICON} />,
    match: (p) => p === '/credentials' || p.startsWith('/credentials/'),
  },
  {
    href: '/capabilities',
    label: '能力',
    icon: <Wrench {...ICON} />,
    countKey: 'drafts',
    match: (p) => p.startsWith('/capabilities'),
  },
  {
    href: '/skills',
    label: 'SKILL',
    icon: <Sparkles {...ICON} />,
    match: (p) => p.startsWith('/skills'),
  },
  { href: '/agent', label: 'Agent', icon: <Bot {...ICON} />, match: (p) => p.startsWith('/agent') },
  {
    href: '/access',
    label: '接入与令牌',
    icon: <KeyRound {...ICON} />,
    match: (p) => p.startsWith('/access'),
  },
  {
    href: '/calls',
    label: '调用记录',
    icon: <Activity {...ICON} />,
    match: (p) => p.startsWith('/calls'),
  },
  {
    href: '/tasks',
    label: '任务',
    icon: <ListChecks {...ICON} />,
    countKey: 'failedTasks',
    match: (p) => p.startsWith('/tasks'),
  },
  {
    href: '/settings',
    label: '设置',
    icon: <Settings {...ICON} />,
    match: (p) => p.startsWith('/settings'),
  },
];

const SEARCH_TARGETS: { href: string; label: string; keywords: string }[] = [
  { href: '/', label: '概览', keywords: 'overview 首页 指标' },
  { href: '/providers', label: '提供商', keywords: 'provider 凭证 credential' },
  { href: '/credentials', label: '凭证列表', keywords: 'credential 凭证 过期 expiring 指纹' },
  { href: '/capabilities', label: '能力列表', keywords: 'capability 能力' },
  { href: '/capabilities/new', label: '新建能力', keywords: 'new capability 向导 openapi' },
  { href: '/skills', label: 'SKILL 列表', keywords: 'skill 技能' },
  { href: '/skills/new', label: '新建 / 导入 SKILL', keywords: 'import skill' },
  { href: '/agent', label: 'Agent 工作台', keywords: 'agent 会话 流程 审批' },
  { href: '/access', label: '接入与令牌', keywords: 'token agent 令牌' },
  { href: '/calls', label: '调用记录', keywords: 'calls 日志 request_id' },
  { href: '/tasks', label: '任务', keywords: 'tasks 异步 队列' },
  { href: '/settings', label: '设置', keywords: 'settings 预算 保留 审计' },
  { href: '/settings/engine', label: '引擎升级', keywords: 'engine 升级 回滚 候选' },
];

const ENGINE_TONE: Record<EngineState, { dot: string; label: string }> = {
  available: { dot: 'bg-ok', label: '引擎可用' },
  degraded: { dot: 'bg-warn', label: '引擎降级' },
  unavailable: { dot: 'bg-danger', label: '引擎不可用' },
  needs_adapter: { dot: 'bg-warn', label: '引擎待适配' },
  permission_limited: { dot: 'bg-info', label: '引擎受权限限制' },
  unsupported: { dot: 'bg-[var(--sr-text-muted)]', label: '引擎不支持' },
};

export interface AppShellProps {
  children: React.ReactNode;
  counts?: NavCounts;
  me?: MeResponse | null;
  environment?: string;
  engineStatus?: EngineState;
  engineVersion?: string | null;
  /** 浮窗 Agent 挂载点（由 (app)/layout 注入，保证跨路由不销毁） */
  agentSlot?: React.ReactNode;
}

const SIDEBAR_KEY = 'sr:sidebar-collapsed';

export function AppShell({
  children,
  counts,
  me,
  environment = 'production',
  engineStatus = 'unavailable',
  engineVersion,
  agentSlot,
}: AppShellProps) {
  const pathname = usePathname() ?? '/';
  const router = useRouter();
  const { toast } = useToast();

  const [collapsed, setCollapsed] = React.useState(false);
  const [mobileOpen, setMobileOpen] = React.useState(false);
  const [searchOpen, setSearchOpen] = React.useState(false);
  const [searchTerm, setSearchTerm] = React.useState('');
  const [online, setOnline] = React.useState(true);
  const [maintenance, setMaintenance] = React.useState(false);
  const [narrow, setNarrow] = React.useState(false);

  React.useEffect(() => {
    try {
      setCollapsed(window.localStorage.getItem(SIDEBAR_KEY) === '1');
    } catch {
      /* 隐私模式忽略 */
    }
    const onResize = (): void => setNarrow(window.innerWidth < 768);
    onResize();
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  React.useEffect(() => {
    const onOffline = (event: Event): void => {
      const detail = (event as CustomEvent<{ online: boolean }>).detail;
      setOnline(detail?.online ?? navigator.onLine);
    };
    const onMaintenance = (): void => setMaintenance(true);
    const onNativeOffline = (): void => setOnline(false);
    const onNativeOnline = (): void => setOnline(true);
    window.addEventListener(OFFLINE_EVENT, onOffline);
    window.addEventListener(MAINTENANCE_EVENT, onMaintenance);
    window.addEventListener('offline', onNativeOffline);
    window.addEventListener('online', onNativeOnline);
    return () => {
      window.removeEventListener(OFFLINE_EVENT, onOffline);
      window.removeEventListener(MAINTENANCE_EVENT, onMaintenance);
      window.removeEventListener('offline', onNativeOffline);
      window.removeEventListener('online', onNativeOnline);
    };
  }, []);

  /**
   * 维护横幅读 `/healthz.maintenance`（spec §A.3.13），而不是只靠「撞到一次 503」。
   * 首帧立刻拉一次，之后每 30s 轮询；`maintenance: false` 时横幅自动消失。
   * 503 事件（MAINTENANCE_EVENT）仍然立即点亮横幅：轮询有间隔，写操作失败更早可见。
   */
  React.useEffect(() => {
    const controller = new AbortController();
    let stopped = false;
    const poll = async (): Promise<void> => {
      const health = await fetchHealthz(controller.signal);
      if (stopped || !health) return;
      setMaintenance(health.maintenance);
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 30_000);
    return () => {
      stopped = true;
      controller.abort();
      window.clearInterval(timer);
    };
  }, []);

  // Ctrl/⌘+K 全局搜索
  React.useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      const isK = event.key.toLowerCase() === 'k';
      if (isK && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        setSearchOpen((v) => !v);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const toggleCollapsed = (): void => {
    setCollapsed((prev) => {
      const next = !prev;
      try {
        window.localStorage.setItem(SIDEBAR_KEY, next ? '1' : '0');
      } catch {
        /* ignore */
      }
      return next;
    });
  };

  const handleLogout = async (): Promise<void> => {
    try {
      await logout();
      router.replace('/login');
    } catch (error) {
      toast({
        title: '退出登录失败',
        description: isApiError(error) ? error.message : '请稍后重试',
        tone: 'danger',
      });
    }
  };

  const engine = ENGINE_TONE[engineStatus];
  const results = React.useMemo(() => {
    const term = searchTerm.trim().toLowerCase();
    if (!term) return SEARCH_TARGETS;
    return SEARCH_TARGETS.filter(
      (target) =>
        target.label.toLowerCase().includes(term) || target.keywords.toLowerCase().includes(term) ||
        target.href.toLowerCase().includes(term),
    );
  }, [searchTerm]);

  return (
    <div className="flex min-h-screen bg-bg">
      {/* 跳到主内容（键盘用户） */}
      <a
        href="#sr-main"
        className="sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-toast focus:rounded focus:border focus:border-border focus:bg-surface focus:px-3 focus:py-2 focus:text-13"
      >
        跳到主内容
      </a>

      {/* 侧边导航 */}
      <aside
        data-sr-chrome="true"
        aria-label="主导航"
        className={cn(
          'fixed bottom-0 left-0 top-0 z-panel flex flex-col border-r border-border bg-surface transition-[width] duration-150 ease-sr',
          collapsed ? 'w-[64px]' : 'w-[240px]',
          mobileOpen ? 'flex' : 'hidden md:flex',
        )}
      >
        <div className={cn('flex h-14 items-center gap-2 border-b border-border px-3')}>
          <span
            aria-hidden
            className="flex h-7 w-7 shrink-0 items-center justify-center rounded-sm border border-accent text-12 font-semibold text-accent"
          >
            SR
          </span>
          {!collapsed ? (
            <span className="min-w-0 truncate text-14 font-semibold text-content">Skill Router</span>
          ) : null}
          <button
            type="button"
            onClick={() => setMobileOpen(false)}
            aria-label="关闭导航"
            className="ml-auto rounded-sm p-1 text-content-muted hover:bg-hover focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)] md:hidden"
          >
            <X size={20} strokeWidth={1.75} aria-hidden />
          </button>
        </div>

        <nav aria-label="页面" className="flex-1 overflow-y-auto px-2 py-2 sr-scroll">
          <ul className="flex flex-col gap-0.5">
            {NAV_ITEMS.map((item) => {
              const active = item.match(pathname);
              const count = item.countKey ? counts?.[item.countKey] : undefined;
              const link = (
                <Link
                  href={item.href}
                  aria-current={active ? 'page' : undefined}
                  onClick={() => setMobileOpen(false)}
                  className={cn(
                    'flex items-center gap-2 rounded-sm px-2 py-2 text-13 transition-colors duration-150 ease-sr',
                    'focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]',
                    active
                      ? 'bg-[var(--sr-accent-weak)] text-accent'
                      : 'text-content-muted hover:bg-hover hover:text-content',
                    collapsed && 'justify-center px-0',
                  )}
                >
                  <span className={active ? 'text-accent' : 'text-content-muted'}>{item.icon}</span>
                  {!collapsed ? <span className="flex-1 truncate">{item.label}</span> : null}
                  {!collapsed && count ? (
                    <Badge tone={item.countKey === 'failedTasks' ? 'danger' : 'neutral'}>{count}</Badge>
                  ) : null}
                  {collapsed && count ? (
                    <span className="sr-only">（{count} 条待处理）</span>
                  ) : null}
                </Link>
              );
              return (
                <li key={item.href}>
                  {collapsed ? (
                    <Tooltip side="right" content={`${item.label}${count ? `（${count}）` : ''}`}>
                      <span className="block">{link}</span>
                    </Tooltip>
                  ) : (
                    link
                  )}
                </li>
              );
            })}
          </ul>
        </nav>

        <div className="border-t border-border p-2">
          <button
            type="button"
            onClick={toggleCollapsed}
            aria-expanded={!collapsed}
            aria-label={collapsed ? '展开导航' : '折叠导航'}
            className={cn(
              'flex w-full items-center gap-2 rounded-sm px-2 py-2 text-12 text-content-muted hover:bg-hover hover:text-content',
              'focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]',
              collapsed && 'justify-center px-0',
            )}
          >
            {collapsed ? (
              <ChevronRight size={20} strokeWidth={1.75} aria-hidden />
            ) : (
              <ChevronLeft size={20} strokeWidth={1.75} aria-hidden />
            )}
            {!collapsed ? '折叠导航' : null}
          </button>
        </div>
      </aside>

      {/* 主区 */}
      <div
        className={cn(
          'flex min-h-screen flex-1 flex-col transition-[padding] duration-150 ease-sr',
          collapsed ? 'md:pl-[64px]' : 'md:pl-[240px]',
        )}
      >
        <header
          data-sr-chrome="true"
          className="sticky top-0 z-dock flex h-14 items-center gap-3 border-b border-border bg-surface px-3 md:px-4"
        >
          <button
            type="button"
            onClick={() => setMobileOpen(true)}
            aria-label="打开导航"
            className="rounded-sm p-1.5 text-content-muted hover:bg-hover focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)] md:hidden"
          >
            <Menu size={20} strokeWidth={1.75} aria-hidden />
          </button>

          <Badge tone={environment === 'production' ? 'danger' : 'warn'} dot aria-label={`环境 ${environment}`}>
            {environment}
          </Badge>

          <Link
            href="/settings/engine"
            className="inline-flex items-center gap-1.5 rounded-sm px-1.5 py-1 text-12 text-content-muted hover:bg-hover hover:text-content focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
            aria-label={`${engine.label}${engineVersion ? `（${engineVersion}）` : ''}，前往引擎设置`}
          >
            <span aria-hidden className={cn('h-2 w-2 rounded-full', engine.dot)} />
            <Cpu size={16} strokeWidth={1.75} aria-hidden />
            <span className="hidden sm:inline">{engineVersion ?? engine.label}</span>
          </Link>

          <button
            type="button"
            onClick={() => setSearchOpen(true)}
            className={cn(
              'ml-auto inline-flex items-center gap-2 rounded border border-border bg-surface px-2 py-1.5 text-13 text-content-muted',
              'hover:bg-hover hover:text-content focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]',
            )}
            aria-label="全局搜索（Ctrl+K）"
          >
            <Search size={16} strokeWidth={1.75} aria-hidden />
            <span className="hidden sm:inline">搜索</span>
            <span className="sr-kbd hidden sm:inline-flex">Ctrl K</span>
          </button>

          <DropdownMenu.Root>
            <DropdownMenu.Trigger asChild>
              <button
                type="button"
                aria-label="管理员菜单"
                className="inline-flex items-center gap-1.5 rounded-sm border border-border px-2 py-1.5 text-13 text-content hover:bg-hover focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
              >
                <CircleUser size={16} strokeWidth={1.75} aria-hidden />
                <span className="hidden max-w-[120px] truncate lg:inline">
                  {me?.admin.username ?? '管理员'}
                </span>
              </button>
            </DropdownMenu.Trigger>
            <DropdownMenu.Portal>
              <DropdownMenu.Content
                align="end"
                sideOffset={6}
                className="z-toast min-w-[200px] rounded border border-border bg-surface p-1"
              >
                <DropdownMenu.Label className="px-2 py-1.5 text-12 text-content-muted">
                  {me?.admin.username ?? '管理员'}
                  {me?.session?.expires_at ? (
                    <span className="block">会话到期：{me.session.expires_at.slice(0, 16).replace('T', ' ')}</span>
                  ) : null}
                </DropdownMenu.Label>
                <DropdownMenu.Separator className="my-1 h-px bg-border" />
                <DropdownMenu.Item asChild>
                  <Link
                    href="/change-password"
                    className="flex cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-13 text-content outline-none data-[highlighted]:bg-hover"
                  >
                    <KeyRound size={16} strokeWidth={1.75} aria-hidden />
                    修改密码
                  </Link>
                </DropdownMenu.Item>
                <DropdownMenu.Item asChild>
                  <Link
                    href="/settings/engine"
                    className="flex cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-13 text-content outline-none data-[highlighted]:bg-hover"
                  >
                    <ClipboardList size={16} strokeWidth={1.75} aria-hidden />
                    引擎与升级记录
                  </Link>
                </DropdownMenu.Item>
                <DropdownMenu.Item
                  onSelect={() => {
                    const root = document.documentElement;
                    const next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
                    root.setAttribute('data-theme', next);
                    try {
                      window.localStorage.setItem('sr:theme', next);
                    } catch {
                      /* ignore */
                    }
                  }}
                  className="flex cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-13 text-content outline-none data-[highlighted]:bg-hover"
                >
                  <Moon size={16} strokeWidth={1.75} aria-hidden />
                  切换深色 / 浅色
                </DropdownMenu.Item>
                <DropdownMenu.Item
                  onSelect={() => {
                    void handleLogout();
                  }}
                  className="flex cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-13 text-danger outline-none data-[highlighted]:bg-hover"
                >
                  <LogOut size={16} strokeWidth={1.75} aria-hidden />
                  退出登录
                </DropdownMenu.Item>
              </DropdownMenu.Content>
            </DropdownMenu.Portal>
          </DropdownMenu.Root>
        </header>

        {/* 横幅：离线 / 维护 / 窄窗 */}
        {!online ? (
          <div
            role="status"
            className="flex items-center gap-2 border-b border-border bg-[var(--sr-warn-weak)] px-4 py-2 text-13 text-content"
          >
            <WifiOff size={16} strokeWidth={1.75} className="text-warn" aria-hidden />
            连接已断开，正在重试…
          </div>
        ) : null}
        {maintenance ? (
          <div
            role="alert"
            className="flex items-center gap-2 border-b border-border bg-[var(--sr-danger-weak)] px-4 py-2 text-13 text-content"
          >
            <span aria-hidden className="h-1.5 w-1.5 shrink-0 rounded-full bg-danger" />
            维护中：正在恢复备份，请稍候（写操作已暂停）
          </div>
        ) : null}
        {narrow ? (
          <div className="border-b border-border bg-[var(--sr-surface-sunken)] px-4 py-1.5 text-12 text-content-muted">
            建议使用更宽窗口（≥768px）以获得完整表格视图；当前布局功能仍可用。
          </div>
        ) : null}

        <main id="sr-main" className="flex-1 px-4 py-4 pb-24 md:px-6 md:pb-8" style={{ paddingRight: 'var(--sr-content-safe-right)' }}>
          <div className="mx-auto w-full max-w-content">{children}</div>
        </main>
      </div>

      {agentSlot}

      {/* 全局搜索 */}
      <Dialog open={searchOpen} onOpenChange={setSearchOpen}>
        <DialogContent title="全局搜索" description="跳转到页面或功能；支持关键字。" width="sm">
          <div className="flex flex-col gap-2">
            <input
              autoFocus
              value={searchTerm}
              onChange={(event) => setSearchTerm(event.target.value)}
              placeholder="输入页面名称或关键字，例如「令牌」「引擎」"
              aria-label="搜索关键字"
              className="h-9 w-full rounded border border-border bg-surface px-3 text-14 focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
            />
            <ul className="max-h-72 divide-y divide-[var(--sr-border)] overflow-y-auto sr-scroll">
              {results.map((target) => (
                <li key={target.href}>
                  <button
                    type="button"
                    onClick={() => {
                      setSearchOpen(false);
                      setSearchTerm('');
                      router.push(target.href);
                    }}
                    className="flex w-full items-center justify-between gap-2 px-2 py-2 text-left text-13 text-content hover:bg-hover focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
                  >
                    <span>{target.label}</span>
                    <code className="sr-mono text-12 text-content-muted">{target.href}</code>
                  </button>
                </li>
              ))}
              {results.length === 0 ? (
                <li className="px-2 py-3 text-13 text-content-muted">没有匹配的页面</li>
              ) : null}
            </ul>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
