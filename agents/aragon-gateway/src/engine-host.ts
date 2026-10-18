/**
 * Engine host — the engine lives in a CHILD PROCESS so a version switch does not
 * require restarting the gateway container (spec §7.12-6, §12.4-4).
 *
 * This file has two roles:
 *
 * 1. **Child**: `node engine-host.js --pin <version> --home <dir>`. Loads the pinned
 *    engine, reports `{type:'ready', engine:{version}, tools:[...]}` on stdout as
 *    NDJSON, then serves `turn`/`abort`/`steer`/`shutdown` frames from stdin and
 *    streams `event` frames back.
 * 2. **Parent-side supervisor** (`HostSupervisor`): spawns such a child, proxies turns
 *    into it, and implements the four reload steps:
 *        ① start a child on the target pin
 *        ② wait for its `ready` frame
 *        ③ drain the old child (`abort()` + `waitForIdle()`, 60s ceiling)
 *        ④ close the old child and update the reported version
 *    Any failure closes the *new* child, leaves the old one serving and does NOT
 *    change the pin (spec §7.12-6: "切换失败时能够恢复上一可用版本").
 *
 * No signals are used to stop the child (upstream noted Windows signal differences:
 * only Ctrl-C produces a final result, `taskkill` just kills — aragonagent-upstream.md
 * §7). The child exits on its own after a `shutdown` frame, and the parent arms a
 * timeout in case it does not. No Docker socket, no container restart.
 */

import { spawn, type ChildProcess } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';

import { engineHome, loadConfig, type Config } from './config.js';
import {
  DRAIN_TIMEOUT_MS,
  EngineRuntime,
  EngineUnavailableError,
  describeError,
  loadCore,
} from './engine.js';
import { PlatformClient } from './platform-client.js';
import { StreamRegistry, type TurnStream, type SseSink } from './sse.js';
import {
  NULL_LOGGER,
  type EngineAvailability,
  type EngineIdentity,
  type HostToParentFrame,
  type Logger,
  type ParentToHostFrame,
  type PlatformEvent,
  type ReloadResult,
  type StopReport,
  type TurnAccepted,
  type TurnRequest,
} from './types.js';

/** How long a freshly spawned child may take before it must report `ready`. */
export const READY_TIMEOUT_MS = 60_000;
/** How long a closed child may take to exit after `shutdown`. */
export const CLOSE_TIMEOUT_MS = 10_000;

// ---------------------------------------------------------------------------
// Child role
// ---------------------------------------------------------------------------

interface ChildArgs {
  pin: string;
  home: string;
}

function parseChildArgs(argv: string[]): ChildArgs | null {
  const pinIndex = argv.indexOf('--pin');
  const homeIndex = argv.indexOf('--home');
  if (pinIndex === -1) return null;
  const pin = argv[pinIndex + 1];
  if (pin === undefined || pin === '' || pin.startsWith('--')) return null;
  const home = homeIndex === -1 ? '' : (argv[homeIndex + 1] ?? '');
  return { pin, home };
}

/** Minimal NDJSON logger for the child: stderr only, so stdout stays machine-readable. */
function childLogger(level: string): Logger {
  const levels = { debug: 10, info: 20, warn: 30, error: 40 } as const;
  const threshold = levels[level as keyof typeof levels] ?? 20;
  const write = (bindings: Record<string, unknown>) => {
    return (lvl: keyof typeof levels, event: string, fields?: Record<string, unknown>): void => {
      if (levels[lvl] < threshold) return;
      process.stderr.write(`${JSON.stringify({ ts: new Date().toISOString(), level: lvl, event, ...bindings, ...fields })}\n`);
    };
  };
  const base = write({ role: 'engine-host' });
  return {
    debug: (event, fields) => base('debug', event, fields),
    info: (event, fields) => base('info', event, fields),
    warn: (event, fields) => base('warn', event, fields),
    error: (event, fields) => base('error', event, fields),
    child: (bindings) => {
      const scoped = write({ role: 'engine-host', ...bindings });
      return {
        debug: (event, fields) => scoped('debug', event, fields),
        info: (event, fields) => scoped('info', event, fields),
        warn: (event, fields) => scoped('warn', event, fields),
        error: (event, fields) => scoped('error', event, fields),
        child: () => NULL_LOGGER,
      };
    },
  };
}

