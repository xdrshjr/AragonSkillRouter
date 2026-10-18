import type { Config } from 'tailwindcss';

/**
 * Tailwind 令牌映射（spec §13.4）。
 * 颜色/圆角/间距/字号全部来自 CSS 变量，便于 [data-theme="dark"] 切换。
 */
const config: Config = {
  content: [
    './app/**/*.{ts,tsx}',
    './components/**/*.{ts,tsx}',
    './lib/**/*.{ts,tsx}',
  ],
  theme: {
    extend: {
      colors: {
        bg: 'var(--sr-bg)',
        surface: 'var(--sr-surface)',
        hover: 'var(--sr-hover)',
        border: {
          DEFAULT: 'var(--sr-border)',
          strong: 'var(--sr-border-strong)',
        },
        content: {
          DEFAULT: 'var(--sr-text)',
          muted: 'var(--sr-text-muted)',
        },
        accent: {
          DEFAULT: 'var(--sr-accent)',
          weak: 'var(--sr-accent-weak)',
        },
        ok: { DEFAULT: 'var(--sr-ok)', weak: 'var(--sr-ok-weak)' },
        warn: { DEFAULT: 'var(--sr-warn)', weak: 'var(--sr-warn-weak)' },
        danger: { DEFAULT: 'var(--sr-danger)', weak: 'var(--sr-danger-weak)' },
        info: { DEFAULT: 'var(--sr-info)', weak: 'var(--sr-info-weak)' },
      },
      borderRadius: {
        sm: 'var(--sr-radius-sm)',
        DEFAULT: 'var(--sr-radius-sm)',
        md: 'var(--sr-radius)',
      },
      spacing: {
        '1': 'var(--sr-space-1)',
        '2': 'var(--sr-space-2)',
        '3': 'var(--sr-space-3)',
        '4': 'var(--sr-space-4)',
        '6': 'var(--sr-space-6)',
        '8': 'var(--sr-space-8)',
        '12': 'var(--sr-space-12)',
      },
      fontSize: {
        '12': ['12px', '1.5'],
        '13': ['13px', '1.55'],
        '14': ['14px', '1.55'],
        '16': ['16px', '1.5'],
        '20': ['20px', '1.35'],
        '24': ['24px', '1.3'],
        '32': ['32px', '1.2'],
      },
      fontFamily: {
        sans: ['var(--sr-font-sans)'],
        mono: ['var(--sr-font-mono)'],
      },
      transitionTimingFunction: {
        sr: 'cubic-bezier(0.22, 0.61, 0.36, 1)',
      },
      maxWidth: {
        content: '1320px',
      },
      zIndex: {
        dock: '40',
        panel: '45',
        overlay: '50',
        toast: '60',
      },
    },
  },
  plugins: [],
};

export default config;
