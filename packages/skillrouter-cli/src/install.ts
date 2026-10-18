import { promises as fs } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  currentEnv,
  detectTarget,
  formatDetection,
  type AgentTarget,
  type Detection,
  type Env,
} from './detect.js';
import {
  ApiFailure,
  DEFAULT_TIMEOUT_MS,
  EXIT,
  apiRequest,
  asArray,
  asRecord,
  asString,
  isValidTokenFormat,
  maskToken,
  normalizeBaseUrl,
  pickItems,
  resolveStoredToken,
  saveToken,
  tokenRefFor,
  writeConfig,
  type CapabilitySnapshot,
  type ExitCode,
  type Logger,
  type SkillRouterConfig,
  type Transport,
} from './http.js';
import { runVerify, type VerifyReport } from './verify.js';

/** CLI 自身版本，同时写进 config 与产物注释（与 package.json 保持一致）。 */
export const CLI_VERSION = '0.1.0';

export const SKILL_FILENAME = 'SKILL.md';
export const INVOKE_FILENAME = 'invoke.mjs';
export const README_FILENAME = 'README.md';

/** 生成的 SKILL 正文中出现的示例能力键（有真实能力时用第一个，否则保留占位符）。 */
const EXAMPLE_CAPABILITY_PLACEHOLDER = '<capability-key>';

const DEFAULT_DESCRIPTION =
  '通过 Skill Router 平台调用标准化 AI 能力（文本、图像、语音、视频等）。当任务需要外部 AI 能力、或需要现场发现平台能力时使用。';

export interface SkillDetail {
  name: string;
  title: string;
  description: string;
  version: string;
  requiredCapabilities: string[];
  /** 平台下发的 SKILL 正文（不含 frontmatter）；CLI 只采用正文，frontmatter 一律自己生成 */
  body: string | undefined;
  /** 平台下发的 invoke.mjs 内容（§9.8-2：助手内容由后端 bundle 下发） */
  invokeMjs: string | undefined;
}

export interface HealthInfo {
  status: string;
  version: string;
  engineVersion: string | null;
}

/**
 * 模板目录探测：兼容三种布局（构建产物旁、包根下 templates/、源码树 src/templates/）。
 */
export function templateDirs(): string[] {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const root = path.dirname(here);
  return [
    path.join(here, 'templates'),
    path.join(root, 'templates'),
    path.join(root, 'src', 'templates'),
  ];
}

export async function readTemplate(name: string): Promise<string> {
  const attempts: string[] = [];
  for (const dir of templateDirs()) {
    const candidate = path.join(dir, name);
    attempts.push(candidate);
    try {
      const raw = await fs.readFile(candidate, 'utf8');
      return raw.replace(/\r\n/g, '\n');
    } catch {
      // 继续尝试下一个候选目录
    }
  }
  throw new ApiFailure({
    code: 'TEMPLATE_MISSING',
    message: `找不到模板 ${name}（尝试过 ${attempts.join(', ')}）`,
    hint: '重新安装 @skillrouter/cli（模板随包分发在 src/templates）',
    exitCode: EXIT.FAILURE,
  });
}

export function renderTemplate(template: string, vars: Record<string, string>): string {
  return template.replace(/\{\{([A-Z0-9_]+)\}\}/g, (full, key: string) => {
    const value = vars[key];
    return value === undefined ? full : value;
  });
}

export function splitFrontmatter(markdown: string): { frontmatter: string; body: string } {
  const normalized = markdown.replace(/\r\n/g, '\n');
  if (!normalized.startsWith('---\n')) {
    return { frontmatter: '', body: normalized };
  }
  const end = normalized.indexOf('\n---', 3);
  if (end < 0) {
    return { frontmatter: '', body: normalized };
  }
  const frontmatter = normalized.slice(4, end);
  const rest = normalized.slice(end + 4);
  return { frontmatter, body: rest.replace(/^\n+/, '') };
}

/**
 * 硬约束（§7.9-1）：frontmatter 只能声明目标环境真实存在的 `bash`，不能出现 `sr_*` 或其它未实测工具。
 */
export function assertSkillContract(markdown: string): void {
  const { frontmatter } = splitFrontmatter(markdown);
  const allowedLine = /^allowed-tools:\s*\[bash\]\s*$/m.test(frontmatter);
  if (!allowedLine) {
    throw new ApiFailure({
      code: 'SKILL_CONTRACT_VIOLATION',
      message: '生成的 SKILL.md frontmatter 未声明 `allowed-tools: [bash]`',
      hint: '这是 CLI 的内部一致性错误，请报给维护者',
      exitCode: EXIT.FAILURE,
    });
  }
  if (/\bsr_[a-z0-9_]+/i.test(frontmatter)) {
    throw new ApiFailure({
      code: 'SKILL_CONTRACT_VIOLATION',
      message: '生成的 SKILL.md frontmatter 声明了 `sr_*` 工具（外部 Agent 环境不存在）',
      hint: '这是 CLI 的内部一致性错误，请报给维护者',
      exitCode: EXIT.FAILURE,
    });
  }
}