function writeFrame(frame: HostToParentFrame): void {
  process.stdout.write(`${JSON.stringify(frame)}\n`);
}

/** Sink that forwards every SSE-worthy event to the parent as one NDJSON frame. */
function frameSink(turnId: string): SseSink & { emitFrame: (event: PlatformEvent) => void } {
  const noop = (): void => undefined;
  return {
    // The TurnStream serialises here; we re-parse the `data:` JSON so the parent gets
    // the event object rather than a pre-rendered SSE chunk.
    write(chunk: string): boolean {
      for (const line of chunk.split('\n')) {
        if (!line.startsWith('data: ')) continue;
        try {
          const event = JSON.parse(line.slice(6)) as PlatformEvent;
          writeFrame({ type: 'event', turn_id: turnId, event });
        } catch {
          /* keep-alive comments and partial frames are ignored */
        }
      }
      return true;
    },
    once: noop,
    end: noop,
    emitFrame: noop,
  };
}

async function runChild(argv: string[]): Promise<void> {
  const args = parseChildArgs(argv);
  if (!args) {
    writeFrame({ type: 'fatal', message: 'usage: engine-host.js --pin <version> [--home <dir>]' });
    process.exitCode = 2;
    return;
  }

  // ARAGON_HOME is read by the SDK at module load time (aragonagent-upstream.md §6), so
  // it MUST be set before `loadCore()` runs — and per-version, so rollback reuses its
  // own directory (spec §12.4-5).
  if (args.home !== '') process.env['ARAGON_HOME'] = args.home;

  const config = loadConfig(process.env);
  const logger = childLogger(config.logLevel);
  const platform = new PlatformClient({
    internalUrl: config.internalUrl,
    sharedSecret: config.sharedSecret,
    timeoutMs: config.platformTimeout,
  });

  const runtime = new EngineRuntime({ config, platform, logger });
  await runtime.start();
  const status = runtime.status();
  if (status.availability !== 'available') {
    writeFrame({ type: 'fatal', message: status.detail ?? 'engine unavailable' });
    return;
  }

  const identity = runtime.identity();
  writeFrame({
    type: 'ready',
    engine: { version: identity.version, package: identity.package, schema_version: identity.schema_version },
    tools: runtime.toolNames(),
  });

  const turns = new Map<string, { request: TurnRequest; stream: TurnStream }>();
  let shuttingDown = false;

  const shutdown = async (): Promise<void> => {
    if (shuttingDown) return;
    shuttingDown = true;
    await runtime.shutdown();
    process.exit(0);
  };

  const handle = async (frame: ParentToHostFrame): Promise<void> => {
    switch (frame.type) {
      case 'turn': {
        try {
          const accepted: TurnAccepted = await runtime.startTurn(frame.request, frame.requestId ?? randomUUID().slice(0, 16));
          const stream = runtime.stream(frame.request.turn_id);
          if (stream) {
            // `attach` replays everything emitted during startTurn (session.started,
            // turn.queued, …) and then live-streams the rest to the parent.
            stream.attach(frameSink(frame.request.turn_id));
            turns.set(frame.request.turn_id, { request: frame.request, stream });
          }
          writeFrame({ type: 'notice', level: 'info', message: `turn ${accepted.turn_id} accepted` });
        } catch (err) {
          writeFrame({ type: 'turn_finished', turn_id: frame.request.turn_id, ok: false, detail: describeError(err) });
        }
        return;
      }
      case 'abort': {
        const report: StopReport = await runtime.interrupt(frame.turn_id);
        writeFrame({ type: 'stopped', turn_id: frame.turn_id, report });
        return;
      }
      case 'steer': {
        await runtime.steer(frame.turn_id, frame.message);
        return;
      }
      case 'shutdown': {
        await shutdown();
        return;
      }
      default: {
        writeFrame({ type: 'notice', level: 'warn', message: `unknown frame ${JSON.stringify(frame)}` });
      }
    }
  };

  const rl = createInterface({ input: process.stdin });
  let queue: Promise<void> = Promise.resolve();
  rl.on('line', (line) => {
    const trimmed = line.trim();
    if (trimmed === '') return;
    let frame: ParentToHostFrame;
    try {
      frame = JSON.parse(trimmed) as ParentToHostFrame;
    } catch {
      writeFrame({ type: 'notice', level: 'warn', message: 'malformed NDJSON frame ignored' });
      return;
    }
    // Serialise frames: a `turn` frame is not awaited by the reader.
    queue = queue.then(() => handle(frame)).catch((err: unknown) => {
      writeFrame({ type: 'notice', level: 'error', message: describeError(err) });
    });
  });

  // Parent died / closed our stdin: leave quietly.
  rl.on('close', () => {
    void shutdown();
  });
}

