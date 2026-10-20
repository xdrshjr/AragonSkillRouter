/**
 * `--smoke-test` 的实现（spec §4.4）。
 *
 * 分工（P1-10）：`checks[]` 只放**被测进程运行时能自证**的项；
 * 「退出后有没有孤儿进程」由外壳 `scripts/smoke.mjs` 用 `shellChecks` 回填 —— 让进程自证
 * 「我死后没有孤儿」在因果上不成立，那是最脆的假证据。
 */
import fs from 'node:fs';
import path from 'node:path';

import { SMOKE_REPORT_FILENAME } from '../shared/constants';
import type { SmokeCheck, SmokeReport } from '../shared/types';

export interface SmokeContext {
  appVersion: string;
  electronVersion: string;
  /** 进程启动时刻（`main.ts` 模块加载时取的），用于报告里的 `startedAt` / `durationMs`。 */
  startedAt: string;
  ports: { api: number | null; gateway: number | null; web: number | null };
  /** `routes-manifest.json` 里 `/api/:path*` 烧死的端口（端口契约的第三方，§2.3.1）。 */
  manifestPort: number | null;
  dataDir: string;
  resources: { sidecar: string; web: string; gateway: string };
  /** 静态部分必判：网关 env 里的 SKILLROUTER_INTERNAL_URL（P0-1）。 */
  gatewayInternalUrl: string;
  probe: (url: string, init?: RequestInit) => Promise<{ status: number; body: string; headers: Headers }>;
  /** 单实例锁复查：拉起第二个实例并断言它立刻以 0 退出、不启动任何服务。 */
  checkSingleInstance: () => Promise<{ ok: boolean; detail: string }>;
  /** `--role bootstrap` 的真实退出码（`null` = 没跑到 / 拿不到）。 */
  bootstrapExitCode: number | null;
  migrationMode: string;
  adminCreated: boolean;
  apiSchemaVersion: number | null;
}

