import * as fs from 'node:fs';
import * as path from 'node:path';

import { expect, test } from '@playwright/test';

/**
 * 停止（StopReport）真实闭环：发一轮**长任务**，在它跑的时候按「停止」，
 * 断言界面出现停止报告 + external_pending 说明 + 对账入口（spec §7.12-5 / §13.3）。
 *
 * ```cmd
 * set SR_E2E_ADMIN_USER=admin
 * set SR_E2E_ADMIN_PASSWORD=<密码>
 * set SR_LIVE_AGENT=1
 * npx playwright test e2e/agent-stop.spec.ts --headed
 * ```
 */

const USERNAME = process.env.SR_E2E_ADMIN_USER;
const PASSWORD = process.env.SR_E2E_ADMIN_PASSWORD;
const LIVE = process.env.SR_LIVE_AGENT === '1';
const SLOWMO = Number.parseInt(process.env.SR_E2E_SLOWMO ?? '0', 10) || 0;
const HOLD_MS = Number.parseInt(process.env.SR_E2E_HOLD_MS ?? '20000', 10) || 0;
const SHOT_DIR = process.env.SR_SHOT_DIR ?? path.resolve(process.cwd(), '..', '..', 'var', 'dev', 'shots');

test.use({ launchOptions: SLOWMO > 0 ? { slowMo: SLOWMO } : {} });

fs.mkdirSync(SHOT_DIR, { recursive: true });
let index = 0;
async function shot(page: import('@playwright/test').Page, name: string): Promise<void> {
  index += 1;
  const file = path.join(SHOT_DIR, `stop-${String(index).padStart(2, '0')}-${name}.png`);
  await page.screenshot({ path: file, fullPage: true });
  console.log(`[shot] ${file}`);
}

test.describe('Agent 停止（有头）', () => {
  test.skip(!USERNAME || !PASSWORD, '需要 SR_E2E_ADMIN_USER / SR_E2E_ADMIN_PASSWORD');
  test.skip(!LIVE, '需要 SR_LIVE_AGENT=1（真实模型）');
  test.setTimeout(300_000);

  test('长任务按停止 → 出现 StopReport 与对账入口', async ({ page }) => {
    await page.goto('/login');
    await page.getByLabel('用户名').fill(USERNAME ?? '');
    await page.getByLabel('密码').fill(PASSWORD ?? '');
    await page.getByRole('button', { name: '登录' }).click();
    await page.waitForURL(/^(?!.*\/login).*$/);

    await page.goto('/agent');
    await expect(page.getByRole('heading', { name: 'Agent 工作台' })).toBeVisible({ timeout: 120_000 });
    await page.getByRole('button', { name: '新建会话' }).click();

    // 让模型连跑多轮工具：这样才有充足的时间在中间按停止
    await page
      .getByLabel('给 Agent 的消息')
      .fill(
        '请依次调用 sr_capabilities_list、sr_capability_get（对每个能力都要调用一次），' +
          '每调用一个工具就汇报一次进度，不要提前结束。',
      );
    await shot(page, 'prompt');
    await page.getByRole('button', { name: '发送消息' }).click();

    // 等到真的开始执行（徽标「执行中」）再按停止
    // 徽标文案恰好是「执行中」；输入框提示是「执行中，可继续输入…」→ 必须精确匹配
    await expect(page.getByText('执行中', { exact: true })).toBeVisible({ timeout: 60_000 });
    await page.waitForTimeout(2_500);
    await shot(page, 'running');

    await page.getByRole('button', { name: '停止' }).click();
    await shot(page, 'stopped');

    // StopReport 区块：已停止 + 可能仍在远端运行 + 去任务对账
    await expect(page.getByText(/已停止/)).toBeVisible({ timeout: 60_000 });
    await expect(page.getByText(/可能仍在远端运行|去任务里对账/)).toBeVisible();
    const report = await page.locator('div').filter({ hasText: /已停止：/ }).first().innerText();
    console.log(`[stop-report] ${report.replace(/\s+/g, ' ').slice(0, 300)}`);

    // 停止后必须回到非执行中（不能卡在「执行中」）
    await expect(page.getByText('执行中', { exact: true })).toHaveCount(0, { timeout: 60_000 });
    await shot(page, 'idle-after-stop');

    if (HOLD_MS > 0) await page.waitForTimeout(HOLD_MS);
  });
});
