/**
 * Tool policy: the gateway's *only* permission boundary (spec §7.12-2, §12.2, §12.3).
 *
 * The upstream SDK takes a `tools: AgentTool[]` array and that array IS the tool
 * surface the model can see and plan with (aragonagent-upstream.md §3.1-1). So the
 * policy file can only ever *narrow* a hard-coded allowlist of the 12 platform
 * tools; it can never widen it:
 *
 * - Anything not in `PLATFORM_TOOL_NAMES` is rejected, so a *new* upstream tool
 *   (or a typo, or a hostile edit of `var/agent-policy.json`) is default-off and
 *   cannot become reachable by upgrading the engine (spec §12.4-3).
 * - Upstream builtin tools that would reach the filesystem or a shell (`bash`,
 *   `write_file`, …) are named explicitly in `UPSTREAM_BUILTIN_TOOL_NAMES` and are
 *   rejected with a security-flavoured error even if they somehow appear in a
 *   policy document.
 */

import { readFileSync } from 'node:fs';
import { isAbsolute, resolve } from 'node:path';

/** The 12 platform tools — spec §12.3. Frozen: adding one is a spec change. */
export const PLATFORM_TOOL_NAMES = [
  'sr_capabilities_list',
  'sr_capability_get',
  'sr_fetch_source',
  'sr_parse_document',
  'sr_propose_capability_draft',
  'sr_update_capability_draft',
  'sr_test_capability',
  'sr_sample_invoke',
  'sr_create_skill_draft',
  'sr_validate_skill',
  'sr_record_finding',
  'sr_request_approval',
] as const;

export type PlatformToolName = (typeof PLATFORM_TOOL_NAMES)[number];

/**
 * Upstream builtin tools observed in `@aragon-agent/core` 0.2.34 / CLI 0.6.22.
 *
 * `HOST_TOOL_NAMES` in the conformance sense (spec §12.4-3). The gateway never
 * registers any of these — the platform's SKILL import pipeline replaces
 * `skill_install`/`skill_create`, and filesystem/shell access is explicitly
 * out of scope (spec §12.2 "受权限限制（禁用）").
 */
export const UPSTREAM_BUILTIN_TOOL_NAMES = [
  'bash',
  'read_file',
  'write_file',
  'edit_file',
  'list_dir',
  'glob',
  'grep',
  'skill',
  'skill_find',
  'skill_install',
  'skill_create',
  'web_fetch',
  'web_search',
  'todo_write',
  'task',
  'team',
  'fast_review',
] as const;

const PLATFORM_SET: ReadonlySet<string> = new Set(PLATFORM_TOOL_NAMES);
const UPSTREAM_SET: ReadonlySet<string> = new Set(UPSTREAM_BUILTIN_TOOL_NAMES);

export function isPlatformTool(name: string): name is PlatformToolName {
  return PLATFORM_SET.has(name);
}

export function isUpstreamBuiltin(name: string): boolean {
  return UPSTREAM_SET.has(name);
}

export class PolicyError extends Error {
  readonly problems: string[];
  constructor(problems: string[]) {
    super(`invalid tool policy:\n- ${problems.join('\n- ')}`);
    this.name = 'PolicyError';
    this.problems = problems;
  }
}

export interface ToolPolicy {
  /** Tools the engine is allowed to register, in §12.3 order. */
  enabled: string[];
  /** Platform tools explicitly turned off by the policy document. */
  disabled: string[];
  /** Where the policy came from. */
  source: 'default' | 'file';
  /** Absolute-ish path that was consulted. */
  path: string;
  /** Non-fatal observations (unknown disabled entries, empty result, …). */
  warnings: string[];
}

interface PolicyDocument {
  version?: number;
  mode?: string;
  enabled_tools?: unknown;
  disabled_tools?: unknown;
}

/**
 * Read and validate `var/agent-policy.json`.
 *
 * A missing file means "platform defaults": all 12 platform tools enabled. That is
 * the documented behaviour of a freshly bootstrapped install (the settings page
 * writes the file on first save), and it still cannot expose an upstream tool.
 */
