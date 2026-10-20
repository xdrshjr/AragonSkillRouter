import { expect, test, type Page } from '@playwright/test';

/**
 * 模型服务（`/settings/agent`）端到端用例（spec §7.2 E-05 / §3.8）。
 *
 * 设计要点：
 * - 4 条用例 + 1 条暗色断言（R-21）；
 * - 全部用 `page.route()` 拦截 `/api/admin/llm/**`、`/api/admin/agent/config`、
 *   `/api/admin/credentials`，所以**不需要真实上游、也不需要已保存的凭证**；
 * - 登录仍然是真的：没有 `SR_E2E_ADMIN_USER / SR_E2E_ADMIN_PASSWORD` 时整组 skip
 *   （与 `core-loop.spec.ts` / `agent-panel.spec.ts` 的约定一致），不伪造成功。
 */

const USERNAME = process.env.SR_E2E_ADMIN_USER;
const PASSWORD = process.env.SR_E2E_ADMIN_PASSWORD;

/* ── 假数据：形状严格照 spec §5.1 / §3.4 / §5.3 / §5.5 ── */

const PROTOCOLS = {
  items: [
    {
      key: 'openai',
      label: 'OpenAI',
      label_zh: 'OpenAI',
      one_liner: 'Chat Completions 与 /models',
      wire: 'openai',
      default_base_url: 'https://api.openai.com/v1',
      base_url_hint: 'https://api.openai.com/v1（含 /v1）',
      docs_url: 'https://platform.openai.com/docs',
      auth_styles: ['bearer', 'api_key', 'header'],
      default_auth_style: 'bearer',
      models_path: 'models',
      chat_path: 'chat/completions',
      supports_models_list: true,
    },
    {
      key: 'anthropic',
      label: 'Anthropic',
      label_zh: 'Anthropic',
      one_liner: 'Messages API（Claude 家族）',
      wire: 'anthropic',
      default_base_url: 'https://api.anthropic.com',
      base_url_hint: 'https://api.anthropic.com（不要带 /v1）',
      docs_url: 'https://docs.anthropic.com',
      auth_styles: ['api_key'],
      default_auth_style: 'api_key',
      models_path: '/v1/models',
      chat_path: '/v1/messages',
      supports_models_list: true,
    },
    {
      key: 'google',
      label: 'Google',
      label_zh: 'Google',
      one_liner: 'Gemini generateContent',
      wire: 'google',
      default_base_url: 'https://generativelanguage.googleapis.com',
      base_url_hint: 'https://generativelanguage.googleapis.com（不要带 /v1beta）',
      docs_url: 'https://ai.google.dev/api',
      auth_styles: ['api_key'],
      default_auth_style: 'api_key',
      models_path: '/v1beta/models',
      chat_path: '/v1beta/models/{model}:generateContent',
      supports_models_list: true,
    },
    {
      key: 'custom',
      label: 'Custom',
      label_zh: '自定义',
      one_liner: '任意兼容网关（vLLM / LiteLLM / One-API / 自建）',
      wire: null,
      default_base_url: '',
      base_url_hint: 'https://your-gateway/v1',
      docs_url: '',
      auth_styles: ['bearer', 'api_key', 'basic', 'header', 'none'],
      default_auth_style: 'bearer',
      models_path: null,
      chat_path: null,
      supports_models_list: true,
      paths_inherit_from_wire: true,
      paginate: null,
    },
  ],
  wires: ['openai', 'anthropic', 'google'],
};

const AUTH_STAGE = {
  stage: 'auth',
  ok: true,
  http_status: 200,
  latency_ms: 212,
  detail: 'GET /models → 200，识别到 2 个模型',
  model_count: 2,
};

const COMPLETION_STAGE = {
  stage: 'completion',
  ok: true,
  http_status: 200,
  latency_ms: 640,
  model: 'gpt-4o-mini',
  detail: '模型正常回话',
  text_chars: 2,
};

