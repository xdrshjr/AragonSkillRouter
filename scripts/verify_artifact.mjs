#!/usr/bin/env node
/**
 * 从候选产物 `dist` 读出「导出符号集合」与「事件名集合」，并给出 node 侧能做的两项检查
 * （导出清单兼容 / 事件映射契约）。spec §A.3.7-1 / §A.5.3。
 *
 * 用法：
 *   node scripts/verify_artifact.mjs <dist-dir> [--expected=A,B] [--event-names=X,Y]
 *
 * 输出（stdout，单个 JSON 对象）：
 *   {dist, ok, status: 'passed'|'failed'|'blocked', symbols[], event_names[],
 *    checks: [{check, status, detail}], detail}
 *
 * 设计约束：
 * - **不 import 候选包**：`import()` 会执行产物代码，验证阶段绝不能先跑起来再说；
 *   这里只做静态解析（`.d.ts` / `.js` 的导出声明）。
 * - **不写死数量**：期望集合由调用方（网关的 `ALL_EVENT_NAMES`、适配层的 `EXPECTED_SYMBOLS`）传入。
 * - 事件名从 `.d.ts` 的判别式字面量（`type: 'x'`）里读，那是上游 `AgentEvent` 联合的真实投影。
 */

import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

const EXPORT_BLOCK = /export\s+(type\s+)?\{([^}]*)\}/gs;
const EXPORT_DECL = /export\s+(?:declare\s+)?(?:abstract\s+)?(?:class|function|const|let|var)\s+([A-Za-z_$][\w$]*)/g;
const EVENT_LITERAL = /type:\s*'([A-Za-z][\w.]*)'/g;

function parseArgs(argv) {
  const positional = [];
  const flags = {};
  for (const arg of argv.slice(2)) {
    if (arg.startsWith('--')) {
      const [key, ...rest] = arg.slice(2).split('=');
      flags[key] = rest.join('=');
    } else {
      positional.push(arg);
    }
  }
  return { dist: positional[0] ?? '', expected: splitList(flags.expected), eventNames: splitList(flags['event-names']) };
}

function splitList(raw) {
  if (typeof raw !== 'string' || raw.trim() === '') return [];
  return raw
    .split(',')
    .map((item) => item.trim())
    .filter((item) => item !== '');
}

function readText(path) {
  try {
    return readFileSync(path, 'utf8');
  } catch {
    return '';
  }
}

/**
 * 运行时导出符号（`.d.ts` 里 `export type` 的只进类型，不算运行时可用的符号）。
 *
 * 导出块里可能夹注释（上游 barrel 就有），先剥掉 `//` 行注释再按逗号切分，
 * 否则符号集合里会出现带换行的注释片段。
 */
function exportedSymbols(dist) {
  const symbols = new Set();
  for (const name of ['index.d.ts', 'index.js']) {
    const text = readText(join(dist, name));
    if (text === '') continue;
    for (const match of text.matchAll(EXPORT_BLOCK)) {
      if (match[1] !== undefined) continue;
      const body = match[2].replace(/\/\/[^\n]*/g, '');
      for (const raw of body.split(',')) {
        const part = raw.trim();
        if (part === '' || part.startsWith('type ')) continue;
        const alias = part.split(' as ').pop().trim();
        if (alias !== '') symbols.add(alias);
      }
    }
    for (const match of text.matchAll(EXPORT_DECL)) symbols.add(match[1]);
  }
  return [...symbols].sort();
}

function collectDTypes(dir, depth = 0) {
  const out = [];
  if (depth > 6) return out;
  let entries;
  try {
    entries = readdirSync(dir, { withFileTypes: true });
  } catch {
    return out;
  }
  for (const entry of entries) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...collectDTypes(path, depth + 1));
    else if (entry.name.endsWith('.d.ts')) out.push(path);
  }
  return out;
}

const AGENT_EVENT_ALIAS = /export\s+type\s+AgentEvent\s*=\s*([^;]+);/s;

