import * as fs from 'node:fs';
import * as path from 'node:path';

import { expect, test } from '@playwright/test';

/**
 * 真实联调（有头）：配置 Agent 模型 → 保存 → 测试连通性 → 让 Agent 跑一轮并调用平台工具。
 *
 * 这是「Agent 到底能不能用」的可复现证据：只有真实后端 + 真实上游凭证才跑得起来，
 * 因此默认 skip，必须显式开启：
 *
 * ```cmd
 * set SR_E2E_ADMIN_USER=admin
 * set SR_E2E_ADMIN_PASSWORD=<已改密的密码>
 * set SR_E2E_CREDENTIAL_MATCH=GLM
 * set SR_LIVE_AGENT=1
 * npx playwright test e2e/agent-live.spec.ts --headed
 * ```
 *
 * 每一步都截图到 `SR_SHOT_DIR`（默认 `var/dev/shots/`），便于复盘「我看到了什么」。
 */

const USERNAME = process.env.SR_E2E_ADMIN_USER;
const PASSWORD = process.env.SR_E2E_ADMIN_PASSWORD;
const LIVE = process.env.SR_LIVE_AGENT === '1';

const PROVIDER = process.env.SR_E2E_AGENT_PROVIDER ?? 'anthropic';
const MODEL = process.env.SR_E2E_AGENT_MODEL ?? 'glm-5.3-flash';
const BASE_URL = process.env.SR_E2E_AGENT_BASE_URL ?? 'https://open.bigmodel.cn/api/anthropic';
const CREDENTIAL_MATCH = process.env.SR_E2E_CREDENTIAL_MATCH ?? '';

const SHOT_DIR = process.env.SR_SHOT_DIR ?? path.resolve(process.cwd(), '..', '..', 'var', 'dev', 'shots');
fs.mkdirSync(SHOT_DIR, { recursive: true });

let shotIndex = 0;
async function shot(page: import('@playwright/test').Page, name: string): Promise<void> {
  shotIndex += 1;
  const file = path.join(SHOT_DIR, `${String(shotIndex).padStart(2, '0')}-${name}.png`);
  await page.screenshot({ path: file, fullPage: true });
  // 有头运行时肉眼可见：控制台同时留一行轨迹
  console.log(`[shot] ${file}`);
}

// 有头运行时的慢动作：默认 0（不拖慢 CI），设 SR_E2E_SLOWMO=400 可以肉眼跟每一步
const SLOWMO = Number.parseInt(process.env.SR_E2E_SLOWMO ?? '0', 10) || 0;
test.use({ launchOptions: SLOWMO > 0 ? { slowMo: SLOWMO } : {} });

// 结束时保持窗口可见的秒数（0 = 立刻关闭）。默认 20s，方便人工看结果。
const HOLD_MS = Number.parseInt(process.env.SR_E2E_HOLD_MS ?? '20000', 10) || 0;