// ---------------------------------------------------------------------------
// Parent role: supervisor
// ---------------------------------------------------------------------------

interface ChildHandle {
  child: ChildProcess;
  pin: string;
  version: string;
  tools: string[];
  readyAt: number;
  exited: boolean;
  /** Set while the parent is deliberately closing this child (reload/shutdown). */
  closing: boolean;
}

interface HostTurn {
  sessionId: string;
  stream: TurnStream;
  done: Promise<void>;
  resolveDone: () => void;
  stopped: ((report: StopReport) => void) | null;
  finished: boolean;
}

export interface HostSupervisorOptions {
  config: Config;
  platform: PlatformClient;
  logger?: Logger;
}

export class HostSupervisor {
  private readonly config: Config;
  private readonly logger: Logger;
  private active: ChildHandle | null = null;
  private readonly streams: StreamRegistry;
  private readonly turns = new Map<string, HostTurn>();
  private availability: EngineAvailability = 'starting';
  private detail: string | null = null;
  private startedAt = Date.now();
  private reloading = false;

  constructor(options: HostSupervisorOptions) {
    this.config = options.config;
    this.logger = options.logger ?? NULL_LOGGER;
    this.streams = new StreamRegistry(this.config.timeouts.sseKeepAlive);
  }

  async start(): Promise<void> {
    const handle = await this.spawnChild(this.config.enginePin);
    this.active = handle;
    this.availability = 'available';
    this.detail = null;
    this.logger.info('engine_host.ready', { version: handle.version, tools: handle.tools });
  }

  identity(): EngineIdentity {
    return {
      package: '@aragon-agent/core',
      version: this.active?.version ?? this.config.enginePin,
      commit: process.env['ENGINE_COMMIT'] ?? null,
      schema_version: 1,
    };
  }

  status(): { availability: EngineAvailability; detail?: string } {
    return this.detail === null ? { availability: this.availability } : { availability: this.availability, detail: this.detail };
  }

  homeDir(): string {
    return engineHome(this.config, this.identity().version);
  }

  toolNames(): string[] {
    return this.active?.tools ?? [];
  }

  activeTurnCount(): number {
    let n = 0;
    for (const turn of this.turns.values()) if (!turn.finished) n += 1;
    return n;
  }

  uptimeSeconds(): number {
    return Math.floor((Date.now() - this.startedAt) / 1000);
  }

  stream(turnId: string): TurnStream | undefined {
    return this.turns.get(turnId)?.stream;
  }

  async startTurn(request: TurnRequest, requestId: string): Promise<TurnAccepted> {
    const child = this.active;
    if (!child || child.exited || this.availability !== 'available') {
      throw new EngineUnavailableError(this.detail ?? 'no engine child is serving');
    }
    if (this.turns.has(request.turn_id)) {
      throw new EngineUnavailableError(`turn ${request.turn_id} already exists`);
    }
    const stream = this.streams.ensure(request.turn_id);
    let resolveDone: () => void = () => undefined;
    const done = new Promise<void>((resolve) => {
      resolveDone = resolve;
    });
    this.turns.set(request.turn_id, {
      sessionId: request.session_id,
      stream,
      done,
      resolveDone,
      stopped: null,
      finished: false,
    });
    this.send(child, { type: 'turn', request, requestId });
    return { accepted: true, ipc_stream: `/internal/turns/${request.turn_id}/events`, turn_id: request.turn_id };
  }

  async interrupt(turnId: string): Promise<StopReport> {
    const turn = this.turns.get(turnId);
    const child = this.active;
    if (!turn || !child) {
      return {
        turn_id: turnId,
        stopped: false,
        reason: 'unknown_turn',
        stopped_steps: [],
        external_pending: [],
        detail: 'no such turn in this engine child',
      };
    }
    const reportPromise = new Promise<StopReport>((resolve) => {
      turn.stopped = resolve;
    });
    this.send(child, { type: 'abort', turn_id: turnId });
    const timeout = new Promise<StopReport>((resolve) => {
      const timer = setTimeout(() => {
        resolve({
          turn_id: turnId,
          stopped: false,
          reason: 'timeout',
          stopped_steps: [],
          external_pending: [],
          detail: `engine child did not report a stop within ${DRAIN_TIMEOUT_MS}ms`,
        });
      }, DRAIN_TIMEOUT_MS + 5_000);
      timer.unref?.();
    });
    return Promise.race([reportPromise, timeout]);
  }

