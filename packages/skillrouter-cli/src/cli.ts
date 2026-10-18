#!/usr/bin/env node
import { promises as fs } from 'node:fs';
import path from 'node:path';

import {
  candidateSkillDirs,
  currentEnv,
  detectTarget,
  formatDetection,
  type AgentTarget,
  type Env,
} from './detect.js';
import {
  ApiFailure,
  CONFIG_FILENAME,
  EXIT,
  credentialsPath,
  locateInstallation,
  readCredentials,
  tokenRefFor,
  resolveStoredToken,
  normalizeBaseUrl,
  maskToken,
  type ExitCode,
  type Logger,
  type Transport,
} from './http.js';
import { CLI_VERSION, INVOKE_FILENAME, README_FILENAME, SKILL_FILENAME, runInstall } from './install.js';
import { runUninstall } from './uninstall.js';
import { runUpdate } from './update.js';
import { describeStep, runVerify, type VerifyReport } from './verify.js';

type FlagValue = string | true;

interface ParsedCli {
  command: string | undefined;
  positionals: string[];
  flags: Map<string, FlagValue>;
}

const BOOLEAN_FLAGS = new Set([
  'force',
  'no-verify',
  'token-stdin',
  'json',
  'keep-config',
  'help',
  'version',
]);

const VALUE_FLAGS = new Set([
  'base-url',
  'token',
  'agent',
  'transport',
  'dir',
  'skill',
  'probe-capability',
  'timeout',
]);

const GLOBAL_FLAGS = new Set(['json', 'help', 'version']);

const COMMAND_FLAGS: Record<string, Set<string>> = {
  install: new Set(['base-url', 'token', 'token-stdin', 'agent', 'transport', 'dir', 'skill', 'no-verify', 'force']),
  verify: new Set(['base-url', 'token', 'dir', 'probe-capability', 'timeout']),
  list: new Set(['dir']),
  update: new Set(['dir', 'timeout']),
  uninstall: new Set(['dir', 'keep-config']),
  doctor: new Set([]),
};

class ConsoleLogger implements Logger {
  readonly messages: string[] = [];
  private readonly json: boolean;

  constructor(json: boolean) {
    this.json = json;
  }

  info(message: string): void {
    this.push(message);
  }

  success(message: string): void {
    this.push(`✓ ${message}`);
  }

  fail(message: string): void {
    this.push(`✗ ${message}`);
  }

  warn(message: string): void {
    this.push(`! ${message}`);
  }

  private push(message: string): void {
    this.messages.push(message);
    if (!this.json) {
      process.stdout.write(`${message}\n`);
    }
  }
}

function usageError(message: string, hint: string): ApiFailure {
  return new ApiFailure({ code: 'USAGE_ERROR', message, hint, exitCode: EXIT.USAGE });
}

function parseArgv(argv: string[]): ParsedCli {
  const flags = new Map<string, FlagValue>();
  const positionals: string[] = [];
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === undefined) continue;
    if (arg === '-h') {
      flags.set('help', true);
      continue;
    }
    if (arg === '-v') {
      flags.set('version', true);
      continue;
    }
    if (arg.startsWith('--')) {
      const equals = arg.indexOf('=');
      if (equals > 2) {
        const name = arg.slice(2, equals);
        const value = arg.slice(equals + 1);
        if (!BOOLEAN_FLAGS.has(name) && !VALUE_FLAGS.has(name)) {
          throw usageError(`未知参数 --${name}`, '运行 skillrouter --help 查看支持的参数');
        }
        flags.set(name, value);
        continue;
      }
      const name = arg.slice(2);
      if (BOOLEAN_FLAGS.has(name)) {
        flags.set(name, true);
        continue;
      }
      if (VALUE_FLAGS.has(name)) {
        const next = argv[index + 1];
        if (next === undefined || next.startsWith('--')) {
          throw usageError(`--${name} 缺少取值`, `例如 --${name} <value>`);
        }
        flags.set(name, next);
        index += 1;
        continue;
      }
      throw usageError(`未知参数 --${name}`, '运行 skillrouter --help 查看支持的参数');
    }
    positionals.push(arg);
  }
  return { command: positionals.shift(), positionals, flags };
}

