import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Env } from '../src/detect.js';
import { ApiFailure, EXIT, maskToken, tokenRefFor, type Logger } from '../src/http.js';
import {
  CLI_VERSION,
  INVOKE_FILENAME,
  README_FILENAME,
  SKILL_FILENAME,
  assertSkillContract,
  runInstall,
  splitFrontmatter,
} from '../src/install.js';

const TOKEN = 'srk_ab12cd34_secretsecretsecretsecret';
const BASE_URL = 'https://router.example.com';

let root: string;
let calls: string[];
const realFetch = globalThis.fetch;

class SilentLogger implements Logger {
  readonly lines: string[] = [];
  info(message: string): void {
    this.lines.push(`info:${message}`);
  }
  success(message: string): void {
    this.lines.push(`ok:${message}`);
  }
  fail(message: string): void {
    this.lines.push(`fail:${message}`);
  }
  warn(message: string): void {
    this.lines.push(`warn:${message}`);
  }
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json', 'x-request-id': 'rid_test' },
  });
}

function mockFetch(handler: (url: string, init?: RequestInit) => Response): void {
  const stub = (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
    calls.push(`${init?.method ?? 'GET'} ${url}`);
    return Promise.resolve(handler(url, init));
  };
  globalThis.fetch = stub as unknown as typeof fetch;
}

function defaultHandler(url: string): Response {
  if (url.endsWith('/api/v1/health')) {
    return jsonResponse({ status: 'ok', version: '1.0.0', engine: { version: '0.2.34', status: 'available' } });
  }
  if (url.includes('/api/v1/capabilities')) {
    return jsonResponse({ items: [{ key: 'text.generate.gpt4o-mini' }, { key: 'image.generate.x' }] });
  }
  if (url.includes('/api/v1/whoami')) {
    return jsonResponse({
      token_id: 'tok_1',
      agent: { id: 'agt_1', name: 'local-cli' },
      prefix: 'srk_ab12cd34',
      scopes: ['*'],
      allowed_modes: ['sync', 'async'],
    });
  }
  return jsonResponse({ error: { code: 'NOT_FOUND', message: 'not found' } }, 404);
}

function envFor(cwd: string, home: string): Env {
  return { cwd, home, aragonHome: undefined, platform: process.platform, env: { SKILLROUTER_HOME: home } };
}

async function readJson<T>(file: string): Promise<T> {
  return JSON.parse(await fs.readFile(file, 'utf8')) as T;
}

async function listDir(dir: string): Promise<string[]> {
  const entries = await fs.readdir(dir);
  return entries.sort();
}

beforeEach(async () => {
  root = await fs.mkdtemp(path.join(os.tmpdir(), 'sr-install-'));
  calls = [];
  globalThis.fetch = realFetch;
});

afterEach(async () => {
  globalThis.fetch = realFetch;
  vi.restoreAllMocks();
  await fs.rm(root, { recursive: true, force: true });
});

