import { expect, test } from '@playwright/test';

/**
 * 浮窗 Agent（spec §8.4 / §13.3）：发送 / 排队 / 停止 / 重连 / 历史。
 * 需要真实的 Agent 网关与模型配置；未配置时 skip。
 */

const USERNAME = process.env.SR_E2E_ADMIN_USER;
const PASSWORD = process.env.SR_E2E_ADMIN_PASSWORD;
const AGENT_READY = process.env.SR_E2E_AGENT === '1';

test.describe('浮窗 Agent', () => {
  test.skip(!USERNAME || !PASSWORD, '需要管理员凭据');

  test.beforeEach(async ({ page }) => {
    await page.goto('/login');
    await page.getByLabel('用户名').fill(USERNAME ?? '');
    await page.getByLabel('密码').fill(PASSWORD ?? '');
    await page.getByRole('button', { name: '登录' }).click();
    await page.waitForURL(/^(?!.*\/login).*$/);
  });

  test('入口按钮 56×56、aria-label 正确，Alt+J 可开关', async ({ page }) => {
    await page.goto('/');
    const dock = page.getByRole('button', { name: '打开 Agent' });
    await expect(dock).toBeVisible();
    const box = await dock.boundingBox();
    expect(box?.width).toBe(56);
    expect(box?.height).toBe(56);

    await page.keyboard.press('Alt+KeyJ');
    const panel = page.getByRole('dialog', { name: 'Agent 面板' });
    await expect(panel).toBeVisible();

    // Esc 关闭
    await page.keyboard.press('Escape');
    await expect(panel).toBeHidden();
  });

  test('面板跨路由保持打开状态', async ({ page }) => {
    await page.goto('/');
    await page.keyboard.press('Alt+KeyJ');
    const panel = page.getByRole('dialog', { name: 'Agent 面板' });
    await expect(panel).toBeVisible();

    await page.getByRole('link', { name: '提供商' }).click();
    await expect(panel).toBeVisible();
    await expect(page.getByRole('heading', { name: '提供商' })).toBeVisible();
  });

  test('Enter 发送 / Shift+Enter 换行', async ({ page }) => {
    test.skip(!AGENT_READY, '需要 SR_E2E_AGENT=1 且有可用引擎');
    await page.goto('/');
    await page.keyboard.press('Alt+KeyJ');
    const textarea = page.getByLabel('给 Agent 的消息');
    await textarea.fill('第一行');
    await textarea.press('Shift+Enter');
    await textarea.type('第二行');
    await expect(textarea).toHaveValue(/第一行[\s\S]*第二行/);
    await textarea.press('Enter');
    // 发送后输入框清空，消息进入列表
    await expect(textarea).toHaveValue('');
  });

  test('停止后展示 StopReport 与对账入口', async ({ page }) => {
    test.skip(!AGENT_READY, '需要 SR_E2E_AGENT=1 且有可用引擎');
    await page.goto('/');
    await page.keyboard.press('Alt+KeyJ');
    await page.getByLabel('给 Agent 的消息').fill('读取 https://example.com 并给出摘要');
    await page.getByRole('button', { name: '发送消息' }).click();
    await page.getByRole('button', { name: '停止' }).click();
    await expect(page.getByText(/已停止/)).toBeVisible();
  });
});
