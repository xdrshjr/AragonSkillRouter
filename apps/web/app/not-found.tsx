import Link from 'next/link';

/**
 * App Router 的 404 页（`app/not-found.tsx`）。
 *
 * 为什么必须有这个文件：本项目只有 `app/`，没有 `pages/`。缺它的时候，`next build`
 * 回落到 Pages Router 的内置 `/404` + `/_error` 去预渲染，而这两个组件内部走
 * `useHtmlContext()`，在没有 `pages/_document` 的纯 App Router 项目里会抛
 * `<Html> should not be imported outside of pages/_document`，导致整个构建失败
 * （实测：GitHub Actions ubuntu-latest 上 `next build` 直接退出码 1）。
 *
 * 全局错误页（`app/global-error.tsx`）同理，一并补齐，避免再回落到 `/_error`。
 */
export default function NotFound() {
  return (
    <main className="flex min-h-screen items-center justify-center bg-bg px-4 py-8">
      <div className="w-full max-w-[420px] text-center">
        <p className="sr-mono text-13 text-content-muted">404</p>
        <h1 className="mt-1 text-20 font-semibold text-content">页面不存在</h1>
        <p className="mt-1 text-13 text-content-muted">
          链接可能已失效，或该对象已被删除。回到概览可以继续操作。
        </p>
        <Link
          href="/"
          className="mt-4 inline-flex h-8 items-center justify-center rounded-sm border border-accent bg-accent px-3 text-13 font-medium text-white hover:bg-[color-mix(in_srgb,var(--sr-accent)_88%,black)] focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
        >
          回到概览
        </Link>
      </div>
    </main>
  );
}