const MODELS = {
  items: [
    {
      id: 'gpt-4o-mini',
      display_name: 'GPT-4o mini',
      kind: 'chat',
      recommended: true,
      context_window: 128000,
      methods: [],
      created_at: null,
      owned_by: 'openai',
    },
    {
      id: 'gemini-2.5-pro',
      display_name: 'Gemini 2.5 Pro',
      kind: 'chat',
      recommended: false,
      context_window: 1048576,
      methods: [],
      created_at: null,
      owned_by: 'google',
    },
  ],
  count: 2,
  source: 'upstream',
  fetched_at: new Date().toISOString(),
  expires_at: new Date(Date.now() + 600_000).toISOString(),
  truncated: false,
  probe_id: 'llmp_e2e_models',
  notes: [],
  warning: null,
};

const ONE_CREDENTIAL = {
  id: 'crd_e2e_anthropic',
  provider_id: 'prov_e2e_anthropic',
  name: 'anthropic-e2e',
  kind: 'llm',
  auth_type: 'api_key',
  header_name: null,
  secret_mask: 'sk-…-9f2c',
  secret_fingerprint: '9f2c1a2b3c4d',
  meta: { llm_protocol: 'anthropic' },
  meta_redacted_keys: [],
  meta_sealed: false,
  status: 'active',
  expires_at: null,
  last_verified_at: null,
  last_verify_ok: null,
  rotation_of: null,
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
  provider_name: 'Anthropic',
};

/** 失败态：`ok:false`（正常的坏结果，200）与传输层 502 两种都要能渲染 */
type FailureMode = 'none' | 'result-not-ok' | 'transport';

interface StubOptions {
  /** 凭证列表（默认空数组 = 「还没有可用凭证」，草稿流程） */
  credentials?: unknown[];
}

interface StubHandle {
  patchBodies: Record<string, unknown>[];
  setFailure: (mode: FailureMode) => void;
}

function agentConfigBody(overrides: {
  protocol?: string;
  model?: string;
  base_url?: string;
  credential_id?: string | null;
  ready?: boolean;
} = {}): unknown {
  const protocol = overrides.protocol ?? 'openai';
  const wire = protocol === 'custom' ? 'openai' : protocol;
  const model = overrides.model ?? '';
  const credentialId = overrides.credential_id ?? null;
  const ready = overrides.ready ?? false;
  return {
    model: {
      protocol,
      wire: protocol === 'custom' ? 'openai' : null,
      provider: protocol,
      model,
      base_url: overrides.base_url ?? 'https://api.openai.com/v1',
      temperature: 0.2,
      max_tokens: 4096,
      credential_id: credentialId,
    },
    effective: {
      ready,
      protocol,
      wire,
      model: ready ? { providerId: wire, modelId: model, baseUrl: overrides.base_url } : null,
      credential_refs: ready && credentialId ? { [wire]: credentialId } : {},
      credential:
        ready && credentialId
          ? {
              id: credentialId,
              name: 'anthropic-e2e',
              provider_id: 'prov_e2e_anthropic',
              provider_name: 'Anthropic',
              secret_mask: 'sk-…-9f2c',
            }
          : null,
      problems: ready ? [] : ['还没有选择模型'],
      warnings: [],
      notes: [],
    },
    providers: ['openai', 'anthropic', 'google'],
  };
}

/**
 * 桩掉所有 LLM 相关请求：页面因此完全不依赖真实上游与已保存凭证。
 * `/api/admin/auth/me`、`/overview` 等仍走真实服务（登录是真的）。
 */