export async function fetchHealth(baseUrl: string): Promise<HealthInfo> {
  const response = await apiRequest({
    baseUrl,
    path: '/api/v1/health',
    timeoutMs: 10_000,
  });
  const record = asRecord(response.data) ?? {};
  const engine = asRecord(record.engine);
  return {
    status: asString(record.status) ?? 'unknown',
    version: asString(record.version) ?? 'unknown',
    engineVersion: engine ? (asString(engine.version) ?? null) : null,
  };
}

export async function fetchCapabilitySnapshot(
  baseUrl: string,
  token: string,
  timeoutMs: number = DEFAULT_TIMEOUT_MS,
): Promise<CapabilitySnapshot> {
  const response = await apiRequest({
    baseUrl,
    path: '/api/v1/capabilities',
    token,
    timeoutMs,
  });
  const items = pickItems(response.data);
  const keys: string[] = [];
  for (const item of items) {
    const record = asRecord(item);
    const key = record ? asString(record.key) : undefined;
    if (key) keys.push(key);
  }
  return {
    fetched_at: new Date().toISOString(),
    count: items.length,
    keys,
  };
}

export async function fetchSkillDetail(
  baseUrl: string,
  token: string,
  name: string,
  timeoutMs: number = DEFAULT_TIMEOUT_MS,
): Promise<SkillDetail | undefined> {
  let data: unknown;
  try {
    const response = await apiRequest({
      baseUrl,
      path: `/api/v1/skills/${encodeURIComponent(name)}`,
      token,
      timeoutMs,
    });
    data = response.data;
  } catch (error: unknown) {
    if (error instanceof ApiFailure && error.status === 404) {
      return undefined;
    }
    throw error;
  }
  const record = asRecord(data);
  if (!record) return undefined;
  const markdownCandidates = ['skill_md', 'skillMd', 'content', 'markdown'];
  let body: string | undefined;
  for (const field of markdownCandidates) {
    const value = asString(record[field]);
    if (value && value.trim().length > 0) {
      body = splitFrontmatter(value).body;
      break;
    }
  }
  const requiredRaw = asArray(record.required_capabilities);
  const requiredCapabilities = requiredRaw.filter((item): item is string => typeof item === 'string');
  const invokeMjs =
    asString(record.invoke_mjs) ?? asString(record.invokeMjs) ?? asString(record.helper_source);
  return {
    name: asString(record.name) ?? name,
    title: asString(record.title) ?? (asString(record.name) ?? name),
    description: asString(record.description) ?? DEFAULT_DESCRIPTION,
    version: asString(record.version) ?? CLI_VERSION,
    requiredCapabilities,
    body,
    invokeMjs: invokeMjs && invokeMjs.trim().length > 0 ? invokeMjs : undefined,
  };
}

export interface InstallOptions {
  baseUrl: string;
  token?: string;
  tokenStdin?: boolean;
  agent?: AgentTarget;
  transport?: Transport;
  dir?: string;
  skill?: string;
  noVerify?: boolean;
  force?: boolean;
  /** 覆盖本次调用的超时（毫秒），默认 15s */
  timeoutMs?: number;
  env?: Env;
  log: Logger;
  /** 测试注入点：替换交互式隐藏输入 */
  promptToken?: () => Promise<string>;
  /** 测试注入点：替换 stdin 读取 */
  readStdinToken?: () => Promise<string>;
}

export interface InstallResult {
  exitCode: ExitCode;
  dir: string;
  detection: Detection;
  files: string[];
  config: SkillRouterConfig;
  credentialsPath: string;
  tokenMasked: string;
  verify: VerifyReport | undefined;
  serverVersion: string | null;
  warnings: string[];
}

async function readStdinText(): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin as AsyncIterable<Buffer>) {
    chunks.push(Buffer.from(chunk));
  }
  return Buffer.concat(chunks).toString('utf8').trim();
}

