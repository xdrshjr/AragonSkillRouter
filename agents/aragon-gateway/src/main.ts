/**
 * Fastify entry point: internal auth, route mount, request ids, graceful shutdown.
 *
 * The routes are exactly the six the spec assigns to the gateway (§9.5):
 *
 *   GET  /healthz                              engine version + tool summary
 *   POST /internal/turns                       start a turn
 *   GET  /internal/turns/:turnId/events        SSE AgentEvent stream
 *   POST /internal/turns/:turnId/interrupt     stop report
 *   POST /internal/turns/:turnId/steer         queue a user message into the turn
 *   POST /internal/admin/reload                four-step engine hot reload
 *
 * Everything except `/healthz` requires `X-Internal-Token: $AGENT_GATEWAY_SHARED_SECRET`.
 * `/healthz` is deliberately unauthenticated and exposes no secret: the container
 * healthcheck and §12.4's "3 consecutive healthy" gate poll it, and a health probe that
 * needs a credential cannot be used by an orchestrator.
 */

import { createHash, randomUUID, timingSafeEqual } from 'node:crypto';
import { dirname, isAbsolute, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import Fastify, { type FastifyInstance, type FastifyReply, type FastifyRequest } from 'fastify';

import {
  PathEscapeError,
  buildArtifact,
  findVerifyScript,
  isInsideRoot,
  verifyArtifact,
  type ArtifactDeps,
} from './artifact.js';
import {
  ConfigError,
  engineResolvedPath,
  loadConfig,
  maintenanceEnabled,
  readEnginePointer,
  type Config,
} from './config.js';
import { EngineManager, TurnConflictError, EngineUnavailableError, describeError } from './engine.js';
import { buildHealthReport } from './health.js';
import { PlatformClient } from './platform-client.js';
import { loadPolicy, PolicyError } from './policy.js';
import type { SseSink } from './sse.js';
import type { BuildRequest, Logger, ReloadRequest, TurnRequest, VerifyRequest } from './types.js';

const LEVELS = { debug: 10, info: 20, warn: 30, error: 40 } as const;

/** JSON-lines logger (spec §7.14 field set, reduced to what the gateway owns). */
export function createLogger(level: keyof typeof LEVELS = 'info', bindings: Record<string, unknown> = {}): Logger {
  const threshold = LEVELS[level];
  const emit = (lvl: keyof typeof LEVELS, event: string, fields?: Record<string, unknown>): void => {
    if (LEVELS[lvl] < threshold) return;
    const line = JSON.stringify({ ts: new Date().toISOString(), level: lvl, event, ...bindings, ...fields });
    if (lvl === 'error' || lvl === 'warn') process.stderr.write(`${line}\n`);
    else process.stdout.write(`${line}\n`);
  };
  return {
    debug: (event, fields) => emit('debug', event, fields),
    info: (event, fields) => emit('info', event, fields),
    warn: (event, fields) => emit('warn', event, fields),
    error: (event, fields) => emit('error', event, fields),
    child: (extra) => createLogger(level, { ...bindings, ...extra }),
  };
}

export interface GatewayOptions {
  config: Config;
  logger?: Logger;
  /** Injection seam for tests; defaults to a real `EngineManager`. */
  engine?: EngineManager;
  /** Injection seam for the artifact endpoints (build/verify runner). */
  artifactDeps?: Partial<ArtifactDeps>;
}

export interface Gateway {
  app: FastifyInstance;
  engine: EngineManager;
  logger: Logger;
  close: () => Promise<void>;
}

function tokenMatches(provided: string | undefined, expected: string): boolean {
  if (provided === undefined || provided === '') return false;
  const a = createHash('sha256').update(provided, 'utf8').digest();
  const b = createHash('sha256').update(expected, 'utf8').digest();
  return timingSafeEqual(a, b);
}

function newRequestId(): string {
  return randomUUID().replace(/-/g, '').slice(0, 16);
}

/** Attach a Fastify reply to an SSE sink (backpressure-aware). */
function replySink(reply: FastifyReply): SseSink {
  const raw = reply.raw;
  return {
    write(chunk: string): boolean {
      return raw.write(chunk);
    },
    once(event: 'drain' | 'close' | 'error', listener: (...args: unknown[]) => void): void {
      raw.once(event, listener);
    },
    end(): void {
      raw.end();
    },
  };
}

/**
 * Resolve which source currently decides the engine pin.
 *
 * Re-read on every `/healthz` call: the platform writes `current.json` during a hot
 * switch, so the boot-time conclusion goes stale and would misreport `pin_source`
 * (spec §A.5.3 / T-47 -- the "installed but inert" probe must not lie).
 */
function livePinSource(config: Config): { pinSource: 'pointer' | 'env'; version: string } {
  const read = readEnginePointer(config.engineRuntimeDir, config.engineHomeRoot);
  if (read.pointer !== null) {
    return { pinSource: 'pointer', version: read.pointer.version };
  }
  return { pinSource: 'env', version: config.enginePin };
}

export function buildGateway(options: GatewayOptions): Gateway {
  const { config } = options;
  const logger = options.logger ?? createLogger(config.logLevel);
  // Every artifact path must live under the shared data volume (`/data` in containers).
  const dataRoot = resolve(dirname(config.buildsDir));
  const artifactDeps: ArtifactDeps = {
    buildsDir: config.buildsDir,
    dataRoot,
    verifyScript: findVerifyScript(
      [
        process.env['AGENT_VERIFY_ARTIFACT_SCRIPT'] ?? '',
        resolve(process.cwd(), 'scripts', 'verify_artifact.mjs'),
        '/app/scripts/verify_artifact.mjs',
        resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', 'scripts', 'verify_artifact.mjs'),
      ].filter((candidate) => candidate !== ''),
    ),
    logger: logger.child({ component: 'artifact' }),
    ...options.artifactDeps,
  };
  const platform = new PlatformClient({
    internalUrl: config.internalUrl,
    sharedSecret: config.sharedSecret,
    timeoutMs: config.platformTimeout,
  });
  const engine =
    options.engine ?? new EngineManager({ config, platform, logger: logger.child({ component: 'engine' }) });

  const app = Fastify({
    logger: false,
    bodyLimit: 2 * 1024 * 1024,
    genReqId: () => newRequestId(),
    // Request logging is done by the onResponse hook below, not by Fastify's built-in
    // logger (which we disable entirely to keep the JSON-lines shape uniform).
  });

  // -- request id + internal token ------------------------------------------
  app.addHook('onRequest', async (request: FastifyRequest, reply: FastifyReply) => {
    const requestId = request.id;
    reply.header('X-Request-Id', requestId);

    const path = request.url.split('?')[0] ?? '';
    if (path === '/healthz') return;

    const provided = request.headers['x-internal-token'];
    const token = Array.isArray(provided) ? provided[0] : provided;
    if (!tokenMatches(token, config.sharedSecret)) {
      logger.warn('internal.auth_rejected', { request_id: requestId, path, remote: request.ip });
      await reply.code(401).send({
        code: 'UNAUTHORIZED',
        message: 'missing or invalid X-Internal-Token',
        request_id: requestId,
      });
      return reply;
    }
    return undefined;
  });

  app.addHook('onResponse', async (request, reply) => {
    logger.debug('http.response', {
      request_id: request.id,
      method: request.method,
      path: request.url.split('?')[0],
      status: reply.statusCode,
      latency_ms: Math.round(reply.elapsedTime),
    });
  });

  // -- health ---------------------------------------------------------------
  app.get('/healthz', async (_request, reply) => {
    // pin_source 必须在**请求时**从指针文件重新解析：热切换之后启动时的结论已过期，
    // 否则「装了但没生效」的排查入口会给出错误答案（§A.5.3 / T-47）。
    const live = livePinSource(config);
    const report = buildHealthReport({
      engine,
      pin: live.version || config.enginePin,
      pinSource: live.pinSource,
      resolvedPath: engineResolvedPath(config, live.version || config.enginePin),
      maintenance: maintenanceEnabled(config),
    });
    return reply.code(200).send(report);
  });

  // -- turns ----------------------------------------------------------------
  app.post('/internal/turns', async (request, reply) => {
    const body = request.body;
    if (body === null || typeof body !== 'object' || Array.isArray(body)) {
      return reply.code(400).send(errorBody(request, 'INVALID_BODY', 'body must be a JSON object'));
    }
    const turn = body as TurnRequest;
    const missing = ['session_id', 'turn_id'].filter((key) => {
      const value = (turn as unknown as Record<string, unknown>)[key];
      return typeof value !== 'string' || value.trim() === '';
    });
    if (missing.length > 0) {
      return reply
        .code(400)
        .send(errorBody(request, 'INVALID_BODY', `missing required fields: ${missing.join(', ')}`));
    }
    if (turn.budget !== undefined && (typeof turn.budget !== 'object' || turn.budget === null)) {
      return reply.code(400).send(errorBody(request, 'INVALID_BODY', 'budget must be an object'));
    }

    try {
      const accepted = await engine.startTurn(turn, request.id);
      logger.info('turn.accepted', {
        request_id: request.id,
        session_id: turn.session_id,
        turn_id: turn.turn_id,
      });
      return reply.code(200).send(accepted);
    } catch (err) {
      if (err instanceof EngineUnavailableError) {
        return reply.code(503).send(errorBody(request, 'ENGINE_UNAVAILABLE', err.detail));
      }
      if (err instanceof TurnConflictError) {
        return reply.code(409).send(errorBody(request, 'TURN_CONFLICT', err.message));
      }
      logger.error('turn.accept_failed', { request_id: request.id, error: describeError(err) });
      return reply.code(500).send(errorBody(request, 'TURN_ACCEPT_FAILED', describeError(err)));
    }
  });

  app.get('/internal/turns/:turnId/events', async (request, reply) => {
    const { turnId } = request.params as { turnId: string };
    const stream = engine.stream(turnId);
    if (!stream) {
      return reply
        .code(404)
        .send(errorBody(request, 'UNKNOWN_TURN', `no event stream for turn ${turnId} in this process`));
    }

    const query = request.query as { last_event_id?: string; lastEventId?: string };
    const header = request.headers['last-event-id'];
    const headerValue = Array.isArray(header) ? header[0] : header;
    const rawLast = query.last_event_id ?? query.lastEventId ?? headerValue ?? '0';
    const lastEventId = Number.parseInt(String(rawLast), 10);

    reply.hijack();
    const raw = reply.raw;
    raw.writeHead(200, {
      'content-type': 'text/event-stream; charset=utf-8',
      'cache-control': 'no-cache, no-transform',
      connection: 'keep-alive',
      'x-accel-buffering': 'no',
      'X-Request-Id': request.id,
    });
    raw.flushHeaders?.();

    const sink = replySink(reply);
    stream.attach(sink, Number.isFinite(lastEventId) && lastEventId > 0 ? lastEventId : 0);
    if (stream.replayGap > 0) {
      stream.emit('engine.notice', {
        code: 'REPLAY_TRUNCATED',
        message: `events before seq ${lastEventId} are no longer buffered; ${stream.replayGap} event(s) missing`,
      });
    }

    const detach = (): void => stream.detach(sink);
    request.raw.once('close', detach);
    request.raw.once('aborted', detach);
    logger.debug('sse.attached', { request_id: request.id, turn_id: turnId, last_event_id: lastEventId });
    return reply;
  });

  app.post('/internal/turns/:turnId/interrupt', async (request, reply) => {
    const { turnId } = request.params as { turnId: string };
    try {
      const report = await engine.interrupt(turnId, 'interrupt');
      logger.info('turn.interrupt', {
        request_id: request.id,
        turn_id: turnId,
        stopped: report.stopped,
        reason: report.reason,
      });
      return reply.code(200).send(report);
    } catch (err) {
      if (err instanceof EngineUnavailableError) {
        return reply.code(503).send(errorBody(request, 'ENGINE_UNAVAILABLE', err.detail));
      }
      logger.error('turn.interrupt_failed', { request_id: request.id, turn_id: turnId, error: describeError(err) });
      return reply.code(500).send(errorBody(request, 'INTERRUPT_FAILED', describeError(err)));
    }
  });

  app.post('/internal/turns/:turnId/steer', async (request, reply) => {
    const { turnId } = request.params as { turnId: string };
    const body = request.body;
    if (body === null || typeof body !== 'object' || Array.isArray(body)) {
      return reply.code(400).send(errorBody(request, 'INVALID_BODY', 'body must be a JSON object'));
    }
    const message = (body as { message?: unknown; text?: unknown }).message ?? (body as { text?: unknown }).text;
    if (typeof message !== 'string' || message.trim() === '') {
      return reply.code(400).send(errorBody(request, 'INVALID_BODY', 'message must be a non-empty string'));
    }
    try {
      const result = await engine.steer(turnId, message);
      if (!result.accepted) {
        return reply.code(409).send({
          code: 'STEER_REJECTED',
          message: result.reason ?? 'turn is not accepting steering',
          request_id: request.id,
        });
      }
      return reply.code(200).send({ accepted: true, turn_id: turnId });
    } catch (err) {
      if (err instanceof EngineUnavailableError) {
        return reply.code(503).send(errorBody(request, 'ENGINE_UNAVAILABLE', err.detail));
      }
      logger.error('turn.steer_failed', { request_id: request.id, turn_id: turnId, error: describeError(err) });
      return reply.code(500).send(errorBody(request, 'STEER_FAILED', describeError(err)));
    }
  });

  // -- engine hot reload (spec §7.12-6 / §12.4-4 / §A.5.3) --------------------
  app.post('/internal/admin/reload', async (request, reply) => {
    const body = (request.body ?? {}) as ReloadRequest;
    const target = typeof body.version === 'string' ? body.version : body.target_version;
    if (typeof target !== 'string' || target.trim() === '') {
      return reply
        .code(400)
        .send(errorBody(request, 'INVALID_BODY', 'version is required (exact semver, e.g. "0.2.37")'));
    }
    const engineHome = typeof body.engine_home === 'string' ? body.engine_home.trim() : '';
    if (engineHome !== '' && (!isAbsolute(engineHome) || !isInsideRoot(dataRoot, engineHome))) {
      return reply
        .code(400)
        .send(
          errorBody(
            request,
            'INVALID_PATH',
            `engine_home must be an absolute path under ${dataRoot} (got ${JSON.stringify(engineHome)})`,
          ),
        );
    }
    const previousVersion = engine.identity().version;
    try {
      const result = await engine.reload(target.trim(), engineHome === '' ? undefined : engineHome);
      const enriched = {
        ...result,
        previous_version: previousVersion,
        // A caller-supplied home is the platform's pointer decision; otherwise the
        // gateway's own boot-time resolution stands (spec §A.5.3).
        pin_source: engineHome === '' ? config.pinSource : ('pointer' as const),
        ...(engineHome === '' ? {} : { engine_home: engineHome }),
      };
      logger[result.ok ? 'info' : 'warn']('engine.reload', {
        request_id: request.id,
        ok: result.ok,
        stage: result.stage,
        engine_version: result.engine_version,
        previous_version: previousVersion,
        engine_home: engineHome === '' ? undefined : engineHome,
        detail: result.detail,
      });
      return reply.code(200).send(enriched);
    } catch (err) {
      if (err instanceof EngineUnavailableError) {
        return reply.code(503).send(errorBody(request, 'ENGINE_UNAVAILABLE', err.detail));
      }
      logger.error('engine.reload_failed', { request_id: request.id, error: describeError(err) });
      return reply.code(500).send(errorBody(request, 'RELOAD_FAILED', describeError(err)));
    }
  });

  // -- isolated build (spec §A.3.7-0 / §A.5.3) --------------------------------
  app.post('/internal/admin/build', async (request, reply) => {
    const body = (request.body ?? {}) as BuildRequest;
    if (typeof body.version !== 'string' || !/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/.test(body.version)) {
      return reply
        .code(400)
        .send(errorBody(request, 'INVALID_BODY', 'version must be an exact semver (e.g. "0.2.37")'));
    }
    try {
      const result = await buildArtifact(body, artifactDeps);
      logger[result.ok ? 'info' : 'warn']('artifact.build', {
        request_id: request.id,
        version: body.version,
        status: result.status,
        stage: result.stage,
      });
      return reply.code(200).send(result);
    } catch (err) {
      if (err instanceof PathEscapeError) {
        return reply.code(400).send(errorBody(request, 'INVALID_PATH', err.message));
      }
      logger.error('artifact.build_failed', { request_id: request.id, error: describeError(err) });
      return reply.code(500).send(errorBody(request, 'BUILD_FAILED', describeError(err)));
    }
  });

  // -- artifact verification (spec §A.3.7-1 / §A.5.3) ------------------------
  app.post('/internal/admin/verify-artifact', async (request, reply) => {
    const body = (request.body ?? {}) as VerifyRequest;
    const artifactDir = typeof body.artifact_dir === 'string' ? body.artifact_dir.trim() : '';
    if (artifactDir === '' || !isAbsolute(artifactDir) || !isInsideRoot(dataRoot, artifactDir)) {
      return reply
        .code(400)
        .send(
          errorBody(
            request,
            'INVALID_PATH',
            `artifact_dir must be an absolute path under ${dataRoot} (got ${JSON.stringify(artifactDir)})`,
          ),
        );
    }
    const expected = Array.isArray(body.expected_symbols) ? body.expected_symbols.map(String) : [];
    try {
      const result = await verifyArtifact(artifactDir, expected, artifactDeps);
      logger[result.status === 'passed' ? 'info' : 'warn']('artifact.verify', {
        request_id: request.id,
        status: result.status,
        checks: result.checks.map((check) => `${check.check}:${check.status}`),
      });
      return reply.code(200).send(result);
    } catch (err) {
      if (err instanceof PathEscapeError) {
        return reply.code(400).send(errorBody(request, 'INVALID_PATH', err.message));
      }
      logger.error('artifact.verify_failed', { request_id: request.id, error: describeError(err) });
      return reply.code(500).send(errorBody(request, 'VERIFY_FAILED', describeError(err)));
    }
  });

  app.setNotFoundHandler(async (request, reply) =>
    reply.code(404).send({
      code: 'NOT_FOUND',
      message: `no route for ${request.method} ${request.url.split('?')[0]}`,
      request_id: request.id,
    }),
  );

  app.setErrorHandler(async (error: Error & { statusCode?: number }, request: FastifyRequest, reply: FastifyReply) => {
    const status = typeof error.statusCode === 'number' && error.statusCode >= 400 ? error.statusCode : 500;
    logger.error('http.error', { request_id: request.id, path: request.url.split('?')[0], error: error.message });
    return reply.code(status).send({
      code: status === 400 ? 'BAD_REQUEST' : 'INTERNAL_ERROR',
      message: error.message,
      request_id: request.id,
    });
  });

  return {
    app,
    engine,
    logger,
    close: async (): Promise<void> => {
      await app.close().catch(() => undefined);
      await engine.shutdown().catch(() => undefined);
      platform.clearCredentials();
    },
  };
}

function errorBody(request: FastifyRequest, code: string, message: string): Record<string, unknown> {
  return { code, message, request_id: request.id };
}

/** Boot the gateway: config → policy → engine → HTTP listener. */
export async function startGateway(env: NodeJS.ProcessEnv = process.env): Promise<Gateway> {
  // The logger must exist BEFORE `loadConfig()`: a broken engine pointer is logged there
  // (`engine_pointer_invalid`) and "not silently" is the whole point (spec §A.3.7-4).
  const bootstrapLevel = (env['AGENT_GATEWAY_LOG_LEVEL'] ?? 'info').trim().toLowerCase();
  const bootstrapLogger = createLogger(
    (['debug', 'info', 'warn', 'error'].includes(bootstrapLevel) ? bootstrapLevel : 'info') as keyof typeof LEVELS,
    { component: 'gateway' },
  );
  const config = loadConfig(env, bootstrapLogger);
  const logger = createLogger(config.logLevel, { component: 'gateway' });
  if (config.pinSource === 'pointer') {
    logger.info('engine.pointer_used', {
      version: config.enginePin,
      env_pin: config.enginePinEnv,
      runtime_dir: config.engineRuntimeDir,
    });
  }

  // Fail fast on a broken policy: a gateway that silently registered nothing (or
  // everything) would be a permission bug, so this is a startup error, not a warning.
  try {
    const policy = loadPolicy(config.policyPath);
    logger.info('policy.loaded', { source: policy.source, enabled: policy.enabled.length, warnings: policy.warnings });
  } catch (err) {
    if (err instanceof PolicyError) {
      logger.error('policy.invalid', { problems: err.problems });
      throw err;
    }
    throw err;
  }

  const gateway = buildGateway({ config, logger });
  await gateway.engine.start();

  const status = gateway.engine.status();
  logger.info('gateway.starting', {
    bind: config.bind,
    port: config.port,
    pin: config.enginePin,
    pin_source: config.pinSource,
    engine: status.availability,
    mode: config.engineMode,
    ...(status.detail === undefined ? {} : { detail: status.detail }),
  });

  await gateway.app.listen({ host: config.bind, port: config.port });
  logger.info('gateway.listening', { url: `http://${config.bind}:${config.port}` });
  return gateway;
}

const isMain = process.argv[1] !== undefined && /[\\/]main\.(ts|js)$/.test(process.argv[1]);

if (isMain) {
  let gatewayRef: Gateway | null = null;
  const shutdown = (signal: string): void => {
    process.stdout.write(`${JSON.stringify({ ts: new Date().toISOString(), level: 'info', event: 'gateway.shutdown', signal })}\n`);
    void gatewayRef
      ?.close()
      .catch(() => undefined)
      .finally(() => {
        process.exit(0);
      });
    // Hard ceiling: a wedged engine must not keep the container alive.
    const timer = setTimeout(() => process.exit(1), 15_000);
    timer.unref?.();
  };
  process.once('SIGINT', () => shutdown('SIGINT'));
  process.once('SIGTERM', () => shutdown('SIGTERM'));

  startGateway()
    .then((gateway) => {
      gatewayRef = gateway;
    })
    .catch((err: unknown) => {
      const message = err instanceof ConfigError ? err.message : describeError(err);
      process.stderr.write(
        `${JSON.stringify({ ts: new Date().toISOString(), level: 'error', event: 'gateway.boot_failed', message })}\n`,
      );
      process.exit(2);
    });
}
