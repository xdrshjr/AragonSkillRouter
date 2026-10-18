/**
 * Isolated build + artifact verification **inside the gateway container** (spec §A.3.7-0).
 *
 * Why here: `deploy/Dockerfile.api` ships no node/npm/git, so a Python-side build would
 * make *every* upgrade `blocked` and `policy=auto` would never take effect. The gateway
 * image is `node:22-slim` (+ `git`) and shares the `/data` volume with api/worker, so it
 * can build into `<data>/engine-builds/<version>-<provenance>/runtime` and let Python
 * orchestrate. This module owns only the two commands the gateway exposes:
 *
 *   POST /internal/admin/build            → clone / npm ci / build / prune
 *   POST /internal/admin/verify-artifact → `scripts/verify_artifact.mjs` (exports + events)
 *
 * Both are path-guarded: any path outside `/data` is rejected by `main.ts` before it gets
 * here, and `assertInsideRoot()` re-checks it at the point of use.
 */

import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { copyFile, cp, rm } from 'node:fs/promises';
import { join, resolve, sep } from 'node:path';

import { ALL_EVENT_NAMES } from './engine.js';
import type { BuildRequest, BuildResult, BuildStep, Logger, VerifyResult } from './types.js';

/** Wall clock ceiling per command (ms) — matches `BUILD_TIMEOUT_S` on the Python side. */
export const BUILD_TIMEOUT_MS = 15 * 60 * 1000;
const TAIL_LINES = 200;
const UPSTREAM_REPO = 'https://github.com/xdrshjr/AragonAgent';

export interface ArtifactDeps {
  /** `<data>/engine-builds` (shared volume with the api/worker). */
  buildsDir: string;
  /** Root every path must live under (`/data`). */
  dataRoot: string;
  /** Absolute path of `scripts/verify_artifact.mjs` inside the image. */
  verifyScript: string;
  logger: Logger;
  /** Injection seam for tests. */
  run?: Runner;
}

export type Runner = (cmd: string, args: string[], cwd: string, timeoutMs?: number) => Promise<{ code: number; output: string }>;

/** True when `candidate` is `root` or lives under it (after `resolve()`). */
export function isInsideRoot(root: string, candidate: string): boolean {
  const normalizedRoot = resolve(root);
  const normalized = resolve(candidate);
  return normalized === normalizedRoot || normalized.startsWith(`${normalizedRoot}${sep}`);
}

export function assertInsideRoot(root: string, candidate: string): void {
  if (!isInsideRoot(root, candidate)) {
    throw new PathEscapeError(`path ${candidate} is outside ${root}`);
  }
}

export class PathEscapeError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'PathEscapeError';
  }
}

/** Default runner: no shell, captured output, hard timeout. */
export const spawnRunner: Runner = (cmd, args, cwd, timeoutMs = BUILD_TIMEOUT_MS) =>
  new Promise((resolvePromise) => {
    let output = '';
    const child = spawn(cmd, args, { cwd, shell: false, windowsHide: true });
    const timer = setTimeout(() => {
      child.kill();
      output += `\n命令超时（${timeoutMs}ms）：${cmd} ${args.join(' ')}`;
    }, timeoutMs);
    timer.unref?.();
    child.stdout?.on('data', (chunk: Buffer) => {
      output += chunk.toString('utf8');
    });
    child.stderr?.on('data', (chunk: Buffer) => {
      output += chunk.toString('utf8');
    });
    child.once('error', (err: Error) => {
      clearTimeout(timer);
      resolvePromise({ code: 127, output: `${output}\n命令不可用：${cmd}（${err.message}）` });
    });
    child.once('close', (code) => {
      clearTimeout(timer);
      resolvePromise({ code: code ?? 1, output });
    });
  });

function tail(output: string): string {
  const lines = output.split('\n');
  return lines.slice(-TAIL_LINES).join('\n').slice(0, 8000);
}

function sha256(path: string): string | null {
  try {
    return createHash('sha256').update(readFileSync(path)).digest('hex');
  } catch {
    return null;
  }
}

/**
 * Build a candidate artifact.
 *
 * `blocked` (not `failed`) is returned when the toolchain itself is missing — that is an
 * environment problem, and marking the candidate as failed would be a lie.
 */