describe('runInstall', () => {
  it('writes exactly the four documented artifacts, and no secret outside credentials', async () => {
    const target = path.join(root, 'skills');
    const home = path.join(root, 'home');
    mockFetch((url) => defaultHandler(url));

    const result = await runInstall({
      baseUrl: BASE_URL,
      token: TOKEN,
      dir: target,
      env: envFor(root, home),
      log: new SilentLogger(),
    });

    expect(result.exitCode).toBe(EXIT.OK);
    expect(await listDir(target)).toEqual(['README.md', 'SKILL.md', INVOKE_FILENAME, 'skillrouter.config.json']);
    expect(result.files).toHaveLength(4);
    for (const file of result.files) {
      expect(path.isAbsolute(file)).toBe(true);
      expect((await fs.stat(file)).isFile()).toBe(true);
    }

    const skillMd = await fs.readFile(path.join(target, SKILL_FILENAME), 'utf8');
    expect(skillMd).not.toContain(TOKEN);
    expect(skillMd).toContain('allowed-tools: [bash]');
    expect(skillMd).toContain(BASE_URL);
    expect(skillMd).toContain('invoke.mjs');
    expect(skillMd).not.toMatch(/\bsr_invoke_capability\b/);

    const configRaw = await fs.readFile(path.join(target, 'skillrouter.config.json'), 'utf8');
    expect(configRaw).not.toContain(TOKEN);
    const config = await readJson<Record<string, unknown>>(path.join(target, 'skillrouter.config.json'));
    expect(config.base_url).toBe(BASE_URL);
    expect(config.transport).toBe('http');
    expect(config.cli_version).toBe(CLI_VERSION);
    expect(config.server_version).toBe('1.0.0');
    expect(typeof config.installed_at).toBe('string');
    expect(typeof config.token_ref).toBe('string');
    expect(JSON.stringify(config)).not.toContain('secretsecretsecret');

    const readme = await fs.readFile(path.join(target, README_FILENAME), 'utf8');
    expect(readme).toContain('待验证');
    expect(readme).not.toContain(TOKEN);

    const invoke = await fs.readFile(path.join(target, INVOKE_FILENAME), 'utf8');
    expect(invoke).not.toContain(TOKEN);
    expect(invoke).not.toContain('{{');

    const credentialsFile = path.join(home, 'credentials.json');
    const credentials = await readJson<{ tokens: Record<string, { token: string }> }>(credentialsFile);
    const tokenRef = String(config.token_ref);
    expect(credentials.tokens[tokenRef]?.token).toBe(TOKEN);
    expect(result.credentialsPath).toBe(credentialsFile);
  });

  it('keeps the platform-provided skill name, version and required capabilities', async () => {
    const target = path.join(root, 'skills');
    const home = path.join(root, 'home');
    mockFetch((url) => {
      if (url.includes('/api/v1/skills/')) {
        return jsonResponse({
          name: 'video-short-clip',
          title: '短视频生成',
          description: '生成 5 秒竖版短视频',
          version: '1.2.0',
          required_capabilities: ['video.generate.kling-v2'],
          skill_md: '---\nname: video-short-clip\nallowed-tools: [bash]\n---\n# 使用说明\n调用 invoke.mjs。\n',
        });
      }
      return defaultHandler(url);
    });

    await runInstall({
      baseUrl: BASE_URL,
      token: TOKEN,
      dir: target,
      skill: 'video-short-clip',
      env: envFor(root, home),
      log: new SilentLogger(),
    });

    const skillMd = await fs.readFile(path.join(target, SKILL_FILENAME), 'utf8');
    const { frontmatter, body } = splitFrontmatter(skillMd);
    expect(frontmatter).toContain('name: video-short-clip');
    expect(frontmatter).toContain('version: 1.2.0');
    expect(frontmatter).toContain('required_capabilities: [video.generate.kling-v2]');
    expect(body).toContain('平台技能说明');
    expect(body).toContain('调用 invoke.mjs');
    // 服务端正文里的 frontmatter 不会被拼进 CLI 生成的 frontmatter
    expect(frontmatter.match(/allowed-tools:/g)).toHaveLength(1);
  });

  it('fails with TARGET_EXISTS without --force, and overwrites with it', async () => {
    const target = path.join(root, 'skills');
    const home = path.join(root, 'home');
    mockFetch((url) => defaultHandler(url));

    await runInstall({
      baseUrl: BASE_URL,
      token: TOKEN,
      dir: target,
      noVerify: true,
      env: envFor(root, home),
      log: new SilentLogger(),
    });

    await expect(
      runInstall({
        baseUrl: BASE_URL,
        token: TOKEN,
        dir: target,
        env: envFor(root, home),
        log: new SilentLogger(),
      }),
    ).rejects.toMatchObject({ code: 'TARGET_EXISTS', exitCode: EXIT.FAILURE });

    const forced = await runInstall({
      baseUrl: BASE_URL,
      token: TOKEN,
      dir: target,
      force: true,
      env: envFor(root, home),
      log: new SilentLogger(),
    });
    expect(forced.exitCode).toBe(EXIT.OK);
  });

  it('--no-verify performs no network calls at all', async () => {
    const target = path.join(root, 'skills');
    const home = path.join(root, 'home');
    mockFetch((url) => defaultHandler(url));

    const result = await runInstall({
      baseUrl: BASE_URL,
      token: TOKEN,
      dir: target,
      noVerify: true,
      env: envFor(root, home),
      log: new SilentLogger(),
    });

    expect(calls).toHaveLength(0);
    expect(result.serverVersion).toBeNull();
    expect(result.verify).toBeUndefined();
    expect(result.config.capabilities_snapshot).toBeUndefined();
    const config = await readJson<Record<string, unknown>>(path.join(target, 'skillrouter.config.json'));
    expect(config.server_version).toBeNull();
    expect(config).not.toHaveProperty('capabilities_snapshot');
  });

  it('surfaces a connectivity failure as ApiFailure(connectivity) with exit code 3', async () => {
    const target = path.join(root, 'skills');
    const home = path.join(root, 'home');
    globalThis.fetch = (() => Promise.reject(new TypeError('fetch failed'))) as unknown as typeof fetch;

    const error = await runInstall({
      baseUrl: BASE_URL,
      token: TOKEN,
      dir: target,
      env: envFor(root, home),
      log: new SilentLogger(),
    }).catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiFailure);
    const failure = error as ApiFailure;
    expect(failure.connectivity).toBe(true);
    expect(failure.exitCode).toBe(EXIT.CONNECTIVITY);
  });

  it('rejects an invalid token format before touching the filesystem', async () => {
    const target = path.join(root, 'skills');
    const home = path.join(root, 'home');

    await expect(
      runInstall({
        baseUrl: BASE_URL,
        token: 'not-a-platform-token',
        dir: target,
        noVerify: true,
        env: envFor(root, home),
        log: new SilentLogger(),
      }),
    ).rejects.toMatchObject({ code: 'USAGE_TOKEN_INVALID', exitCode: EXIT.USAGE });

    await expect(fs.access(target)).rejects.toBeDefined();
  });

  it('rejects --transport mcp because the MCP path is not verified yet', async () => {
    await expect(
      runInstall({
        baseUrl: BASE_URL,
        token: TOKEN,
        dir: path.join(root, 'skills'),
        transport: 'mcp',
        noVerify: true,
        env: envFor(root, path.join(root, 'home')),
        log: new SilentLogger(),
      }),
    ).rejects.toMatchObject({ code: 'USAGE_TRANSPORT_UNVERIFIED', exitCode: EXIT.USAGE });
  });

  it('reads the token from --token-stdin and never from argv', async () => {
    const target = path.join(root, 'skills');
    const home = path.join(root, 'home');
    mockFetch((url) => defaultHandler(url));

    const result = await runInstall({
      baseUrl: BASE_URL,
      tokenStdin: true,
      readStdinToken: () => Promise.resolve(`${TOKEN}\n`),
      dir: target,
      noVerify: true,
      env: envFor(root, home),
      log: new SilentLogger(),
    });

    const credentials = await readJson<{ tokens: Record<string, { token: string }> }>(
      path.join(home, 'credentials.json'),
    );
    expect(credentials.tokens[result.config.token_ref]?.token).toBe(TOKEN);
    expect(result.tokenMasked).toBe(maskToken(TOKEN));
    expect(result.tokenMasked).not.toBe(TOKEN);
  });

  it('reuses a stored token instead of prompting again', async () => {
    const target = path.join(root, 'skills');
    const home = path.join(root, 'home');
    mockFetch((url) => defaultHandler(url));

    await runInstall({
      baseUrl: BASE_URL,
      token: TOKEN,
      dir: target,
      noVerify: true,
      env: envFor(root, home),
      log: new SilentLogger(),
    });

    const second = await runInstall({
      baseUrl: BASE_URL,
      tokenStdin: false,
      promptToken: () => Promise.reject(new Error('should not prompt')),
      dir: target,
      force: true,
      noVerify: true,
      env: envFor(root, home),
      log: new SilentLogger(),
    });
    expect(second.exitCode).toBe(EXIT.OK);
    expect(second.config.token_ref).toBe(tokenRefFor(BASE_URL));
  });

  it('reports exit code 4 when the target directory is not writable', async () => {
    const target = path.join(root, 'blocked');
    await fs.writeFile(target, 'not a directory', 'utf8');

    const error = await runInstall({
      baseUrl: BASE_URL,
      token: TOKEN,
      dir: target,
      noVerify: true,
      env: envFor(root, path.join(root, 'home')),
      log: new SilentLogger(),
    }).catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiFailure);
    expect((error as ApiFailure).exitCode).toBe(EXIT.PERMISSION);
  });
});

describe('assertSkillContract', () => {
  it('accepts allowed-tools: [bash]', () => {
    expect(() =>
      assertSkillContract('---\nname: x\nallowed-tools: [bash]\n---\n# hi\n'),
    ).not.toThrow();
  });

  it('rejects sr_* tools in the frontmatter', () => {
    expect(() =>
      assertSkillContract('---\nname: x\nallowed-tools: [sr_invoke_capability]\n---\n# hi\n'),
    ).toThrow(/allowed-tools/);
  });
});
