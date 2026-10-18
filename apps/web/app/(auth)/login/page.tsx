'use client';

import { zodResolver } from '@hookform/resolvers/zod';
import { TriangleAlert } from 'lucide-react';
import { useRouter } from 'next/navigation';
import * as React from 'react';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { EmptyState, RequestIdCopy } from '@/components/common/EmptyState';
import { FormField } from '@/components/common/FormField';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import { isApiError } from '@/lib/api';
import { fetchBootstrap, login } from '@/lib/auth';
import { useQuery } from '@tanstack/react-query';
import { queryKeys } from '@/lib/query';

const schema = z.object({
  username: z.string().min(1, '请输入用户名'),
  password: z.string().min(1, '请输入密码'),
});

type FormValues = z.infer<typeof schema>;

/**
 * 登录页（spec §13.2）：
 * 空 / 加载 / 校验失败 / 限流（带倒计时）/ 服务不可用 / 未初始化。
 */
export default function LoginPage() {
  const router = useRouter();
  const [nextPath, setNextPath] = React.useState('/');

  // 不用 useSearchParams（静态预渲染需要 Suspense 包裹），直接从地址栏读取一次。
  React.useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const next = params.get('next');
    if (next && next.startsWith('/')) setNextPath(next);
  }, []);

  const [serverError, setServerError] = React.useState<{
    code: string;
    message: string;
    hint: string | null;
    requestId: string | null;
  } | null>(null);
  const [throttle, setThrottle] = React.useState(0);

  const bootstrap = useQuery({
    queryKey: queryKeys.auth.bootstrap(),
    queryFn: fetchBootstrap,
    retry: false,
    staleTime: 30_000,
  });

  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { username: '', password: '' },
    mode: 'onSubmit',
  });

  // 限流倒计时
  React.useEffect(() => {
    if (throttle <= 0) return;
    const timer = setInterval(() => setThrottle((value) => Math.max(0, value - 1)), 1000);
    return () => clearInterval(timer);
  }, [throttle]);

  const onSubmit = form.handleSubmit(async (values) => {
    setServerError(null);
    try {
      const result = await login(values);
      const target = result.admin.must_change_password ? '/change-password' : nextPath;
      router.replace(target);
    } catch (cause) {
      if (isApiError(cause)) {
        if (cause.code === 'LOGIN_THROTTLED') {
          const retryAfter = cause.details['retry_after'];
          const seconds = typeof retryAfter === 'number' ? retryAfter : 60;
          setThrottle(seconds);
        }
        setServerError({
          code: cause.code,
          message: cause.message,
          hint: cause.hint,
          requestId: cause.requestId,
        });
      } else {
        setServerError({
          code: 'UNKNOWN',
          message: '登录失败，请稍后重试',
          hint: null,
          requestId: null,
        });
      }
    }
  });

  const notInitialized = serverError?.code === 'NOT_INITIALIZED' || bootstrap.data?.initialized === false;
  const unavailable = serverError?.code === 'MAINTENANCE' || bootstrap.isError;

  return (
    <main className="flex min-h-screen items-center justify-center bg-bg px-4 py-8">
      <div className="w-full max-w-[400px]">
        <div className="mb-6 flex items-center gap-2">
          <span
            aria-hidden
            className="flex h-8 w-8 items-center justify-center rounded-sm border border-accent text-13 font-semibold text-accent"
          >
            SR
          </span>
          <div>
            <h1 className="text-20 font-semibold text-content">Skill Router</h1>
            <p className="text-12 text-content-muted">能力 / 凭证 / SKILL 路由控制台</p>
          </div>
        </div>

        <div className="rounded-md border border-border bg-surface p-4">
          {bootstrap.isLoading ? (
            <div className="flex flex-col gap-3" aria-busy="true">
              <Skeleton height={14} width="30%" />
              <Skeleton height={36} />
              <Skeleton height={14} width="30%" />
              <Skeleton height={36} />
              <Skeleton height={36} width="40%" />
            </div>
          ) : unavailable ? (
            <EmptyState
              kind="unavailable"
              title="平台暂时不可用"
              description={
                bootstrap.isError && isApiError(bootstrap.error)
                  ? bootstrap.error.message
                  : '无法连接到平台服务。请确认 API 进程已启动，或稍后重试。'
              }
              requestId={isApiError(bootstrap.error) ? bootstrap.error.requestId : null}
              actionLabel="重试"
              onAction={() => {
                void bootstrap.refetch();
              }}
            />
          ) : notInitialized ? (
            <EmptyState
              kind="first-use"
              title="平台尚未初始化"
              description="在服务器上运行初始化脚本创建数据库与管理员账号，然后刷新本页。"
              requirement="python scripts/bootstrap.py"
              actionLabel="刷新"
              onAction={() => window.location.reload()}
            />
          ) : (
            <form onSubmit={onSubmit} noValidate className="flex flex-col gap-1">
              <h2 className="text-16 font-semibold text-content">管理员登录</h2>
              <p className="mb-2 text-13 text-content-muted">
                使用初始化时创建的管理员账号登录。会话使用 Cookie，写请求需要 CSRF 令牌。
              </p>

              <FormField id="username" label="用户名" required error={form.formState.errors.username?.message}>
                <Input
                  id="username"
                  autoComplete="username"
                  autoFocus
                  invalid={Boolean(form.formState.errors.username)}
                  aria-describedby="username-error"
                  {...form.register('username')}
                />
              </FormField>

              <FormField id="password" label="密码" required error={form.formState.errors.password?.message}>
                <Input
                  id="password"
                  type="password"
                  autoComplete="current-password"
                  invalid={Boolean(form.formState.errors.password)}
                  aria-describedby="password-error"
                  {...form.register('password')}
                />
              </FormField>

              {serverError ? (
                <div
                  role="alert"
                  className="mb-2 rounded border border-danger bg-[var(--sr-danger-weak)] px-3 py-2 text-13 text-content"
                >
                  <p className="flex items-start gap-1.5">
                    <TriangleAlert size={16} strokeWidth={1.75} className="mt-0.5 shrink-0 text-danger" aria-hidden />
                    <span>{serverError.message}</span>
                  </p>
                  {serverError.hint ? (
                    <p className="mt-1 text-12 text-content-muted">{serverError.hint}</p>
                  ) : null}
                  {throttle > 0 ? (
                    <p className="mt-1 text-12 text-warn" aria-live="polite">
                      请在 {throttle} 秒后重试登录。
                    </p>
                  ) : null}
                  {serverError.requestId ? (
                    <RequestIdCopy requestId={serverError.requestId} className="mt-1" />
                  ) : null}
                </div>
              ) : null}

              <Button
                type="submit"
                variant="primary"
                loading={form.formState.isSubmitting}
                disabled={throttle > 0}
                className="mt-1 w-full"
              >
                {throttle > 0 ? `${throttle} 秒后可重试` : '登录'}
              </Button>
            </form>
          )}
        </div>

        <p className="mt-4 text-12 text-content-muted">
          首次使用？初始化脚本会输出一次性管理员密码，并要求在首次登录后立即修改（不可跳过）。
        </p>
      </div>
    </main>
  );
}