  async steer(turnId: string, message: string): Promise<{ accepted: boolean; reason?: string }> {
    const child = this.active;
    if (!child) return { accepted: false, reason: 'engine_unavailable' };
    const turn = this.turns.get(turnId);
    if (!turn) return { accepted: false, reason: 'unknown_turn' };
    if (turn.finished) return { accepted: false, reason: 'turn is finished' };
    this.send(child, { type: 'steer', turn_id: turnId, message });
    return { accepted: true };
  }

  async waitForIdle(turnId?: string): Promise<void> {
    if (turnId === undefined) {
      await Promise.all([...this.turns.values()].map((turn) => turn.done));
      return;
    }
    await this.turns.get(turnId)?.done;
  }

  /**
   * Four-step hot reload (spec §7.12-6). Every failure path keeps the OLD child
   * serving and leaves the pin untouched.
   */
  async reload(targetVersion: string): Promise<ReloadResult> {
    const current = this.identity().version;
    if (!/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/.test(targetVersion)) {
      return { ok: false, engine_version: current, stage: 'rejected', detail: `invalid target_version ${JSON.stringify(targetVersion)}` };
    }
    if (targetVersion === current) {
      return { ok: true, engine_version: current, stage: 'done', detail: 'target version already active' };
    }
    if (this.reloading) {
      return { ok: false, engine_version: current, stage: 'rejected', detail: 'a reload is already in progress' };
    }
    this.reloading = true;
    const old = this.active;
    let fresh: ChildHandle | null = null;
    try {
      // ① start a child on the target pin
      try {
        fresh = await this.spawnChild(targetVersion);
      } catch (err) {
        return {
          ok: false,
          engine_version: current,
          stage: 'start_child',
          detail: `could not start engine-host for ${targetVersion}: ${describeError(err)}`,
        };
      }

      // ② it must self-report ready with the expected version
      if (fresh.version !== targetVersion) {
        return {
          ok: false,
          engine_version: current,
          stage: 'wait_ready',
          detail: `child reported version ${fresh.version}, expected ${targetVersion}`,
        };
      }

      // ③ drain the old child: abort active turns, wait for idle with a 60s ceiling
      if (old) {
        try {
          await this.drainChild(old);
        } catch (err) {
          return {
            ok: false,
            engine_version: current,
            stage: 'drain_old',
            detail: `old engine did not drain: ${describeError(err)}`,
          };
        }

        // ④ close the old child and only then switch the reported version
        try {
          await this.closeChild(old);
        } catch (err) {
          return {
            ok: false,
            engine_version: current,
            stage: 'close_old',
            detail: `old engine did not exit after shutdown: ${describeError(err)}`,
          };
        }
      }

      this.active = fresh;
      this.availability = 'available';
      this.detail = null;
      this.logger.info('engine_host.reloaded', { from: current, to: fresh.version });
      return {
        ok: true,
        engine_version: fresh.version,
        stage: 'done',
        detail: `engine switched ${current} → ${fresh.version}; ${fresh.tools.length} platform tools registered`,
      };
    } finally {
      // Any failure above leaves `fresh` un-owned: shut it down so a failed switch
      // cannot leak a process (the old child keeps serving).
      if (fresh !== null && this.active !== fresh) {
        try {
          await this.closeChild(fresh);
        } catch (err) {
          this.logger.warn('engine_host.stale_child_not_closed', { error: describeError(err) });
        }
      }
      this.reloading = false;
    }
  }

  async shutdown(): Promise<void> {
    const child = this.active;
    this.streams.closeAll();
    this.turns.clear();
    if (!child) return;
    this.active = null;
    try {
      await this.closeChild(child);
    } catch (err) {
      this.logger.warn('engine_host.shutdown_timeout', { error: describeError(err) });
    }
  }

  // -- child plumbing -------------------------------------------------------

