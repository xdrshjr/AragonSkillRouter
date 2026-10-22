'use client';

import { createContext, useCallback, useContext, useEffect, useMemo, type ReactNode } from 'react';

import { setCsrfFallback } from '@/lib/api';
import { useSessionQuery } from '@/lib/queries';
import type { OnboardingSteps, SessionPayload, SessionUser } from '@/lib/types';

export type AuthStatus = 'loading' | 'authenticated' | 'anonymous' | 'error';

export interface AuthContextValue {
  status: AuthStatus;
  session: SessionPayload | null;
  user: SessionUser | null;
  csrfToken: string | null;
  onboarding: OnboardingSteps | null;
  mustChangePassword: boolean;
  error: Error | null;
  refresh: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export const ONBOARDING_LABELS: Array<{ key: keyof OnboardingSteps; label: string; hint: string; href: string }> = [
  {
    key: 'agent_model_configured',
    label: 'Agent model configured',
    hint: 'Point the assistant at a model so agent steps can run.',
    href: '/settings',
  },
  {
    key: 'first_capability_published',
    label: 'First capability published',
    hint: 'Create, validate, test and publish one capability end to end.',
    href: '/capabilities',
  },
  {
    key: 'first_agent_token_created',
    label: 'First agent token issued',
    hint: 'Give an external agent a scoped token so it can call the router.',
    href: '/access',
  },
];

export function SessionProvider({ children }: { children: ReactNode }) {
  const query = useSessionQuery();

  useEffect(() => {
    if (query.data?.csrf_token) setCsrfFallback(query.data.csrf_token);
  }, [query.data?.csrf_token]);

  const status = useMemo<AuthStatus>(() => {
    if (query.data) return 'authenticated';
    if (query.isLoading) return 'loading';
    if (query.error) {
      // The session probe is the only 401 we handle without a hard redirect.
      return 'anonymous';
    }
    return 'anonymous';
  }, [query.data, query.isLoading, query.error]);

  const refresh = useCallback(() => {
    void query.refetch();
  }, [query]);

  const value = useMemo<AuthContextValue>(
    () => ({
      status,
      session: query.data ?? null,
      user: query.data?.user ?? null,
      csrfToken: query.data?.csrf_token ?? null,
      onboarding: query.data?.onboarding ?? null,
      mustChangePassword: Boolean(query.data?.must_change_password),
      error: query.error ?? null,
      refresh,
    }),
    [status, query.data, query.error, refresh],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useSession(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useSession must be used inside <SessionProvider>.');
  return context;
}