function parseJson(text: string): Record<string, unknown> | null {
  try {
    const value = JSON.parse(text) as unknown;
    return value !== null && typeof value === 'object' ? (value as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

/** 组装并落盘报告；返回退出码（0 全部通过 / 1 有失败）。 */
export async function runSmoke(ctx: SmokeContext, outPath: string): Promise<{ code: number; report: SmokeReport }> {
  const startedAt = ctx.startedAt;
  const t0 = Date.parse(startedAt);
  const checks: SmokeCheck[] = [];

  const push = (c: SmokeCheck): void => {
    checks.push(c);
  };

  // 1) 资源存在性
  const resourcePaths = [ctx.resources.sidecar, ctx.resources.web, ctx.resources.gateway];
  const missing = resourcePaths.filter((p) => !fs.existsSync(p));
  push({ name: 'resources_present', ok: missing.length === 0, missing });

  // 2) 端口契约（运行期那一半，§2.3.1）：desktop.json 的 apiPort 与 routes-manifest 烧死值必须相等。
  //    只能比「读到的那两个值」—— 读不到 manifest 时 honest 地判 false，不冒充已验证。
  push({
    name: 'port_contract',
    ok: ctx.ports.api !== null && ctx.manifestPort !== null && ctx.manifestPort === ctx.ports.api,
    apiPort: ctx.ports.api,
    manifestPort: ctx.manifestPort,
  });

  // 3) bootstrap（P0-3 的迁移硬校验就在这条路径里：退出码 21 会让状态机停在 E-D03）
  push({
    name: 'sidecar_bootstrap',
    ok: ctx.bootstrapExitCode === 0 && ctx.migrationMode !== 'unknown',
    code: ctx.bootstrapExitCode,
    migrationMode: ctx.migrationMode,
    adminCreated: ctx.adminCreated,
  });

  // 4) api /healthz
  let apiOk = false;
  let schemaVersion: number | null = ctx.apiSchemaVersion;
  if (ctx.ports.api !== null) {
    try {
      const res = await ctx.probe(`http://127.0.0.1:${ctx.ports.api}/healthz`);
      const body = parseJson(res.body);
      schemaVersion = typeof body?.db === 'object' && body.db !== null ? Number((body.db as { schema_version?: number }).schema_version) : schemaVersion;
      apiOk = res.status === 200 && body?.ok === true && body?.initialized === true;
      push({
        name: 'api_healthz',
        ok: apiOk,
        status: res.status,
        schemaVersion,
        initialized: body?.initialized === true,
      });
    } catch (err) {
      push({ name: 'api_healthz', ok: false, error: String(err) });
    }
  } else {
    push({ name: 'api_healthz', ok: false, error: 'no api port' });
  }

  // 5) 网关 /healthz
  let gatewayOk = false;
  if (ctx.ports.gateway !== null) {
    try {
      const res = await ctx.probe(`http://127.0.0.1:${ctx.ports.gateway}/healthz`);
      gatewayOk = res.status === 200;
      push({ name: 'gateway_healthz', ok: gatewayOk, status: res.status });
    } catch (err) {
      push({ name: 'gateway_healthz', ok: false, error: String(err) });
    }
  } else {
    push({ name: 'gateway_healthz', ok: false, error: 'no gateway port' });
  }

  // 6) 网关平台回调目标（P0-1 / §4.4）：静态部分必判，动态部分按证据口径标 skipped
  const internalOk = ctx.ports.api !== null && ctx.gatewayInternalUrl === `http://127.0.0.1:${ctx.ports.api}`;
  push({
    name: 'gateway_platform_reach',
    ok: internalOk,
    internalUrl: ctx.gatewayInternalUrl,
    dynamic: 'skipped',
    reason: 'engine_not_configured',
  });

  // 7) 控制台登录页
  let webOk = false;
  if (ctx.ports.web !== null) {
    try {
      const res = await ctx.probe(`http://127.0.0.1:${ctx.ports.web}/login`);
      webOk = res.status === 200;
      push({ name: 'web_login_page', ok: webOk, status: res.status });
    } catch (err) {
      push({ name: 'web_login_page', ok: false, error: String(err) });
    }
  } else {
    push({ name: 'web_login_page', ok: false, error: 'no web port' });
  }

  // 8) 反代链路（只探 /healthz 的冒烟会给「绿灯但完全不可用」的假证据）
  if (ctx.ports.web !== null) {
    try {
      const res = await ctx.probe(`http://127.0.0.1:${ctx.ports.web}/api/health`);
      const body = parseJson(res.body);
      push({
        name: 'web_api_proxy',
        ok: res.status === 200 && body?.status === 'ok',
        status: res.status,
        service: typeof body?.service === 'string' ? body.service : undefined,
      });
    } catch (err) {
      push({ name: 'web_api_proxy', ok: false, error: String(err) });
    }
  } else {
    push({ name: 'web_api_proxy', ok: false, error: 'no web port' });
  }

  // 9) 登录闭环（经 Next 反代 → api；Cookie 与 must_change_password 都要看）
  if (ctx.ports.web !== null) {
    try {
      const res = await ctx.probe(`http://127.0.0.1:${ctx.ports.web}/api/admin/auth/login`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ username: 'admin', password: 'adminjr' }),
      });
      const body = parseJson(res.body);
      const cookies = res.headers.get('set-cookie') ?? '';
      const admin = (body?.admin ?? null) as { must_change_password?: boolean } | null;
      const ok = res.status === 200 && /sr_session/i.test(cookies) && /sr_csrf/i.test(cookies);
      push({
        name: 'login',
        ok,
        status: res.status,
        mustChangePassword: admin?.must_change_password === true,
        cookies: /sr_session/i.test(cookies) ? 'sr_session+sr_csrf' : cookies.slice(0, 120),
      });
    } catch (err) {
      push({ name: 'login', ok: false, error: String(err) });
    }
  } else {
    push({ name: 'login', ok: false, error: 'no web port' });
  }

  // 10) 单实例（第二次启动必须以 0 退出且不 spawn 服务）
  const single = await ctx.checkSingleInstance();
  push({ name: 'single_instance', ok: single.ok, detail: single.detail });

  const report: SmokeReport = {
    ok: checks.every((c) => c.ok),
    appVersion: ctx.appVersion,
    electron: ctx.electronVersion,
    startedAt,
    durationMs: Date.now() - t0,
    ports: ctx.ports,
    dataDir: ctx.dataDir,
    resources: ctx.resources,
    externalRuntime: {
      pythonOnPath: process.env.SR_SMOKE_PYTHON_ON_PATH === '1',
      nodeOnPath: process.env.SR_SMOKE_NODE_ON_PATH === '1',
      pathUsed: process.env.PATH ?? '',
    },
    checks,
    shellChecks: {
      note: '以下由 scripts/smoke.mjs 在被测进程退出后执行并回填（P1-10）',
    },
  };

  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  fs.writeFileSync(outPath, `${JSON.stringify(report, null, 2)}\n`, 'utf8');
  // 报告同时打到 stdout，便于 CI 直接归档
  process.stdout.write(`${JSON.stringify({ smokeReportPath: outPath, report }, null, 2)}\n`);
  return { code: report.ok ? 0 : 1, report };
}

export function defaultSmokeOutPath(logsDir: string): string {
  return path.join(logsDir, SMOKE_REPORT_FILENAME);
}