  private async spawnChild(pin: string): Promise<ChildHandle> {
    const home = engineHome(this.config, pin);
    const { execArgs, cwd } = hostCommand(pin, home);
    const child = spawn(process.execPath, execArgs, {
      cwd,
      env: {
        ...process.env,
        ENGINE_PIN: pin,
        ARAGON_HOME: home,
        NODE_ENV: process.env['NODE_ENV'] ?? 'production',
      },
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    });

    const handle: ChildHandle = {
      child,
      pin,
      version: pin,
      tools: [],
      readyAt: 0,
      exited: false,
      closing: false,
    };

    const ready = new Promise<ChildHandle>((resolve, reject) => {
      const timer = setTimeout(() => {
        reject(new Error(`engine-host for ${pin} did not report ready within ${READY_TIMEOUT_MS}ms`));
      }, READY_TIMEOUT_MS);
      timer.unref?.();

      const stdout = child.stdout;
      if (!stdout) {
        clearTimeout(timer);
        reject(new Error('engine-host has no stdout pipe'));
        return;
      }
      const rl = createInterface({ input: stdout });
      rl.on('line', (line) => {
        const trimmed = line.trim();
        if (trimmed === '') return;
        let frame: HostToParentFrame;
        try {
          frame = JSON.parse(trimmed) as HostToParentFrame;
        } catch {
          this.logger.warn('engine_host.unparsable_stdout', { line: trimmed.slice(0, 200) });
          return;
        }
        switch (frame.type) {
          case 'ready':
            handle.version = frame.engine.version;
            handle.tools = frame.tools;
            handle.readyAt = Date.now();
            clearTimeout(timer);
            resolve(handle);
            return;
          case 'event':
            this.onChildEvent(frame.turn_id, frame.event);
            return;
          case 'turn_finished': {
            const turn = this.turns.get(frame.turn_id);
            if (turn) {
              if (!frame.ok && frame.detail !== undefined) {
                this.logger.warn('engine_host.turn_rejected', { turn_id: frame.turn_id, detail: frame.detail });
              }
              this.finishTurn(turn);
            }
            return;
          }
          case 'stopped': {
            const turn = this.turns.get(frame.turn_id);
            if (turn?.stopped) {
              turn.stopped(frame.report);
              turn.stopped = null;
            }
            return;
          }
          case 'notice':
            this.logger[frame.level === 'error' ? 'error' : frame.level](`engine_host.${frame.level}`, {
              message: frame.message,
            });
            return;
          case 'fatal':
            clearTimeout(timer);
            reject(new Error(`engine-host fatal: ${frame.message}`));
            return;
          default:
            this.logger.warn('engine_host.unknown_frame', { frame: trimmed.slice(0, 200) });
        }
      });

      child.stderr?.on('data', (chunk: Buffer) => {
        const text = chunk.toString('utf8').trim();
        if (text !== '') this.logger.info('engine_host.stderr', { line: text.slice(0, 500) });
      });

      child.once('error', (err) => {
        clearTimeout(timer);
        reject(err);
      });
      child.once('exit', (code, signal) => {
        handle.exited = true;
        clearTimeout(timer);
        if (handle.readyAt === 0) {
          reject(new Error(`engine-host exited before ready (code=${String(code)}, signal=${String(signal)})`));
          return;
        }
        this.onChildExit(handle, code, signal);
      });
    });

    try {
      return await ready;
    } catch (err) {
      child.kill();
      throw err;
    }
  }

  private onChildExit(handle: ChildHandle, code: number | null, signal: NodeJS.Signals | null): void {
    if (this.active !== handle) return;
    if (handle.closing) {
      // Deliberate close (reload drain or shutdown): not an outage.
      this.logger.info('engine_host.child_closed', { version: handle.version, code, signal });
      return;
    }
    this.availability = 'unavailable';
    this.detail = `engine child exited unexpectedly (code=${String(code)}, signal=${String(signal)})`;
    this.logger.error('engine_host.child_exited', { code, signal });
    for (const turn of this.turns.values()) {
      turn.stream.emit('engine.notice', { code: 'ENGINE_CHILD_EXITED', message: this.detail });
      this.finishTurn(turn);
    }
    this.turns.clear();
  }

