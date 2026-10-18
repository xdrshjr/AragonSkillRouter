#!/usr/bin/env node
/**
 * Skill Router 调用助手（零依赖，Node >= 18）。
 *
 * 由 @skillrouter/cli {{CLI_VERSION}} 于 {{INSTALLED_AT}} 生成，安装详情见同目录 skillrouter.config.json。
 * 所有平台访问都走这里：Agent 只需一条稳定命令，不需要理解认证头与错误映射。
 * 令牌永不来自 argv；只从 ~/.skillrouter/credentials.json（0600）或 OS 凭据库解析。
 *
 * 子命令：
 *   capabilities.list [--refresh]
 *   capabilities.get <key>
 *   invoke --capability <key> [--version <v>] [--mode sync|async|stream] --input '<json>'
 *   task.status <task_id>
 *   task.cancel <task_id>
 *   skills.list
 *   skills.read <name>
 *   health
 *
 * 输出契约：成功时打印一行 JSON（= 平台响应体）；失败时打印一行 JSON 并以非 0 退出：
 *   {"ok":false,"error":{"code","message","hint","request_id","http_status"}}
 */
import { readFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

/** 安装时写入的默认值（config 文件缺失时兜底）。 */
const DEFAULTS = {
  base_url: '{{BASE_URL}}',
  token_ref: '{{TOKEN_REF}}',
  skill: '{{SKILL_NAME}}',
  cli_version: '{{CLI_VERSION}}',
};

const HERE = path.dirname(fileURLToPath(import.meta.url));
const EXIT = { ok: 0, failure: 1, usage: 2, connectivity: 3 };
const DEFAULT_TIMEOUT_MS = 60000;

function emit(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

function fail(code, message, extra) {
  const error = { code, message };
  const details = extra || {};
  if (details.hint !== undefined) error.hint = details.hint;
  if (details.request_id !== undefined) error.request_id = details.request_id;
  if (details.http_status !== undefined) error.http_status = details.http_status;
  if (details.details !== undefined) error.details = details.details;
  emit({ ok: false, error });
  process.exit(typeof details.exitCode === 'number' ? details.exitCode : EXIT.failure);
}

function parseArgs(argv) {
  const flags = new Map();
  const positionals = [];
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '-h' || arg === '--help') {
      flags.set('help', true);
      continue;
    }
    if (arg.startsWith('--')) {
      const eq = arg.indexOf('=');
      if (eq > 1) {
        flags.set(arg.slice(2, eq), arg.slice(eq + 1));
        continue;
      }
      const name = arg.slice(2);
      const next = argv[i + 1];
      if (next === undefined || next.startsWith('--')) {
        flags.set(name, true);
      } else {
        flags.set(name, next);
        i += 1;
      }
      continue;
    }
    positionals.push(arg);
  }
  return { flags, positionals };
}

async function readJsonFile(file) {
  let raw;
  try {
    raw = await readFile(file, 'utf8');
  } catch {
    return undefined;
  }
  try {
    return JSON.parse(raw);
  } catch {
    fail('CONFIG_CORRUPT', `${file} 不是合法 JSON`, {
      hint: '重新运行 skillrouter install --force',
    });
    return undefined;
  }
}

async function loadConfig() {
  const file = process.env.SKILLROUTER_CONFIG || path.join(HERE, 'skillrouter.config.json');
  const parsed = await readJsonFile(file);
  if (!parsed || typeof parsed !== 'object') {
    return {
      base_url: DEFAULTS.base_url,
      token_ref: DEFAULTS.token_ref,
      skill: DEFAULTS.skill,
      transport: 'http',
      source: 'defaults',
    };
  }
  return {
    base_url: typeof parsed.base_url === 'string' ? parsed.base_url : DEFAULTS.base_url,
    token_ref: typeof parsed.token_ref === 'string' ? parsed.token_ref : DEFAULTS.token_ref,
    skill: typeof parsed.skill === 'string' ? parsed.skill : DEFAULTS.skill,
    transport: parsed.transport === 'mcp' ? 'mcp' : 'http',
    source: file,
  };
}

function credentialsPath() {
  const home = process.env.SKILLROUTER_HOME || path.join(homedir(), '.skillrouter');
  return path.join(home, 'credentials.json');
}

