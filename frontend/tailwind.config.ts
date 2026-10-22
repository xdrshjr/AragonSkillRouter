import type { Config } from 'tailwindcss';

/**
 * Tailwind is mapped onto the `--sr-*` design tokens declared in
 * `app/globals.css`, so a theme change happens in one place.
 * Spacing deliberately stays on the 8px grid (Tailwind's default scale).
 */
const config: Config = {
  content: ['./app/**/*.{ts,tsx}', './components/**/*.{ts,tsx}', './lib/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        bg: 'var(--sr-bg)',
        surface: 'var(--sr-surface)',
        'surface-raised': 'var(--sr-surface-raised)',
        border: 'var(--sr-border)',
        'border-strong': 'var(--sr-border-strong)',
        text: 'var(--sr-text)',
        muted: 'var(--sr-muted)',
        accent: 'var(--sr-accent)',
        'accent-strong': 'var(--sr-accent-strong)',
        'accent-soft': 'var(--sr-accent-soft)',
        danger: 'var(--sr-danger)',
        'danger-soft': 'var(--sr-danger-soft)',
        success: 'var(--sr-success)',
        'success-soft': 'var(--sr-success-soft)',
        warning: 'var(--sr-warning)',
        'warning-soft': 'var(--sr-warning-soft)',
        'info-soft': 'var(--sr-info-soft)',
      },
      borderRadius: {
        control: 'var(--sr-radius-control)',
        card: 'var(--sr-radius-card)',
        pill: '9999px',
      },
      fontFamily: {
        sans: [
          '-apple-system',
          'BlinkMacSystemFont',
          '"Segoe UI"',
          'Roboto',
          '"Helvetica Neue"',
          'Arial',
          '"Noto Sans"',
          'sans-serif',
        ],
        mono: ['ui-monospace', 'SFMono-Regular', '"Cascadia Mono"', 'Consolas', '"Liberation Mono"', 'monospace'],
      },
      fontSize: {
        xs: ['13px', { lineHeight: '18px' }],
        sm: ['14px', { lineHeight: '20px' }],
        base: ['16px', { lineHeight: '24px' }],
        lg: ['20px', { lineHeight: '28px' }],
        xl: ['24px', { lineHeight: '32px' }],
      },
      boxShadow: {
        overlay: 'var(--sr-shadow-overlay)',
      },
      transitionDuration: {
        DEFAULT: '150ms',
      },
      maxWidth: {
        content: '1180px',
      },
      keyframes: {
        'sr-fade-in': {
          from: { opacity: '0' },
          to: { opacity: '1' },
        },
        'sr-slide-in': {
          from: { transform: 'translateY(4px)', opacity: '0' },
          to: { transform: 'translateY(0)', opacity: '1' },
        },
        'sr-pulse': {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0.55' },
        },
        'sr-spin': {
          to: { transform: 'rotate(360deg)' },
        },
      },
      animation: {
        'fade-in': 'sr-fade-in 150ms ease-out',
        'slide-in': 'sr-slide-in 150ms ease-out',
        shimmer: 'sr-pulse 1.6s ease-in-out infinite',
        spin: 'sr-spin 700ms linear infinite',
      },
    },
  },
  plugins: [],
};

export default config;