function flagString(flags: Map<string, FlagValue>, name: string): string | undefined {
  const value = flags.get(name);
  return typeof value === 'string' ? value : undefined;
}

function flagBoolean(flags: Map<string, FlagValue>, name: string): boolean {
  return flags.get(name) === true;
}

function parseAgent(raw: string | undefined): AgentTarget | undefined {
  if (raw === undefined) return undefined;
  if (raw === 'aragon' || raw === 'claude' || raw === 'generic') return raw;
  throw usageError(`--agent 只能是 aragon|claude|generic，实际 ${raw}`, '默认按探测结果决定');
}

function parseTransport(raw: string | undefined): Transport {
  if (raw === undefined) return 'http';
  if (raw === 'http' || raw === 'mcp') return raw;
  throw usageError(`--transport 只能是 http|mcp，实际 ${raw}`, '默认 http（MCP 通路尚未实测）');
}

function parseTimeout(raw: string | undefined): number | undefined {
  if (raw === undefined) return undefined;
  const value = Number(raw);
  if (!Number.isFinite(value) || value <= 0) {
    throw usageError(`--timeout 需要正整数毫秒，实际 ${raw}`, '例如 --timeout 30000');
  }
  return value;
}

function validateFlags(flags: Map<string, FlagValue>, command: string): void {
  const allowed = COMMAND_FLAGS[command];
  if (allowed === undefined) return;
  for (const name of flags.keys()) {
    if (!allowed.has(name) && !GLOBAL_FLAGS.has(name)) {
      throw usageError(`命令 ${command} 不支持 --${name}`, '运行 skillrouter --help 查看每个命令的参数');
    }
  }
}

function printHelp(): void {
  const lines = [
    `skillrouter ${CLI_VERSION} — Skill Router 安装器`,
    '',
    '用法:',
    '  skillrouter install   --base-url <url> [--token <srk_...> | --token-stdin]',
    '                        [--agent aragon|claude|generic] [--transport http|mcp]',
    '                        [--dir <path>] [--skill <name>] [--no-verify] [--force]',
    '  skillrouter verify    [--base-url <url>] [--token <token>] [--dir <path>]',
    '                        [--probe-capability <key>] [--timeout <ms>]',
    '  skillrouter list      [--dir <path>]',
    '  skillrouter update    [--dir <path>]',
    '  skillrouter uninstall [--dir <path>] [--keep-config]',
    '  skillrouter doctor',
    '',
    '全局: --json（机器可读输出）, --help, --version',
    '',
    '退出码: 0 成功 | 1 一般失败 | 2 参数错误 | 3 连通性失败 | 4 无写入权限 | 5 目标 Agent 未探测到',
    '',
    '探测顺序: --dir → ./.aragon/skills → ./.claude/skills → $ARAGON_HOME/skills',
    '          → ~/.aragon-agent/skills → ./skillrouter-skill',
  ];
  process.stdout.write(`${lines.join('\n')}\n`);
}

function printVerifySteps(report: VerifyReport, logger: Logger): void {
  for (const step of report.steps) {
    const text = describeStep(step);
    if (step.ok) {
      logger.success(text.replace(/^✓ /, ''));
    } else {
      logger.fail(text.replace(/^✗ /, ''));
    }
  }
}

interface CommandResult {
  exitCode: ExitCode;
  payload: Record<string, unknown>;
}

