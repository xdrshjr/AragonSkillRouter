/**
 * A local stand-in for the Skill Router external API.
 *
 * It answers exactly the routes the installer uses, with the response shapes from
 * `backend/app/api/external.py`, and records every request so tests can assert what
 * the CLI actually sent. No network access, no credentials, no real platform.
 *
 * This is what "verified against a mock server" means in the README: the file
 * layout and the generated content are proven here; the platform's own responses
 * are exercised by backend/tests/test_external_api.py.
 */

import { createServer } from "node:http";

export const TEST_TOKEN = "sr_live_mock0000000000000000000000000000abcd";

/**
 * @param {{slug: string, name?: string, version?: number}[]} [capabilities]
 */
export function descriptorList(capabilities = [{ slug: "text.summarize", name: "Summarize", version: 2 }]) {
  return capabilities.map((entry) => ({
    slug: entry.slug,
    name: entry.name || entry.slug,
    type: "text",
    description: `${entry.name || entry.slug} capability (mock).`,
    versions: { current: entry.version === undefined ? 1 : entry.version, available: [1, entry.version === undefined ? 1 : entry.version] },
    modes: { sync: true, async: false, stream: true },
    input_schema: {
      type: "object",
      properties: { prompt: { type: "string" }, max_words: { type: "integer" } },
      required: ["prompt"],
      additionalProperties: false,
    },
    output_schema: { type: "object", properties: { summary: { type: "string" } } },
    limits: { timeout_s: 60, max_input_bytes: 262144, max_output_bytes: 8388608, file_input: false, max_file_mb: 10, max_duration_s: null },
    examples: [{ input: { prompt: "summarise this", max_words: 60 }, note: "Short summary." }],
    needs_live_agent: false,
    updated_at: "2026-10-08T09:14:05.000000Z",
    version_token: `cap-${entry.slug}`,
  }));
}

export class MockPlatform {
  /** @param {{token?: string, capabilities?: {slug: string, name?: string, version?: number}[], invocation?: any}} [options] */
  constructor(options = {}) {
    this.token = options.token || TEST_TOKEN;
    this.capabilities = descriptorList(options.capabilities);
    this.invocationResult = options.invocation || null;
    /** @type {{method: string, path: string, authorization: string, body: any}[]} */
    this.requests = [];
    this.server = createServer((req, res) => {
      void this.#handle(req, res);
    });
    this.url = "";
  }

  async start() {
    await new Promise((resolve) => this.server.listen(0, "127.0.0.1", resolve));
    const address = this.server.address();
    const port = typeof address === "object" && address ? address.port : 0;
    this.url = `http://127.0.0.1:${port}`;
    return this;
  }

  async stop() {
    await new Promise((resolve) => this.server.close(resolve));
  }

  /** @param {{slug: string, name?: string, version?: number}[]} capabilities */
  setCapabilities(capabilities) {
    this.capabilities = descriptorList(capabilities);
  }

  /** @param {any} result */
  setInvocationResult(result) {
    this.invocationResult = result;
  }

  /** @param {string} path */
  requestsTo(path) {
    return this.requests.filter((item) => item.path === path);
  }