/** 交互式隐藏输入（不回显），仅在 TTY 下可用。 */
async function promptHiddenToken(question: string): Promise<string> {
  const stdin = process.stdin;
  const stdout = process.stdout;
  if (!stdin.isTTY) {
    throw new ApiFailure({
      code: 'USAGE_TOKEN_REQUIRED',
      message: '需要平台令牌，但当前不是交互式终端',
      hint: '用 --token-stdin 从标准输入读取（推荐），或 --token <srk_...>',
      exitCode: EXIT.USAGE,
    });
  }
  stdout.write(question);
  stdin.setRawMode(true);
  stdin.resume();
  stdin.setEncoding('utf8');
  return await new Promise<string>((resolve) => {
    let value = '';
    const finish = (result: string): void => {
      stdin.setRawMode(false);
      stdin.pause();
      stdin.removeListener('data', onData);
      stdout.write('\n');
      resolve(result.trim());
    };
    const onData = (chunk: string): void => {
      for (const char of chunk) {
        if (char === '\r' || char === '\n') {
          finish(value);
          return;
        }
        if (char === '\u0003') {
          finish('');
          return;
        }
        if (char === '\u007f' || char === '\b') {
          value = value.slice(0, -1);
          continue;
        }
        value += char;
      }
    };
    stdin.on('data', onData);
  });
}

function validateToken(token: string): string {
  const trimmed = token.trim();
  if (trimmed.length === 0) {
    throw new ApiFailure({
      code: 'USAGE_TOKEN_REQUIRED',
      message: '平台令牌为空',
      hint: '用 --token-stdin 传入 srk_<keyid>_<secret>',
      exitCode: EXIT.USAGE,
    });
  }
  if (!isValidTokenFormat(trimmed)) {
    throw new ApiFailure({
      code: 'USAGE_TOKEN_INVALID',
      message: '令牌格式应为 srk_<keyid>_<secret>',
      hint: '令牌由平台「接入 → Agent → 创建令牌」一次性展示，请重新复制',
      exitCode: EXIT.USAGE,
    });
  }
  return trimmed;
}

async function ensureWritableDir(dir: string): Promise<void> {
  const notWritable = new Set(['EACCES', 'EPERM', 'EROFS', 'EEXIST', 'ENOTDIR', 'EISDIR']);
  try {
    await fs.mkdir(dir, { recursive: true });
  } catch (error: unknown) {
    const code = error instanceof Error && 'code' in error ? String(error.code) : '';
    if (notWritable.has(code)) {
      throw new ApiFailure({
        code: 'TARGET_NOT_WRITABLE',
        message: `目标目录不可写 ${dir}: ${error instanceof Error ? error.message : String(error)}`,
        hint: '换一个可写目录（--dir），或用管理员权限重试',
        exitCode: EXIT.PERMISSION,
      });
    }
    throw new ApiFailure({
      code: 'TARGET_CREATE_FAILED',
      message: `无法创建目录 ${dir}: ${error instanceof Error ? error.message : String(error)}`,
      hint: '换一个可写目录（--dir）',
      exitCode: EXIT.FAILURE,
    });
  }
  const probe = path.join(dir, `.skillrouter-write-probe-${process.pid}`);
  try {
    await fs.writeFile(probe, 'ok', 'utf8');
    await fs.rm(probe, { force: true });
  } catch (error: unknown) {
    const code = error instanceof Error && 'code' in error ? String(error.code) : '';
    throw new ApiFailure({
      code: 'TARGET_NOT_WRITABLE',
      message: `目录不可写 ${dir}: ${error instanceof Error ? error.message : String(error)}`,
      hint: '换一个可写目录（--dir），或用管理员权限重试',
      exitCode: code === 'EACCES' || code === 'EPERM' ? EXIT.PERMISSION : EXIT.FAILURE,
    });
  }
}

export async function writeFileText(target: string, content: string): Promise<string> {
  try {
    await fs.writeFile(target, content, 'utf8');
  } catch (error: unknown) {
    const code = error instanceof Error && 'code' in error ? String(error.code) : '';
    throw new ApiFailure({
      code: code === 'EACCES' || code === 'EPERM' ? 'TARGET_NOT_WRITABLE' : 'TARGET_WRITE_FAILED',
      message: `写入失败 ${target}: ${error instanceof Error ? error.message : String(error)}`,
      hint: '检查目录权限后用 --dir 换到可写目录',
      exitCode: code === 'EACCES' || code === 'EPERM' ? EXIT.PERMISSION : EXIT.FAILURE,
    });
  }
  return target;
}

export interface InstallArtifacts {
  skillMd: string;
  invokeMjs: string;
  readme: string;
  config: SkillRouterConfig;
}

