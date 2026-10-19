'use client';

import * as React from 'react';

/**
 * 全局错误页（`app/global-error.tsx`）：App Router 的项目如果没有它，
 * 构建期会回落到 Pages Router 的 `/_error` 去预渲染，而那条路径依赖
 * `pages/_document` 提供的 HtmlContext —— 纯 App Router 项目里会抛
 * `<Html> should not be imported outside of pages/_document` 让 `next build` 失败。
 *
 * 它必须自带 <html>/<body>（此时根 layout 已被替换）。
 */
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  React.useEffect(() => {
    // 控制台留痕即可：这是最后一道兜底，不再依赖任何业务组件
    console.error('global-error', error);
  }, [error]);

  return (
    <html lang="zh-CN">
      <body style={{ margin: 0, fontFamily: 'ui-sans-serif, system-ui, sans-serif' }}>
        <main
          style={{
            display: 'flex',
            minHeight: '100vh',
            alignItems: 'center',
            justifyContent: 'center',
            padding: '2rem',
          }}
        >
          <div style={{ maxWidth: 420, textAlign: 'center' }}>
            <h1 style={{ margin: 0, fontSize: 20, fontWeight: 600 }}>页面出错了</h1>
            <p style={{ marginTop: 8, fontSize: 13, opacity: 0.7 }}>
              这是一个未预期的错误{error.digest ? `（digest ${error.digest}）` : ''}。
            </p>
            <button
              type="button"
              onClick={reset}
              style={{
                marginTop: 16,
                height: 32,
                padding: '0 12px',
                fontSize: 13,
                fontWeight: 500,
                border: '1px solid transparent',
                borderRadius: 4,
                background: '#1c1a17',
                color: '#fff',
                cursor: 'pointer',
              }}
            >
              重试
            </button>
          </div>
        </main>
      </body>
    </html>
  );
}