  #versionToken() {
    return `vt-${this.capabilities.map((item) => item.slug).join("-")}`;
  }

  #json(res, status, body, headers = {}) {
    const payload = JSON.stringify(body);
    res.writeHead(status, { "content-type": "application/json", "content-length": Buffer.byteLength(payload), ...headers });
    res.end(payload);
  }

  #error(res, status, code, message, actions = []) {
    this.#json(res, status, { error: { code, message, details: null, request_id: "req_mock0001", actions } });
  }

  async #handle(req, res) {
    const url = new URL(req.url || "/", "http://127.0.0.1");
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const raw = Buffer.concat(chunks).toString("utf8");
    let body = null;
    if (raw) {
      try {
        body = JSON.parse(raw);
      } catch {
        body = raw;
      }
    }
    this.requests.push({ method: req.method || "GET", path: url.pathname, authorization: req.headers.authorization || "", body });

    const authorization = req.headers.authorization || "";
    const token = authorization.toLowerCase().startsWith("bearer ") ? authorization.slice(7).trim() : "";
    if (token !== this.token) {
      this.#error(res, 401, "invalid_token", "The agent token is not valid.", ["Issue a new token in Access -> Tokens."]);
      return;
    }

    if (url.pathname === "/api/v1/whoami") {
      this.#json(res, 200, {
        service: "skill-router",
        api_version: "1",
        agent: { id: "agent_mock01", name: "mock-research-bot" },
        scopes: this.capabilities.map((item) => item.slug),
        limits: { rate_limit_per_min: 60, max_concurrency: 4, budget_usd: 25 },
        counters: { calls_last_minute: 0, concurrency: 0 },
        capabilities_count: this.capabilities.length,
        capabilities_url: "/api/v1/capabilities",
        mcp_url: "/api/mcp",
        server_time: "2026-10-08T09:14:05.000000Z",
      });
      return;
    }

    if (url.pathname === "/api/v1/capabilities") {
      const versionToken = this.#versionToken();
      if (req.headers["if-none-match"] === `"${versionToken}"`) {
        res.writeHead(304, { etag: `"${versionToken}"` });
        res.end();
        return;
      }
      this.#json(
        res,
        200,
        {
          capabilities: this.capabilities,
          count: this.capabilities.length,
          version_token: versionToken,
          generated_at: "2026-10-08T09:14:05.000000Z",
          refresh_url: "/api/v1/capabilities?refresh=1",
          notes: ["mock"],
        },
        { etag: `"${versionToken}"` },
      );
      return;
    }

    if (url.pathname === "/api/v1/skill") {
      this.#json(res, 200, {
        schema: "skill-router.connection/v1",
        format: "skill",
        content_type: "text/markdown",
        platform: { base_url: this.url, api_base: "/api/v1", mcp_url: "/api/mcp" },
        version_token: this.#versionToken(),
        content: [
          "---",
          "name: skill-router-mock-research-bot",
          "description: Server-rendered connection SKILL (mock).",
          "schema: skill-router/external-skill@1",
          "---",
          "",
          "SERVER-RENDERED-BODY: this text comes from GET /api/v1/skill.",
          "",
          "## Granted capabilities",
          "",
          ...this.capabilities.map((item) => `- \`${item.slug}\` v${item.versions.current} - ${item.name}`),
        ].join("\n"),
        sha256: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        includes_credentials: false,
      });
      return;
    }

    if (url.pathname === "/api/v1/invocations" && req.method === "POST") {
      if (this.invocationResult) {
        this.#json(res, 200, this.invocationResult);
        return;
      }
      this.#json(res, 200, {
        request_id: "req_mockinv01",
        invocation_id: "inv_mock01",
        status: "succeeded",
        mode: "sync",
        capability: body && body.capability,
        version: 1,
        task_id: null,
        result: { summary: "mocked summary" },
        artifacts: [],
        usage: { input_tokens: 10, output_tokens: 5 },
        cost: { estimated_usd: 0.0001, actual_usd: null, currency: "USD" },
        provider: { id: "prov_mock", name: "Mock Provider" },
        route_reason: "mock",
        error: null,
        http_status: 200,
        latency_ms: 42,
        attempts: 1,
        deduplicated: false,
      });
      return;
    }

    this.#error(res, 404, "not_found", `No route for ${req.method} ${url.pathname}.`);
  }
}

/**
 * @param {{token?: string, capabilities?: any[], invocation?: any}} [options]
 * @returns {Promise<MockPlatform>}
 */
export async function startMockPlatform(options) {
  const platform = new MockPlatform(options);
  await platform.start();
  return platform;
}