export async function buildArtifact(request: BuildRequest, deps: ArtifactDeps): Promise<BuildResult> {
  const run = deps.run ?? spawnRunner;
  const version = request.version;
  const provenance = request.provenance ?? 'tagged';
  const steps: BuildStep[] = [];
  const target = join(deps.buildsDir, `${version}-${provenance}`);
  assertInsideRoot(deps.dataRoot, target);

  const blocked = (stage: string, detail: string): BuildResult => ({
    ok: false,
    status: 'blocked',
    stage,
    detail,
    steps,
  });
  const failed = (stage: string, detail: string): BuildResult => ({
    ok: false,
    status: 'failed',
    stage,
    detail,
    steps,
  });

  try {
    rmSync(target, { recursive: true, force: true });
  } catch {
    /* best effort */
  }
  mkdirSync(target, { recursive: true });

  let workdir = target;
  if (provenance === 'npm-only') {
    const packed = await run('npm', ['pack', `@aragon-agent/core@${version}`], target);
    steps.push({ name: 'npm_pack', code: packed.code, detail: tail(packed.output) });
    if (packed.code === 127) return blocked('npm_pack', `缺少命令 npm：${tail(packed.output)}`);
    if (packed.code !== 0) return failed('npm_pack', tail(packed.output));
    // npm 包内没有 `packages/core` 工作区与构建脚本，`npm ci && npm run build -w packages/core`
    // 在 tarball 目录里必然失败（不是候选的错）。按 §A.3.7-1「不能验证就说不能验证」处理成
    // blocked，并指明可执行的下一步：走 tagged（源码通道）构建。
    return blocked(
      'npm_only_layout',
      '@aragon-agent/core 的 npm 包不含 packages/core 工作区与构建脚本，无法完成隔离构建；' +
        `请改用 tagged（源码）通道构建 ${version}，或人工确认 tarball 内容后手动安装`,
    );
  }

  const ref = request.commit_sha ?? `core-v${version}`;
  const cloned = await run(
    'git',
    ['clone', '--filter=blob:none', '--depth', '1', '--branch', ref, UPSTREAM_REPO, 'src'],
    target,
  );
  steps.push({ name: 'clone', code: cloned.code, detail: tail(cloned.output) });
  if (cloned.code === 127) return blocked('clone', `缺少命令 git：${tail(cloned.output)}`);
  if (cloned.code !== 0) return failed('clone', tail(cloned.output));
  workdir = join(target, 'src');

  const installed = await run('npm', ['ci', '--ignore-scripts'], workdir);
  steps.push({ name: 'npm_ci_build', code: installed.code, detail: tail(installed.output) });
  if (installed.code === 127) return blocked('npm_ci_build', `缺少命令 npm：${tail(installed.output)}`);
  if (installed.code !== 0) return failed('npm_ci_build', tail(installed.output));

  const built = await run('npm', ['run', 'build', '-w', 'packages/core'], workdir);
  steps.push({ name: 'build_core', code: built.code, detail: tail(built.output) });
  if (built.code !== 0) return failed('build_core', tail(built.output));

  const dist = join(workdir, 'packages', 'core', 'dist');
  if (!existsSync(dist) || !statSync(dist).isDirectory()) {
    return failed('build_core', `构建完成后未找到 packages/core/dist（${dist}）`);
  }

  const pruned = join(target, 'runtime');
  mkdirSync(pruned, { recursive: true });
  for (const name of ['package.json', 'package-lock.json']) {
    const source = join(workdir, name);
    if (existsSync(source)) await copyFile(source, join(pruned, name));
  }
  const prunedInstall = await run('npm', ['ci', '--omit=dev', '--ignore-scripts'], pruned);
  steps.push({ name: 'prune_runtime', code: prunedInstall.code, detail: tail(prunedInstall.output) });
  if (prunedInstall.code === 127) return blocked('prune_runtime', `缺少命令 npm：${tail(prunedInstall.output)}`);
  if (prunedInstall.code !== 0) return failed('prune_runtime', tail(prunedInstall.output));

  const targetDist = join(pruned, 'packages', 'core', 'dist');
  mkdirSync(join(pruned, 'packages', 'core'), { recursive: true });
  await cp(dist, targetDist, { recursive: true, force: true, filter: (source) => !source.endsWith('node_modules') });

  const manifest: Record<string, unknown> = {
    version,
    commit: request.commit_sha ?? null,
    node: process.version,
    deps_lock_sha256: sha256(join(workdir, 'package-lock.json')),
    pruned_lock_sha256: sha256(join(pruned, 'package-lock.json')),
    built_at: new Date().toISOString(),
    adapter_version: '1',
    provenance,
    built_by: 'gateway',
  };
  writeFileSync(join(pruned, 'build-manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`, 'utf8');
  deps.logger.info('artifact.built', { version, provenance, artifact_dir: pruned });
  return {
    ok: true,
    status: 'built',
    stage: 'built',
    detail: `构建完成：${pruned}`,
    artifact_dir: pruned,
    manifest,
    steps,
  };
}

/**
 * Run the node-side artifact checks.
 *
 * The authoritative event contract is `ALL_EVENT_NAMES` (exported by `engine.ts`): the
 * candidate's declared upstream event names must cover it, otherwise the platform would
 * silently lose events into `engine.notice`. Counts are never hardcoded.
 */
export async function verifyArtifact(
  artifactDir: string,
  expectedSymbols: readonly string[],
  deps: ArtifactDeps,
): Promise<VerifyResult> {
  const run = deps.run ?? spawnRunner;
  const dist = join(artifactDir, 'packages', 'core', 'dist');
  assertInsideRoot(deps.dataRoot, dist);
  if (!existsSync(dist)) {
    const detail = `构建产物缺少 packages/core/dist：${dist}`;
    return {
      status: 'failed',
      checks: [
        { check: '导出清单兼容', status: 'failed', detail },
        { check: '事件映射契约', status: 'failed', detail },
      ],
      detail,
    };
  }
  if (!existsSync(deps.verifyScript)) {
    const detail = `网关缺少校验脚本：${deps.verifyScript}（镜像需包含 scripts/verify_artifact.mjs）`;
    deps.logger.error('artifact.verify_script_missing', { path: deps.verifyScript });
    return {
      status: 'blocked',
      checks: [
        { check: '导出清单兼容', status: 'blocked', detail },
        { check: '事件映射契约', status: 'blocked', detail },
      ],
      detail,
    };
  }

  const args = [
    deps.verifyScript,
    dist,
    `--expected=${expectedSymbols.join(',')}`,
    `--event-names=${ALL_EVENT_NAMES.join(',')}`,
  ];
  const result = await run(process.execPath, args, deps.dataRoot, 5 * 60 * 1000);
  if (result.code === 127) {
    const detail = `无法执行 node 校验脚本：${tail(result.output)}`;
    return {
      status: 'blocked',
      checks: [
        { check: '导出清单兼容', status: 'blocked', detail },
        { check: '事件映射契约', status: 'blocked', detail },
      ],
      detail,
    };
  }
  let payload: VerifyResult | null = null;
  for (const line of result.output.split('\n').reverse()) {
    const trimmed = line.trim();
    if (!trimmed.startsWith('{')) continue;
    try {
      payload = JSON.parse(trimmed) as VerifyResult;
      break;
    } catch {
      /* keep scanning */
    }
  }
  if (payload === null) {
    const detail = `校验脚本输出无法解析（code=${result.code}）：${tail(result.output)}`;
    return {
      status: 'blocked',
      checks: [
        { check: '导出清单兼容', status: 'blocked', detail },
        { check: '事件映射契约', status: 'blocked', detail },
      ],
      detail,
    };
  }
  deps.logger.info('artifact.verified', { status: payload.status, checks: payload.checks.length });
  return payload;
}

/** Copy a helper so callers do not need `node:fs` directly in tests. */
export function readJson(path: string): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(readFileSync(path, 'utf8')) as unknown;
    return parsed !== null && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

/** Best-effort cleanup used when a build fails halfway. */
export async function discard(path: string): Promise<void> {
  await rm(path, { recursive: true, force: true }).catch(() => undefined);
}

/** Resolve `scripts/verify_artifact.mjs` for the current layout (container + repo checkout). */
export function findVerifyScript(candidates: readonly string[]): string {
  for (const candidate of candidates) {
    if (existsSync(candidate)) return candidate;
  }
  return candidates[0] ?? '';
}
