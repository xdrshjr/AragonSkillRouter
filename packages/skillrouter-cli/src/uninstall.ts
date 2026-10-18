import { promises as fs } from 'node:fs';
import path from 'node:path';

import { currentEnv, type Env } from './detect.js';
import {
  ApiFailure,
  CONFIG_FILENAME,
  EXIT,
  deleteStoredToken,
  locateInstallation,
  type ExitCode,
  type Logger,
} from './http.js';
import { INVOKE_FILENAME, README_FILENAME, SKILL_FILENAME } from './install.js';

export interface UninstallOptions {
  dir?: string;
  /** 保留 `skillrouter.config.json` 与凭据，便于稍后重装 */
  keepConfig?: boolean;
  env?: Env;
  log: Logger;
}

export interface UninstallResult {
  exitCode: ExitCode;
  dir: string;
  removed: string[];
  kept: string[];
  credentialRemoved: boolean;
  dirRemoved: boolean;
}

async function removeFile(target: string): Promise<boolean> {
  try {
    await fs.unlink(target);
    return true;
  } catch (error: unknown) {
    const code = error instanceof Error && 'code' in error ? String(error.code) : '';
    if (code === 'ENOENT') return false;
    if (code === 'EACCES' || code === 'EPERM') {
      throw new ApiFailure({
        code: 'TARGET_NOT_WRITABLE',
        message: `无权删除 ${target}`,
        hint: '用管理员权限重试，或手工删除该文件',
        exitCode: EXIT.PERMISSION,
      });
    }
    throw new ApiFailure({
      code: 'UNINSTALL_FAILED',
      message: `删除 ${target} 失败: ${error instanceof Error ? error.message : String(error)}`,
      exitCode: EXIT.FAILURE,
    });
  }
}

/** `skillrouter uninstall`：清理安装产物（`--keep-config` 保留配置与凭据）。 */
export async function runUninstall(options: UninstallOptions): Promise<UninstallResult> {
  const env = options.env ?? currentEnv();
  const log = options.log;

  const located = await locateInstallation(env, options.dir);
  if (!located) {
    throw new ApiFailure({
      code: 'INSTALL_NOT_FOUND',
      message: options.dir
        ? `${path.resolve(env.cwd, options.dir)} 下没有 ${CONFIG_FILENAME}`
        : '未在任何已探测目录里找到 ' + CONFIG_FILENAME,
      hint: '用 --dir 指定安装目录（skillrouter doctor 会列出候选目录）',
      exitCode: EXIT.NO_AGENT,
    });
  }

  const { dir, config } = located;
  const removed: string[] = [];
  const kept: string[] = [];

  for (const name of [SKILL_FILENAME, INVOKE_FILENAME, README_FILENAME]) {
    const target = path.join(dir, name);
    if (await removeFile(target)) {
      removed.push(target);
    }
  }

  const configFile = path.join(dir, CONFIG_FILENAME);
  if (options.keepConfig === true) {
    kept.push(configFile);
    log.info(`保留配置 ${configFile}（--keep-config）`);
  } else if (await removeFile(configFile)) {
    removed.push(configFile);
  }

  let credentialRemoved = false;
  if (options.keepConfig === true) {
    log.info(`保留凭据 token_ref=${config.token_ref}（--keep-config）`);
  } else {
    credentialRemoved = await deleteStoredToken(env, config.token_ref);
    if (credentialRemoved) {
      log.info(`已删除凭据 token_ref=${config.token_ref}`);
    }
  }

  let dirRemoved = false;
  try {
    const entries = await fs.readdir(dir);
    if (entries.length === 0) {
      await fs.rmdir(dir);
      dirRemoved = true;
      removed.push(dir);
    }
  } catch {
    // 目录非空或无法删除：都不是错误
  }

  return { exitCode: EXIT.OK, dir, removed, kept, credentialRemoved, dirRemoved };
}
