/**
 * 认证引导（spec §9.2 `/auth/bootstrap`、`/auth/me`）。
 * 强制改密不可跳过：`must_change_password` 为真时除 `/change-password` 与 `/login` 之外全部重定向。
 */

import { useQuery } from '@tanstack/react-query';
import { apiFetch, isApiError } from './api';
import { queryKeys } from './query';
import type { BootstrapInfo, LoginResult, MeResponse } from './types';

export const LOGIN_PATH = '/login';
export const CHANGE_PASSWORD_PATH = '/change-password';

/** 无需登录即可访问的路由 */
export const PUBLIC_PATHS = [LOGIN_PATH] as const;
/** 登录后仍可访问（改密必须可达） */
export const SESSION_ALLOWED_PATHS = [CHANGE_PASSWORD_PATH] as const;

export async function fetchBootstrap(): Promise<BootstrapInfo> {
  return apiFetch<BootstrapInfo>('/admin/auth/bootstrap', { redirectOnAuthError: false });
}

export async function fetchMe(): Promise<MeResponse | null> {
  try {
    return await apiFetch<MeResponse>('/admin/auth/me', { redirectOnAuthError: false });
  } catch (error) {
    if (isApiError(error) && error.isUnauthenticated) return null;
    if (isApiError(error) && error.status === 403) return null;
    throw error;
  }
}

export async function login(payload: {
  username: string;
  password: string;
}): Promise<LoginResult> {
  return apiFetch<LoginResult>('/admin/auth/login', {
    method: 'POST',
    body: payload,
    redirectOnAuthError: false,
  });
}

export async function logout(): Promise<void> {
  await apiFetch<null>('/admin/auth/logout', { method: 'POST', redirectOnAuthError: false });
}

export async function changePassword(payload: {
  current_password: string;
  new_password: string;
}): Promise<void> {
  await apiFetch<null>('/admin/auth/change-password', {
    method: 'POST',
    body: payload,
    redirectOnAuthError: false,
  });
}

/**
 * 校验密码强度（spec §9.2）：≥12 字符、含字母+数字、不等于默认值。
 * 返回问题列表（中文），空数组表示通过。
 */
export function checkPasswordStrength(password: string): string[] {
  const issues: string[] = [];
  if (password.length < 12) issues.push('至少 12 个字符');
  if (!/[A-Za-z]/.test(password)) issues.push('至少包含一个字母');
  if (!/[0-9]/.test(password)) issues.push('至少包含一个数字');
  if (/^(?:admin|password|changeme|skillrouter)[0-9!@#]*$/i.test(password)) {
    issues.push('不能使用默认密码或其变体');
  }
  return issues;
}

export interface PasswordStrength {
  score: 0 | 1 | 2 | 3 | 4;
  label: string;
}

export function passwordStrength(password: string): PasswordStrength {
  let score = 0;
  if (password.length >= 12) score += 1;
  if (password.length >= 16) score += 1;
  if (/[A-Z]/.test(password) && /[a-z]/.test(password)) score += 1;
  if (/[0-9]/.test(password) && /[^A-Za-z0-9]/.test(password)) score += 1;
  const clamped = Math.max(0, Math.min(4, score)) as PasswordStrength['score'];
  const labels = ['太弱', '偏弱', '可用', '较强', '很强'] as const;
  return { score: clamped, label: labels[clamped] ?? '偏弱' };
}

/** 是否允许在该路径停下来（未登录 / 未改密） */
export function requiresPasswordChange(pathname: string): boolean {
  return pathname.startsWith(CHANGE_PASSWORD_PATH);
}

export function isPublicPath(pathname: string): boolean {
  return PUBLIC_PATHS.some((p) => pathname === p || pathname.startsWith(`${p}/`));
}

/** 当前管理员的查询（顶栏/设置页使用；失败不阻塞页面） */
export function useMe() {
  return useQuery<MeResponse | null>({
    queryKey: queryKeys.auth.me(),
    queryFn: fetchMe,
    staleTime: 60_000,
    retry: false,
  });
}
