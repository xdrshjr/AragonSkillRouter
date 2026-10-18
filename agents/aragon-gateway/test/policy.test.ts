/**
 * Policy tests (spec §7.12-2, §12.2, §12.4-3).
 *
 * The invariant under test: a policy file can only ever NARROW the 12-tool platform
 * allowlist. A newly added upstream tool — the exact scenario every engine upgrade
 * creates — must be unreachable, and asking for a filesystem/shell builtin must be a
 * hard error rather than a silent ignore.
 */

import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  PLATFORM_TOOL_NAMES,
  PolicyError,
  UPSTREAM_BUILTIN_TOOL_NAMES,
  checkUpstreamToolList,
  isPlatformTool,
  isUpstreamBuiltin,
  loadPolicy,
} from '../src/policy.js';

let dir: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'aragon-policy-'));
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

function writePolicy(document: unknown): string {
  const path = join(dir, 'agent-policy.json');
  writeFileSync(path, JSON.stringify(document), 'utf8');
  return path;
}

describe('defaults', () => {
  it('enables all 12 platform tools when the file is absent', () => {
    const policy = loadPolicy(join(dir, 'missing.json'));
    expect(policy.source).toBe('default');
    expect(policy.enabled).toEqual([...PLATFORM_TOOL_NAMES]);
    expect(policy.warnings.join(' ')).toContain('not found');
  });

  it('treats an empty document as "all platform tools"', () => {
    const policy = loadPolicy(writePolicy({ version: 1, mode: 'allowlist' }));
    expect(policy.source).toBe('file');
    expect(policy.enabled).toEqual([...PLATFORM_TOOL_NAMES]);
  });
});

describe('narrowing', () => {
  it('honours an explicit enabled_tools list', () => {
    const policy = loadPolicy(writePolicy({ enabled_tools: ['sr_capabilities_list', 'sr_record_finding'] }));
    expect(policy.enabled).toEqual(['sr_capabilities_list', 'sr_record_finding']);
  });

  it('honours disabled_tools', () => {
    const policy = loadPolicy(writePolicy({ disabled_tools: ['sr_fetch_source', 'sr_sample_invoke'] }));
    expect(policy.enabled).not.toContain('sr_fetch_source');
    expect(policy.enabled).not.toContain('sr_sample_invoke');
    expect(policy.disabled).toEqual(['sr_fetch_source', 'sr_sample_invoke']);
    expect(policy.enabled).toHaveLength(10);
  });

  it('warns instead of failing when everything is disabled', () => {
    const policy = loadPolicy(writePolicy({ disabled_tools: [...PLATFORM_TOOL_NAMES] }));
    expect(policy.enabled).toEqual([]);
    expect(policy.warnings.join(' ')).toContain('no tools');
  });
});

describe('rejections (permission boundary)', () => {
  it('rejects an upstream builtin tool in enabled_tools', () => {
    for (const builtin of ['bash', 'write_file', 'skill_install']) {
      expect(() => loadPolicy(writePolicy({ enabled_tools: [builtin] }))).toThrow(PolicyError);
      expect(() => loadPolicy(writePolicy({ enabled_tools: [builtin] }))).toThrow(/upstream builtin tool/);
    }
  });

  it('rejects an unknown tool name (new upstream tools are default-off)', () => {
    expect(() => loadPolicy(writePolicy({ enabled_tools: ['sr_does_not_exist'] }))).toThrow(/unknown tool/);
    expect(() => loadPolicy(writePolicy({ enabled_tools: ['brand_new_upstream_tool'] }))).toThrow(/unknown tool/);
  });

  it('rejects malformed documents', () => {
    const path = join(dir, 'broken.json');
    writeFileSync(path, '{not json', 'utf8');
    expect(() => loadPolicy(path)).toThrow(/not valid JSON/);
    expect(() => loadPolicy(writePolicy({ enabled_tools: 'sr_record_finding' }))).toThrow(/must be an array/);
    expect(() => loadPolicy(writePolicy({ mode: 'denylist' }))).toThrow(/allowlist/);
  });

  it('ignores unknown disabled_tools entries with a warning', () => {
    const policy = loadPolicy(writePolicy({ disabled_tools: ['bash'] }));
    expect(policy.enabled).toEqual([...PLATFORM_TOOL_NAMES]);
    expect(policy.warnings.join(' ')).toContain('not a platform tool');
  });
});

describe('upstream tool list drift', () => {
  it('classifies known, added and leaked names', () => {
    const result = checkUpstreamToolList(['bash', 'read_file', 'future_tool_xyz', 'sr_record_finding']);
    expect(result.known).toEqual(['bash', 'read_file']);
    expect(result.added).toEqual(['future_tool_xyz']);
    // A platform tool appearing in an upstream tool list is a naming collision, not a
    // registration: report it so the adapter review notices.
    expect(result.leaked).toEqual(['sr_record_finding']);
  });

  it('every known builtin is recognised and none is a platform tool', () => {
    for (const name of UPSTREAM_BUILTIN_TOOL_NAMES) {
      expect(isUpstreamBuiltin(name)).toBe(true);
      expect(isPlatformTool(name)).toBe(false);
    }
  });

  it('a policy naming every upstream tool cannot produce a usable policy', () => {
    expect(() => loadPolicy(writePolicy({ enabled_tools: [...UPSTREAM_BUILTIN_TOOL_NAMES] }))).toThrow(PolicyError);
  });
});
