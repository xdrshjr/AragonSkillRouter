/**
 * 用 openapi-typescript 从 `docs/api/openapi.json` 生成 `src/api.d.ts`。
 *
 * 设计要点（§8.5）：
 * - 生成器（`openapi-typescript`）或文档（`docs/api/openapi.json`，由后端构建产物提供）任一缺失时
 *   **优雅降级**：打印原因并以 0 退出，手写基线 `src/api.d.ts` 继续可用。CI 里不会因此挂掉。
 * - 只有「文档损坏」这种真错误才以 1 退出。
 * - `--check` 用于 CI：生成结果与现有 `src/api.d.ts` 有差异 → 退出 1（要求提交生成结果）。
 * - `--dry-run` 只打印将要写入的路径，不落盘。
 *
 * 用法：
 *   node --experimental-strip-types scripts/gen.ts [--check] [--dry-run]
 *   OPENAPI_PATH=/abs/path/openapi.json node --experimental-strip-types scripts/gen.ts
 */
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PACKAGE_ROOT = path.dirname(HERE);
const REPO_ROOT = path.resolve(PACKAGE_ROOT, '..', '..');

const DEFAULT_INPUT = path.join(REPO_ROOT, 'docs', 'api', 'openapi.json');
const OUTPUT = path.join(PACKAGE_ROOT, 'src', 'api.d.ts');
const HANDWRITTEN_MARKER = '手写基线版本';

interface Options {
  check: boolean;
  dryRun: boolean;
  input: string;
}

function parseArgs(argv: string[]): Options {
  const options: Options = { check: false, dryRun: false, input: process.env.OPENAPI_PATH ?? DEFAULT_INPUT };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === '--check') {
      options.check = true;
      continue;
    }
    if (arg === '--dry-run') {
      options.dryRun = true;
      continue;
    }
    if (arg === '--input') {
      const next = argv[index + 1];
      if (next === undefined) {
        throw new Error('--input 需要一个路径');
      }
      options.input = next;
      index += 1;
      continue;
    }
    if (arg === '--help' || arg === '-h') {
      process.stdout.write('用法: node --experimental-strip-types scripts/gen.ts [--check] [--dry-run] [--input <openapi.json>]\n');
      process.exit(0);
    }
    throw new Error(`未知参数: ${arg}`);
  }
  return options;
}