export function loadPolicy(policyPath: string, cwd: string = process.cwd()): ToolPolicy {
  const full = isAbsolute(policyPath) ? policyPath : resolve(cwd, policyPath);
  const warnings: string[] = [];

  let raw: string;
  try {
    raw = readFileSync(full, 'utf8');
  } catch (err) {
    const code = (err as NodeJS.ErrnoException).code;
    if (code === 'ENOENT') {
      return {
        enabled: [...PLATFORM_TOOL_NAMES],
        disabled: [],
        source: 'default',
        path: full,
        warnings: ['policy file not found; defaulting to all 12 platform tools'],
      };
    }
    throw new PolicyError([`cannot read policy file ${full}: ${(err as Error).message}`]);
  }

  let doc: PolicyDocument;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new Error('policy document must be a JSON object');
    }
    doc = parsed as PolicyDocument;
  } catch (err) {
    throw new PolicyError([`${full} is not valid JSON: ${(err as Error).message}`]);
  }

  if (doc.mode !== undefined && doc.mode !== 'allowlist') {
    throw new PolicyError([`policy mode must be "allowlist" (got ${JSON.stringify(doc.mode)})`]);
  }

  const problems: string[] = [];

  const readNameList = (value: unknown, field: string): string[] => {
    if (value === undefined) return [];
    if (!Array.isArray(value)) {
      problems.push(`${field} must be an array of strings`);
      return [];
    }
    const out: string[] = [];
    for (const entry of value) {
      if (typeof entry !== 'string' || entry.trim() === '') {
        problems.push(`${field} contains a non-string entry: ${JSON.stringify(entry)}`);
        continue;
      }
      out.push(entry.trim());
    }
    return out;
  };

  const requested = readNameList(doc.enabled_tools, 'enabled_tools');
  const disabled = readNameList(doc.disabled_tools, 'disabled_tools');

  for (const name of requested) {
    if (isUpstreamBuiltin(name)) {
      problems.push(
        `enabled_tools contains upstream builtin tool "${name}"; the platform never registers ` +
          'filesystem/shell/skill builtins (spec §12.2, §7.12-2)',
      );
    } else if (!isPlatformTool(name)) {
      problems.push(
        `enabled_tools contains unknown tool "${name}"; only the 12 sr_* platform tools are ` +
          'registrable — new upstream tools are default-off by design (spec §12.4-3)',
      );
    }
  }
  for (const name of disabled) {
    if (isUpstreamBuiltin(name) || !isPlatformTool(name)) {
      warnings.push(`disabled_tools entry "${name}" is not a platform tool; ignored`);
    }
  }

  if (problems.length > 0) throw new PolicyError(problems);

  const disabledSet = new Set(disabled.filter((n): n is PlatformToolName => isPlatformTool(n)));
  const requestedSet = new Set(requested.filter((n): n is PlatformToolName => isPlatformTool(n)));

  // An empty/absent enabled_tools means "everything not explicitly disabled",
  // which keeps the file minimal for the common case (writes from the settings
  // page list only the switches the admin touched).
  const enabled = PLATFORM_TOOL_NAMES.filter((name) =>
    requestedSet.size === 0 ? !disabledSet.has(name) : requestedSet.has(name) && !disabledSet.has(name),
  );

  if (enabled.length === 0) {
    warnings.push('policy disables every platform tool; the agent will run with no tools at all');
  }

  return { enabled: [...enabled], disabled: [...disabledSet], source: 'file', path: full, warnings };
}

/**
 * Report how an upstream tool-list change interacts with the platform allowlist.
 *
 * Used by the conformance/upgrade suite (spec §12.4-3): a new upstream tool must
 * NOT be automatically registrable, so `leaked` must always be empty.
 */
export function checkUpstreamToolList(upstreamNames: readonly string[]): {
  added: string[];
  known: string[];
  leaked: string[];
} {
  // A platform tool name appearing in an upstream tool list is reported as `leaked`
  // (a naming collision worth reviewing), never as a registrable tool.
  const added = upstreamNames.filter((n) => !UPSTREAM_SET.has(n) && !PLATFORM_SET.has(n));
  const known = upstreamNames.filter((n) => UPSTREAM_SET.has(n));
  const leaked = upstreamNames.filter((n) => PLATFORM_SET.has(n));
  return { added, known, leaked };
}