async function commandInstall(parsed: ParsedCli, env: Env, logger: Logger): Promise<CommandResult> {
  const baseUrlRaw = flagString(parsed.flags, 'base-url');
  if (baseUrlRaw === undefined) {
    throw usageError('install 需要 --base-url', 'skillrouter install --base-url https://router.example.com --token-stdin');
  }
  const token = flagString(parsed.flags, 'token');
  const result = await runInstall({
    baseUrl: baseUrlRaw,
    token,
    tokenStdin: flagBoolean(parsed.flags, 'token-stdin'),
    agent: parseAgent(flagString(parsed.flags, 'agent')),
    transport: parseTransport(flagString(parsed.flags, 'transport')),
    dir: flagString(parsed.flags, 'dir'),
    skill: flagString(parsed.flags, 'skill'),
    noVerify: flagBoolean(parsed.flags, 'no-verify'),
    force: flagBoolean(parsed.flags, 'force'),
    env,
    log: logger,
  });

  for (const warning of result.warnings) {
    logger.warn(warning);
  }
  logger.success(`安装完成：${result.files.length} 个文件`);
  for (const file of result.files) {
    logger.info(`  ${file}`);
  }
  logger.info(`  凭据（唯一含令牌的文件）: ${result.credentialsPath}`);
  logger.info(`  令牌 ${result.tokenMasked} → token_ref=${result.config.token_ref}`);
  if (result.verify) {
    logger.info('验证结果:');
    printVerifySteps(result.verify, logger);
  }
  if (result.exitCode === EXIT.OK) {
    logger.info('');
    logger.info('下一步（在安装目录执行）:');
    logger.info('  node ./invoke.mjs capabilities.list');
    logger.info('  node ./invoke.mjs invoke --capability <key> --input \'{"prompt":"hi"}\'');
  } else {
    logger.warn(`文件已写入，但验证未全部通过（退出码 ${result.exitCode}）`);
  }

  return {
    exitCode: result.exitCode,
    payload: {
      dir: result.dir,
      files: result.files,
      credentials_path: result.credentialsPath,
      token_ref: result.config.token_ref,
      token_masked: result.tokenMasked,
      base_url: result.config.base_url,
      transport: result.config.transport,
      server_version: result.serverVersion,
      detection: { candidate: result.detection.candidate, reason: result.detection.reason },
      warnings: result.warnings,
      verify: result.verify
        ? {
            ok: result.verify.ok,
            steps: result.verify.steps.map((step) => ({
              name: step.name,
              ok: step.ok,
              detail: step.detail,
              request_id: step.requestId ?? null,
            })),
          }
        : null,
    },
  };
}

interface VerifyContext {
  baseUrl: string;
  token: string;
}

async function resolveVerifyContext(parsed: ParsedCli, env: Env, logger: Logger): Promise<VerifyContext> {
  const explicitBaseUrl = flagString(parsed.flags, 'base-url');
  const explicitToken = flagString(parsed.flags, 'token');

  if (explicitBaseUrl !== undefined) {
    const baseUrl = normalizeBaseUrl(explicitBaseUrl);
    if (explicitToken !== undefined) {
      return { baseUrl, token: explicitToken.trim() };
    }
    const stored = await resolveStoredToken(env, tokenRefFor(baseUrl));
    if (stored === undefined) {
      throw usageError(
        `未找到 ${baseUrl} 对应的已存储令牌`,
        '改用 skillrouter verify --base-url <url> --token <srk_...>，或先 install',
      );
    }
    return { baseUrl, token: stored };
  }

  const located = await locateInstallation(env, flagString(parsed.flags, 'dir'));
  if (!located) {
    throw new ApiFailure({
      code: 'INSTALL_NOT_FOUND',
      message: '未找到已安装的 SKILL（没有 skillrouter.config.json）',
      hint: '用 --dir 指定安装目录，或用 --base-url + --token 直接验证平台',
      exitCode: EXIT.NO_AGENT,
    });
  }
  logger.info(`使用已安装配置：${path.join(located.dir, CONFIG_FILENAME)}`);
  const token = explicitToken?.trim() ?? (await resolveStoredToken(env, located.config.token_ref));
  if (token === undefined) {
    throw usageError(
      `未找到 token_ref=${located.config.token_ref} 对应的令牌`,
      '重新运行 skillrouter install --token-stdin --force',
    );
  }
  return { baseUrl: located.config.base_url, token };
}

