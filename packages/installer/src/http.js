/**
 * Minimal HTTP client on top of the global `fetch` (Node >= 18). No dependencies.
 *
 * Two behaviours matter for a connectivity tool:
 *   * a transport failure is reported with the URL and a *guess at the cause*,
 *     not with `fetch failed`;
 *   * a platform error envelope is unpacked into code/message/actions so `verify`
 *     can print the platform's own advice instead of inventing its own.
 */

import { ConnectError } from "./errors.js";

export const DEFAULT_TIMEOUT_MS = 20000;

/**
 * Normalise what an operator types into a bare origin.
 * Accepts `http://host:8000`, `http://host:8000/`, `http://host:8000/api/v1`,
 * and a host with no scheme (assumed http).
 * @param {string} raw
 * @returns {string}
 */
export function normaliseBaseUrl(raw) {
  let value = String(raw || "").trim();
  if (!value) {
    throw new ConnectError("No platform URL was supplied.", {
      actions: ["Pass --url https://your-skill-router.example.com", "Or run `install` once so the URL is stored in .skill-router.json."],
    });
  }
  if (!/^https?:\/\//i.test(value)) value = `http://${value}`;
  value = value.replace(/\/+$/, "");
  value = value.replace(/\/api\/v1$/i, "").replace(/\/api$/i, "");
  return value;
}

/**
 * @typedef {object} HttpResponse
 * @property {number} status
 * @property {any} body        parsed JSON, or null when the body was not JSON
 * @property {string} text     raw body (truncated for very large payloads)
 * @property {Headers} headers
 */

/**
 * @param {string} url
 * @param {{method?: string, token?: string, body?: unknown, timeoutMs?: number, accept?: string, extraHeaders?: Record<string, string>}} [options]
 * @returns {Promise<HttpResponse>}
 */
export async function httpRequest(url, options = {}) {
  const method = options.method || "GET";
  const timeoutMs = options.timeoutMs || DEFAULT_TIMEOUT_MS;
  /** @type {Record<string, string>} */
  const headers = { accept: options.accept || "application/json" };
  if (options.extraHeaders) Object.assign(headers, options.extraHeaders);
  if (options.token) headers.authorization = `Bearer ${options.token}`;
  let payload;
  if (options.body !== undefined) {
    headers["content-type"] = "application/json";
    payload = JSON.stringify(options.body);
  }

  let response;
  try {
    response = await fetch(url, { method, headers, body: payload, signal: AbortSignal.timeout(timeoutMs) });
  } catch (error) {
    throw transportError(url, error, timeoutMs);
  }

  const text = await response.text().catch(() => "");
  let body = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
  }
  return { status: response.status, body, text, headers: response.headers };
}

/**
 * Turn `fetch` noise into something an operator can act on.
 * @param {string} url
 * @param {unknown} error
 * @param {number} timeoutMs
 * @returns {ConnectError}
 */
function transportError(url, error, timeoutMs) {
  const name = error && /** @type {any} */ (error).name;
  const code = error && /** @type {any} */ (error).cause && /** @type {any} */ (error).cause.code;
  if (name === "TimeoutError" || name === "AbortError") {
    return new ConnectError(`No answer from ${url} within ${Math.round(timeoutMs / 1000)}s.`, {
      code: "connect_timeout",
      actions: [
        "Check the API is running: `python -m app.cli doctor` in backend/, or `GET /api/health`.",
        "Check that the URL is the platform the browser uses (no /api/v1 suffix needed).",
        "Raise the limit with `--timeout-ms 60000` on a slow link.",
      ],
    });
  }
  const reason = code === "ENOTFOUND" ? "the host name does not resolve" : code === "ECONNREFUSED" ? "nothing is listening on that port" : error instanceof Error ? error.message : String(error);
  return new ConnectError(`Cannot reach ${url}: ${reason}.`, {
    code: "connect_failed",
    details: code || null,
    actions: [
      "Check the URL and that the API process (or nginx) is up.",
      "If the platform runs in Docker, the API is on port 8000 inside the compose network; use the public URL from outside.",
    ],
  });
}

/**
 * Unpack the platform error envelope
 * `{"error": {"code","message","details","request_id","actions"}}`.
 * Falls back to the raw text so an nginx 502 page is still readable.
 *
 * @param {HttpResponse} response
 * @param {string} what  short description of the call, for the message
 * @returns {ConnectError}
 */
export function errorFromResponse(response, what) {
  const envelope = response.body && typeof response.body === "object" ? response.body.error : null;
  if (envelope && typeof envelope === "object") {
    const code = String(envelope.code || `http_${response.status}`);
    const message = String(envelope.message || "The platform refused the request.");
    const requestId = envelope.request_id ? ` (request id ${envelope.request_id})` : "";
    const details = envelope.details === undefined ? null : envelope.details;
    return new ConnectError(`${what} failed: ${code} - ${message}${requestId}`, {
      code,
      details,
      actions: Array.isArray(envelope.actions) && envelope.actions.length ? [...envelope.actions] : defaultActions(code),
    });
  }
  const hint = response.status === 401 ? "The token was rejected." : response.status === 404 ? "That route does not exist on this server." : response.status === 502 || response.status === 503 ? "A proxy or the API itself is unavailable." : `HTTP ${response.status}.`;
  const snippet = (response.text || "").replace(/\s+/g, " ").slice(0, 200);
  return new ConnectError(`${what} failed: HTTP ${response.status} - ${hint}${snippet ? ` Body: ${snippet}` : ""}`, {
    code: `http_${response.status}`,
    actions: defaultActions(`http_${response.status}`),
  });
}

/**
 * @param {string} code
 * @returns {string[]}
 */
export function defaultActions(code) {
  switch (code) {
    case "unauthenticated":
    case "invalid_token":
      return ["Re-copy the token from Access -> Tokens on the platform.", "Check for a trailing newline or quotes around the pasted token."];
    case "token_revoked":
      return ["The token was revoked. Issue a new one in Access -> Tokens, then re-run `install --force`."];
    case "token_expired":
      return ["The token expired. Issue a new one in Access -> Tokens, then re-run `install --force`."];
    case "capability_not_granted":
      return ["Grant the capability to this agent in Access -> Agents, then refresh the catalogue."];
    case "capability_not_published":
      return ["Publish the capability version on the platform, or ask its owner to."];
    case "rate_limited":
      return ["Wait for the window to reset (see `limits.rate_limit_per_min` in `whoami`).", "Ask the administrator to raise the limit."];
    case "concurrency_limited":
      return ["Wait for an in-flight call to finish, then retry."];
    case "budget_exceeded":
      return ["Ask the administrator to raise the agent budget."];
    case "input_schema_mismatch":
    case "unsupported_parameter":
      return ["Compare `--input` with the capability's input_schema (`GET /api/v1/capabilities/{slug}`)."];
    default:
      return ["Run `skill-router-connect verify` for the connectivity check.", "Read the response `request_id` and look it up in Records on the platform."];
  }
}
