/**
 * The commands the Overview page tells people to run.
 *
 * Kept as pure functions so the exact strings the UI renders can be executed by a
 * verification script — a guide whose command does not run is worse than no guide,
 * and copying a command out of a React component to test it is how drift starts.
 *
 * Rules encoded here, each learned by running the CLI:
 *  * the CLI is distributed locally (it is not on the public npm registry), so it is
 *    invoked with `node "<absolute path>"` and never `npx skill-router-connect`;
 *  * `--user` on install resolves the base dir to `~/.codex` (or `~/.claude`), and
 *    `verify` resolves its stored config from the same base dir — so verify needs the
 *    same `--user` flag, or it looks in the current directory and reports
 *    "No platform URL";
 *  * the token is passed through an environment variable: the command stays identical
 *    for everybody, and the secret is not repeated in every invocation.
 */

export type ConnectTarget = 'codex-skills' | 'claude-code' | 'codex' | 'generic';

export interface ConnectContext {
  /** Platform base URL, from `/api/meta` → `public_base_url`. */
  baseUrl: string;
  /** Absolute path of `skill-router-connect.js` from `/api/meta` → `installer.cli_path`. */
  cliPath: string;
  target?: ConnectTarget;
  /** User scope (`~/.codex`, `~/.claude`) when true, current directory when false. */
  userScope?: boolean;
}

export interface ConnectCommand {
  id: string;
  title: string;
  why: string;
  code: string;
  language: string;
}

/** Absolute path quoted for PowerShell, where programs live in `Program Files`. */
export function quotePath(value: string): string {
  return `"${value.replace(/"/g, '`"')}"`;
}

/** Base URL without a trailing slash, so `${base}/api/v1` never doubles it. */
export function normaliseBaseUrl(value: string): string {
  return value.replace(/\/+$/, '');
}

/**
 * The three commands, in the order they must run.
 *
 * The token step is an assignment, not a CLI call: `skill-router-connect install`
 * has no interactive prompt by design, and storing the token in the connection config
 * is its job — the variable only carries it from one line to the next.
 */
export function connectCommands(context: ConnectContext): ConnectCommand[] {
  const baseUrl = normaliseBaseUrl(context.baseUrl);
  const cli = quotePath(context.cliPath);
  const target = context.target ?? 'codex-skills';
  const scope = context.userScope === false ? '' : ' --user';
  return [
    {
      id: 'token',
      title: '1. Put your agent token in a variable',
      why: 'Create the token under Access → Tokens; it is shown once. Nothing is saved by this line.',
      language: 'powershell',
      code: '$env:SR_TOKEN = "sr_live_…paste the token here…"',
    },
    {
      id: 'install',
      title: '2. Install the connection SKILL into your agent',
      why:
        target === 'codex-skills'
          ? 'Writes ~/.codex/skills/<skill-name>/SKILL.md plus ~/.codex/.skill-router.json (your URL and token, not readable by other users).'
          : 'Writes the skill file for the target and .skill-router.json next to it.',
      language: 'powershell',
      code: `node ${cli} install --url ${baseUrl} --token $env:SR_TOKEN --target ${target}${scope}`,
    },
    {
      id: 'verify',
      title: '3. Check the connection',
      why: 'Calls the platform, prints who you are and which capabilities the token can use. Exit code 0 means it works.',
      language: 'powershell',
      code: `node ${cli} verify${scope}`,
    },
  ];
}

/** A single call, so people can prove the token works without their agent. */
export function firstCallCommand(context: ConnectContext, capabilitySlug: string, input: Record<string, unknown>): string {
  const baseUrl = normaliseBaseUrl(context.baseUrl);
  // One line on purpose: PowerShell continuations use the backtick, which is also the
  // string delimiter in TypeScript and the character people most often lose when
  // copying. A single line cannot be broken by a stray keystroke.
  const body = JSON.stringify({ capability: capabilitySlug, input });
  return `Invoke-RestMethod -Method POST -Uri "${baseUrl}/api/v1/invocations" -Headers @{ Authorization = "Bearer $env:SR_TOKEN" } -ContentType "application/json" -Body '${body}'`;
}

/** Human label for a target, used in the picker. */
export const TARGET_LABELS: Record<ConnectTarget, string> = {
  'codex-skills': 'Codex (skills directory)',
  'claude-code': 'Claude Code',
  codex: 'Codex / AGENTS.md only',
  generic: 'Any agent (plain SKILL.md)',
};