export interface BuildArtifactsInput {
  baseUrl: string;
  tokenRef: string;
  dir: string;
  agent: AgentTarget;
  transport: Transport;
  skillName: string;
  installedAt: string;
  serverVersion: string | null;
  skillDetail: SkillDetail | undefined;
  capabilityKeys: string[];
}

/** 渲染四件产物（纯函数，便于单测与 update 复用）。 */
export async function buildArtifacts(input: BuildArtifactsInput): Promise<InstallArtifacts> {
  const skillTemplate = await readTemplate('skill.md.tpl');
  const readmeTemplate = await readTemplate('README.md.tpl');

  const detail = input.skillDetail;
  const skillName = detail ? detail.name : input.skillName;
  const title = detail ? detail.title : `${skillName}（Skill Router 接入）`;
  const description = detail ? detail.description : DEFAULT_DESCRIPTION;
  const version = detail ? detail.version : CLI_VERSION;
  const requiredCapabilities = detail ? detail.requiredCapabilities : [];
  const exampleCapability = input.capabilityKeys[0] ?? EXAMPLE_CAPABILITY_PLACEHOLDER;
  const extraBody = detail?.body
    ? `\n## 平台技能说明（服务端下发）\n\n${detail.body.trim()}\n`
    : '';

  const skillMd = renderTemplate(skillTemplate, {
    SKILL_NAME: skillName,
    SKILL_TITLE: title,
    DESCRIPTION: description,
    SKILL_VERSION: version,
    BASE_URL: input.baseUrl,
    TRANSPORT: input.transport,
    REQUIRED_CAPABILITIES: requiredCapabilities.join(', '),
    EXAMPLE_CAPABILITY: exampleCapability,
    CLI_VERSION,
    INSTALLED_AT: input.installedAt,
    SKILL_BODY_EXTRA: extraBody,
  });

  const invokeTemplate = await readTemplate('invoke.mjs.tpl');
  const invokeSource = detail?.invokeMjs ?? invokeTemplate;
  const invokeMjs = renderTemplate(invokeSource.replace(/\r\n/g, '\n'), {
    BASE_URL: input.baseUrl,
    TOKEN_REF: input.tokenRef,
    SKILL_NAME: skillName,
    CLI_VERSION,
    INSTALLED_AT: input.installedAt,
  });

  const readme = renderTemplate(readmeTemplate, {
    SKILL_NAME: skillName,
    SKILL_TITLE: title,
    BASE_URL: input.baseUrl,
    TRANSPORT: input.transport,
    AGENT: input.agent,
    DIR: input.dir,
    TOKEN_REF: input.tokenRef,
    SERVER_VERSION: input.serverVersion ?? 'unknown',
    CLI_VERSION,
    INSTALLED_AT: input.installedAt,
  });

  const config: SkillRouterConfig = {
    base_url: input.baseUrl,
    transport: input.transport,
    token_ref: input.tokenRef,
    installed_at: input.installedAt,
    cli_version: CLI_VERSION,
    server_version: input.serverVersion,
    skill: skillName,
    agent: input.agent,
    capabilities_snapshot: {
      fetched_at: new Date().toISOString(),
      count: input.capabilityKeys.length,
      keys: input.capabilityKeys,
    },
  };

  return { skillMd, invokeMjs, readme, config };
}

