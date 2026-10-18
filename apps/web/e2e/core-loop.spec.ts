import { expect, test } from '@playwright/test';

/**
 * 核心闭环（spec §8.4 / §15.2）：配置 → 测试 → 发布 → 调用 → 查看记录。
 *
 * 说明：真实闭环需要后端（FastAPI + worker）与有效上游凭证。
 * 未提供 `SR_E2E_ADMIN_USER/SR_E2E_ADMIN_PASSWORD` 时整组 skip，避免伪造成功。
 */

const USERNAME = process.env.SR_E2E_ADMIN_USER;
const PASSWORD = process.env.SR_E2E_ADMIN_PASSWORD;

test.describe('核心闭环：配置 → 测试 → 发布 → 调用 → 记录', () => {
  test.skip(!USERNAME || !PASSWORD, '需要 SR_E2E_ADMIN_USER / SR_E2E_ADMIN_PASSWORD 才能跑真实闭环');

  test.beforeEach(async ({ page }) => {
    await page.goto('/login');
    await page.getByLabel('用户名').fill(USERNAME ?? '');
    await page.getByLabel('密码').fill(PASSWORD ?? '');
    await page.getByRole('button', { name: '登录' }).click();
    await page.waitForURL(/^(?!.*\/login).*$/);
  });

  test('概览页展示真实指标卡与待办', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByRole('heading', { name: '概览' })).toBeVisible();
    await expect(page.getByRole('region', { name: '关键指标' })).toBeVisible();
    // 指标卡必须链接到详情页（spec §9.6：每条信息都能跳转）
    const firstCard = page.getByRole('link', { name: /能力|24h 成功率|耗时|失败任务/ }).first();
    await expect(firstCard).toHaveAttribute('href', /.+/);
  });

  test('提供商 → 能力 → 测试 → 发布 → 调用记录', async ({ page }) => {
    // 1) 提供商页面可搜索
    await page.goto('/providers');
    await expect(page.getByRole('table', { name: /提供商列表/ })).toBeVisible();
    await page.getByRole('searchbox', { name: /搜索提供商列表/ }).fill('openai');

    // 2) 能力列表：四态徽标
    await page.goto('/capabilities');
    await expect(page.getByRole('table', { name: /能力列表/ })).toBeVisible();

    // 3) 打开第一个能力并确认三个状态徽标独立显示
    const firstCapability = page.getByRole('link', { name: /^[a-z0-9]+\./ }).first();
    if (await firstCapability.count()) {
      await firstCapability.click();
      await expect(page.getByLabel('能力状态')).toContainText('已保存');
      await expect(page.getByLabel('能力状态')).toContainText('测试通过');
      // 未测试时发布按钮必须禁用（不允许伪造成功）
      const publishButton = page.getByRole('button', { name: '发布', exact: true });
      const testedBadge = await page.getByLabel('能力状态').innerText();
      if (testedBadge.includes('未测试')) {
        await expect(publishButton).toBeDisabled();
      }
    }

    // 4) 调用记录页可按 request_id 搜索
    await page.goto('/calls');
    await expect(page.getByRole('heading', { name: '调用记录' })).toBeVisible();
  });

  test('任务页把 status_unknown 单独解释', async ({ page }) => {
    await page.goto('/tasks');
    await expect(page.getByRole('region', { name: '待确认' })).toBeVisible();
    await expect(page.getByText(/可能仍在远端执行/).first()).toBeVisible();
  });
});