async function resolveToken(config) {
  const fromEnv = process.env.SKILLROUTER_TOKEN;
  if (typeof fromEnv === 'string' && fromEnv.trim().length > 0) {
    return fromEnv.trim();
  }
  const file = credentialsPath();
  const parsed = await readJsonFile(file);
  if (!parsed || typeof parsed !== 'object') {
    fail('TOKEN_MISSING', `未找到凭据文件 ${file}`, {
      hint: '让用户重新运行 skillrouter install --token-stdin',
    });
    return undefined;
  }
  const tokens = parsed.tokens;
  if (!tokens || typeof tokens !== 'object') {
    fail('TOKEN_MISSING', `凭据文件 ${file} 中没有 tokens 段`, {
      hint: '让用户重新运行 skillrouter install --token-stdin',
    });
    return undefined;
  }
  const entry = tokens[config.token_ref];
  const token = entry && typeof entry.token === 'string' ? entry.token : undefined;
  if (!token) {
    fail('TOKEN_MISSING', `凭据文件中没有 token_ref=${config.token_ref}`, {
      hint: '让用户重新运行 skillrouter install --token-stdin（或检查 SKILLROUTER_HOME）',
    });
    return undefined;
  }
  return token;
}

function joinUrl(baseUrl, suffix) {
  const base = String(baseUrl).replace(/\/+$/, '');
  const tail = suffix.startsWith('/') ? suffix : `/${suffix}`;
  return `${base}${tail}`;
}

function mapErrorPayload(body, status, requestId) {
  if (body && typeof body === 'object' && body.error && typeof body.error === 'object') {
    const error = body.error;
    return {
      code: typeof error.code === 'string' ? error.code : `HTTP_${status}`,
      message: typeof error.message === 'string' ? error.message : `请求失败（HTTP ${status}）`,
      hint: typeof error.hint === 'string' ? error.hint : undefined,
      request_id: typeof error.request_id === 'string' ? error.request_id : requestId,
      http_status: status,
      details: error.details && typeof error.details === 'object' ? error.details : undefined,
    };
  }
  const message =
    typeof body === 'string' && body.length > 0 && body.length <= 400
      ? body
      : `请求失败（HTTP ${status}）`;
  return { code: `HTTP_${status}`, message, request_id: requestId, http_status: status };
}

async function request(config, method, apiPath, options) {
  const opts = options || {};
  const url = joinUrl(config.base_url, apiPath);
  const headers = { accept: opts.accept || 'application/json', 'user-agent': 'skillrouter-invoke' };
  if (opts.token) {
    headers.authorization = `Bearer ${opts.token}`;
  }
  if (opts.body !== undefined) {
    headers['content-type'] = 'application/json';
  }
  if (opts.etag) {
    headers['if-none-match'] = opts.etag;
  }
  let response;
  try {
    response = await fetch(url, {
      method,
      headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      signal: AbortSignal.timeout(opts.timeoutMs || DEFAULT_TIMEOUT_MS),
    });
  } catch (error) {
    const name = error && error.name ? error.name : '';
    const timeout = name === 'TimeoutError' || name === 'AbortError';
    fail(timeout ? 'NETWORK_TIMEOUT' : 'NETWORK_ERROR', `${url} 请求失败: ${error && error.message ? error.message : error}`, {
      hint: '检查 base_url 是否可达（网络 / 代理 / VPN）',
      exitCode: EXIT.connectivity,
    });
  }
  return response;
}