async function stubLlmApi(page: Page, options: StubOptions = {}): Promise<StubHandle> {
  const patchBodies: Record<string, unknown>[] = [];
  let failure: FailureMode = 'none';

  await page.route(/\/api\/admin\/llm\/protocols/, (route) =>
    route.fulfill({ json: PROTOCOLS }),
  );

  await page.route(/\/api\/admin\/llm\/probes/, (route) =>
    route.fulfill({ json: { items: [], page: 1, page_size: 10, total: 0 } }),
  );

  await page.route(/\/api\/admin\/llm\/models/, async (route) => {
    await route.fulfill({ json: MODELS });
  });

  await page.route(/\/api\/admin\/llm\/test-connection/, async (route) => {
    const request = route.request();
    const body = (request.postDataJSON() ?? {}) as Record<string, unknown>;
    const mode = typeof body.mode === 'string' ? body.mode : 'auth';
    const protocol = typeof body.protocol === 'string' ? body.protocol : 'openai';
    const wire = protocol === 'custom' && typeof body.wire === 'string' ? body.wire : protocol;

    if (failure === 'transport') {
      await route.fulfill({
        status: 502,
        json: {
          error: {
            code: 'UPSTREAM_AUTH',
            message: '上游拒绝鉴权',
            hint: '检查 API key 是否属于该项目，或该 key 是否启用了对应的 API',
            retryable: false,
            request_id: 'req_e2e_502',
            details: {},
          },
        },
      });
      return;
    }

    if (failure === 'result-not-ok') {
      await route.fulfill({
        json: {
          ok: false,
          protocol,
          wire,
          base_url: body.base_url ?? null,
          resolved_url: `${String(body.base_url ?? 'https://api.openai.com/v1')}/models`,
          mode,
          stages: [
            {
              stage: 'auth',
              ok: false,
              http_status: 403,
              latency_ms: 188,
              detail: '{"error":{"code":403,"message":"API key not valid"}}',
            },
          ],
          probe_id: 'llmp_e2e_failed',
          checked_at: new Date().toISOString(),
          error_code: 'UPSTREAM_AUTH',
          hint: '检查 API key 是否属于该项目，或该 key 是否启用了 Generative Language API',
          problems: ['鉴权阶段失败，未发出计费请求'],
          credential: null,
          inferred: false,
          notes: [],
        },
      });
      return;
    }

    const stages =
      mode === 'both' ? [AUTH_STAGE, COMPLETION_STAGE] : mode === 'completion' ? [COMPLETION_STAGE] : [AUTH_STAGE];
    await route.fulfill({
      json: {
        ok: true,
        protocol,
        wire,
        base_url: body.base_url ?? null,
        resolved_url: `${String(body.base_url ?? 'https://api.openai.com/v1')}/models?limit=1000`,
        mode,
        stages,
        probe_id: `llmp_e2e_${mode}`,
        checked_at: new Date().toISOString(),
        problems: [],
        credential: null,
        inferred: false,
        notes: [],
      },
    });
  });

  await page.route(/\/api\/admin\/credentials(\?|$)/, (route) =>
    route.fulfill({
      json: {
        items: options.credentials ?? [],
        page: 1,
        page_size: 100,
        total: (options.credentials ?? []).length,
      },
    }),
  );

  await page.route(/\/api\/admin\/agent\/config$/, async (route) => {
    const request = route.request();
    if (request.method() === 'PATCH') {
      const body = (request.postDataJSON() ?? {}) as Record<string, unknown>;
      patchBodies.push(body);
      await route.fulfill({
        json: agentConfigBody({
          protocol: typeof body.protocol === 'string' ? body.protocol : 'openai',
          model: typeof body.model === 'string' ? body.model : '',
          base_url: typeof body.base_url === 'string' ? body.base_url : 'https://api.openai.com/v1',
          credential_id: typeof body.credential_id === 'string' ? body.credential_id : null,
          ready: true,
        }),
      });
      return;
    }
    await route.fulfill({ json: agentConfigBody() });
  });

  return {
    patchBodies,
    setFailure: (mode: FailureMode) => {
      failure = mode;
    },
  };
}

