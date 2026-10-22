/** @type {import('next').NextConfig} */
const apiOrigin = process.env.SR_API_ORIGIN ?? 'http://127.0.0.1:8000';

const nextConfig = {
  reactStrictMode: true,
  output: 'standalone',
  poweredByHeader: false,
  eslint: {
    // Lint is a separate CI step (`npm run lint`); a build should not fail on style.
    ignoreDuringBuilds: true,
  },
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: `${apiOrigin}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
