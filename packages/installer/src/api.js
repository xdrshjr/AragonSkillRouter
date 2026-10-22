/**
 * The three `/api/v1` calls the installer needs, plus the optional invocation used
 * by `verify --capability`.
 *
 * Shapes are taken from `backend/app/api/external.py`:
 *   GET  /api/v1/whoami                  -> {agent, scopes, limits, counters, capabilities_count, mcp_url}
 *   GET  /api/v1/capabilities            -> {capabilities[], count, version_token, generated_at, refresh_url}
 *   GET  /api/v1/skill                   -> {content (markdown), sha256, version_token, platform{...}, ...}
 *   POST /api/v1/invocations             -> {request_id, status, result, error, latency_ms, ...}
 */

import { ConnectError } from "./errors.js";
import { errorFromResponse, httpRequest, normaliseBaseUrl } from "./http.js";

export const API_PREFIX = "/api/v1";

/**
 * @typedef {{baseUrl: string, token: string, timeoutMs?: number}} Connection
 */

/**
 * @param {Connection} connection
 * @returns {string}
 */
function apiBase(connection) {
  return `${normaliseBaseUrl(connection.baseUrl)}${API_PREFIX}`;
}

/**
 * `GET /api/v1/whoami` - the connectivity check. Never carries the token back.
 * @param {Connection} connection
 */
export async function getWhoami(connection) {
  const url = `${apiBase(connection)}/whoami`;
  const response = await httpRequest(url, { token: connection.token, timeoutMs: connection.timeoutMs });
  if (response.status === 401) throw errorFromResponse(response, "GET /api/v1/whoami");
  if (response.status !== 200 || !response.body) throw errorFromResponse(response, "GET /api/v1/whoami");
  return response.body;
}

/**
 * `GET /api/v1/capabilities` - granted, published capabilities plus a version token.
 * Sends `If-None-Match` when a previous token is known so an unchanged catalogue
 * answers 304; the caller then keeps the descriptors it already has.
 *
 * @param {Connection} connection
 * @param {{knownVersionToken?: string|null, refresh?: boolean}} [options]
 */
export async function getCapabilities(connection, options = {}) {
  const url = `${apiBase(connection)}/capabilities${options.refresh ? "?refresh=1" : ""}`;
  const response = await httpRequest(url, {
    token: connection.token,
    timeoutMs: connection.timeoutMs,
    extraHeaders: options.knownVersionToken ? { "if-none-match": `"${options.knownVersionToken}"` } : undefined,
  });
  if (response.status === 304) return { notModified: true, capabilities: [], version_token: options.knownVersionToken || null, count: null };
  if (response.status !== 200 || !response.body) throw errorFromResponse(response, "GET /api/v1/capabilities");
  return {
    notModified: false,
    capabilities: Array.isArray(response.body.capabilities) ? response.body.capabilities : [],
    version_token: response.body.version_token || null,
    count: typeof response.body.count === "number" ? response.body.count : null,
    generated_at: response.body.generated_at || null,
    refresh_url: response.body.refresh_url || `${API_PREFIX}/capabilities?refresh=1`,
  };
}

/**
 * `GET /api/v1/skill` - the server-rendered connection SKILL (markdown in
 * `content`, JSON variant in `document`). Contains no credentials.
 * @param {Connection} connection
 */
export async function getSkill(connection) {
  const url = `${apiBase(connection)}/skill`;
  const response = await httpRequest(url, { token: connection.token, timeoutMs: connection.timeoutMs });
  if (response.status !== 200 || !response.body) throw errorFromResponse(response, "GET /api/v1/skill");
  const bundle = response.body;
  if (typeof bundle.content !== "string" || bundle.content.trim() === "") {
    throw new ConnectError("GET /api/v1/skill answered without a markdown `content` field.", {
      code: "skill_bundle_empty",
      actions: ["Check the platform version: /api/v1/skill must include `content`.", "Fall back to `install --force`, which renders the SKILL from /api/v1/capabilities."],
    });
  }
  return bundle;
}

/**
 * `POST /api/v1/invocations` - one real call. Used by `verify --capability`.
 * @param {Connection} connection
 * @param {{capability: string, input?: Record<string, unknown>, version?: number|string|null, mode?: string}} request
 */
export async function invokeCapability(connection, request) {
  const url = `${apiBase(connection)}/invocations`;
  /** @type {Record<string, unknown>} */
  const body = { capability: request.capability, input: request.input || {} };
  if (request.version !== undefined && request.version !== null) body.version = request.version;
  if (request.mode) body.options = { mode: request.mode };
  const response = await httpRequest(url, { method: "POST", token: connection.token, body, timeoutMs: Math.max(connection.timeoutMs || 0, 60000) });
  if (response.status >= 400) throw errorFromResponse(response, `POST /api/v1/invocations (${request.capability})`);
  if (!response.body) throw errorFromResponse(response, `POST /api/v1/invocations (${request.capability})`);
  return response.body;
}