async function login(page: Page): Promise<void> {
  await page.goto('/login');
  await page.getByLabel('用户名').fill(USERNAME ?? '');
  await page.getByLabel('密码').fill(PASSWORD ?? '');
  await page.getByRole('button', { name: '登录' }).click();
  await page.waitForURL(/^(?!.*\/login).*$/);
}

/** 打开页面并等到六段式里的①出现 */
async function openModelService(page: Page): Promise<void> {
  await page.goto('/settings/agent');
  await expect(page.getByRole('heading', { name: '模型服务' })).toBeVisible();
  await expect(page.getByRole('radiogroup', { name: '协议' }).getByRole('radio')).toHaveCount(4);
}

const tester = (page: Page) => page.getByRole('region', { name: '实时验证' });

test.describe('模型服务：协议 / 草稿测试 / 取模型保存 / 失败态', () => {
  test.skip(!USERNAME || !PASSWORD, '需要 SR_E2E_ADMIN_USER / SR_E2E_ADMIN_PASSWORD（登录是真的，只有上游被桩掉）');

  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test('① 切 4 个协议卡片：地址联动、方向键、切协议清空模型', async ({ page }) => {
    await stubLlmApi(page);
    await openModelService(page);

    const group = page.getByRole('radiogroup', { name: '协议' });
    const openai = group.getByRole('radio', { name: /OpenAI/ });
    const anthropic = group.getByRole('radio', { name: /Anthropic/ });
    const google = group.getByRole('radio', { name: /Google/ });
    const custom = group.getByRole('radio', { name: /自定义/ });
    const address = page.getByLabel('地址');

    // 已保存 openai → 卡片选中且地址是它的默认值
    await expect(openai).toHaveAttribute('aria-checked', 'true');
    await expect(address).toHaveValue('https://api.openai.com/v1');

    // 鼠标点 Anthropic：地址未被用户改过 → 自动跟随新协议默认值
    await anthropic.click();
    await expect(anthropic).toHaveAttribute('aria-checked', 'true');
    await expect(address).toHaveValue('https://api.anthropic.com');
    await expect(page.getByTestId('llm-protocol-badge')).toHaveText('Anthropic');

    // 键盘：焦点在卡片上用方向键切换（roving tabindex）
    await anthropic.focus();
    await page.keyboard.press('ArrowRight');
    await expect(google).toHaveAttribute('aria-checked', 'true');
    await expect(address).toHaveValue('https://generativelanguage.googleapis.com');
    await page.keyboard.press('Home');
    await expect(openai).toHaveAttribute('aria-checked', 'true');

    // 用户改过地址后再切协议：地址保持不动
    await address.fill('https://gw.internal.example/v1');
    await custom.click();
    await expect(custom).toHaveAttribute('aria-checked', 'true');
    await expect(address).toHaveValue('https://gw.internal.example/v1');

    // 切协议同时清空 ④ 的模型清单与已选模型
    await openai.click();
    // ③ 的按钮在 ② 缺必填项时是禁用的（spec §3.8：只有填了凭证或草稿密钥才可测），
    // 所以先给一把草稿密钥，再点「获取模型列表」。
    await page.getByLabel('密钥（草稿）').fill('sk-e2e-draft-secret');
    await tester(page).getByRole('button', { name: '获取模型列表' }).click();
    const modelInput = page.getByRole('combobox', { name: /模型/ });
    await modelInput.click();
    await page.getByRole('option', { name: /GPT-4o mini/ }).click();
    await expect(modelInput).toHaveValue('gpt-4o-mini');

    await anthropic.click();
    await expect(modelInput).toHaveValue('');
  });

  test('② 草稿密钥：测试连接出 auth 阶段，测试生成（计费 1 次）出 completion 阶段，且保存按钮变成跳凭证抽屉', async ({
    page,
  }) => {
    const stub = await stubLlmApi(page, { credentials: [] });
    await openModelService(page);

    // 没有凭证 → 空态给「去创建凭证」
    await expect(page.getByText('还没有可用凭证')).toBeVisible();
    await expect(page.getByRole('button', { name: '去创建凭证', exact: true })).toBeVisible();

    // 草稿来源：填密钥就能测
    await page.getByLabel('密钥（草稿）').fill('sk-e2e-draft-secret');
    await expect(page.getByRole('button', { name: '去创建凭证并保存' })).toBeVisible();
    await expect(page.getByRole('button', { name: '保存', exact: true })).toHaveCount(0);

    // 测试连接（mode=auth，不计费）
    await tester(page).getByRole('button', { name: '测试连接' }).click();
    await expect(tester(page).getByText('鉴权与模型清单')).toBeVisible();
    await expect(tester(page).getByText('HTTP 200 · 212ms')).toBeVisible();
    // auth 之后 completion 行必须写清「未执行」的原因，而不是空白
    await expect(tester(page).getByText(/未执行（点「测试生成/)).toBeVisible();

    // 测试生成（计费 1 次）需要先有一个模型名（spec §3.4：完成态探测必须有 model）
    await tester(page).getByRole('button', { name: '获取模型列表' }).click();
    const modelInput = page.getByRole('combobox', { name: /模型/ });
    await modelInput.click();
    await page.getByRole('option', { name: /GPT-4o mini/ }).click();
    await expect(modelInput).toHaveValue('gpt-4o-mini');

    // 测试生成（计费 1 次）→ completion 阶段真的出现
    await tester(page).getByRole('button', { name: '测试生成（计费 1 次）' }).click();
    await expect(tester(page).getByText(/回话 2 字符/)).toBeVisible();
    await expect(tester(page).getByText('HTTP 200 · 640ms')).toBeVisible();

    // 草稿没有 credential_id ⇒ 禁止 PATCH（§3.3.1 / C-2）
    expect(stub.patchBodies).toHaveLength(0);
    await expect(page.getByRole('button', { name: '去创建凭证并保存' })).toBeVisible();
  });

  test('③ 获取模型 → 选择 → 保存 → 就绪徽标', async ({ page }) => {
    const stub = await stubLlmApi(page, { credentials: [ONE_CREDENTIAL] });
    await openModelService(page);

    // 选已有凭证 → 保存按钮回到「保存」（有 credential_id 才允许 PATCH）
    await page.getByLabel('凭证').click();
    await page.getByRole('option', { name: /anthropic-e2e/ }).click();
    await expect(page.getByRole('button', { name: '保存', exact: true })).toBeVisible();

    // 获取模型列表 → ④ 里有清单
    await tester(page).getByRole('button', { name: '获取模型列表' }).click();
    const modelInput = page.getByRole('combobox', { name: /模型/ });
    await modelInput.click();
    await page.getByRole('option', { name: /GPT-4o mini/ }).click();
    await expect(modelInput).toHaveValue('gpt-4o-mini');

    await page.getByRole('button', { name: '保存', exact: true }).click();

    // PATCH 带的是「先有 credential_id，再保存配置」的顺序
    await expect.poll(() => stub.patchBodies.length).toBe(1);
    expect(stub.patchBodies[0]).toMatchObject({
      protocol: 'openai',
      model: 'gpt-4o-mini',
      credential_id: 'crd_e2e_anthropic',
    });

    // 顶部与 ⑤ 的就绪徽标（用稳定 testid，不依赖 toast / aria-live 内部实现）
    await expect(page.getByTestId('llm-ready-badge')).toContainText('就绪');
    await expect(
      page.getByRole('region', { name: '当前生效引用' }).getByText('就绪', { exact: true }),
    ).toBeVisible();
    await expect(page.getByText(/已保存且就绪/)).toBeVisible();
  });

  test('④ 失败态：整页只有 1 个 role="alert"，且文案含处置建议与 request_id', async ({ page }) => {
    const stub = await stubLlmApi(page, { credentials: [ONE_CREDENTIAL] });
    await openModelService(page);

    await page.getByLabel('凭证').click();
    await page.getByRole('option', { name: /anthropic-e2e/ }).click();

    // 1) 上游 403 → 200 + ok:false（「正常的坏结果」也要能渲染）
    stub.setFailure('result-not-ok');
    await tester(page).getByRole('button', { name: '测试连接' }).click();

    // 「整页只有一个 alert」限定在页面主体（`main`）内：应用外壳自己还有
    // 离线横幅 / 浮窗 Agent 面板的 alert，那不是本页的职责（spec §3.8 是页面级约束）。
    const alerts = page.locator('main').getByRole('alert');
    await expect(alerts).toHaveCount(1);
    await expect(page.getByTestId('llm-alert')).toContainText('UPSTREAM_AUTH');
    await expect(page.getByTestId('llm-alert')).toContainText('检查 API key');
    await expect(page.getByTestId('llm-alert')).toContainText('鉴权阶段失败，未发出计费请求');

    // 2) 传输层 502 → ApiError，仍然只有 1 个 alert，并带 request_id
    stub.setFailure('transport');
    await tester(page).getByRole('button', { name: '测试连接' }).click();
    await expect(page.locator('main').getByRole('alert')).toHaveCount(1);
    const alert = page.getByTestId('llm-alert');
    await expect(alert).toContainText('UPSTREAM_AUTH');
    await expect(alert).toContainText('检查 API key');
    await expect(alert).toContainText('req_e2e_502');
  });
});

