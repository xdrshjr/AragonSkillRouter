/**
 * Supervision of the three processes the desktop app owns:
 *
 *   skillrouter-backend   FastAPI API + durable worker   (PyInstaller build)
 *   node main.js          AragonAgent sidecar            (@aragon-agent/core)
 *   node server.js        Next.js standalone UI server
 *
 * Design rules that matter for reliability:
 *
 *  * Every port is chosen by binding to port 0 first, so a second instance or an
 *    unrelated service on 8000/3000 cannot collide. The chosen ports are passed
 *    to the children and to the caller (the shell proxy needs them).
 *  * The shell port is reserved first because the backend must trust that exact
 *    origin for cookie + CSRF checks (`SR_TRUSTED_ORIGINS`).
 *  * Secrets (the sidecar token) travel through the environment, never through a
 *    command line, which other local users can read.
 *  * Health is polled, not assumed. A child that dies is reported with its log
 *    path; restarts are bounded, because a process that dies three times in a
 *    minute is broken rather than unlucky.
 *  * The agent engine is optional: if the sidecar cannot start, published
 *    capabilities still work (that is the product's stated guarantee).
 */

import { ChildProcess, spawn } from 'node:child_process';
import { createWriteStream, existsSync } from 'node:fs';
import { createServer } from 'node:net';
import path from 'node:path';

import type { DesktopPaths } from './paths';

export interface LogSink {
  info(message: string, meta?: unknown): void;
  warn(message: string, meta?: unknown): void;
  error(message: string, meta?: unknown): void;
}

export interface ManagedProcess {
  name: string;
  pid(): number | null;
  running(): boolean;
  stop(reason: string): Promise<void>;
}

export interface HealthReport {
  ok: boolean;
  backendReady: boolean;
  webReady: boolean;
  sidecarReady: boolean;
  detail: string;
  backendPort: number;
  webPort: number;
  sidecarPort: number;
  shellPort: number;
}

const READY_TIMEOUT_MS = 60_000;
const STOP_GRACE_MS = 6_000;

/** Ask the OS for a free TCP port on loopback. */
export function findFreePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.unref();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      if (address && typeof address === 'object') {
        const { port } = address;
        server.close(() => resolve(port));
      } else {
        server.close(() => reject(new Error('could not determine a free port')));
      }
    });
  });
}

/**
 * Is a specific port free on loopback?
 *
 * Used to keep the previous ports across restarts. An agent token is installed into
 * another program with the platform URL baked in (`skill-router-connect install
 * --url http://127.0.0.1:<shell>`), so a port that changes on every launch silently
 * breaks that installation. Reusing the last ports makes the URL durable; if one is
 * taken by something else the caller falls back to an arbitrary free port.
 */
export function isPortFree(port: number): Promise<boolean> {
  if (!Number.isInteger(port) || port < 1024 || port > 65535) return Promise.resolve(false);
  return new Promise((resolve) => {
    const server = createServer();
    server.unref();
    server.once('error', () => resolve(false));
    server.listen(port, '127.0.0.1', () => {
      server.close(() => resolve(true));
    });
  });
}

/** Prefer `wanted` when it is still free, otherwise take any free port. */
export async function findByPreferredPort(wanted?: number): Promise<number> {
  if (wanted && (await isPortFree(wanted))) return wanted;
  return findFreePort();
}

export async function waitForHttp(
  url: string,
  timeoutMs: number,
  shouldAbort?: () => string | null,
  acceptStatus?: (status: number) => boolean,
): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const abortReason = shouldAbort?.();
    if (abortReason) throw new Error(abortReason);
    try {
      const response = await fetch(url, { redirect: 'error' });
      if (acceptStatus ? acceptStatus(response.status) : response.ok) return true;
    } catch {
      /* connection refused while the process is still booting */
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return false;
}

interface SpawnSpec {
  name: string;
  command: string;
  args: string[];
  cwd: string;
  env: NodeJS.ProcessEnv;
  logFile: string;
  sink: LogSink;
  onExit?: (code: number | null, signal: NodeJS.Signals | null) => void;
}

