import { expect, test, type ConsoleMessage, type Page, type Response } from '@playwright/test';

/**
 * 控制台「零错误」全路由巡检 + 本轮修复的回归守卫。
 *
 * 运行前置（缺任何一个就整组 skip，绝不伪造成功）：
 *   SR_E2E_ADMIN_USER / SR_E2E_ADMIN_PASSWORD —— 已改过密的管理员账号
 * 可选：
 *   SR_BASE_URL       控制台地址（默认 http://127.0.0.1:3000）
 *   SR_E2E_CHANNEL    浏览器通道，例如 `chrome`（用系统 Chrome，省去 playwright install）
 *   SR_E2E_CAPABILITY 某个已发布能力的 id（跳过时不影响其他断言）
 *
 * 已覆盖的回归点：
 *   1. 每个管理台路由都不产生控制台错误 / 页面异常 / 4xx-5xx 响应
 *      （曾因 `/healthz` 本地 404、缺 favicon 而在每页刷错误）
 *   2. `/healthz` 经 Web 反代能拿到维护标志（否则维护横幅永远不生效）
 *   3. 高于视口的对话框必须完整居中，「签发」按钮可点
 *      （`.sr-enter` 曾用 transform 动画覆盖 Tailwind 的 -translate-y-1/2，按钮被挤出视口）
 *   4. 调用详情抽屉能正常渲染耗时分解
 *      （前端读 `timings`，后端返回 `latency_breakdown`，曾整块白屏）
 */

const BASE = process.env.SR_BASE_URL ?? 'http://127.0.0.1:3000';
const USER = process.env.SR_E2E_ADMIN_USER;
const PASSWORD = process.env.SR_E2E_ADMIN_PASSWORD;
const ROUTES = [
  '/',
  '/providers',
  '/credentials',
  '/capabilities',
  '/capabilities/new',
  '/skills',
  '/skills/new',
  '/calls',
  '/tasks',
  '/agent',
  '/access',
  '/settings',
  '/settings/engine',
];

/** 允许出现的 4xx：无业务数据时的正常空态探针不算缺陷 */
function isAllowed(status: number, url: string): boolean {
  // 未登录时的 401/403 是守卫预期行为，登录用例不会触发；这里只放行「探测型」请求
  if (url.includes('/favicon.ico')) return true;
  return status < 400;
}

async function login(page: Page): Promise<void> {
  await page.goto(`${BASE}/login`, { waitUntil: 'domcontentloaded' });
  await page.getByLabel('用户名').fill(USER ?? '');
  await page.getByLabel('密码').fill(PASSWORD ?? '');
  await page.getByRole('button', { name: '登录' }).click();
  await page.waitForURL((url) => !url.pathname.startsWith('/login'), { timeout: 30_000 });
}

test.describe('有头浏览器巡检', () => {
  test.skip(!USER || !PASSWORD, '需要 SR_E2E_ADMIN_USER / SR_E2E_ADMIN_PASSWORD 才能跑真实巡检');

  test('全部管理台路由零控制台错误', async ({ page }) => {
    const problems: string[] = [];
    page.on('console', (message: ConsoleMessage) => {
      if (message.type() === 'error') problems.push(`[console] ${message.text()}`);
    });
    page.on('pageerror', (error: Error) => problems.push(`[pageerror] ${error.message}`));
    page.on('response', (response: Response) => {
      if (!isAllowed(response.status(), response.url())) {
        problems.push(`[http ${response.status()}] ${response.request().method()} ${response.url()}`);
      }
    });

    await login(page);

    for (const route of ROUTES) {
      const before = problems.length;
      const response = await page.goto(`${BASE}${route}`, { waitUntil: 'domcontentloaded' });
      expect(response?.status(), `${route} 应返回 200`).toBe(200);
      // 等首屏数据与浮窗 Agent 的 SSE 握手稳定后再断言
      await page.waitForTimeout(2500);
      const added = problems.splice(before);
      expect(added, `${route} 出现前端错误`).toEqual([]);
    }
  });

  test('/healthz 经反代返回维护标志', async ({ page }) => {
    await login(page);
    const health = await page.evaluate(async () => {
      const response = await fetch('/healthz', { headers: { Accept: 'application/json' } });
      if (!response.ok) return { ok: false, status: response.status, body: null } as const;
      return { ok: true, status: response.status, body: await response.json() } as const;
    });
    expect(health.status).toBe(200);
    expect(health.ok).toBe(true);
    expect(typeof (health.body as { maintenance?: unknown }).maintenance).toBe('boolean');
  });

  test('高对话框完整居中，底部按钮可点', async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/access`, { waitUntil: 'domcontentloaded' });
    // 先选中任意一个外部 Agent（没有则跳过：不是本用例要验证的对象）
    const agentButton = page.locator('section[aria-label="外部 Agent"] li button').first();
    // 列表来自接口，等它就位再判断「有没有 Agent」，避免误 skip
    await page
      .waitForSelector('section[aria-label="外部 Agent"] li button', { timeout: 15_000 })
      .catch(() => null);
    test.skip(!(await agentButton.count()), '还没有外部 Agent，跳过高对话框用例');
    await agentButton.click();
    await page.getByRole('button', { name: '签发令牌' }).click();

    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible();
    const box = await dialog.boundingBox();
    const viewport = page.viewportSize();
    expect(box).not.toBeNull();
    expect(viewport).not.toBeNull();
    if (box && viewport) {
      // 顶部不能有负偏移、底部不能溢出视口
      expect(box.y).toBeGreaterThanOrEqual(0);
      expect(box.y + box.height).toBeLessThanOrEqual(viewport.height + 1);
      // 垂直居中（容差 8px，留出亚像素与动效误差）
      const centered = viewport.height / 2 - (box.y + box.height / 2);
      expect(Math.abs(centered)).toBeLessThanOrEqual(8);
    }
    // 关键：底部主按钮必须在视口内，否则用户根本点不到
    const submit = dialog.getByRole('button', { name: '签发', exact: true });
    await expect(submit).toBeVisible();
    const submitBox = await submit.boundingBox();
    expect(submitBox).not.toBeNull();
    if (submitBox && viewport) {
      expect(submitBox.y + submitBox.height).toBeLessThanOrEqual(viewport.height);
    }
  });

  test('调用详情抽屉渲染耗时分解', async ({ page }) => {
    const problems: string[] = [];
    page.on('pageerror', (error: Error) => problems.push(error.message));

    await login(page);
    await page.goto(`${BASE}/calls`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2000);
    const firstRow = page.getByRole('row').nth(1);
    test.skip(!(await firstRow.count()), '还没有调用记录，跳过调用详情用例');
    await firstRow.click();

    const drawer = page.getByRole('dialog');
    await expect(drawer).toBeVisible();
    await expect(drawer.getByText('耗时分解')).toBeVisible();
    await expect(drawer.getByText('排队', { exact: true })).toBeVisible();
    await expect(drawer.getByText('总计', { exact: true })).toBeVisible();
    expect(problems, '调用详情不应出现页面异常').toEqual([]);
  });
});
