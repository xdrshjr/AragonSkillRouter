'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import {
  Boxes,
  History,
  KeyRound,
  LayoutDashboard,
  Package,
  Server,
  Settings,
  type LucideIcon,
} from 'lucide-react';

import { cn } from '@/lib/utils';
import { useGuardedRouter } from '@/lib/navigation-guard';

export interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
  description: string;
  /** Routes rendered by another owner land here; still linkable, no fake page. */
  exact?: boolean;
}

export const NAV_ITEMS: NavItem[] = [
  { href: '/', label: 'Overview', icon: LayoutDashboard, description: 'Health, problems, recent calls', exact: true },
  { href: '/providers', label: 'Providers', icon: Server, description: 'Upstream services and credentials' },
  { href: '/capabilities', label: 'Capabilities', icon: Boxes, description: 'Config, tests, publishing, routes' },
  { href: '/skills', label: 'SKILLs', icon: Package, description: 'Composed plans for agents' },
  { href: '/access', label: 'Access', icon: KeyRound, description: 'Agents, tokens and budgets' },
  { href: '/records', label: 'Records', icon: History, description: 'Invocations and async tasks' },
  { href: '/settings', label: 'Settings', icon: Settings, description: 'Runtime, logging and engine' },
];

export function isActivePath(pathname: string, item: NavItem): boolean {
  if (item.exact) return pathname === item.href;
  return pathname === item.href || pathname.startsWith(`${item.href}/`);
}

export function navSectionFor(pathname: string): { title: string; description: string } {
  const match = NAV_ITEMS.filter((item) => isActivePath(pathname, item)).sort(
    (a, b) => b.href.length - a.href.length,
  )[0];
  if (match) return { title: match.label, description: match.description };
  return { title: 'Skill Router', description: 'Admin console' };
}

export function NavList({ onNavigate }: { onNavigate: (href: string) => void }) {
  const pathname = usePathname() ?? '/';
  const router = useGuardedRouter();

  return (
    <nav aria-label="Main navigation" className="flex-1 overflow-y-auto p-2">
      <ul className="flex flex-col gap-0.5">
        {NAV_ITEMS.map((item) => {
          const active = isActivePath(pathname, item);
          const Icon = item.icon;
          return (
            <li key={item.href}>
              <Link
                href={item.href}
                aria-current={active ? 'page' : undefined}
                onClick={(event) => {
                  // Route changes go through the unsaved-changes guard.
                  event.preventDefault();
                  onNavigate(item.href);
                  router.push(item.href);
                }}
                className={cn(
                  'flex items-center gap-2 rounded-control px-2 py-2 text-sm transition-colors duration-150 hover:no-underline',
                  active
                    ? 'bg-accent-soft font-medium text-accent-strong'
                    : 'text-text hover:bg-info-soft',
                )}
              >
                <Icon aria-hidden className={cn('h-4 w-4', active ? 'text-accent' : 'text-muted')} />
                <span className="truncate">{item.label}</span>
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
