/**
 * HTTP surface (CONTRACT §10).
 *
 * node:http only — no framework. Every route except `/health` requires
 * `Authorization: Bearer $SR_ARAGON_SIDECAR_TOKEN`, compared in constant time.
 * The listener is loopback-only (config refuses anything else), so there is no
 * CORS layer to get wrong and no TLS to terminate.
 *
 * SSE IS THE POINT OF THIS FILE. `GET /sessions/{id}/events` sends a real
 * `text/event-stream` with:
 *   * `id:` = the session's monotonic `seq`, so a client that reconnects with
 *     `Last-Event-ID` (or `?since=`) gets exactly the events it missed;
 *   * a backlog replay first, because the platform attaches *after* the 202 that
 *     accepted the message and would otherwise miss `run.started`;
 *   * `: keepalive` comments so a proxy or the platform's read timeout does not
 *     kill a stream that is merely quiet (a long tool call is quiet).
 */

import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { timingSafeEqual } from "node:crypto";

import { defaultModelConfigured, supportedProviders, type SidecarConfig } from "./config.js";
import { PLATFORM_TOOL_NAMES, ToolCeilingError } from "./tools.js";
import { SidecarSession, SessionError, type SessionCreateInput, type SessionOptions } from "./session.js";
import { checkReleases, type ReleaseCheckResult } from "./releases.js";

export const ADAPTER_VERSION = "1.0.0";
export const SIDECAR_VERSION = "1.0.0";

const SESSION_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

export interface SidecarServer {
  server: Server;
  sessions: Map<string, SidecarSession>;
  info(): Record<string, unknown>;
  close(): Promise<void>;
}

interface Route {
  method: string;
  segments: string[];
}

function secureEquals(a: string, b: string): boolean {
  const left = Buffer.from(a, "utf8");
  const right = Buffer.from(b, "utf8");
  if (left.length !== right.length) {
    // Still burn a comparison so length is not a timing oracle for the prefix.
    timingSafeEqual(left, left);
    return false;
  }
  return timingSafeEqual(left, right);
}

function sendJson(res: ServerResponse, status: number, body: unknown): void {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(payload).toString(),
    "cache-control": "no-store",
  });
  res.end(payload);
}

function sendError(
  res: ServerResponse,
  status: number,
  code: string,
  message: string,
  extra: Record<string, unknown> = {},
): void {
  sendJson(res, status, { ok: false, code, message, ...extra });
}

async function readBody(req: IncomingMessage, limit: number): Promise<unknown> {
  const chunks: Buffer[] = [];
  let total = 0;
  for await (const chunk of req) {
    const buf = chunk as Buffer;
    total += buf.length;
    if (total > limit) {
      throw new SessionError("payload_too_large", `Request body exceeds ${limit} bytes.`, 413);
    }
    chunks.push(buf);
  }
  if (total === 0) return {};
  const text = Buffer.concat(chunks).toString("utf8");
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new SessionError("invalid_json", "Request body is not valid JSON.", 400);
  }
}

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function parseSeq(raw: string | null | undefined): number | null {
  if (raw === null || raw === undefined || raw === "") return null;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}