function log(message: string): void {
  process.stdout.write(`[contracts:gen] ${message}\n`);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

async function exists(target: string): Promise<boolean> {
  try {
    await fs.access(target);
    return true;
  } catch {
    return false;
  }
}

type Generate = (input: unknown, options?: unknown) => Promise<unknown>;

interface Generator {
  generate: Generate;
  /** openapi-typescript v7 默认返回 AST，需要 astToString 才能得到文本 */
  astToString: ((node: unknown) => string) | undefined;
}

/** 动态加载 openapi-typescript；缺失时返回 undefined（不抛错，保持可降级）。 */
async function loadGenerator(): Promise<Generator | undefined> {
  const specifier = 'openapi-typescript';
  let loaded: unknown;
  try {
    loaded = await import(specifier);
  } catch {
    return undefined;
  }
  if (!isRecord(loaded)) return undefined;

  const pick = (value: unknown, depth: number): ((input: unknown, options?: unknown) => unknown) | undefined => {
    if (typeof value === 'function') {
      return value as (input: unknown, options?: unknown) => unknown;
    }
    if (depth > 0 && isRecord(value)) {
      return pick(value.default, depth - 1);
    }
    return undefined;
  };

  const generate = pick(loaded.default, 1);
  if (generate === undefined) return undefined;
  const astToString = typeof loaded.astToString === 'function' ? (loaded.astToString as (node: unknown) => string) : undefined;
  return {
    generate: async (input: unknown, options?: unknown) => generate(input, options),
    astToString,
  };
}

async function readOpenApi(input: string): Promise<Record<string, unknown> | undefined> {
  if (!(await exists(input))) {
    log(`跳过：OpenAPI 文档不存在（${input}）`);
    log('提示：该文件是后端构建产物（CI 生成），可用 OPENAPI_PATH 或 --input 指定别的路径');
    return undefined;
  }
  let raw: string;
  try {
    raw = await fs.readFile(input, 'utf8');
  } catch (error: unknown) {
    throw new Error(`无法读取 ${input}: ${error instanceof Error ? error.message : String(error)}`);
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (error: unknown) {
    throw new Error(`${input} 不是合法 JSON: ${error instanceof Error ? error.message : String(error)}`);
  }
  if (!isRecord(parsed)) {
    throw new Error(`${input} 顶层必须是对象`);
  }
  const openapi = parsed.openapi ?? parsed.swagger;
  if (typeof openapi !== 'string' || !isRecord(parsed.paths)) {
    throw new Error(`${input} 缺少 openapi/swagger 版本号或 paths`);
  }
  return parsed;
}

function banner(input: string, generator: string): string {
  return [
    '/**',
    ' * Skill Router 对外统一接口（`/api/v1`）的类型契约。',
    ' *',
    ' * 本文件由 `packages/contracts/scripts/gen.ts` 生成，请勿手工编辑。',
    ` * 来源: ${path.relative(REPO_ROOT, input).replace(/\\/g, '/')}`,
    ` * 生成器: ${generator}`,
    ' * 重新生成: `npm run gen --workspace @skillrouter/contracts`（或 `npm --prefix packages/contracts run gen`）',
    ' * 校验: `npm run gen:check --workspace @skillrouter/contracts`（CI 要求无 diff）',
    ' */',
    '',
  ].join('\n');
}

async function main(): Promise<number> {
  const options = parseArgs(process.argv.slice(2));

  const document = await readOpenApi(options.input);
  if (document === undefined) {
    return 0; // 优雅降级
  }

  const generate = await loadGenerator();
  if (generate === undefined) {
    log('跳过：未安装 openapi-typescript（生成器不可用）');
    log('提示：npm install --include=dev（本机 NODE_ENV=production 时会跳过 devDependencies）');
    log('手写基线 src/api.d.ts 继续可用；装上生成器后重跑本脚本即可覆盖');
    return 0;
  }

  let generated: string;
  try {
    const raw: unknown = await generate.generate(document, { alphabetical: true, exportType: true });
    if (typeof raw === 'string') {
      generated = raw;
    } else if (generate.astToString !== undefined) {
      generated = generate.astToString(raw);
    } else {
      throw new Error('无法把生成结果序列化为文本（openapi-typescript 版本不兼容）');
    }
  } catch (error: unknown) {
    throw new Error(`openapi-typescript 生成失败: ${error instanceof Error ? error.message : String(error)}`);
  }
  const output = `${banner(options.input, 'openapi-typescript')}${generated.trim()}\n`;

  if (options.dryRun) {
    log(`--dry-run：将写入 ${output}（${output.length} 字节）`);
    return 0;
  }

  const current = (await exists(OUTPUT)) ? await fs.readFile(OUTPUT, 'utf8') : '';
  if (options.check) {
    if (current === output) {
      log(`检查通过：${path.relative(REPO_ROOT, OUTPUT).replace(/\\/g, '/')} 与生成结果一致`);
      return 0;
    }
    log('检查失败：src/api.d.ts 与 OpenAPI 生成结果不一致');
    log('请运行 `npm run gen` 并提交生成结果');
    if (current.includes(HANDWRITTEN_MARKER)) {
      log('（当前是手写基线版本，说明生成结果尚未提交）');
    }
    return 1;
  }

  if (current === output) {
    log('无变化：src/api.d.ts 已是最新');
    return 0;
  }
  await fs.writeFile(OUTPUT, output, 'utf8');
  log(`已写入 ${path.relative(REPO_ROOT, OUTPUT).replace(/\\/g, '/')}（${output.length} 字节）`);
  return 0;
}

main()
  .then((code) => {
    process.exitCode = code;
  })
  .catch((error: unknown) => {
    process.stderr.write(`[contracts:gen] 失败: ${error instanceof Error ? error.message : String(error)}\n`);
    process.exitCode = 1;
  });
