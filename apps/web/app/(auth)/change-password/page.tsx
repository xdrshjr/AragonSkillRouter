'use client';

import { zodResolver } from '@hookform/resolvers/zod';
import { useQuery } from '@tanstack/react-query';
import { CircleCheck, CircleX, LogOut } from 'lucide-react';
import { useRouter } from 'next/navigation';
import * as React from 'react';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { RequestIdCopy } from '@/components/common/EmptyState';
import { FormField } from '@/components/common/FormField';
import { Button } from '@/components/ui/button';
import { cn } from '@/components/ui/cn';
import { Input } from '@/components/ui/input';
import { isApiError } from '@/lib/api';
import { changePassword, checkPasswordStrength, fetchMe, logout, passwordStrength } from '@/lib/auth';
import { queryKeys } from '@/lib/query';

const schema = z
  .object({
    current_password: z.string().min(1, '请输入当前密码'),
    new_password: z.string().min(12, '新密码至少 12 个字符'),
    confirm: z.string().min(1, '请再次输入新密码'),
  })
  .refine((values) => values.new_password === values.confirm, {
    path: ['confirm'],
    message: '两次输入的新密码不一致',
  })
  .refine((values) => checkPasswordStrength(values.new_password).length === 0, {
    path: ['new_password'],
    message: '新密码不满足强度要求',
  });

type FormValues = z.infer<typeof schema>;

/**
 * 强制改密（spec §13.2）：不可跳过；(app) 布局会把 must_change_password 会话重定向到本页。
 */
export default function ChangePasswordPage() {
  const router = useRouter();
  const [serverError, setServerError] = React.useState<{
    code: string;
    message: string;
    hint: string | null;
    requestId: string | null;
  } | null>(null);
  const [done, setDone] = React.useState(false);

  const me = useQuery({
    queryKey: queryKeys.auth.me(),
    queryFn: fetchMe,
    retry: false,
  });

  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { current_password: '', new_password: '', confirm: '' },
    mode: 'onChange',
  });

  const newPassword = form.watch('new_password');
  const strength = passwordStrength(newPassword ?? '');
  const issues = checkPasswordStrength(newPassword ?? '');

  const onSubmit = form.handleSubmit(async (values) => {
    setServerError(null);
    try {
      await changePassword({
        current_password: values.current_password,
        new_password: values.new_password,
      });
      setDone(true);
      setTimeout(() => router.replace('/'), 1200);
    } catch (cause) {
      if (isApiError(cause)) {
        setServerError({
          code: cause.code,
          message: cause.message,
          hint: cause.hint,
          requestId: cause.requestId,
        });
        if (cause.code === 'AUTH_INVALID_CREDENTIALS') {
          form.setError('current_password', { message: '当前密码不正确' });
        }
        if (cause.code === 'UNAUTHENTICATED' || cause.code === 'SESSION_EXPIRED') {
          router.replace('/login');
        }
      } else {
        setServerError({
          code: 'UNKNOWN',
          message: '修改失败，请稍后重试',
          hint: null,
          requestId: null,
        });
      }
    }
  });

  const handleLogout = async (): Promise<void> => {
    try {
      await logout();
    } finally {
      router.replace('/login');
    }
  };

  return (
    <main className="flex min-h-screen items-center justify-center bg-bg px-4 py-8">
      <div className="w-full max-w-[440px]">
        <div className="mb-4">
          <h1 className="text-20 font-semibold text-content">必须修改密码</h1>
          <p className="mt-1 text-13 text-content-muted">
            当前账号仍在使用初始密码。修改成功前无法访问其它页面。
            {me.data?.admin.username ? `（账号：${me.data.admin.username}）` : null}
          </p>
        </div>

        <div className="rounded-md border border-border bg-surface p-4">
          {done ? (
            <div className="flex flex-col items-center gap-2 py-6 text-center" role="status">
              <CircleCheck size={24} strokeWidth={1.75} className="text-ok" aria-hidden />
              <p className="text-14 font-medium text-content">密码已更新</p>
              <p className="text-13 text-content-muted">其它会话已被吊销，正在跳转到概览…</p>
            </div>
          ) : (
            <form onSubmit={onSubmit} noValidate className="flex flex-col gap-1">
              <FormField
                id="current_password"
                label="当前密码"
                required
                error={form.formState.errors.current_password?.message}
              >
                <Input
                  id="current_password"
                  type="password"
                  autoComplete="current-password"
                  invalid={Boolean(form.formState.errors.current_password)}
                  aria-describedby="current_password-error"
                  {...form.register('current_password')}
                />
              </FormField>

              <FormField
                id="new_password"
                label="新密码"
                required
                hint="≥12 字符，含字母与数字"
                error={form.formState.errors.new_password?.message}
              >
                <Input
                  id="new_password"
                  type="password"
                  autoComplete="new-password"
                  invalid={Boolean(form.formState.errors.new_password)}
                  aria-describedby="new_password-error"
                  {...form.register('new_password')}
                />
              </FormField>

              {/* 强度提示：文字 + 分段条，不只靠颜色 */}
              <div className="-mt-1 mb-1" aria-live="polite">
                <div className="flex items-center gap-1.5">
                  {[0, 1, 2, 3].map((index) => (
                    <span
                      key={index}
                      aria-hidden
                      className={cn(
                        'h-1 w-8 rounded-full',
                        newPassword && index < strength.score
                          ? strength.score <= 2
                            ? 'bg-warn'
                            : 'bg-ok'
                          : 'bg-[var(--sr-border)]',
                      )}
                    />
                  ))}
                  <span className="text-12 text-content-muted">
                    强度：{newPassword ? strength.label : '未输入'}
                  </span>
                </div>
                {issues.length > 0 ? (
                  <ul className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5">
                    {issues.map((issue) => (
                      <li key={issue} className="flex items-center gap-1 text-12 text-content-muted">
                        <CircleX size={12} strokeWidth={1.75} className="text-danger" aria-hidden />
                        {issue}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>

              <FormField
                id="confirm"
                label="确认新密码"
                required
                error={form.formState.errors.confirm?.message}
              >
                <Input
                  id="confirm"
                  type="password"
                  autoComplete="new-password"
                  invalid={Boolean(form.formState.errors.confirm)}
                  aria-describedby="confirm-error"
                  {...form.register('confirm')}
                />
              </FormField>

              {serverError ? (
                <div
                  role="alert"
                  className="mb-2 rounded border border-danger bg-[var(--sr-danger-weak)] px-3 py-2 text-13 text-content"
                >
                  <p>{serverError.message}</p>
                  {serverError.hint ? (
                    <p className="mt-1 text-12 text-content-muted">{serverError.hint}</p>
                  ) : null}
                  {serverError.requestId ? (
                    <RequestIdCopy requestId={serverError.requestId} className="mt-1" />
                  ) : null}
                </div>
              ) : null}

              <Button type="submit" variant="primary" loading={form.formState.isSubmitting} className="mt-1 w-full">
                修改密码
              </Button>
            </form>
          )}
        </div>

        <div className="mt-4 flex items-center justify-between">
          <p className="text-12 text-content-muted">修改成功后其它会话会被吊销，需重新登录。</p>
          <Button variant="ghost" size="sm" onClick={() => void handleLogout()} icon={<LogOut size={16} strokeWidth={1.75} />}>
            退出登录
          </Button>
        </div>
      </div>
    </main>
  );
}