function spawnManaged(spec: SpawnSpec): ManagedProcess {
  const out = createWriteStream(spec.logFile, { flags: 'a' });
  const stamp = () => new Date().toISOString();
  out.write(`\n===== ${stamp()} starting ${spec.name}: ${spec.command} ${spec.args.join(' ')}\n`);

  const child: ChildProcess = spawn(spec.command, spec.args, {
    cwd: spec.cwd,
    env: spec.env,
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const stdout = child.stdout;
  const stderr = child.stderr;
  if (!stdout || !stderr) {
    throw new Error(`${spec.name}: the child process has no pipes; it cannot be supervised`);
  }

  const forward = (chunk: Buffer): void => {
    out.write(chunk);
    const text = chunk.toString('utf8').trim();
    if (!text) return;
    for (const line of text.split(/\r?\n/).slice(-4)) spec.sink.info(`[${spec.name}] ${line}`);
  };
  stdout.on('data', forward);
  stderr.on('data', forward);

  child.on('error', (error) => {
    out.write(`${stamp()} spawn error: ${error.message}\n`);
    spec.sink.error(`${spec.name} failed to start: ${error.message}`);
  });
  child.on('exit', (code, signal) => {
    out.write(`${stamp()} exited code=${code} signal=${signal}\n`);
    spec.sink.warn(`${spec.name} exited (code=${code ?? 'null'} signal=${signal ?? 'none'})`);
    spec.onExit?.(code, signal);
  });

  return {
    name: spec.name,
    pid: () => child.pid ?? null,
    running: () => child.exitCode === null && !child.killed,
    async stop(reason: string) {
      if (child.exitCode !== null || child.killed) return;
      spec.sink.info(`stopping ${spec.name}: ${reason}`);
      child.kill();
      const started = Date.now();
      while (child.exitCode === null && Date.now() - started < STOP_GRACE_MS) {
        await new Promise((resolve) => setTimeout(resolve, 100));
      }
      if (child.exitCode === null) {
        spec.sink.warn(`${spec.name} ignored the stop request; terminating`);
        child.kill('SIGKILL');
      }
    },
  };
}

export interface SupervisorOptions {
  paths: DesktopPaths;
  sink: LogSink;
  sidecarToken: string;
  startAgentEngine: boolean;
  /** Port reserved for the shell proxy, so the backend can trust its origin. */
  shellPort: number;
  /** Ports used last time; reused when still free so the platform URL is stable. */
  preferredPorts?: { backend?: number; sidecar?: number; web?: number };
  /** Extra SR_* values from the user's environment that must survive. */
  inheritEnv?: Record<string, string | undefined>;
}

export class Supervisor {
  readonly paths: DesktopPaths;
  private readonly sink: LogSink;
  private backend: ManagedProcess | null = null;
  private sidecar: ManagedProcess | null = null;
  private web: ManagedProcess | null = null;
  private backendPort = 0;
  private sidecarPort = 0;
  private webPort = 0;
  private readonly shellPort: number;
  private readonly sidecarToken: string;
  private readonly startAgentEngine: boolean;
  private readonly inheritEnv: Record<string, string | undefined>;
  private readonly preferredPorts: { backend?: number; sidecar?: number; web?: number };
  private backendStartFailures: number[] = [];
  private intentionalStop = false;
  private lastBackendError = '';
  private lastWebError = '';

  constructor(options: SupervisorOptions) {
    this.paths = options.paths;
    this.sink = options.sink;
    this.sidecarToken = options.sidecarToken;
    this.startAgentEngine = options.startAgentEngine;
    this.shellPort = options.shellPort;
    this.inheritEnv = options.inheritEnv ?? {};
    this.preferredPorts = options.preferredPorts ?? {};
  }

  get ports(): { backend: number; sidecar: number; web: number; shell: number } {
    return { backend: this.backendPort, sidecar: this.sidecarPort, web: this.webPort, shell: this.shellPort };
  }

  get backendOrigin(): string {
    return `http://127.0.0.1:${this.backendPort}`;
  }

  get webOrigin(): string {
    return `http://127.0.0.1:${this.webPort}`;
  }

  get shellOrigin(): string {
    return `http://127.0.0.1:${this.shellPort}`;
  }

  /**
   * Environment for the backend.
   *
   * `SR_TRUSTED_ORIGINS` must contain the shell origin: the window talks to the
   * shell, and the shell forwards with that Origin semantics. Cookies are not
   * marked Secure because the desktop UI is loopback http (a Secure cookie would
   * be dropped by the browser and sign-in would silently fail).
   */
  private backendEnv(): NodeJS.ProcessEnv {
    return {
      ...this.inheritEnv,
      SR_DESKTOP_MODE: '1',
      SR_ENVIRONMENT: 'production',
      SR_DATA_DIR: this.paths.data,
      SR_DATABASE_PATH: path.join(this.paths.data, 'skillrouter.db'),
      SR_MASTER_KEY_FILE: path.join(this.paths.data, 'keys', 'master.keyring.json'),
      SR_BACKEND_BASE_URL: this.backendOrigin,
      SR_PUBLIC_BASE_URL: this.shellOrigin,
      SR_TRUSTED_ORIGINS: this.shellOrigin,
      SR_COOKIE_SECURE: 'false',
      SR_LOG_LEVEL: 'info',
      SR_ARAGON_SIDECAR_URL: `http://127.0.0.1:${this.sidecarPort}`,
      SR_ARAGON_SIDECAR_TOKEN: this.sidecarToken,
      SR_WORKER_CONCURRENCY: '4',
    };
  }

  private sidecarEnv(): NodeJS.ProcessEnv {
    return {
      ...this.inheritEnv,
      SR_ARAGON_SIDECAR_HOST: '127.0.0.1',
      SR_ARAGON_SIDECAR_PORT: String(this.sidecarPort),
      SR_ARAGON_SIDECAR_TOKEN: this.sidecarToken,
      SR_ARAGON_ENGINE_COMMIT: process.env.SR_ARAGON_ENGINE_COMMIT ?? '',
      NODE_ENV: 'production',
      ELECTRON_RUN_AS_NODE: '1',
    };
  }

  private webEnv(): NodeJS.ProcessEnv {
    return {
      NODE_ENV: 'production',
      PORT: String(this.webPort),
      HOSTNAME: '127.0.0.1',
      NEXT_TELEMETRY_DISABLED: '1',
      // Electron's binary is the Node runtime we ship. Without this flag the child
      // starts a second Electron *app*, hits our single-instance lock and exits 0 —
      // which looks like a clean exit and leaves the UI server missing.
      ELECTRON_RUN_AS_NODE: '1',
      NEXT_PRIVATE_STANDALONE: '1',
    };
  }

  private guardRuntime(): void {
    if (!existsSync(this.paths.backendExe)) {
      throw new Error(
        `The bundled API is missing at ${this.paths.backendExe}. ` +
          'This desktop package was built without its Python runtime — reinstall Skill Router.',
      );
    }
    if (!existsSync(this.paths.webEntry)) {
      throw new Error(
        `The bundled interface is missing at ${this.paths.webEntry}. ` +
          'This desktop package was built without its UI runtime — reinstall Skill Router.',
      );
    }
  }

  /** One-shot first-run setup, so the window never shows a half-initialised app. */
  async bootstrapBackend(): Promise<void> {
    this.guardRuntime();
    this.backendPort = this.backendPort || (await findByPreferredPort(this.preferredPorts.backend));
    const child = spawn(this.paths.backendExe, ['bootstrap', '--json'], {
      cwd: this.paths.backendDir,
      env: this.backendEnv(),
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    const chunks: Buffer[] = [];
    child.stdout.on('data', (chunk: Buffer) => chunks.push(chunk));
    child.stderr.on('data', (chunk: Buffer) => chunks.push(chunk));
    const code: number = await new Promise((resolve) => {
      const timer = setTimeout(() => {
        child.kill();
        resolve(-1);
      }, 120_000);
      child.on('exit', (exitCode) => {
        clearTimeout(timer);
        resolve(exitCode ?? -1);
      });
      child.on('error', () => {
        clearTimeout(timer);
        resolve(-1);
      });
    });
    const output = Buffer.concat(chunks).toString('utf8').trim();
    if (code !== 0) {
      throw new Error(
        `First-run setup failed (exit ${code}). Output: ${output.slice(-600) || '(none)'}. Logs: ${this.paths.logs}`,
      );
    }
    this.sink.info('first-run bootstrap complete', { output: output.slice(0, 300) });
  }

  async startAll(): Promise<HealthReport> {
    this.intentionalStop = false;
    this.guardRuntime();
    if (!this.backendPort) this.backendPort = await findByPreferredPort(this.preferredPorts.backend);
    if (!this.sidecarPort) this.sidecarPort = await findByPreferredPort(this.preferredPorts.sidecar);
    if (!this.webPort) this.webPort = await findByPreferredPort(this.preferredPorts.web);

    if (!this.startAgentEngine) {
      this.sink.info('agent engine disabled in desktop settings; skipping the sidecar');
    } else if (existsSync(this.paths.sidecarEntry)) {
      this.sidecar = spawnManaged({
        name: 'sidecar',
        command: process.execPath,
        args: [this.paths.sidecarEntry],
        cwd: this.paths.sidecarDir,
        env: this.sidecarEnv(),
        logFile: path.join(this.paths.logs, 'sidecar.log'),
        sink: this.sink,
        onExit: (code) => {
          if (this.intentionalStop) return;
          if (code !== 0) {
            // The engine is optional: published capabilities must keep working.
            this.sink.error(
              `the agent engine stopped (exit ${code ?? 'unknown'}); published capabilities remain available`,
            );
          }
        },
      });
    } else {
      this.sink.warn(`no sidecar at ${this.paths.sidecarEntry}; agent sessions will be unavailable`);
    }

    this.backend = spawnManaged({
      name: 'backend',
      command: this.paths.backendExe,
      args: ['--host', '127.0.0.1', '--port', String(this.backendPort), '--log-level', 'info'],
      cwd: this.paths.backendDir,
      env: this.backendEnv(),
      logFile: path.join(this.paths.logs, 'backend.log'),
      sink: this.sink,
      onExit: (code) => this.handleBackendExit(code),
    });

    this.web = spawnManaged({
      name: 'web',
      command: process.execPath,
      args: [this.paths.webEntry],
      cwd: this.paths.webDir,
      env: this.webEnv(),
      logFile: path.join(this.paths.logs, 'web.log'),
      sink: this.sink,
      onExit: (code) => {
        if (this.intentionalStop) return;
        this.lastWebError = `The interface server exited (code ${code ?? 'unknown'}). Logs: ${path.join(this.paths.logs, 'web.log')}`;
        this.sink.error(this.lastWebError);
      },
    });

    const backendReady = await waitForHttp(`${this.backendOrigin}/api/health/ready`, READY_TIMEOUT_MS, () => {
      if (this.backend && !this.backend.running() && this.lastBackendError) return this.lastBackendError;
      return null;
    });
    // Next answers any path with HTML once it is up; 200 or 404 both prove it booted.
    const webReady = await waitForHttp(
      `${this.webOrigin}/login`,
      45_000,
      () => (this.web && !this.web.running() && this.lastWebError ? this.lastWebError : null),
      (status) => status < 500,
    );
    const sidecarReady = this.sidecar
      ? await waitForHttp(`http://127.0.0.1:${this.sidecarPort}/health`, 20_000)
      : false;

    let detail: string;
    if (!backendReady) detail = this.lastBackendError || 'The API did not become ready in time.';
    else if (!webReady) detail = this.lastWebError || 'The interface server did not become ready in time.';
    else if (!sidecarReady && this.startAgentEngine)
      detail = 'Ready. The agent engine is not running; published capabilities still work.';
    else detail = 'Ready.';

    return {
      ok: backendReady && webReady,
      backendReady,
      webReady,
      sidecarReady,
      backendPort: this.backendPort,
      webPort: this.webPort,
      sidecarPort: this.sidecarPort,
      shellPort: this.shellPort,
      detail,
    };
  }

  private handleBackendExit(code: number | null): void {
    if (this.intentionalStop) return;
    const now = Date.now();
    this.backendStartFailures = this.backendStartFailures.filter((stamp) => now - stamp < 60_000);
    this.backendStartFailures.push(now);
    this.lastBackendError =
      this.backendStartFailures.length >= 3
        ? `The API stopped ${this.backendStartFailures.length} times in a minute (last exit code ${code ?? 'unknown'}). ` +
          `This is not a transient failure. Logs: ${path.join(this.paths.logs, 'backend.log')}`
        : `The API exited unexpectedly (code ${code ?? 'unknown'}).`;
    this.sink.error(this.lastBackendError);
  }

  async stopAll(reason: string): Promise<void> {
    this.intentionalStop = true;
    await Promise.all([this.backend?.stop(reason), this.web?.stop(reason), this.sidecar?.stop(reason)]);
    this.backend = null;
    this.web = null;
    this.sidecar = null;
  }

  async restartBackend(): Promise<HealthReport> {
    await this.backend?.stop('restart requested');
    this.backend = null;
    this.backendStartFailures = [];
    this.lastBackendError = '';
    return this.startAll();
  }

  async checkHealth(): Promise<HealthReport> {
    if (!this.backendPort || !this.webPort) {
      return {
        ok: false,
        backendReady: false,
        webReady: false,
        sidecarReady: false,
        backendPort: this.backendPort,
        webPort: this.webPort,
        sidecarPort: this.sidecarPort,
        shellPort: this.shellPort,
        detail: 'not started',
      };
    }
    const backendReady = await waitForHttp(`${this.backendOrigin}/api/health/ready`, 4_000);
    const webReady = await waitForHttp(`${this.webOrigin}/login`, 3_000, undefined, (status) => status < 500);
    const sidecarReady = this.sidecarPort
      ? await waitForHttp(`http://127.0.0.1:${this.sidecarPort}/health`, 3_000)
      : false;
    return {
      ok: backendReady && webReady,
      backendReady,
      webReady,
      sidecarReady,
      backendPort: this.backendPort,
      webPort: this.webPort,
      sidecarPort: this.sidecarPort,
      shellPort: this.shellPort,
      detail: backendReady && webReady ? 'API and interface are responding.' : 'A service is not responding.',
    };
  }

  status(): {
    backend: boolean;
    web: boolean;
    sidecar: boolean;
    backendPid: number | null;
    webPid: number | null;
    sidecarPid: number | null;
  } {
    return {
      backend: this.backend?.running() ?? false,
      web: this.web?.running() ?? false,
      sidecar: this.sidecar?.running() ?? false,
      backendPid: this.backend?.pid() ?? null,
      webPid: this.web?.pid() ?? null,
      sidecarPid: this.sidecar?.pid() ?? null,
    };
  }
}