async function commandVerify(parsed: ParsedCli, env: Env, logger: Logger): Promise<CommandResult> {
  const context = await resolveVerifyContext(parsed, env, logger);
  const report = await runVerify({
    baseUrl: context.baseUrl,
    token: context.token,
    probeCapability: flagString(parsed.flags, 'probe-capability'),
    timeoutMs: parseTimeout(flagString(parsed.flags, 'timeout')),
  });
  printVerifySteps(report, logger);
  if (report.ok) {
    logger.success('全部检查通过');
  } else {
    logger.fail(`检查未通过（退出码 ${report.exitCode}）`);
  }
  return {
    exitCode: report.exitCode,
    payload: {
      base_url: context.baseUrl,
      token_masked: maskToken(context.token),
      ok: report.ok,
      steps: report.steps.map((step) => ({
        name: step.name,
        ok: step.ok,
        detail: step.detail,
        failure: step.failure,
        request_id: step.requestId ?? null,
      })),
    },
  };
}

async function fileExists(target: string): Promise<boolean> {
  try {
    await fs.access(target);
    return true;
  } catch {
    return false;
  }
}

async function commandList(parsed: ParsedCli, env: Env, logger: Logger): Promise<CommandResult> {
  const explicitDir = flagString(parsed.flags, 'dir');
  const located = await locateInstallation(env, explicitDir);
  if (!located) {
    const candidates = candidateSkillDirs(env);
    throw new ApiFailure({
      code: 'INSTALL_NOT_FOUND',
      message: '未找到已安装的 SKILL',
      hint: `已检查: ${candidates.join(', ')}；用 --dir 指定安装目录`,
      exitCode: EXIT.NO_AGENT,
    });
  }
  const { dir, config } = located;
  logger.success(`安装目录 ${dir}`);
  const rows: Array<[string, string]> = [
    ['SKILL 名称', config.skill],
    ['平台地址', config.base_url],
    ['通道', config.transport],
    ['目标 Agent', config.agent],
    ['token_ref', config.token_ref],
    ['安装时间', config.installed_at],
    ['最近更新', config.updated_at ?? '—'],
    ['CLI 版本', config.cli_version],
    ['平台版本', config.server_version ?? 'unknown'],
    [
      '能力快照',
      config.capabilities_snapshot
        ? `${config.capabilities_snapshot.count} 个（${config.capabilities_snapshot.fetched_at}）`
        : '—',
    ],
  ];
  const files: Record<string, boolean> = {};
  for (const name of [SKILL_FILENAME, INVOKE_FILENAME, CONFIG_FILENAME, README_FILENAME]) {
    files[name] = await fileExists(path.join(dir, name));
  }
  for (const [label, value] of rows) {
    logger.info(`  ${label}: ${value}`);
  }
  for (const [name, exists] of Object.entries(files)) {
    if (exists) {
      logger.success(`  ${name}`);
    } else {
      logger.fail(`  ${name}（缺失）`);
    }
  }
  return {
    exitCode: EXIT.OK,
    payload: { dir, config, files },
  };
}

async function commandUpdate(parsed: ParsedCli, env: Env, logger: Logger): Promise<CommandResult> {
  const result = await runUpdate({
    dir: flagString(parsed.flags, 'dir'),
    env,
    log: logger,
    timeoutMs: parseTimeout(flagString(parsed.flags, 'timeout')),
  });
  logger.success(`已刷新 ${result.updatedFiles.length} 个文件`);
  for (const file of result.updatedFiles) {
    logger.info(`  ${file}`);
  }
  logger.info(`能力快照：${result.capabilityKeys.length} 个能力，平台版本 ${result.serverVersion ?? 'unknown'}`);
  return {
    exitCode: result.exitCode,
    payload: {
      dir: result.dir,
      updated_files: result.updatedFiles,
      capabilities: result.capabilityKeys,
      server_version: result.serverVersion,
    },
  };
}

async function commandUninstall(parsed: ParsedCli, env: Env, logger: Logger): Promise<CommandResult> {
  const result = await runUninstall({
    dir: flagString(parsed.flags, 'dir'),
    keepConfig: flagBoolean(parsed.flags, 'keep-config'),
    env,
    log: logger,
  });
  if (result.removed.length === 0) {
    logger.info('没有文件需要删除');
  } else {
    for (const file of result.removed) {
      logger.success(`已删除 ${file}`);
    }
  }
  for (const file of result.kept) {
    logger.info(`已保留 ${file}`);
  }
  if (result.dirRemoved) {
    logger.info(`目录已空，已移除 ${result.dir}`);
  }
  return {
    exitCode: result.exitCode,
    payload: {
      dir: result.dir,
      removed: result.removed,
      kept: result.kept,
      credential_removed: result.credentialRemoved,
      dir_removed: result.dirRemoved,
    },
  };
}