test.describe('模型服务：暗色主题', () => {
  test.skip(!USERNAME || !PASSWORD, '需要 SR_E2E_ADMIN_USER / SR_E2E_ADMIN_PASSWORD');

  test('data-theme="dark" 下 ①②③ 仍达标', async ({ page }) => {
    const stub = await stubLlmApi(page, { credentials: [ONE_CREDENTIAL] });
    await login(page);
    // 主题由 localStorage 决定（app/layout.tsx 的首屏脚本读它）
    await page.addInitScript(() => {
      window.localStorage.setItem('sr:theme', 'dark');
    });
    await openModelService(page);

    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
    // 卡片背景来自 --sr-surface：暗色下不能还是浅色 #ffffff
    const cardBackground = await page
      .getByRole('region', { name: '协议' })
      .evaluate((element) => getComputedStyle(element).backgroundColor);
    expect(cardBackground).not.toBe('rgb(255, 255, 255)');

    // ① 切协议 + 地址联动
    const group = page.getByRole('radiogroup', { name: '协议' });
    await group.getByRole('radio', { name: /Anthropic/ }).click();
    await expect(page.getByLabel('地址')).toHaveValue('https://api.anthropic.com');

    // ② 草稿测试出 auth 阶段
    await group.getByRole('radio', { name: /OpenAI/ }).click();
    await page.getByLabel('凭证').click();
    await page.getByRole('option', { name: /草稿/ }).click();
    await page.getByLabel('密钥（草稿）').fill('sk-e2e-dark');
    await tester(page).getByRole('button', { name: '测试连接' }).click();
    await expect(tester(page).getByText('鉴权与模型清单')).toBeVisible();

    // ③ 取模型 → 选择 → 保存 → 就绪
    await page.getByLabel('凭证').click();
    await page.getByRole('option', { name: /anthropic-e2e/ }).click();
    await tester(page).getByRole('button', { name: '获取模型列表' }).click();
    const modelInput = page.getByRole('combobox', { name: /模型/ });
    await modelInput.click();
    await page.getByRole('option', { name: /GPT-4o mini/ }).click();
    await page.getByRole('button', { name: '保存', exact: true }).click();

    await expect.poll(() => stub.patchBodies.length).toBe(1);
    await expect(
      page.getByRole('region', { name: '当前生效引用' }).getByText('就绪', { exact: true }),
    ).toBeVisible();
  });
});
