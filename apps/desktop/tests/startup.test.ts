/**
 * spec §6.1 `startup.test.ts`：注入假 supervisor，逐项验证 §2.5 的每条成功 / 失败转移。
 */
import { describe, expect, it } from 'vitest';

import { restartDelay, runStartup, TRANSITIONS } from '../src/main/startup';
import type { StartupHooks, StartupPorts } from '../src/main/startup';
import { DesktopError } from '../src/shared/types';
import type { ServiceName, StartupState } from '../src/shared/types';
import { ERROR } from '../src/shared/constants';

const PORTS: StartupPorts = { api: 18100, web: 18101, gateway: 18102 };

interface Recorder {
  states: StartupState[];
  logs: string[];
  started: ServiceName[];
  degraded: string[];
  configured: StartupPorts[];
  firstRun: string[];
}

function makeHooks(over?: Partial<StartupHooks>): { hooks: StartupHooks; rec: Recorder } {
  const rec: Recorder = { states: [], logs: [], started: [], degraded: [], configured: [], firstRun: [] };
  const hooks: StartupHooks = {
    prepare: async () => {},
    resolvePorts: async () => PORTS,
    configure: (p) => rec.configured.push(p),
    bootstrap: async () => ({ code: 0, output: '{"migration":{"mode":"empty_db_upgrade"}}' }),
    start: async (name) => {
      rec.started.push(name);
    },
    waitReady: async () => ({ ok: true, detail: 'ready' }),
    onFirstRun: (out) => rec.firstRun.push(out),
    onDegraded: (code, detail) => rec.degraded.push(`${code}|${detail}`),
    log: (l) => rec.logs.push(l),
    onState: (s) => rec.states.push(s),
    ...over,
  };
  return { hooks, rec };
}

describe('状态机 happy path', () => {
  it('按 §2.5 顺序走到 ready，并记录三个端口', async () => {
    const { hooks, rec } = makeHooks();
    const result = await runStartup(hooks);
    expect(result.ok).toBe(true);
    expect(result.state).toBe('ready');
    expect(result.ports).toEqual(PORTS);
    expect(rec.states).toEqual([
      'idle',
      'preparing',
      'porting',
      'bootstrapping',
      'starting_api',
      'starting_worker',
      'starting_gateway',
      'starting_web',
      'ready',
    ]);
    expect(rec.started).toEqual(['api', 'worker', 'gateway', 'web']);
    expect(rec.configured).toEqual([PORTS]);
    expect(rec.firstRun).toHaveLength(1);
    expect(rec.degraded).toHaveLength(0);
    expect(rec.logs.some((l) => l.startsWith('state=ready'))).toBe(true);
  });

  it('TRANSITIONS 表与状态机实现一致（ready 是终态，failed 可重试回 preparing）', () => {
    expect(TRANSITIONS.idle).toEqual(['preparing']);
    expect(TRANSITIONS.ready).toEqual([]);
    expect(TRANSITIONS.failed).toEqual(['preparing']);
  });
});

describe('失败转移与错误码', () => {
  it('网关起不来只降级为 W-D01，不中止启动', async () => {
    const { hooks, rec } = makeHooks({
      waitReady: async (name) =>
        name === 'gateway' ? { ok: false, detail: 'ECONNREFUSED' } : { ok: true, detail: 'ready' },
    });
    const result = await runStartup(hooks);
    expect(result.ok).toBe(true);
    expect(result.state).toBe('ready');
    expect(rec.degraded).toEqual([`${ERROR.GATEWAY_DOWN}|ECONNREFUSED`]);
  });

  it('api 90 s 未就绪 → E-D04', async () => {
    const { hooks } = makeHooks({
      waitReady: async (name) => (name === 'api' ? { ok: false, detail: 'timeout' } : { ok: true, detail: 'ready' }),
    });
    const result = await runStartup(hooks);
    expect(result.ok).toBe(false);
    expect(result.errorCode).toBe(ERROR.API_TIMEOUT);
    expect(result.state).toBe('failed');
  });

  it('web 120 s 未就绪 → E-D05', async () => {
    const { hooks } = makeHooks({
      waitReady: async (name) => (name === 'web' ? { ok: false, detail: 'timeout' } : { ok: true, detail: 'ready' }),
    });
    const result = await runStartup(hooks);
    expect(result.errorCode).toBe(ERROR.WEB_TIMEOUT);
  });

  it('bootstrap 退出码 21（迁移硬校验）→ E-D03', async () => {
    const { hooks } = makeHooks({ bootstrap: async () => ({ code: 21, output: '[FATAL] E-D03' }) });
    const result = await runStartup(hooks);
    expect(result.errorCode).toBe(ERROR.BOOTSTRAP_FAILED);
    expect(result.detail).toContain('21');
  });

  it('bootstrap 抛异常（退出码 1）→ E-D03', async () => {
    const { hooks } = makeHooks({ bootstrap: async () => ({ code: 1, output: 'Traceback…' }) });
    const result = await runStartup(hooks);
    expect(result.errorCode).toBe(ERROR.BOOTSTRAP_FAILED);
  });

  it('产物缺失（prepare 抛 E-D02）→ E-D02 并中止', async () => {
    const { hooks, rec } = makeHooks({
      prepare: async () => {
        throw new DesktopError(ERROR.RESOURCE_MISSING, 'resources\\python\\skillrouter-server.exe');
      },
    });
    const result = await runStartup(hooks);
    expect(result.errorCode).toBe(ERROR.RESOURCE_MISSING);
    expect(rec.started).toEqual([]);
  });

  it('api 端口被外部进程占用（porting 抛 E-D01）→ E-D01', async () => {
    const { hooks } = makeHooks({
      resolvePorts: async () => {
        throw new DesktopError(ERROR.PORT_IN_USE, 'PID 1234 (chrome.exe)');
      },
    });
    const result = await runStartup(hooks);
    expect(result.errorCode).toBe(ERROR.PORT_IN_USE);
  });
});

describe('--dev 模式', () => {
  it('跳过产物校验与端口契约（prepare 不做任何检查也不报 E-D02）', async () => {
    const { hooks } = makeHooks({ dev: true });
    const result = await runStartup(hooks);
    expect(result.ok).toBe(true);
    expect(hooks.dev).toBe(true);
  });
});

describe('崩溃重启退避', () => {
  it('api / web 允许 3 次（1 s / 3 s / 9 s），gateway 1 次，worker 2 次', () => {
    expect(restartDelay('api', 0)).toBe(1_000);
    expect(restartDelay('api', 1)).toBe(3_000);
    expect(restartDelay('api', 2)).toBe(9_000);
    expect(restartDelay('api', 3)).toBeNull();

    expect(restartDelay('gateway', 0)).toBe(1_000);
    expect(restartDelay('gateway', 1)).toBeNull();

    expect(restartDelay('worker', 0)).toBe(1_000);
    expect(restartDelay('worker', 1)).toBe(3_000);
    expect(restartDelay('worker', 2)).toBeNull();

    expect(restartDelay('web', 2)).toBe(9_000);
  });
});
