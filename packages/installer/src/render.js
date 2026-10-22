/**
 * SKILL document rendering.
 *
 * The document is deliberately deterministic: no timestamps, no random ids. Two
 * runs against an unchanged platform produce byte-identical output, which is what
 * makes `install` idempotent (and lets the test suite assert it).
 *
 * The token never appears here. The document names the config file that holds it.
 */

import { MARKER } from "./targets.js";

export const INSTALLER_VERSION = "1.0.0";

/**
 * @param {string} value
 * @returns {string}
 */
export function slugify(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48) || "agent";
}

/**
 * @param {any} whoami
 * @returns {string}
 */
export function agentNameOf(whoami) {
  const name = whoami && whoami.agent && whoami.agent.name;
  return typeof name === "string" && name.trim() ? name.trim() : "agent";
}

/**
 * @param {any} whoami
 * @returns {string}
 */
export function skillNameOf(whoami) {
  return `skill-router-${slugify(agentNameOf(whoami))}`;
}

/**
 * @param {{capabilities: any[], versionToken: string|null, agentName: string, baseUrl: string}} context
 * @returns {string[]}
 */
function frontmatterLines(context) {
  const count = context.capabilities.length;
  const description = count === 0
    ? `Skill Router connection at ${context.baseUrl} for agent "${context.agentName}". No capability is granted yet; refresh the catalogue after access is granted.`
    : `Skill Router connection at ${context.baseUrl} for agent "${context.agentName}": call ${count} granted capabilit${count === 1 ? "y" : "ies"} over HTTP or MCP instead of holding provider keys.`;
  return [
    "---",
    `name: ${skillNameOf({ agent: { name: context.agentName } })}`,
    // Quoted: the description contains ": " and quotes, which a plain YAML scalar
    // cannot hold. Frontmatter that fails to parse makes the whole SKILL invisible.
    `description: ${yamlQuote(description)}`,
    "allowed-tools: Bash(curl:*)",
    "---",
  ];
}

/**
 * Double-quoted YAML scalar.
 * @param {string} value
 * @returns {string}
 */
export function yamlQuote(value) {
  return `"${String(value).replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
}

/**
 * A plausible input object from a JSON schema, for the example call.
 * @param {any} schema
 * @returns {Record<string, unknown>}
 */
export function sampleInput(schema) {
  const properties = schema && typeof schema === "object" && schema.properties ? schema.properties : {};
  const required = Array.isArray(schema && schema.required) ? schema.required : Object.keys(properties);
  /** @type {Record<string, unknown>} */
  const out = {};
  for (const key of required) {
    const property = properties[key] || {};
    const type = property.type || "string";
    if (Array.isArray(property.enum) && property.enum.length) out[key] = property.enum[0];
    else if (type === "string") out[key] = `<${key}>`;
    else if (type === "integer" || type === "number") out[key] = 0;
    else if (type === "boolean") out[key] = false;
    else if (type === "array") out[key] = [];
    else out[key] = {};
  }
  return out;
}

/**
 * @param {string} baseUrl
 * @param {string} slug
 * @param {Record<string, unknown>} input
 * @param {number|null} version
 * @returns {string}
 */
export function curlInvoke(baseUrl, slug, input, version) {
  /** @type {Record<string, unknown>} */
  const body = { capability: slug, input };
  if (version !== null && version !== undefined) body.version = version;
  const json = JSON.stringify(body);
  return [
    `curl -s -X POST ${baseUrl}/api/v1/invocations \\`,
    '  -H "Authorization: Bearer $SKILL_ROUTER_TOKEN" \\',
    '  -H "content-type: application/json" \\',
    `  -d '${json}'`,
  ].join("\n");
}

/**
 * @param {any} capability
 * @returns {number|null}
 */
function currentVersion(capability) {
  const versions = capability && capability.versions;
  if (versions && typeof versions.current === "number") return versions.current;
  if (typeof capability.version === "number") return capability.version;
  return null;
}

/**
 * @param {any} capability
 * @returns {string[]}
 */
function modeNames(capability) {
  const modes = (capability && capability.modes) || {};
  const names = Object.keys(modes).filter((key) => modes[key] === true);
  return names.length ? names : ["sync"];
}

/**
 * The capability catalogue: one section per granted capability with its schema and
 * a call example. This is the part agents actually read.
 * @param {any[]} capabilities
 * @param {string} baseUrl
 * @returns {string[]}
 */
function catalogue(capabilities, baseUrl) {
  if (capabilities.length === 0) {
    return [
      "No capability is granted to this token yet.",
      "",
      "Ask the administrator to grant capabilities in **Access -> Agents**, then run",
      "`skill-router-connect update` (or refresh the catalogue inline, see section 2).",
    ];
  }
  /** @type {string[]} */
  const lines = [];
  for (const capability of capabilities) {
    const version = currentVersion(capability);
    lines.push(`### \`${capability.slug}\` - ${capability.name || capability.slug}`);
    lines.push("");
    if (capability.description) {
      lines.push(String(capability.description).trim(), "");
    }
    lines.push(`- version: ${version === null ? "unknown" : version}`);
    lines.push(`- modes: ${modeNames(capability).join(", ")}`);
    if (capability.needs_live_agent) lines.push("- needs live agent reasoning: yes (the platform will not simulate it)");
    const limits = capability.limits || {};
    const limitParts = Object.entries(limits)
      .filter(([, value]) => value !== null && value !== undefined && value !== false)
      .map(([key, value]) => `${key}=${value}`);
    if (limitParts.length) lines.push(`- limits: ${limitParts.join(", ")}`);
    lines.push(`- MCP tool name: \`${String(capability.slug).replace(/\./g, "__")}\``);
    lines.push("");
    lines.push("Input schema:");
    lines.push("");
    lines.push("```json");
    lines.push(JSON.stringify(capability.input_schema || { type: "object", properties: {} }, null, 2));
    lines.push("```");
    lines.push("");
    const example = Array.isArray(capability.examples) && capability.examples.length ? capability.examples[0] : null;
    const input = example && example.input && typeof example.input === "object" ? example.input : sampleInput(capability.input_schema);
    lines.push("Call example:");
    lines.push("");
    lines.push("```bash");
    lines.push(curlInvoke(baseUrl, capability.slug, input, version));
    lines.push("```");
    lines.push("");
  }
  return lines;
}