test.describe('Agent 真实闭环（有头）', () => {
  test.afterEach(async ({ page }, testInfo) => {
    if (testInfo.status !== testInfo.expectedStatus) {
      await shot(page, `FAILED-${testInfo.title.slice(0, 20)}`);
      await hold(page);
    }
  });

  test.skip(!USERNAME || !PASSWORD, '需要 SR_E2E_ADMIN_USER / SR_E2E_ADMIN_PASSWORD');
  test.skip(!LIVE, '需要 SR_LIVE_AGENT=1（真实模型与凭证）');
  test.setTimeout(300_000);

  test('配置模型 → 测试连通性 → Agent 调用平台工具', async ({ page }) => {
    // ── 1. 登录 ──────────────────────────────────────────────
    await page.goto('/login');
    await shot(page, 'login');
    await page.getByLabel('用户名').fill(USERNAME ?? '');
    await page.getByLabel('密码').fill(PASSWORD ?? '');
    await page.getByRole('button', { name: '登录' }).click();
    await page.waitForURL(/^(?!.*\/login).*$/);
    await page.waitForLoadState('networkidle');

    // 强制改密（重置密码后会出现）：页面会要求改密，这里如实完成
    if (page.url().includes('change-password')) {
      await shot(page, 'change-password');
      const NEW = process.env.SR_E2E_ADMIN_NEW_PASSWORD ?? 'SkillRouter-Live-2026';
      await page.getByLabel('当前密码').fill(PASSWORD ?? '');
      await page.getByLabel('新密码', { exact: true }).fill(NEW);
      await page.getByLabel('确认新密码').fill(NEW);
      await page.getByRole('button', { name: '修改密码' }).click();
      await page.waitForURL(/^(?!.*change-password).*$/);
    }
    await shot(page, 'overview');

    // ── 2. 打开「设置 → Agent 模型」 ─────────────────────────
    await page.goto('/settings/agent');
    // Next dev 首次访问会按需编译，给足冷启动时间（否则断言的是编译速度，不是功能）
    await expect(page.getByRole('heading', { name: 'Agent 模型' })).toBeVisible({ timeout: 120_000 });
    await shot(page, 'agent-model-page');

    // 协议族
    await page.getByLabel('协议族').click();
    await page.getByRole('option', { name: new RegExp(`^${PROVIDER}`) }).click();
    // 模型名 / 上游地址
    await page.getByLabel('模型名').fill(MODEL);
    await page.getByLabel('上游地址').fill(BASE_URL);

    // 凭证：按名字挑一个（真实凭证由 API 上传，UI 只做选择）
    await page.getByLabel('凭证').click();
    if (CREDENTIAL_MATCH) {
      await page.getByRole('option', { name: new RegExp(CREDENTIAL_MATCH) }).click();
    } else {
      await page.getByRole('option', { name: /自动选择/ }).click();
    }
    await shot(page, 'agent-model-filled');

    await page.getByRole('button', { name: '保存' }).click();
    // 保存后必须变成「就绪」
    await expect(page.getByText('就绪', { exact: true })).toBeVisible({ timeout: 20_000 });
    await shot(page, 'agent-model-saved');

    // ── 3. 测试连通性（真实请求上游模型）─────────────────────
    await page.getByRole('button', { name: '测试连通性' }).click();
    const result = page.getByRole('region', { name: '连通性测试结果' });
    await expect(result).toBeVisible();
    // 只认「真实结果」：Badge 变成 通过/失败/未发出请求，且出现 HTTP 状态行
    await expect(result.getByText(/^(通过|失败|未发出请求)$/)).toBeVisible({ timeout: 120_000 });
    await expect(result.getByText('HTTP 状态', { exact: true })).toBeVisible();
    await expect(result.getByText('模型正常回话')).toBeVisible();
    await shot(page, 'connectivity-result');
    const resultText = (await result.innerText()).replace(/\s+/g, ' ');
    console.log(`[connectivity] ${resultText.slice(0, 400)}`);
    expect(resultText).not.toContain('还没有测试记录');

    // ── 4. Agent 工作台：真的跑一轮并要求调用平台工具 ───────
    await page.goto('/agent');
    await expect(page.getByRole('heading', { name: 'Agent 工作台' })).toBeVisible({ timeout: 120_000 });
    await shot(page, 'agent-workbench');

    await page.getByRole('button', { name: '新建会话' }).click();
    const composer = page.getByLabel('给 Agent 的消息');
    await composer.fill(
      '请调用 sr_capabilities_list 工具（limit=5），然后用一句话说明：工具是否可用、返回了几条能力。' +
        '如果工具报错，把错误原文贴出来。',
    );
    await shot(page, 'agent-prompt-typed');
    await page.getByRole('button', { name: '发送消息' }).click();

    // 4a. 工具调用落到「进度时间线」（注意：不能只找文本 sr_capabilities_list，
    //     用户提示词里也有这个词，会假通过）
    const timeline = page.getByRole('complementary', { name: '接入流程' });
    await expect(timeline.getByText('sr_capabilities_list').first()).toBeVisible({ timeout: 180_000 });
    await shot(page, 'agent-tool-called');

    // 4b. 有模型产出（流式文本）
    const transcriptRegion = page.getByRole('region', { name: '对话' });
    // 助手气泡由 assistant.delta 流式累加；这里断言「有 Agent 角色且内容非空」
    await expect(transcriptRegion.getByText('Agent', { exact: true }).last()).toBeVisible({ timeout: 180_000 });
    await expect(transcriptRegion.getByText('生成中')).toHaveCount(0, { timeout: 180_000 });

    // 4c. 回到空闲（轮次收尾，不会永久「执行中」）
    await expect(page.getByText('空闲')).toBeVisible({ timeout: 180_000 });
    await shot(page, 'agent-turn-finished');

    const transcript = await page.getByRole('region', { name: '对话' }).innerText();
    console.log(`[transcript]\n${transcript.slice(0, 1500)}`);
    expect(transcript).not.toContain('本轮执行失败');

    await hold(page);
  });
});

/** 让窗口留一会儿：失败的场景下也一样（便于看清卡在哪一步）。 */
async function hold(page: import('@playwright/test').Page): Promise<void> {
  if (HOLD_MS <= 0) return;
  console.log(`[hold] 窗口保留 ${HOLD_MS}ms，可肉眼查看当前页面：${page.url()}`);
  await page.waitForTimeout(HOLD_MS);
}
