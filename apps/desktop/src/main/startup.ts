/**
 * 启动状态机（spec §2.5）——唯一推进点。
 *
 * 与 §2.5 的表格逐项一致；本文件刻意把「怎么 spawn / 怎么探活」抽成注入的 hook，
 * 这样 `tests/startup.test.ts` 可以在不启动任何进程的前提下断言每一条转移与错误码。
 */
import {
  API_READY_TIMEOUT_MS,
  BOOTSTRAP_TIMEOUT_MS,
  ERROR,
  GATEWAY_READY_TIMEOUT_MS,
  RESTART_BACKOFF_MS,
  SIDECAR_EXIT_MIGRATION_MISMATCH,
  WEB_READY_TIMEOUT_MS,
} from '../shared/constants';
import { DesktopError } from '../shared/types';
import type { ServiceName, StartupState } from '../shared/types';

export interface StartupPorts {
  api: number;
  web: number;
  gateway: number;
}

export interface StartupHooks {
  /** `preparing`：目录树 / desktop.json / 端口契约 / 产物校验（`--dev` 跳过产物校验）。 */
  prepare: () => Promise<void>;
  /** `porting`：api 占用检测 + web / gateway 端口扫描。抛 `DesktopError`（E-D01 / W-D06）。 */
  resolvePorts: () => Promise<StartupPorts>;
  /** 端口确定后把 runner 绑到这套端口上（services.ts 的 spec 用它合成 env）。 */
  configure: (ports: StartupPorts) => void;
  /** `bootstrapping`：跑 `--role bootstrap`，返回退出码与 stdout 摘要。 */
  bootstrap: () => Promise<{ code: number; output: string }>;
  start: (name: ServiceName) => Promise<void>;
  waitReady: (name: ServiceName, timeoutMs: number) => Promise<{ ok: boolean; detail: string }>;
  /** 首次运行摘要落盘（`<data>\desktop-first-run.json`）。 */
  onFirstRun?: (output: string) => void;
  /** 降级（网关起不来）：只记录，不阻塞控制台。 */
  onDegraded?: (code: string, detail: string) => void;
  log: (line: string) => void;
  onState: (state: StartupState) => void;
  /** `--dev` 模式跳过产物校验与端口契约（spec §2.5 / P1-15）。 */
  dev?: boolean;
}

export interface StartupResult {
  ok: boolean;
  state: StartupState;
  ports: StartupPorts | null;
  errorCode: string | null;
  detail: string;
  startedAt: string;
  readyAt: string | null;
}

/** 崩溃后的重启策略（§2.10）：返回等待毫秒数；`null` 表示不再重启。 */
export function restartDelay(name: ServiceName, attempt: number): number | null {
  const maxAttempts = name === 'gateway' ? 1 : name === 'worker' ? 2 : 3;
  if (attempt >= maxAttempts) return null;
  const delay = RESTART_BACKOFF_MS[Math.min(attempt, RESTART_BACKOFF_MS.length - 1)];
  return delay ?? null;
}

export async function runStartup(hooks: StartupHooks): Promise<StartupResult> {
  const startedAt = new Date().toISOString();
  let state: StartupState = 'idle';
  let ports: StartupPorts | null = null;
  let errorCode: string | null = null;
  let detail = '';

  const move = (next: StartupState, extra = ''): void => {
    state = next;
    hooks.onState(next);
    hooks.log(`state=${next}${extra}`);
  };

  const fail = (code: string, why: string): StartupResult => {
    errorCode = code;
    detail = why;
    move('failed');
    hooks.log(`error=${code} detail=${why}`);
    return { ok: false, state, ports, errorCode, detail, startedAt, readyAt: null };
  };

  move('idle');

  // idle → preparing
  move('preparing');
  try {
    await hooks.prepare();
  } catch (err) {
    if (err instanceof DesktopError) return fail(err.code, err.detail || err.message);
    return fail(ERROR.RESOURCE_MISSING, String(err instanceof Error ? err.message : err));
  }

  // preparing → porting
  move('porting');
  try {
    ports = await hooks.resolvePorts();
    hooks.configure(ports);
    hooks.log(`ports={"api":${ports.api},"gateway":${ports.gateway},"web":${ports.web}}`);
  } catch (err) {
    if (err instanceof DesktopError) return fail(err.code, err.detail || err.message);
    return fail(ERROR.PORT_IN_USE, String(err instanceof Error ? err.message : err));
  }

  // porting → bootstrapping
  move('bootstrapping');
  const bootstrap = await hooks.bootstrap();
  if (bootstrap.code === SIDECAR_EXIT_MIGRATION_MISMATCH) {
    return fail(
      ERROR.BOOTSTRAP_FAILED,
      `sidecar 退出码 ${SIDECAR_EXIT_MIGRATION_MISMATCH}（迁移硬校验失败，见 launcher.py _migration_problem）`,
    );
  }
  if (bootstrap.code !== 0) {
    return fail(ERROR.BOOTSTRAP_FAILED, `bootstrap 退出码 ${bootstrap.code}（上限 ${BOOTSTRAP_TIMEOUT_MS} ms）`);
  }
  hooks.onFirstRun?.(bootstrap.output);

  // bootstrapping → starting_api
  move('starting_api');
  await hooks.start('api');
  const apiReady = await hooks.waitReady('api', API_READY_TIMEOUT_MS);
  if (!apiReady.ok) return fail(ERROR.API_TIMEOUT, apiReady.detail);

  // starting_api → starting_worker
  move('starting_worker');
  await hooks.start('worker');

  // starting_worker → starting_gateway
  move('starting_gateway');
  await hooks.start('gateway');
  const gwReady = await hooks.waitReady('gateway', GATEWAY_READY_TIMEOUT_MS);
  if (!gwReady.ok) {
    hooks.onDegraded?.(ERROR.GATEWAY_DOWN, gwReady.detail);
    hooks.log(`error=${ERROR.GATEWAY_DOWN} detail=${gwReady.detail}`);
  }

  // starting_gateway → starting_web
  move('starting_web');
  await hooks.start('web');
  const webReady = await hooks.waitReady('web', WEB_READY_TIMEOUT_MS);
  if (!webReady.ok) return fail(ERROR.WEB_TIMEOUT, webReady.detail);

  move('ready', ` ports={"api":${ports.api},"gateway":${ports.gateway},"web":${ports.web}}`);
  const readyAt = new Date().toISOString();
  return { ok: true, state, ports, errorCode: null, detail: 'ready', startedAt, readyAt };
}

/** 供 `--version` / 诊断使用：状态机期望的下一步（避免调用方各自维护一份表）。 */
export const TRANSITIONS: Record<StartupState, readonly StartupState[]> = {
  idle: ['preparing'],
  preparing: ['porting', 'failed'],
  porting: ['bootstrapping', 'failed'],
  bootstrapping: ['starting_api', 'failed'],
  starting_api: ['starting_worker', 'failed'],
  starting_worker: ['starting_gateway', 'failed'],
  starting_gateway: ['starting_web', 'failed'],
  starting_web: ['ready', 'failed'],
  ready: [],
  failed: ['preparing'],
};