export function createSidecarServer(config: SidecarConfig): SidecarServer {
  const sessions = new Map<string, SidecarSession>();

  const sessionOptions: SessionOptions = {
    eventBufferSize: config.eventBufferSize,
    toolTimeoutMs: config.toolTimeoutMs,
    llmCallTimeoutMs: config.llmCallTimeoutMs,
    idleTimeoutMs: config.idleTimeoutMs,
    maxTokens: config.maxTokens,
    maxToolOutputChars: config.maxToolOutputChars,
    stopSettleMs: config.stopSettleMs,
  };

  const fallbackModel =
    defaultModelConfigured(config)
      ? { providerId: config.defaultProviderId, modelId: config.defaultModelId, ...(config.defaultBaseUrl ? { baseUrl: config.defaultBaseUrl } : {}) }
      : null;

  let releaseCache: { at: number; value: ReleaseCheckResult } | null = null;
  const RELEASE_CACHE_MS = 30_000;

  function info(): Record<string, unknown> {
    const sessionStats = [...sessions.values()].map((session) => session.stats());
    const unknownEvents = sessionStats.reduce((sum, stat) => sum + stat.unknown_events, 0);
    const absorbedEvents = sessionStats.reduce((sum, stat) => sum + stat.absorbed_events, 0);
    const seen: Record<string, number> = {};
    for (const stat of sessionStats) {
      for (const [key, value] of Object.entries(stat.seen)) seen[key] = (seen[key] ?? 0) + value;
    }
    const registeredNames = new Set<string>();
    for (const session of sessions.values()) {
      const described = session.describe() as { tools?: unknown };
      if (Array.isArray(described.tools)) {
        for (const name of described.tools) registeredNames.add(String(name));
      }
    }

    return {
      ok: true,
      engine: {
        package: config.enginePackage,
        version: config.engineVersion,
        commit: config.engineCommit,
        commit_source: config.engineCommit ? "env:SR_ARAGON_ENGINE_COMMIT" : "unknown",
        adapter_version: ADAPTER_VERSION,
        sidecar_version: SIDECAR_VERSION,
      },
      model_configured: fallbackModel !== null || sessions.size > 0,
      model_template: {
        configured: fallbackModel !== null,
        provider_id: config.defaultProviderId || null,
        model_id: config.defaultModelId || null,
        base_url_configured: config.defaultBaseUrl !== null,
        source: fallbackModel !== null ? "env" : sessions.size > 0 ? "session" : "none",
        supported_providers: supportedProviders(),
      },
      providers: supportedProviders().map((id) => ({
        id,
        default_base_url: id === "openai" ? "https://api.openai.com/v1" : null,
        base_url_override_supported: true,
      })),
      tools: {
        allowed: [...PLATFORM_TOOL_NAMES],
        registered_now: [...registeredNames].sort(),
        ceiling:
          "Only the platform.* names above can ever be registered. Shell, file, " +
          "code-execution and web tools are not merely denied — they are unnameable.",
      },
      features: [
        "sse",
        "last-event-id-resume",
        "backlog-replay",
        "stop-report",
        "platform-callbacks",
        "base-url-override",
        "releases-check",
      ],
      limits: {
        max_sessions: config.maxSessions,
        sessions_live: sessions.size,
        event_buffer: config.eventBufferSize,
        tool_timeout_ms: config.toolTimeoutMs,
        llm_call_timeout_ms: config.llmCallTimeoutMs,
        idle_timeout_ms: config.idleTimeoutMs,
        max_tokens: config.maxTokens,
        max_tool_output_chars: config.maxToolOutputChars,
        body_limit_bytes: config.bodyLimitBytes,
      },
      unknown_events: unknownEvents,
      absorbed_events: absorbedEvents,
      observed_upstream_events: seen,
      channels: {
        configured: config.channel,
        track_branch: config.trackBranch,
        repo_url: config.repoUrl,
      },
    };
  }

  /** SSE handler. Kept out of the route switch so its lifecycle is obvious. */
  function openEventStream(req: IncomingMessage, res: ServerResponse, session: SidecarSession): void {
    const url = new URL(req.url ?? "/", "http://127.0.0.1");
    const after =
      parseSeq(req.headers["last-event-id"] as string | undefined) ?? parseSeq(url.searchParams.get("since"));

    const replay = session.bus.replaySince(after);
    res.writeHead(200, {
      "content-type": "text/event-stream; charset=utf-8",
      "cache-control": "no-cache, no-transform",
      connection: "keep-alive",
      // Defeat proxy buffering: without this the stream looks dead for minutes.
      "x-accel-buffering": "no",
      "x-replay-gap": replay.gap ? "1" : "0",
      "x-last-event-id": String(session.bus.lastSeq),
    });
    res.write(`: connected session=${session.id} after=${after ?? "start"} gap=${replay.gap ? "1" : "0"}\n\n`);
    if (replay.gap) {
      // A hole in the replay is stated, never hidden: the platform can then
      // re-read its own persisted events instead of pretending continuity.
      res.write(`: replay gap — oldest retained seq is ${session.bus.oldestSeq}\n\n`);
    }

    for (const event of replay.events) writeEvent(res, event);

    const unsubscribe = session.bus.subscribe((event) => writeEvent(res, event));
    const keepalive = setInterval(() => {
      res.write(`: keepalive ${new Date().toISOString()}\n\n`);
    }, config.keepaliveMs);
    // Keep the process from being kept awake by the timer alone.
    keepalive.unref?.();

    const cleanup = () => {
      clearInterval(keepalive);
      unsubscribe();
    };
    req.on("close", cleanup);
    req.on("error", cleanup);
    res.on("error", cleanup);
    res.on("close", cleanup);
  }

  function writeEvent(res: ServerResponse, event: { seq: number; type: string }): void {
    if (res.writableEnded || res.destroyed) return;
    const { seq, type, ...rest } = event as Record<string, unknown> & { seq: number; type: string };
    const frame = { seq, type, ...rest };
    res.write(`id: ${seq}\nevent: ${type}\ndata: ${JSON.stringify(frame)}\n\n`);
  }

  async function handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const url = new URL(req.url ?? "/", "http://127.0.0.1");
    const segments = url.pathname.split("/").filter((part) => part !== "").map(decodeURIComponent);
    const route: Route = { method: (req.method ?? "GET").toUpperCase(), segments };

    // ---- liveness: the only unauthenticated route ---------------------------
    if (route.method === "GET" && segments.length === 1 && segments[0] === "health") {
      const stat = info() as { engine: unknown; model_configured: boolean; model_template: unknown };
      sendJson(res, 200, {
        ok: true,
        engine: stat.engine,
        model_configured: stat.model_configured,
        model_template: stat.model_template,
        sessions: sessions.size,
        auth_required: true,
        uptime_s: Math.round(process.uptime()),
      });
      return;
    }

    // ---- bearer auth for everything else -----------------------------------
    const header = req.headers.authorization ?? "";
    const provided = header.toLowerCase().startsWith("bearer ") ? header.slice(7).trim() : "";
    if (provided === "" || !secureEquals(provided, config.token)) {
      sendError(
        res,
        401,
        "unauthenticated",
        "Missing or invalid bearer token. Send Authorization: Bearer $SR_ARAGON_SIDECAR_TOKEN.",
      );
      return;
    }

    if (route.method === "GET" && segments.length === 1 && segments[0] === "info") {
      sendJson(res, 200, info());
      return;
    }

    if (route.method === "GET" && segments.length === 2 && segments[0] === "releases" && segments[1] === "check") {
      const channel = url.searchParams.get("channel") ?? config.channel;
      const now = Date.now();
      if (releaseCache && releaseCache.value.channel === channel && now - releaseCache.at < RELEASE_CACHE_MS) {
        sendJson(res, 200, { ...releaseCache.value, cached: true });
        return;
      }
      const value = await checkReleases({
        repoUrl: config.repoUrl,
        enginePackage: config.enginePackage,
        channel,
        trackBranch: config.trackBranch,
        timeoutMs: config.releaseCheckTimeoutMs,
      });
      releaseCache = { at: now, value };
      // A failed detection is a 502, not a 200 with ok:false, so the platform's
      // HTTP client cannot mistake it for a successful reading of the remote.
      sendJson(res, value.ok ? 200 : 502, value);
      return;
    }

    // ---- sessions ----------------------------------------------------------
    if (segments[0] !== "sessions") {
      sendError(res, 404, "not_found", `No route for ${route.method} ${url.pathname}.`);
      return;
    }
    if (segments.length === 1) {
      if (route.method !== "POST") {
        sendError(res, 405, "method_not_allowed", "Use POST /sessions.");
        return;
      }
      const body = asRecord(await readBody(req, config.bodyLimitBytes));
      const requestedId = typeof body.session_id === "string" ? body.session_id.trim() : "";
      const sessionId = requestedId === "" ? `ses_${Date.now().toString(36)}` : requestedId;
      if (!SESSION_ID.test(sessionId)) {
        throw new SessionError(
          "invalid_session_id",
          `session_id must match ${SESSION_ID.source} (it becomes part of a URL path).`,
          400,
        );
      }
      let session = sessions.get(sessionId);
      const isNew = session === undefined;
      if (isNew) {
        if (sessions.size >= config.maxSessions) {
          throw new SessionError(
            "session_limit",
            `This sidecar already holds ${sessions.size} sessions (max ${config.maxSessions}). ` +
              "Dispose finished sessions (DELETE /sessions/{id}) or raise SR_ARAGON_MAX_SESSIONS.",
            429,
          );
        }
        const created = new SidecarSession(sessionId, sessionOptions);
        try {
          created.configure(body as SessionCreateInput, { model: fallbackModel });
        } catch (error) {
          // A rejected create must not leave a half-configured session behind.
          sessions.delete(sessionId);
          throw error;
        }
        sessions.set(sessionId, created);
        sendJson(res, 201, { ok: true, resumed: false, ...created.describe() });
        return;
      }
      session.configure(body as SessionCreateInput, { model: fallbackModel });
      sessions.set(sessionId, session);
      sendJson(res, 200, { ok: true, resumed: true, ...session.describe() });
      return;
    }

    const session = sessions.get(segments[1] ?? "");
    if (session === undefined) {
      sendError(res, 404, "session_not_found", `No session '${segments[1]}' in this process.`);
      return;
    }

    // GET /sessions/{id}
    if (segments.length === 2) {
      if (route.method === "GET") {
        sendJson(res, 200, { ok: true, ...session.transcript() });
        return;
      }
      if (route.method === "DELETE") {
        await session.dispose();
        sessions.delete(session.id);
        sendJson(res, 200, { ok: true, disposed: true, session_id: session.id });
        return;
      }
      sendError(res, 405, "method_not_allowed", "Use GET or DELETE on /sessions/{id}.");
      return;
    }

    const action = segments[2];

    if (action === "messages" && segments.length === 3) {
      if (route.method !== "POST") {
        sendError(res, 405, "method_not_allowed", "Use POST /sessions/{id}/messages.");
        return;
      }
      const body = asRecord(await readBody(req, config.bodyLimitBytes));
      const text = typeof body.text === "string" ? body.text : "";
      const runId =
        typeof body.run_id === "string" && /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(body.run_id)
          ? body.run_id
          : `run_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
      const started = await session.startRun(runId, text);
      // 202: the turn is accepted, not finished. Progress is on the event stream.
      sendJson(res, 202, {
        ok: true,
        accepted: true,
        session_id: session.id,
        run_id: started.run_id,
        seq: started.seq,
        events: `/sessions/${session.id}/events`,
      });
      return;
    }

    if (action === "events" && segments.length === 3) {
      if (route.method !== "GET") {
        sendError(res, 405, "method_not_allowed", "Use GET /sessions/{id}/events.");
        return;
      }
      openEventStream(req, res, session);
      return;
    }

    if (action === "stop" && segments.length === 3) {
      if (route.method !== "POST") {
        sendError(res, 405, "method_not_allowed", "Use POST /sessions/{id}/stop.");
        return;
      }
      await readBody(req, config.bodyLimitBytes).catch(() => ({}));
      const report = await session.stop();
      sendJson(res, 200, { ok: true, ...report });
      return;
    }

    sendError(res, 404, "not_found", `No route for ${route.method} ${url.pathname}.`);
  }

  const server = createServer((req, res) => {
    handle(req, res).catch((error: unknown) => {
      if (res.writableEnded || res.destroyed) return;
      if (error instanceof SessionError) {
        sendError(res, error.status, error.code, error.message, {
          details: error.details,
        });
        return;
      }
      if (error instanceof ToolCeilingError) {
        // A permission-ceiling violation is the caller's mistake, not a crash.
        sendError(res, 400, "tool_not_allowed", error.message, {
          details: { rejected: error.rejected, allowed: [...PLATFORM_TOOL_NAMES] },
        });
        return;
      }
      const message = error instanceof Error ? error.message : String(error);
      // Never echo a stack or a request body: it may contain a provider key.
      sendError(res, 500, "internal_error", `Sidecar failed: ${message}`);
    });
  });

  server.headersTimeout = 0;
  server.requestTimeout = 0;
  server.keepAliveTimeout = 65_000;

  return {
    server,
    sessions,
    info,
    close: async () => {
      for (const session of [...sessions.values()]) {
        await session.dispose().catch(() => undefined);
      }
      sessions.clear();
      await new Promise<void>((resolve) => server.close(() => resolve()));
    },
  };
}