/**
 * Build the whole SKILL document.
 *
 * @param {{
 *   baseUrl: string,
 *   whoami: any,
 *   capabilities: any[],
 *   versionToken: string|null,
 * }} context
 * @returns {string}
 */
export function renderSkillDocument(context) {
  const capabilities = Array.isArray(context.capabilities) ? context.capabilities : [];
  const agentName = agentNameOf(context.whoami);
  const count = capabilities.length;
  const lines = frontmatterLines({ capabilities, versionToken: context.versionToken, agentName, baseUrl: context.baseUrl });
  lines.push("");
  lines.push(MARKER);
  lines.push("");
  lines.push(`# Skill Router - connection for \`${agentName}\``);
  lines.push("");
  lines.push(`- Platform: ${context.baseUrl}`);
  lines.push("- HTTP API: `/api/v1` (bearer agent token)");
  lines.push("- MCP: `/api/mcp` (Streamable HTTP, JSON-RPC 2.0, same bearer token)");
  lines.push(`- Granted capabilities: ${count}`);
  lines.push(`- Catalogue version token: ${context.versionToken || "(none returned)"}`);
  lines.push(`- Written by skill-router-connect ${INSTALLER_VERSION}`);
  lines.push("");
  lines.push("The agent token is **not** in this file. It is stored in `.skill-router.json`");
  lines.push("next to this SKILL (mode 0600, plaintext - protect the directory) and is sent as");
  lines.push("`Authorization: Bearer <token>`. Never paste the token into this file, a prompt or");
  lines.push("a chat log, and never commit the config file.");
  lines.push("");
  lines.push("## 1. Verify the connection");
  lines.push("");
  lines.push("```bash");
  lines.push(`curl -s -H "Authorization: Bearer $SKILL_ROUTER_TOKEN" ${context.baseUrl}/api/v1/whoami`);
  lines.push(`curl -s -H "Authorization: Bearer $SKILL_ROUTER_TOKEN" ${context.baseUrl}/api/v1/capabilities`);
  lines.push("```");
  lines.push("");
  lines.push("`whoami` must answer `{agent, scopes, limits, capabilities_count}`. The same check from");
  lines.push("the command line is `npx skill-router-connect verify` (add `--capability <slug> --input");
  lines.push("'{...}'` to make one real call).");
  lines.push("");
  lines.push("## 2. Refresh the catalogue");
  lines.push("");
  lines.push("Re-read `/api/v1/capabilities` and compare `version_token` with the value above.");
  lines.push("When it differs, the granted set, a schema or a version changed: reload the");
  lines.push("descriptors before the next call. Never cache descriptors across a token change.");
  lines.push("");
  lines.push("```bash");
  lines.push("npx skill-router-connect update        # rewrites this file from /api/v1/skill");
  lines.push("```");
  lines.push("");
  lines.push("A capability with a breaking change ships as a **new version**; pin it with");
  lines.push("`\"version\": <n>` in the call body until you have migrated.");
  lines.push("");
  lines.push("## 3. Call a capability");
  lines.push("");
  lines.push("```bash");
  lines.push("curl -s -X POST " + context.baseUrl + "/api/v1/invocations \\");
  lines.push('  -H "Authorization: Bearer $SKILL_ROUTER_TOKEN" \\');
  lines.push('  -H "content-type: application/json" \\');
  lines.push(`  -d '{"capability": "<slug>", "input": {...}, "options": {"mode": "sync"}}'`);
  lines.push("```");
  lines.push("");
  lines.push("The answer is `{request_id, invocation_id, status, result, artifacts[], usage, cost, error}`.");
  lines.push("- `status: \"succeeded\"` - `result` is the payload.");
  lines.push("- `status: \"uncertain\"` - the upstream call may have produced output. Report it; do not retry a paid call blindly.");
  lines.push("- `status: \"failed\"` - `error.code` and `error.actions` say what to do.");
  lines.push("- `task_id` present - asynchronous: poll `/api/v1/tasks/{task_id}` and cancel with `POST /api/v1/tasks/{task_id}/cancel`.");
  lines.push("- Streaming: send `Accept: text/event-stream` (or `options.stream: true`) for `start|delta|usage|done|error` events.");
  lines.push("");
  lines.push("## 4. Capability catalogue");
  lines.push("");
  lines.push(...catalogue(capabilities, context.baseUrl));
  lines.push("## 5. Errors you must handle");
  lines.push("");
  lines.push("`unauthenticated`, `invalid_token`, `token_revoked`, `token_expired`,");
  lines.push("`capability_not_granted`, `capability_not_published`,");
  lines.push("`unsupported_parameter`, `input_schema_mismatch`, `no_compatible_route`,");
  lines.push("`rate_limited`, `concurrency_limited`, `budget_exceeded`,");
  lines.push("`upstream_timeout_uncertain`.");
  lines.push("");
  lines.push("Every error carries `{code, message, details, request_id, actions}`. Quote the");
  lines.push("`request_id` when asking an operator for help - it identifies the call in Records.");
  lines.push("");
  lines.push("## 6. Maintenance");
  lines.push("");
  lines.push("```bash");
  lines.push("npx skill-router-connect verify        # connectivity + catalogue (exit 1 on failure)");
  lines.push("npx skill-router-connect update        # refresh this file from the platform");
  lines.push("npx skill-router-connect uninstall     # remove exactly what install wrote");
  lines.push("```");
  lines.push("");
  return `${lines.join("\n").replace(/\n{3,}/g, "\n\n").trim()}\n`;
}