  /**
   * Feed a child event into the turn's SSE stream and resolve the turn's `done`
   * promise on a terminal event.
   *
   * Resolving here (rather than only on a `turn_finished` frame) is what makes
   * `waitForIdle()` — and therefore a reload's 60s drain step — precise instead of
   * always burning the full timeout.
   */
  private onChildEvent(turnId: string, event: PlatformEvent): void {
    const turn = this.turns.get(turnId);
    if (!turn) return;
    turn.stream.ingest(event);
    if (event.type === 'turn.finished' || event.type === 'turn.failed' || event.type === 'turn.stopped') {
      this.finishTurn(turn);
    }
  }

  private finishTurn(turn: HostTurn): void {
    if (turn.finished) return;
    turn.finished = true;
    turn.resolveDone();
  }

  private send(child: ChildHandle, frame: ParentToHostFrame): void {
    if (child.exited || !child.child.stdin) {
      throw new EngineUnavailableError('engine child is not accepting input');
    }
    child.child.stdin.write(`${JSON.stringify(frame)}\n`);
  }

  /** Abort every live turn on `handle` and wait for the child to go idle. */
  private async drainChild(handle: ChildHandle): Promise<void> {
    const live = [...this.turns.entries()].filter(([, turn]) => !turn.finished);
    for (const [turnId] of live) {
      try {
        this.send(handle, { type: 'abort', turn_id: turnId });
      } catch (err) {
        this.logger.warn('engine_host.drain_send_failed', { turn_id: turnId, error: describeError(err) });
      }
    }
    if (live.length === 0) return;

    let timer: NodeJS.Timeout | undefined;
    const ceiling = new Promise<never>((_, reject) => {
      timer = setTimeout(
        () => reject(new Error(`turns did not finish within ${DRAIN_TIMEOUT_MS}ms`)),
        DRAIN_TIMEOUT_MS,
      );
      timer.unref?.();
    });
    try {
      await Promise.race([Promise.all(live.map(([, turn]) => turn.done)), ceiling]);
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  /** Ask the child to exit, then give it a bounded amount of time to do so. */
  private async closeChild(handle: ChildHandle): Promise<void> {
    if (handle.exited) return;
    handle.closing = true;
    const exited = new Promise<void>((resolve) => {
      handle.child.once('exit', () => resolve());
    });
    try {
      this.send(handle, { type: 'shutdown' });
    } catch {
      /* stdin already closed: fall through to the timeout below */
    }
    let timer: NodeJS.Timeout | undefined;
    const ceiling = new Promise<never>((_, reject) => {
      timer = setTimeout(() => {
        // Last resort, still not a signal to the *engine*: the child failed to exit on
        // its own, so the parent terminates the process it started.
        handle.child.kill();
        reject(new Error(`child did not exit within ${CLOSE_TIMEOUT_MS}ms`));
      }, CLOSE_TIMEOUT_MS);
      timer.unref?.();
    });
    try {
      await Promise.race([exited, ceiling]);
    } finally {
      if (timer) clearTimeout(timer);
    }
    if (this.active === handle) this.active = null;
  }
}

/**
 * Work out how to launch `engine-host` from the *current* runtime:
 * - compiled (`dist/engine-host.js`) → plain node;
 * - TypeScript sources (`src/engine-host.ts`, e.g. `npm run dev`) → node + tsx loader.
 */
export function hostCommand(pin: string, home: string, thisFile: string = fileURLToPath(import.meta.url)): {
  execArgs: string[];
  cwd: string;
} {
  const cwd = process.cwd();
  const args = ['--pin', pin, '--home', home];
  if (thisFile.endsWith('.ts')) {
    return { execArgs: ['--import', 'tsx', thisFile, ...args], cwd };
  }
  return { execArgs: [thisFile, ...args], cwd };
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

const isChildEntry = process.argv[1] !== undefined && /engine-host\.(js|ts)$/.test(process.argv[1]);

if (isChildEntry) {
  void runChild(process.argv.slice(2)).catch((err: unknown) => {
    writeFrame({ type: 'fatal', message: describeError(err) });
    process.exitCode = 1;
  });
}

/** Exported for the conformance suite: verifies the SDK is loadable in host mode. */
export async function probeEngine(pin: string): Promise<{ ok: boolean; detail: string }> {
  const loaded = await loadCore(pin);
  return loaded.ok ? { ok: true, detail: 'engine module importable' } : { ok: false, detail: loaded.error };
}