async function probeOsKeyring(): Promise<boolean> {
  // 可选依赖：本包不引入 keytar，只有用户显式安装了它才会命中 OS 凭据库
  const specifier = 'keytar';
  try {
    const loaded: unknown = await import(specifier);
    return typeof loaded === 'object' && loaded !== null;
  } catch {
    return false;
  }
}

async function commandDoctor(parsed: ParsedCli, env: Env, logger: Logger): Promise<CommandResult> {
  void parsed;
  const nodeVersion = process.versions.node;
  const major = Number(nodeVersion.split('.')[0] ?? '0');
  const nodeOk = Number.isFinite(major) && major >= 18;
  const checks: Array<{ name: string; ok: boolean; detail: string }> = [];
  checks.push({
    name: 'Node 版本',
    ok: nodeOk,
    detail: `v${nodeVersion}（要求 >= 18）`,
  });
  checks.push({ name: '平台', ok: true, detail: `${process.platform} ${process.arch}` });
  checks.push({ name: '工作目录', ok: true, detail: env.cwd });
  checks.push({ name: '用户主目录', ok: true, detail: env.home });
  checks.push({
    name: 'ARAGON_HOME',
    ok: true,
    detail: env.aragonHome ?? '未设置',
  });

  const detection = await detectTarget({ env });
  const chosen: string[] = [];
  for (const line of formatDetection(detection)) {
    chosen.push(line);
  }
  checks.push({
    name: '目标目录探测',
    ok: detection.detected,
    detail: `${detection.dir}（${detection.candidate}）`,
  });

  const candidates: Array<{ path: string; exists: boolean }> = [];
  for (const candidate of candidateSkillDirs(env)) {
    candidates.push({ path: candidate, exists: await fileExists(candidate) });
  }

  const credsFile = credentialsPath(env);
  const credsExists = await fileExists(credsFile);
  let credsMode = 'n/a';
  let credsRefs = 0;
  if (credsExists) {
    try {
      const stat = await fs.stat(credsFile);
      credsMode = `0o${(stat.mode & 0o777).toString(8)}`;
    } catch {
      credsMode = 'unknown';
    }
    try {
      const file = await readCredentials(env);
      credsRefs = Object.keys(file.tokens).length;
    } catch {
      credsRefs = -1;
    }
  }
  checks.push({
    name: '凭据文件',
    ok: credsExists,
    detail: `${credsFile}（${credsExists ? `mode ${credsMode}, ${credsRefs} 条引用` : '不存在'}）`,
  });

  const keyring = await probeOsKeyring();
  checks.push({
    name: '凭据存储后端',
    ok: true,
    detail: keyring
      ? 'OS 凭据库（keytar 可用）'
      : `文件 ${credsFile}（0600；Windows 用 icacls 收紧，未安装可选依赖 keytar）`,
  });

  const located = await locateInstallation(env);
  checks.push({
    name: '已有安装',
    ok: located !== undefined,
    detail: located ? `${located.dir}（skill=${located.config.skill}）` : '未找到 skillrouter.config.json',
  });

  for (const check of checks) {
    if (check.ok) {
      logger.success(`${check.name}: ${check.detail}`);
    } else {
      logger.fail(`${check.name}: ${check.detail}`);
    }
  }
  for (const line of chosen) {
    logger.info(`  ${line}`);
  }
  for (const candidate of candidates) {
    logger.info(`  ${candidate.exists ? '✓' : '·'} ${candidate.path}`);
  }

  return {
    exitCode: nodeOk ? EXIT.OK : EXIT.FAILURE,
    payload: {
      node: { version: nodeVersion, ok: nodeOk },
      platform: `${process.platform} ${process.arch}`,
      cwd: env.cwd,
      home: env.home,
      aragon_home: env.aragonHome ?? null,
      detection: {
        dir: detection.dir,
        candidate: detection.candidate,
        reason: detection.reason,
        detected: detection.detected,
      },
      candidates,
      credentials: {
        path: credsFile,
        exists: credsExists,
        mode: credsMode,
        refs: credsRefs,
        backend: keyring ? 'os-keyring' : 'file',
      },
      installation: located ? { dir: located.dir, config: located.config } : null,
      checks,
    },
  };
}

