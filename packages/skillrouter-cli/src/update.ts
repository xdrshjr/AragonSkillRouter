import path from 'node:path';

import { currentEnv, type AgentTarget, type Env } from './detect.js';
import {
  ApiFailure,
  EXIT,
  locateInstallation,
  resolveStoredToken,
  writeConfig,
  type ExitCode,
  type Logger,
  type SkillRouterConfig,
} from './http.js';
import {
  INVOKE_FILENAME,
  README_FILENAME,
  SKILL_FILENAME,
  assertSkillContract,
  buildArtifacts,
  fetchCapabilitySnapshot,
  fetchHealth,
  fetchSkillDetail,
  writeFileText,
} from './install.js';

export interface UpdateOptions {
  dir?: string;
  env?: Env;
  log: Logger;
  timeoutMs?: number;
}

export interface UpdateResult {
  exitCode: ExitCode;
  dir: string;
  updatedFiles: string[];
  config: SkillRouterConfig;
  capabilityKeys: string[];
  serverVersion: string | null;
}

/**
 * `skillrouter update`：重新拉取 SKILL 正文、`invoke.mjs` 与能力目录快照，但不重装
 * （不重新输入令牌、不改 `installed_at`、不动其它文件）。
 */
export async function runUpdate(options: UpdateOptions): Promise<UpdateResult> {
  const env = options.env ?? currentEnv();
  const log = options.log;

  const located = await locateInstallation(env, options.dir);
  if (!located) {
    throw new ApiFailure({
      code: 'INSTALL_NOT_FOUND',
      message: options.dir
        ? `${path.resolve(env.cwd, options.dir)} 下没有 skillrouter.config.json`
        : '未在任何已探测目录里找到 skillrouter.config.json',
      hint: '用 --dir 指定安装目录，或先运行 skillrouter install',
      exitCode: EXIT.NO_AGENT,
    });
  }

  const { dir, config } = located;
  const token = await resolveStoredToken(env, config.token_ref);
  if (token === undefined) {
    throw new ApiFailure({
      code: 'TOKEN_MISSING',
      message: `未找到 token_ref=${config.token_ref} 对应的令牌`,
      hint: '重新运行 skillrouter install --token-stdin --force',
      exitCode: EXIT.FAILURE,
    });
  }

  log.info(`更新 ${dir}（平台 ${config.base_url}）`);
  const health = await fetchHealth(config.base_url);
  log.info(`平台 /health: status=${health.status} version=${health.version}`);

  const snapshot = await fetchCapabilitySnapshot(config.base_url, token, options.timeoutMs);
  log.info(`平台能力目录：${snapshot.count} 个能力`);

  const skillDetail = await fetchSkillDetail(config.base_url, token, config.skill, options.timeoutMs);
  if (!skillDetail) {
    log.warn(`平台未找到 SKILL「${config.skill}」，保留原有接入说明并只刷新能力快照`);
  }

  const agent: AgentTarget = config.agent;
  const artifacts = await buildArtifacts({
    baseUrl: config.base_url,
    tokenRef: config.token_ref,
    dir,
    agent,
    transport: config.transport,
    skillName: config.skill,
    installedAt: config.installed_at,
    serverVersion: health.version,
    skillDetail,
    capabilityKeys: snapshot.keys,
  });
  assertSkillContract(artifacts.skillMd);

  const updatedFiles: string[] = [];
  updatedFiles.push(await writeFileText(path.join(dir, SKILL_FILENAME), artifacts.skillMd));
  updatedFiles.push(await writeFileText(path.join(dir, INVOKE_FILENAME), artifacts.invokeMjs));
  updatedFiles.push(await writeFileText(path.join(dir, README_FILENAME), artifacts.readme));

  const nextConfig: SkillRouterConfig = {
    ...artifacts.config,
    installed_at: config.installed_at,
    updated_at: new Date().toISOString(),
  };
  updatedFiles.push(await writeConfig(dir, nextConfig));

  return {
    exitCode: EXIT.OK,
    dir,
    updatedFiles,
    config: nextConfig,
    capabilityKeys: snapshot.keys,
    serverVersion: health.version,
  };
}
