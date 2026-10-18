import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright 配置（e2e 可能需要真实后端，默认不随 CI 必跑）。
 * 用 `npm run test:e2e` 手动执行；缺少后端时测试会自行 skip。
 */
export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: process.env.SR_BASE_URL ?? 'http://127.0.0.1:3000',
    trace: 'on-first-retry',
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    // SR_E2E_CHANNEL=chrome 时直接用系统 Chrome（离线机器上装不上 playwright 自带 chromium 的兜底）
    ...(process.env.SR_E2E_CHANNEL
      ? { channel: process.env.SR_E2E_CHANNEL as 'chrome' | 'msedge' }
      : {}),
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: process.env.SR_BASE_URL
    ? undefined
    : {
        command: 'npm run start',
        url: 'http://127.0.0.1:3000/api/health',
        reuseExistingServer: true,
        timeout: 120_000,
      },
});