/**
 * 产物声明的事件名（上游 `AgentEvent` 联合的判别式字面量）。
 *
 * 先按 `AgentEvent = A | B | …` 找到联合成员，再取每个成员的 `type: 'x'`，
 * 这样 `StreamEvent` / `ToolRegistryEvent` 的判别式不会混进来；联合体缺失时
 * 退回全局扫描（宁可多算，也不静默少算）。
 */
function declaredEventNames(dist) {
  const files = collectDTypes(dist);
  const texts = files.map((path) => readText(path));
  const names = new Set();
  for (const text of texts) {
    const union = text.match(AGENT_EVENT_ALIAS);
    if (!union) continue;
    for (const rawMember of union[1].split('|')) {
      const member = rawMember.trim();
      if (member === '') continue;
      const pattern = new RegExp(
        `(?:interface|type)\\s+${member}\\b[\\s\\S]{0,600}?type:\\s*'([A-Za-z][\\w.]*)'`,
      );
      const match = text.match(pattern);
      if (match) names.add(match[1]);
    }
  }
  if (names.size > 0) return [...names].sort();
  for (const text of texts) for (const match of text.matchAll(EVENT_LITERAL)) names.add(match[1]);
  return [...names].sort();
}

function main() {
  const { dist, expected, eventNames } = parseArgs(process.argv);
  const checks = [];

  if (dist === '' || !existsSync(dist) || !statSync(dist).isDirectory()) {
    const detail = `构建产物目录不存在：${dist === '' ? '(未提供)' : dist}`;
    process.stdout.write(
      JSON.stringify({
        dist,
        ok: false,
        status: 'blocked',
        symbols: [],
        event_names: [],
        checks: [
          { check: '导出清单兼容', status: 'blocked', detail },
          { check: '事件映射契约', status: 'blocked', detail },
        ],
        detail,
      }),
    );
    return;
  }

  const symbols = exportedSymbols(dist);
  const event_names = declaredEventNames(dist);
  const indexFile = join(dist, 'index.js');
  if (!existsSync(indexFile) || statSync(indexFile).size === 0) {
    checks.push({ check: '导出清单兼容', status: 'failed', detail: `dist/index.js 缺失或为空：${indexFile}` });
  } else if (expected.length === 0) {
    checks.push({ check: '导出清单兼容', status: 'passed', detail: `未提供期望集合；读得 ${symbols.length} 个导出` });
  } else {
    const missing = expected.filter((name) => !symbols.includes(name));
    checks.push({
      check: '导出清单兼容',
      status: missing.length === 0 ? 'passed' : 'failed',
      detail:
        missing.length === 0
          ? `导出符号集合 ⊇ 适配层期望集合（${symbols.length} 个导出，期望 ${expected.length} 个）`
          : `缺少导出符号：${missing.join(', ')}（读得 ${symbols.length} 个导出）`,
    });
  }

  if (eventNames.length === 0) {
    checks.push({
      check: '事件映射契约',
      status: event_names.length === 0 ? 'blocked' : 'passed',
      detail:
        event_names.length === 0
          ? '未提供网关事件集合，且产物未声明事件名，无法断言'
          : `未提供网关事件集合；产物声明 ${event_names.length} 个事件名`,
    });
  } else {
    const missing = eventNames.filter((name) => !event_names.includes(name));
    checks.push({
      check: '事件映射契约',
      status: missing.length === 0 ? 'passed' : 'failed',
      detail:
        missing.length === 0
          ? `候选事件名集合 ⊇ 网关集合（产物 ${event_names.length} 个事件名，网关 ${eventNames.length} 个）`
          : `缺少事件名（会被降级为 engine.notice）：${missing.join(', ')}`,
    });
  }

  const status = checks.some((check) => check.status === 'failed')
    ? 'failed'
    : checks.some((check) => check.status === 'blocked')
      ? 'blocked'
      : 'passed';
  process.stdout.write(
    JSON.stringify({
      dist,
      ok: status === 'passed',
      status,
      symbols,
      event_names,
      checks,
      detail: checks.map((check) => `${check.check}:${check.status}`).join('; '),
    }),
  );
}

main();