async function dispatch(parsed: ParsedCli, env: Env, logger: Logger): Promise<CommandResult> {
  const command = parsed.command;
  switch (command) {
    case undefined:
      printHelp();
      return { exitCode: EXIT.OK, payload: { help: true } };
    case 'install':
      validateFlags(parsed.flags, 'install');
      return await commandInstall(parsed, env, logger);
    case 'verify':
      validateFlags(parsed.flags, 'verify');
      return await commandVerify(parsed, env, logger);
    case 'list':
      validateFlags(parsed.flags, 'list');
      return await commandList(parsed, env, logger);
    case 'update':
      validateFlags(parsed.flags, 'update');
      return await commandUpdate(parsed, env, logger);
    case 'uninstall':
      validateFlags(parsed.flags, 'uninstall');
      return await commandUninstall(parsed, env, logger);
    case 'doctor':
      validateFlags(parsed.flags, 'doctor');
      return await commandDoctor(parsed, env, logger);
    case 'help':
      printHelp();
      return { exitCode: EXIT.OK, payload: { help: true } };
    case 'version':
      process.stdout.write(`${CLI_VERSION}\n`);
      return { exitCode: EXIT.OK, payload: { version: CLI_VERSION } };
    default:
      throw usageError(`未知命令: ${command}`, '可用命令: install / verify / list / update / uninstall / doctor');
  }
}

async function main(argv: string[]): Promise<number> {
  let parsed: ParsedCli;
  try {
    parsed = parseArgv(argv);
  } catch (error: unknown) {
    if (error instanceof ApiFailure) {
      process.stderr.write(`✗ ${error.describe()}\n`);
      return error.exitCode;
    }
    throw error;
  }

  const json = flagBoolean(parsed.flags, 'json');
  if (flagBoolean(parsed.flags, 'help') || parsed.command === 'help') {
    printHelp();
    return EXIT.OK;
  }
  if (flagBoolean(parsed.flags, 'version') || parsed.command === 'version') {
    process.stdout.write(`${CLI_VERSION}\n`);
    return EXIT.OK;
  }

  const logger = new ConsoleLogger(json);
  const env = currentEnv();

  try {
    const result = await dispatch(parsed, env, logger);
    if (json) {
      const payload = {
        ok: result.exitCode === EXIT.OK,
        exit_code: result.exitCode,
        command: parsed.command ?? null,
        cli_version: CLI_VERSION,
        messages: logger.messages,
        ...result.payload,
      };
      process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
    }
    return result.exitCode;
  } catch (error: unknown) {
    if (error instanceof ApiFailure) {
      logger.fail(error.describe());
      if (json) {
        process.stdout.write(
          `${JSON.stringify(
            {
              ok: false,
              exit_code: error.exitCode,
              command: parsed.command ?? null,
              messages: logger.messages,
              error: error.toPayload(),
            },
            null,
            2,
          )}\n`,
        );
      }
      return error.exitCode;
    }
    const message = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
    logger.fail(`未预期的错误 ${message}`);
    if (error instanceof Error && error.stack) {
      logger.info(error.stack.split('\n').slice(1, 4).join('\n'));
    }
    return EXIT.FAILURE;
  }
}

main(process.argv.slice(2))
  .then((code) => {
    process.exitCode = code;
  })
  .catch((error: unknown) => {
    process.stderr.write(`✗ 致命错误: ${error instanceof Error ? error.stack ?? error.message : String(error)}\n`);
    process.exitCode = EXIT.FAILURE;
  });