export async function runInstall(options: InstallOptions): Promise<InstallResult> {
  const env = options.env ?? currentEnv();
  const log = options.log;
  const warnings: string[] = [];
  const transport: Transport = options.transport ?? 'http';

  if (transport === 'mcp') {
    throw new ApiFailure({
      code: 'USAGE_TRANSPORT_UNVERIFIED',
      message: 'MCP 通道尚未实测（§7.9-4 / 附录 C 全部为「待验证」），本版本拒绝生成 MCP 变体',
      hint: '用 --transport http；等 docs/skills/connect.md 登记实测通过后再启用 mcp',
      exitCode: EXIT.USAGE,
    });
  }

  const baseUrl = normalizeBaseUrl(options.baseUrl);
  const tokenRef = tokenRefFor(baseUrl);

  // 1) 令牌：--token → --token-stdin → 已存储 → 交互式隐藏输入
  let token: string;
  if (options.token !== undefined && options.token.length > 0) {
    log.warn('--token 会进入 shell 历史与进程列表，推荐改用 --token-stdin');
    token = validateToken(options.token);
  } else if (options.tokenStdin === true) {
    const reader = options.readStdinToken ?? readStdinText;
    token = validateToken(await reader());
  } else {
    const stored = await resolveStoredToken(env, tokenRef);
    if (stored !== undefined && isValidTokenFormat(stored)) {
      token = stored;
      log.info(`复用已存储的令牌（token_ref=${tokenRef}，${maskToken(stored)}）`);
    } else {
      const prompt = options.promptToken ?? (() => promptHiddenToken('平台令牌（输入不回显）: '));
      token = validateToken(await prompt());
    }
  }

  // 2) 目标目录
  const detection = await detectTarget({ explicitDir: options.dir, agent: options.agent, env });
  for (const line of formatDetection(detection)) {
    log.info(line);
  }
  if (!detection.detected) {
    warnings.push('未探测到 AragonAgent / Claude Code，已写入通用目录；可用 --dir 指定目标目录');
  }
  if (options.agent !== undefined && detection.agent !== options.agent) {
    warnings.push(`--agent ${options.agent} 与实际目录推断的 ${detection.agent} 不一致，按目录推断执行`);
  }

  await ensureWritableDir(detection.dir);

  const skillPath = path.join(detection.dir, SKILL_FILENAME);
  if (options.force !== true) {
    let existing = false;
    try {
      await fs.access(skillPath);
      existing = true;
    } catch {
      existing = false;
    }
    if (existing) {
      throw new ApiFailure({
        code: 'TARGET_EXISTS',
        message: `${skillPath} 已存在，未覆盖`,
        hint: '确认要覆盖时加 --force',
        exitCode: EXIT.FAILURE,
      });
    }
  }

  // 3) 平台信息（--no-verify 时完全不联网，server_version 记为 null）
  let serverVersion: string | null = null;
  let skillDetail: SkillDetail | undefined;
  let capabilityKeys: string[] = [];
  if (options.noVerify !== true) {
    const health = await fetchHealth(baseUrl);
    serverVersion = health.version;
    log.info(`平台 /health: status=${health.status} version=${health.version}`);
    if (options.skill !== undefined && options.skill.length > 0) {
      skillDetail = await fetchSkillDetail(baseUrl, token, options.skill, options.timeoutMs);
      if (!skillDetail) {
        warnings.push(`平台未找到 SKILL「${options.skill}」，改用通用接入说明`);
      }
    }
    try {
      const snapshot = await fetchCapabilitySnapshot(baseUrl, token, options.timeoutMs);
      capabilityKeys = snapshot.keys;
      log.info(`平台能力目录：${snapshot.count} 个能力`);
    } catch (error: unknown) {
      if (error instanceof ApiFailure && error.connectivity) {
        throw error;
      }
      const message = error instanceof ApiFailure ? error.describe() : String(error);
      warnings.push(`能力目录拉取失败（不影响安装）：${message}`);
    }
  } else {
    log.warn('--no-verify：跳过平台连通性检查与安装后验证');
  }

  // 4) 渲染并写盘（严格四件产物）
  const artifacts = await buildArtifacts({
    baseUrl,
    tokenRef,
    dir: detection.dir,
    agent: detection.agent,
    transport,
    skillName: options.skill ?? 'skillrouter',
    installedAt: new Date().toISOString(),
    serverVersion,
    skillDetail,
    capabilityKeys,
  });
  assertSkillContract(artifacts.skillMd);
  if (options.noVerify === true) {
    // --no-verify 完全不联网：不伪造「能力目录快照」
    delete artifacts.config.capabilities_snapshot;
  }

  const files: string[] = [];
  files.push(await writeFileText(skillPath, artifacts.skillMd));
  files.push(await writeFileText(path.join(detection.dir, INVOKE_FILENAME), artifacts.invokeMjs));
  const configPath = await writeConfig(detection.dir, artifacts.config);
  files.push(configPath);
  files.push(await writeFileText(path.join(detection.dir, README_FILENAME), artifacts.readme));

  // 5) 凭据（唯一含密钥的文件）
  const credentialsPath = await saveToken(env, {
    token,
    base_url: baseUrl,
    token_ref: tokenRef,
    label: `skillrouter@${detection.agent}`,
    created_at: new Date().toISOString(),
    cli_version: CLI_VERSION,
  });

  // 6) 安装后验证（--no-verify 跳过）
  let verifyReport: VerifyReport | undefined;
  let exitCode: ExitCode = EXIT.OK;
  if (options.noVerify !== true) {
    verifyReport = await runVerify({
      baseUrl,
      token,
      timeoutMs: options.timeoutMs,
      probeCapability: undefined,
    });
    if (!verifyReport.ok) {
      exitCode = verifyReport.exitCode;
    }
  }

  return {
    exitCode,
    dir: detection.dir,
    detection,
    files,
    config: artifacts.config,
    credentialsPath,
    tokenMasked: maskToken(token),
    verify: verifyReport,
    serverVersion,
    warnings,
  };
}
