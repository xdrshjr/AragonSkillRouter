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
import Fastify, { type FastifyInstance, type FastifyReply, type FastifyRequest } from 'fastify';

import { ConfigError, loadConfig, type Config } from './config.js';
import { EngineManager, TurnConflictError, EngineUnavailableError, describeError } from './engine.js';
import { buildHealthReport } from './health.js';
import { PlatformClient } from './platform-client.js';
import { loadPolicy, PolicyError } from './policy.js';
import type { SseSink } from './sse.js';
import type { Logger, TurnRequest } from './types.js';

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

export function buildGateway(options: GatewayOptions): Gateway {
  const { config } = options;
  const logger = options.logger ?? createLogger(config.logLevel);
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
    const report = buildHealthReport({ engine, pin: config.enginePin });
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

  // -- engine hot reload (spec §7.12-6 / §12.4-4) ----------------------------
  app.post('/internal/admin/reload', async (request, reply) => {
    const body = request.body;
    const target = (body as { target_version?: unknown } | null)?.target_version;
    if (typeof target !== 'string' || target.trim() === '') {
      return reply
        .code(400)
        .send(errorBody(request, 'INVALID_BODY', 'target_version is required (exact semver, e.g. "0.2.37")'));
    }
    try {
      const result = await engine.reload(target.trim());
      logger[result.ok ? 'info' : 'warn']('engine.reload', {
        request_id: request.id,
        ok: result.ok,
        stage: result.stage,
        engine_version: result.engine_version,
        detail: result.detail,
      });
      return reply.code(200).send(result);
    } catch (err) {
      if (err instanceof EngineUnavailableError) {
        return reply.code(503).send(errorBody(request, 'ENGINE_UNAVAILABLE', err.detail));
      }
      logger.error('engine.reload_failed', { request_id: request.id, error: describeError(err) });
      return reply.code(500).send(errorBody(request, 'RELOAD_FAILED', describeError(err)));
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
  const config = loadConfig(env);
  const logger = createLogger(config.logLevel, { component: 'gateway' });

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
