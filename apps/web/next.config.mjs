/**
 * Next.js 配置（spec §8.4）。
 * - output: 'standalone'（容器镜像只拷贝 .next/standalone）
 * - /api/:path* 反代到 FastAPI（SKILLROUTER_API_URL 为不带 /api 的基址）
 * - 安全响应头
 */

/** @type {string} */
const rawApiBase = (process.env.SKILLROUTER_API_URL ?? 'http://127.0.0.1:8000').replace(/\/+$/, '');
// 允许 SKILLROUTER_API_URL 带或不带 `/api` 后缀（spec §8.4 两种写法都出现过）
const apiBase = rawApiBase.endsWith('/api') ? rawApiBase.slice(0, -4) : rawApiBase;

const securityHeaders = [
  { key: 'X-Frame-Options', value: 'DENY' },
  { key: 'X-Content-Type-Options', value: 'nosniff' },
  { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
  { key: 'X-DNS-Prefetch-Control', value: 'off' },
  {
    key: 'Permissions-Policy',
    value: 'camera=(), microphone=(), geolocation=(), payment=()',
  },
  {
    key: 'Content-Security-Policy',
    value: [
      "default-src 'self'",
      "base-uri 'self'",
      "form-action 'self'",
      "frame-ancestors 'none'",
      "img-src 'self' data: blob:",
      "font-src 'self' data:",
      "style-src 'self' 'unsafe-inline'",
      // Next 15 的内联 bootstrap 脚本需要 unsafe-inline；不使用 unsafe-eval。
      "script-src 'self' 'unsafe-inline'",
      "connect-src 'self'",
      "object-src 'none'",
    ].join('; '),
  },
];

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'standalone',
  reactStrictMode: true,
  poweredByHeader: false,
  eslint: {
    // CI 单独跑 lint；容器内构建不因 lint 阻断（类型错误仍然会让 build 失败）。
    ignoreDuringBuilds: true,
  },
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: `${apiBase}/api/:path*`,
      },
    ];
  },
  async headers() {
    return [
      {
        source: '/:path*',
        headers: securityHeaders,
      },
    ];
  },
};

export default nextConfig;