/**
 * Wrap the platform's own markdown (`GET /api/v1/skill` -> `content`) so `update`
 * ships the server-rendered document. The server frontmatter is replaced by ours,
 * because `allowed-tools` is what the local agent needs and the server cannot know it.
 *
 * @param {{serverMarkdown: string, whoami: any, capabilities: any[], versionToken: string|null, baseUrl: string}} context
 * @returns {string}
 */
export function renderFromServerDocument(context) {
  const agentName = agentNameOf(context.whoami);
  const capabilities = Array.isArray(context.capabilities) ? context.capabilities : [];
  const front = frontmatterLines({ capabilities, versionToken: context.versionToken, agentName, baseUrl: context.baseUrl });
  const body = String(context.serverMarkdown || "").replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n?/, "").trim();
  const trailer = [
    "",
    "## Maintenance",
    "",
    "```bash",
    "npx skill-router-connect verify",
    "npx skill-router-connect update",
    "npx skill-router-connect uninstall",
    "```",
    "",
    `Catalogue version token: ${context.versionToken || "(none returned)"}`,
    `Written by skill-router-connect ${INSTALLER_VERSION}`,
    "",
  ].join("\n");
  return `${front.join("\n")}\n\n${MARKER}\n\n${body}\n${trailer}`.replace(/\n{3,}/g, "\n\n");
}
