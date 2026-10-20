/**
 * 进程监管（spec §2.10）。
 *
 * **所有**子进程只能经 `Supervisor.spawn()` 创建，参数固定为：
 *   spawn(exe, args, { cwd, env, stdio: ['ignore','pipe','pipe'], windowsHide: true, detached: false })
 *
 * `windowsHide: true` 是硬要求（P1-9）：sidecar 用 `console=True` 构建（要 stdout 管道），
 * 不隐藏窗口时 Windows 会给 api / worker 各弹一个黑色控制台窗口，「双击可用」直接崩坏。
 * `shell` 必须为 false（R11：路径含空格时不能用 shell 拼串）。
 */
import { execFile, spawn } from 'node:child_process';
import type { ChildProcess } from 'node:child_process';

import { SHUTDOWN_GRACE_MS } from '../shared/constants';
import type { Logger, LogName } from './logger';
import type { ServiceName } from '../shared/types';

export interface SpawnSpec {
  name: ServiceName;
  exe: string;
  args: string[];
  cwd: string;
  env: Record<string, string>;
}

export interface ChildHandle {
  name: ServiceName;
  child: ChildProcess;
  pid: number | null;
  exited: Promise<{ code: number | null; signal: NodeJS.Signals | null }>;
}

export type ExitListener = (name: ServiceName, code: number | null, signal: NodeJS.Signals | null) => void;

export class Supervisor {
  private readonly handles = new Map<ServiceName, ChildHandle>();
  private readonly exitListeners: ExitListener[] = [];
  private shuttingDown = false;

  constructor(private readonly logger: Logger) {}

  onExit(listener: ExitListener): void {
    this.exitListeners.push(listener);
  }

  markShuttingDown(): void {
    this.shuttingDown = true;
  }

  /** 关停结束后（「重启服务」路径）复位，否则退出回调永远不再通知崩溃监控。 */
  resetShuttingDown(): void {
    this.shuttingDown = false;
  }

  isShuttingDown(): boolean {
    return this.shuttingDown;
  }

  /** 唯一 spawn 入口。 */
  spawn(spec: SpawnSpec): ChildHandle {
    const child = spawn(spec.exe, spec.args, {
      cwd: spec.cwd,
      env: spec.env,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
      detached: false,
      shell: false,
    });

    child.stdout?.on('data', (b: Buffer) => this.logger.child(spec.name as LogName, 'stdout', b.toString('utf8')));
    child.stderr?.on('data', (b: Buffer) => this.logger.child(spec.name as LogName, 'stderr', b.toString('utf8')));

    const exited = new Promise<{ code: number | null; signal: NodeJS.Signals | null }>((resolve) => {
      // spawn 失败（exe 被杀软删掉、路径写错）**不会**走 `exit`，只会发 `error`。
      // 不接住它有两个后果：① 主进程未捕获异常 → 整个应用闪退（用户只看到窗口消失）；
      // ② `exited` 永不 resolve → 关停要白等到 grace 超时。因此这里同时收尾两者。
      child.on('error', (err) => {
        this.logger.main(`error=spawn_failed name=${spec.name} exe=${spec.exe} detail=${String(err)}`);
        this.handles.delete(spec.name);
        resolve({ code: null, signal: null });
      });
      child.once('exit', (code, signal) => {
        this.logger.main(`event=child_exit name=${spec.name} pid=${child.pid ?? 0} exit=${String(code)} signal=${String(signal)}`);
        this.handles.delete(spec.name);
        resolve({ code, signal });
        if (!this.shuttingDown) {
          for (const listener of this.exitListeners) listener(spec.name, code, signal);
        }
      });
    });

    const handle: ChildHandle = { name: spec.name, child, pid: child.pid ?? null, exited };
    this.handles.set(spec.name, handle);
    this.logger.main(`event=spawn name=${spec.name} pid=${child.pid ?? 0} exe=${spec.exe}`);
    return handle;
  }

  has(name: ServiceName): boolean {
    return this.handles.has(name);
  }

  /** 逆序关停：先 `kill()`，等 `exit`（最多 graceMs），仍存活则 `taskkill /PID <pid> /T /F`。 */
  async stop(name: ServiceName, graceMs = SHUTDOWN_GRACE_MS): Promise<void> {
    const handle = this.handles.get(name);
    if (handle === undefined) return;

    try {
      handle.child.kill();
    } catch {
      /* ignore */
    }

    const exited = await Promise.race([
      handle.exited.then(() => true),
      new Promise<boolean>((resolve) => setTimeout(() => resolve(false), graceMs)),
    ]);
    if (exited) {
      this.logger.main(`event=stopped name=${name} pid=${handle.pid ?? 0} graceful=true`);
      return;
    }

    if (handle.pid !== null) {
      await taskkillTree(handle.pid);
      this.logger.main(`event=stopped name=${name} pid=${handle.pid} graceful=false`);
    }
  }

  async stopAll(order: ServiceName[] = ['web', 'gateway', 'worker', 'api']): Promise<void> {
    this.markShuttingDown();
    for (const name of order) {
      await this.stop(name);
    }
  }

  /** 供冒烟与排错使用：当前仍在运行的 pid 列表。 */
  runningPids(): number[] {
    return [...this.handles.values()].map((h) => h.pid).filter((p): p is number => p !== null);
  }
}

/** `taskkill /T /F`：**必须** `/T`（网关可能再 spawn 一个 engine 孙进程，P2-5）。 */
export function taskkillTree(pid: number): Promise<void> {
  return new Promise((resolve) => {
    execFile('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true }, () => resolve());
  });
}

/** 轮询探针：每 `intervalMs` 试一次，直到 `predicate` 为真、给出 `fatal`、或超时。 */
export async function pollUntil(
  probe: () => Promise<{ ok: boolean; detail?: string; fatal?: boolean }>,
  timeoutMs: number,
  intervalMs: number,
): Promise<{ ok: boolean; detail: string }> {
  const deadline = Date.now() + timeoutMs;
  let last = 'no attempt';
  while (Date.now() < deadline) {
    try {
      const res = await probe();
      last = res.detail ?? (res.ok ? 'ok' : 'not ready');
      if (res.ok) return { ok: true, detail: last };
      // 子进程已经死掉这类「再等也不会好」的情况立刻返回，不要白等满超时
      if (res.fatal === true) return { ok: false, detail: last };
    } catch (err) {
      last = String(err);
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  return { ok: false, detail: last };
}

/** 最小 HTTP 探针（不引入第三方依赖）。 */
export async function httpProbe(url: string, timeoutMs = 2_000): Promise<{ status: number; body: string }> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, { signal: controller.signal, redirect: 'manual' });
    const body = await res.text();
    return { status: res.status, body };
  } finally {
    clearTimeout(timer);
  }
}
