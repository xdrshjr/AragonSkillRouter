import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

/**
 * 目标 Agent 类型（§9.8 `--agent aragon|claude|generic`）。
 * `bash` 是三种目标环境里都真实存在的工具，因此 SKILL frontmatter 只声明它。
 */
export type AgentTarget = 'aragon' | 'claude' | 'generic';

/** 探测所需的运行环境快照（全部可注入，便于测试）。 */
export interface Env {
  /** 当前工作目录（相对路径基准） */
  cwd: string;
  /** 用户主目录 */
  home: string;
  /** `ARAGON_HOME` 覆盖的 AragonAgent 安装目录（未设置时为 undefined） */
  aragonHome: string | undefined;
  platform: NodeJS.Platform;
  env: Record<string, string | undefined>;
}

export interface EnvOverrides {
  cwd?: string;
  home?: string;
  aragonHome?: string;
  platform?: NodeJS.Platform;
  env?: Record<string, string | undefined>;
}

/** 通用兜底目录名（未探测到任何 Agent 时使用）。 */
export const GENERIC_DIR_NAME = 'skillrouter-skill';

export function currentEnv(overrides: EnvOverrides = {}): Env {
  const processEnv = overrides.env ?? process.env;
  const home =
    overrides.home ??
    processEnv.HOME ??
    processEnv.USERPROFILE ??
    os.homedir();
  const aragonHome = overrides.aragonHome ?? processEnv.ARAGON_HOME;
  return {
    cwd: overrides.cwd ?? process.cwd(),
    home,
    aragonHome: aragonHome && aragonHome.length > 0 ? aragonHome : undefined,
    platform: overrides.platform ?? process.platform,
    env: processEnv,
  };
}

export interface DetectOptions {
  /** `--dir` 显式指定（相对 env.cwd 解析） */
  explicitDir?: string;
  /** `--agent` 显式指定，仅影响 `--dir` 场景下的标签 */
  agent?: AgentTarget;
  env?: Env;
}

export interface CandidateCheck {
  /** 绝对路径 */
  path: string;
  label: string;
  exists: boolean;
}

export interface Detection {
  /** 选中目录的绝对路径 */
  dir: string;
  agent: AgentTarget;
  /** 必须打印给用户看的理由（§9.8：避免用户找不到文件） */
  reason: string;
  /** false 表示只落到通用兜底目录，没有探测到具体 Agent */
  detected: boolean;
  candidate: string;
  candidates: CandidateCheck[];
}

async function isDirectory(target: string): Promise<boolean> {
  try {
    const stat = await fs.stat(target);
    return stat.isDirectory();
  } catch {
    return false;
  }
}

function inferAgentFromPath(dir: string): AgentTarget {
  const normalized = dir.replace(/\\/g, '/').toLowerCase();
  if (normalized.includes('.aragon') || normalized.includes('aragon-agent')) {
    return 'aragon';
  }
  if (normalized.includes('.claude')) {
    return 'claude';
  }
  return 'generic';
}

/** 有序候选目录（不含 `--dir`），供 doctor 展示与 update/uninstall 反查。 */
export function candidateSkillDirs(env: Env): string[] {
  const dirs: string[] = [
    path.join(env.cwd, '.aragon', 'skills'),
    path.join(env.cwd, '.claude', 'skills'),
  ];
  if (env.aragonHome) {
    dirs.push(path.join(env.aragonHome, 'skills'));
  }
  dirs.push(path.join(env.home, '.aragon-agent', 'skills'));
  dirs.push(path.join(env.cwd, GENERIC_DIR_NAME));
  return dirs;
}

async function buildCandidates(env: Env): Promise<CandidateCheck[]> {
  const entries: Array<{ path: string; label: string }> = [
    { path: path.join(env.cwd, '.aragon', 'skills'), label: '.aragon/skills' },
    { path: path.join(env.cwd, '.claude', 'skills'), label: '.claude/skills' },
  ];
  if (env.aragonHome) {
    entries.push({
      path: path.join(env.aragonHome, 'skills'),
      label: '$ARAGON_HOME/skills',
    });
  }
  entries.push({
    path: path.join(env.home, '.aragon-agent', 'skills'),
    label: '~/.aragon-agent/skills',
  });
  entries.push({
    path: path.join(env.cwd, GENERIC_DIR_NAME),
    label: `./${GENERIC_DIR_NAME}`,
  });

  const checks: CandidateCheck[] = [];
  for (const entry of entries) {
    checks.push({ path: entry.path, label: entry.label, exists: await isDirectory(entry.path) });
  }
  return checks;
}

/**
 * 探测顺序（§9.8，逐条实现）：
 * 1. `--dir`
 * 2. `./.aragon/skills`（存在 `./.aragon` 时）
 * 3. `./.claude/skills`（存在 `./.claude` 时）
 * 4. `$ARAGON_HOME/skills` 或 `~/.aragon-agent/skills`
 * 5. 通用目录 `./skillrouter-skill`
 */
export async function detectTarget(options: DetectOptions = {}): Promise<Detection> {
  const env = options.env ?? currentEnv();
  const candidates = await buildCandidates(env);

  if (options.explicitDir && options.explicitDir.trim().length > 0) {
    const dir = path.resolve(env.cwd, options.explicitDir.trim());
    const agent = options.agent ?? inferAgentFromPath(dir);
    return {
      dir,
      agent,
      reason: `使用 --dir 指定的目录（${options.explicitDir}）`,
      detected: true,
      candidate: '--dir',
      candidates,
    };
  }

  const aragonLocal = path.join(env.cwd, '.aragon');
  if (await isDirectory(aragonLocal)) {
    return {
      dir: path.join(env.cwd, '.aragon', 'skills'),
      agent: 'aragon',
      reason: '检测到 .aragon/，已写入 .aragon/skills',
      detected: true,
      candidate: '.aragon/skills',
      candidates,
    };
  }

  const claudeLocal = path.join(env.cwd, '.claude');
  if (await isDirectory(claudeLocal)) {
    return {
      dir: path.join(env.cwd, '.claude', 'skills'),
      agent: 'claude',
      reason: '检测到 .claude/skills，已写入此处',
      detected: true,
      candidate: '.claude/skills',
      candidates,
    };
  }

  if (env.aragonHome) {
    return {
      dir: path.join(env.aragonHome, 'skills'),
      agent: 'aragon',
      reason: `检测到 ARAGON_HOME=${env.aragonHome}，已写入 $ARAGON_HOME/skills`,
      detected: true,
      candidate: '$ARAGON_HOME/skills',
      candidates,
    };
  }

  const agentHome = path.join(env.home, '.aragon-agent');
  if (await isDirectory(agentHome)) {
    return {
      dir: path.join(agentHome, 'skills'),
      agent: 'aragon',
      reason: '检测到 ~/.aragon-agent/，已写入 ~/.aragon-agent/skills',
      detected: true,
      candidate: '~/.aragon-agent/skills',
      candidates,
    };
  }

  return {
    dir: path.join(env.cwd, GENERIC_DIR_NAME),
    agent: options.agent ?? 'generic',
    reason: `未探测到 AragonAgent / Claude Code，使用通用目录 ./${GENERIC_DIR_NAME}（可用 --dir 指定）`,
    detected: false,
    candidate: `./${GENERIC_DIR_NAME}`,
    candidates,
  };
}

/** 供 CLI 打印「最终选择 + 理由」。 */
export function formatDetection(detection: Detection): string[] {
  return [
    `目录: ${detection.dir}（${detection.candidate}）`,
    `理由: ${detection.reason}`,
  ];
}
