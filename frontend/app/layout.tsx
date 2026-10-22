import type { Metadata, Viewport } from 'next';
import type { ReactNode } from 'react';

import { AppProviders } from '@/components/providers';
import './globals.css';

export const metadata: Metadata = {
  title: {
    default: 'Skill Router',
    template: '%s · Skill Router',
  },
  description: 'Administer capabilities, providers, credentials and routing for the Skill Router.',
  robots: { index: false, follow: false },
  icons: {
    icon: [{ url: '/favicon.svg', type: 'image/svg+xml' }],
  },
};

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
  themeColor: '#f8fafc',
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-bg text-text antialiased">
        <AppProviders>{children}</AppProviders>
      </body>
    </html>
  );
}