async function readBody(response) {
  let text = '';
  try {
    text = await response.text();
  } catch {
    text = '';
  }
  if (text.length === 0) return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

async function jsonCall(config, method, apiPath, options) {
  const opts = options || {};
  const token = opts.noAuth === true ? undefined : await resolveToken(config);
  const response = await request(config, method, apiPath, { ...opts, token });
  const requestId = response.headers.get('x-request-id') || undefined;
  const body = await readBody(response);
  if (!response.ok) {
    const mapped = mapErrorPayload(body, response.status, requestId);
    fail(mapped.code, mapped.message, {
      hint: mapped.hint,
      request_id: mapped.request_id,
      http_status: mapped.http_status,
      details: mapped.details,
      exitCode: mapped.code === 'NETWORK_ERROR' || mapped.code === 'NETWORK_TIMEOUT' ? EXIT.connectivity : EXIT.failure,
    });
  }
  return { body, status: response.status, requestId, etag: response.headers.get('etag') || undefined };
}

async function streamCall(config, apiPath, body) {
  const token = await resolveToken(config);
  const response = await request(config, 'POST', apiPath, {
    token,
    body,
    accept: 'text/event-stream',
    timeoutMs: 300000,
  });
  const requestId = response.headers.get('x-request-id') || undefined;
  const contentType = response.headers.get('content-type') || '';
  if (!response.ok) {
    const parsed = await readBody(response);
    const mapped = mapErrorPayload(parsed, response.status, requestId);
    fail(mapped.code, mapped.message, {
      hint: mapped.hint,
      request_id: mapped.request_id,
      http_status: mapped.http_status,
      details: mapped.details,
    });
  }
  if (!contentType.includes('text/event-stream') || !response.body) {
    // 平台把 stream 降级成普通 JSON（例如能力只有一个 chunk）时照样输出一行
    const parsed = await readBody(response);
    emit(parsed === undefined ? { ok: true } : parsed);
    return EXIT.ok;
  }

  const decoder = new TextDecoder();
  let buffer = '';
  let sawError = false;
  const reader = response.body.getReader();
  for (;;) {
    const chunk = await reader.read();
    if (chunk.done) break;
    buffer += decoder.decode(chunk.value, { stream: true });
    let index = buffer.indexOf('\n');
    while (index >= 0) {
      const line = buffer.slice(0, index).replace(/\r$/, '');
      buffer = buffer.slice(index + 1);
      handleSseLine(line);
      index = buffer.indexOf('\n');
    }
  }
  if (buffer.trim().length > 0) {
    handleSseLine(buffer.replace(/\r$/, ''));
  }
  return sawError ? EXIT.failure : EXIT.ok;

  function handleSseLine(line) {
    const trimmed = line.trim();
    if (trimmed.length === 0 || trimmed.startsWith(':')) {
      return; // keep-alive 注释
    }
    if (!trimmed.startsWith('data:')) {
      return;
    }
    const payload = trimmed.slice(5).trim();
    if (payload.length === 0 || payload === '[DONE]') {
      return;
    }
    let parsed;
    try {
      parsed = JSON.parse(payload);
    } catch {
      emit({ type: 'raw', data: payload });
      return;
    }
    if (parsed && typeof parsed === 'object' && (parsed.type === 'call.error' || parsed.type === 'turn.failed')) {
      sawError = true;
    }
    emit(parsed);
  }
}

function requireFlag(flags, name, usage) {
  const value = flags.get(name);
  if (typeof value !== 'string' || value.length === 0) {
    fail('USAGE_MISSING_FLAG', `缺少 --${name}`, { hint: usage, exitCode: EXIT.usage });
  }
  return value;
}

async function main(argv) {
  const { flags, positionals } = parseArgs(argv);
  const subcommand = positionals[0];

  if (flags.has('help') || subcommand === undefined || subcommand === 'help') {
    emit({
      ok: true,
      usage: {
        commands: [
          'capabilities.list [--refresh]',
          'capabilities.get <key>',
          "invoke --capability <key> [--version <v>] [--mode sync|async|stream] --input '<json>'",
          'task.status <task_id>',
          'task.cancel <task_id>',
          'skills.list',
          'skills.read <name>',
          'health',
        ],
        notes: [
          '令牌只从 ~/.skillrouter/credentials.json（或 SKILLROUTER_TOKEN）解析，永不来自 argv',
          '成功输出一行 JSON；失败输出 {"ok":false,"error":{...}} 并以非 0 退出',
        ],
      },
    });
    return EXIT.ok;
  }

  const config = await loadConfig();

  if (subcommand === 'capabilities.list') {
    const refresh = flags.has('refresh');
    const result = await jsonCall(
      config,
      'GET',
      refresh ? '/api/v1/capabilities?refresh=1' : '/api/v1/capabilities',
    );
    emit(result.body === undefined ? { ok: true, capabilities: [] } : result.body);
    return EXIT.ok;
  }

  if (subcommand === 'capabilities.get') {
    const key = positionals[1] || flags.get('key');
    if (typeof key !== 'string' || key.length === 0) {
      fail('USAGE_MISSING_KEY', '缺少能力键', {
        hint: 'node ./invoke.mjs capabilities.get <capability-key>',
        exitCode: EXIT.usage,
      });
    }
    const result = await jsonCall(config, 'GET', `/api/v1/capabilities/${encodeURIComponent(key)}`);
    emit(result.body === undefined ? { ok: true } : result.body);
    return EXIT.ok;
  }

  if (subcommand === 'invoke') {
    const capability = requireFlag(
      flags,
      'capability',
      "node ./invoke.mjs invoke --capability <key> --input '{\"prompt\":\"...\"}'",
    );
    const modeRaw = flags.get('mode');
    const mode = typeof modeRaw === 'string' ? modeRaw : 'sync';
    if (mode !== 'sync' && mode !== 'async' && mode !== 'stream') {
      fail('USAGE_BAD_MODE', `--mode 只能是 sync|async|stream，实际 ${mode}`, { exitCode: EXIT.usage });
    }
    const inputRaw = flags.get('input');
    if (typeof inputRaw !== 'string') {
      fail('USAGE_MISSING_INPUT', '缺少 --input', {
        hint: "node ./invoke.mjs invoke --capability <key> --input '{\"prompt\":\"hi\"}'",
        exitCode: EXIT.usage,
      });
    }
    let input;
    try {
      input = JSON.parse(inputRaw);
    } catch (error) {
      fail('USAGE_BAD_INPUT', `--input 不是合法 JSON: ${error && error.message ? error.message : error}`, {
        hint: '在 cmd.exe 下用双引号包裹并转义内部引号，或改用单行 JSON',
        exitCode: EXIT.usage,
      });
    }
    const payload = { input, options: { mode } };
    const version = flags.get('version');
    if (typeof version === 'string' && version.length > 0) {
      payload.capability_version = version;
    }
    const apiPath = `/api/v1/capabilities/${encodeURIComponent(capability)}/invoke`;
    if (mode === 'stream') {
      return streamCall(config, apiPath, payload);
    }
    const result = await jsonCall(config, 'POST', apiPath, { body: payload, timeoutMs: 180000 });
    emit(result.body === undefined ? { ok: true, status: result.status } : result.body);
    return EXIT.ok;
  }

  if (subcommand === 'task.status') {
    const taskId = positionals[1];
    if (!taskId) {
      fail('USAGE_MISSING_TASK', '缺少 task_id', {
        hint: 'node ./invoke.mjs task.status <task_id>',
        exitCode: EXIT.usage,
      });
    }
    const result = await jsonCall(config, 'GET', `/api/v1/tasks/${encodeURIComponent(taskId)}`);
    emit(result.body === undefined ? { ok: true } : result.body);
    return EXIT.ok;
  }

  if (subcommand === 'task.cancel') {
    const taskId = positionals[1];
    if (!taskId) {
      fail('USAGE_MISSING_TASK', '缺少 task_id', {
        hint: 'node ./invoke.mjs task.cancel <task_id>',
        exitCode: EXIT.usage,
      });
    }
    const result = await jsonCall(config, 'POST', `/api/v1/tasks/${encodeURIComponent(taskId)}/cancel`, {
      body: {},
    });
    emit(result.body === undefined ? { ok: true } : result.body);
    return EXIT.ok;
  }

  if (subcommand === 'skills.list') {
    const result = await jsonCall(config, 'GET', '/api/v1/skills');
    emit(result.body === undefined ? { ok: true, skills: [] } : result.body);
    return EXIT.ok;
  }

  if (subcommand === 'skills.read') {
    const name = positionals[1];
    if (!name) {
      fail('USAGE_MISSING_NAME', '缺少 SKILL 名称', {
        hint: 'node ./invoke.mjs skills.read <skill-name>',
        exitCode: EXIT.usage,
      });
    }
    const result = await jsonCall(config, 'GET', `/api/v1/skills/${encodeURIComponent(name)}`);
    emit(result.body === undefined ? { ok: true } : result.body);
    return EXIT.ok;
  }

  if (subcommand === 'health') {
    const result = await jsonCall(config, 'GET', '/api/v1/health', { noAuth: true, timeoutMs: 10000 });
    emit(result.body === undefined ? { ok: true } : result.body);
    return EXIT.ok;
  }

  fail('USAGE_UNKNOWN_COMMAND', `未知子命令: ${subcommand}`, {
    hint: 'node ./invoke.mjs --help',
    exitCode: EXIT.usage,
  });
  return EXIT.usage;
}

try {
  const code = await main(process.argv.slice(2));
  process.exitCode = typeof code === 'number' ? code : EXIT.ok;
} catch (error) {
  fail('UNEXPECTED', error && error.message ? error.message : String(error));
}
