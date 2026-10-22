/**
 * Upstream release detection for the upgrade flow (CONTRACT §10 `/releases/check`).
 *
 * TWO CHANNELS, ONE RULE.
 *   * `stable` — the highest semver-looking tag, via `git ls-remote --tags`.
 *   * `branch` — the head commit of `SR_ARAGON_TRACK_BRANCH`, via `git ls-remote`.
 *
 * The rule is: never guess. If git is missing, the network is down or the output
 * does not parse, this returns an error naming the exact command and its exit
 * status. A "no silent fallbacks" detector is what makes the platform's
 * `engine_releases` rows trustworthy — a candidate row that claims commit X must
 * mean some remote actually said X, not that we fell back to a cached value.
 *
 * `git ls-remote` is used rather than the GitHub API on purpose: it needs no
 * token, no rate limit, and it works against any remote — including a private
 * mirror the operator actually deploys from.
 */

import { execFile } from "node:child_process";

export interface ReleaseCheckOptions {
  repoUrl: string;
  /** Which package's tag stream to follow (`core` for `@aragon-agent/core`). */
  enginePackage: string;
  channel: string;
  trackBranch: string;
  timeoutMs: number;
}

export interface ReleaseCheckResult {
  ok: boolean;
  channel: string;
  ref: string | null;
  commit: string | null;
  detected_at: string;
  source: string;
  error?: string;
  /** Version of the selected tag, when the channel is `stable`. */
  version?: string;
  /** Monorepo package prefix of the selected tag (`core`, `cli`, or null). */
  package?: string | null;
  /** Tags examined on the stable channel, newest first. */
  candidates?: TagCandidate[];
  /** Machine-readable reason when ok is false. */
  code?: string;
}

interface GitResult {
  ok: boolean;
  stdout: string;
  stderr: string;
  command: string;
  exit_code: number | null;
  timed_out: boolean;
}

export function runGit(args: string[], timeoutMs: number): Promise<GitResult> {
  const command = `git ${args.join(" ")}`;
  return new Promise((resolve) => {
    execFile(
      "git",
      args,
      { timeout: timeoutMs, windowsHide: true, maxBuffer: 8 * 1024 * 1024 },
      (error, stdout, stderr) => {
        const err = error as (NodeJS.ErrnoException & { code?: number | string; killed?: boolean }) | null;
        const timedOut = Boolean(err?.killed);
        resolve({
          ok: error === null,
          stdout: stdout?.toString() ?? "",
          stderr: stderr?.toString() ?? "",
          command,
          exit_code: typeof err?.code === "number" ? err.code : err ? null : 0,
          timed_out: timedOut,
        });
      },
    );
  });
}

const SEMVERISH = /^(?:([A-Za-z0-9_.-]+)-)?v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$/;

export interface TagCandidate {
  ref: string;
  version: string;
  commit: string;
  /** Package prefix of a monorepo tag: `core-v0.2.34` → `core`. */
  package?: string;
}

/**
 * Parse `git ls-remote --tags` output into tags ordered newest-first by semver.
 *
 * The installed upstream uses MONOREPO tags — `core-v0.2.34`, `cli-v0.6.13` — so a
 * `^v?\d+\.\d+\.\d+$` pattern finds NOTHING and the channel would report "no
 * semver tags" against a repository full of them. The optional `<package>-` prefix
 * is parsed, kept, and used to select the engine's own tag stream.
 */
export function parseTagRefs(output: string): TagCandidate[] {
  const tags: Array<TagCandidate & { parts: number[] }> = [];
  for (const line of output.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (trimmed === "") continue;
    const match = trimmed.match(/^([0-9a-f]{7,40})\s+refs\/tags\/(.+?)(\^\{\})?$/);
    if (!match) continue;
    const [, commit, ref, peeled] = match;
    // `^{}` is the peeled annotated-tag commit — it is the real one, so prefer it
    // and skip the duplicate non-peeled line for the same tag.
    if (peeled === "^{}") {
      const existing = tags.find((entry) => entry.ref === ref);
      if (existing) {
        existing.commit = commit!;
        continue;
      }
    } else if (tags.some((entry) => entry.ref === ref)) {
      continue;
    }
    const versionMatch = ref!.match(SEMVERISH);
    if (!versionMatch) continue;
    const entry: TagCandidate & { parts: number[] } = {
      ref: ref!,
      version: `${versionMatch[2]}.${versionMatch[3]}.${versionMatch[4]}`,
      commit: commit!,
      parts: [Number(versionMatch[2]), Number(versionMatch[3]), Number(versionMatch[4])],
    };
    if (versionMatch[1]) entry.package = versionMatch[1];
    tags.push(entry);
  }

  tags.sort((a, b) => {
    for (let i = 0; i < 3; i += 1) {
      const diff = (b.parts[i] ?? 0) - (a.parts[i] ?? 0);
      if (diff !== 0) return diff;
    }
    return a.ref.localeCompare(b.ref);
  });

  return tags.map(({ ref, version, commit, package: pkg }) =>
    pkg ? { ref, version, commit, package: pkg } : { ref, version, commit },
  );
}

/**
 * Pick the tag stream that belongs to this build.
 *
 * Preference: tags for the engine's own package (`core` for `@aragon-agent/core`),
 * then plain `vX.Y.Z` tags, then whatever semver tags exist. Selection is explicit
 * so an upgrade can never silently follow the CLI's version line.
 */
export function selectEngineTag(tags: TagCandidate[], enginePackage: string): TagCandidate | null {
  if (tags.length === 0) return null;
  const shortName = enginePackage.split("/").pop() ?? enginePackage;
  const own = tags.filter((tag) => tag.package === shortName);
  if (own.length > 0) return own[0]!;
  const unprefixed = tags.filter((tag) => tag.package === undefined);
  if (unprefixed.length > 0) return unprefixed[0]!;
  return tags[0]!;
}

/** Parse `git ls-remote <url> refs/heads/<branch>` output into a commit sha. */
export function parseBranchRef(output: string): { ref: string; commit: string } | null {
  for (const line of output.split(/\r?\n/)) {
    const match = line.trim().match(/^([0-9a-f]{7,40})\s+(refs\/heads\/.+)$/);
    if (match) return { commit: match[1]!, ref: match[2]! };
  }
  return null;
}

const now = (): string => new Date().toISOString();

export async function checkReleases(options: ReleaseCheckOptions): Promise<ReleaseCheckResult> {
  const detectedAt = now();
  const channel = options.channel === "branch" ? "branch" : "stable";
  if (channel === "branch") {
    const ref = `refs/heads/${options.trackBranch}`;
    const result = await runGit(
      ["ls-remote", options.repoUrl, ref],
      options.timeoutMs,
    );
    if (!result.ok) {
      return failure(options, channel, result, `Could not read ${ref} from the remote.`);
    }
    const parsed = parseBranchRef(result.stdout);
    if (!parsed) {
      return {
        ok: false,
        channel,
        ref,
        commit: null,
        detected_at: detectedAt,
        source: result.command,
        code: "ref_not_found",
        error:
          `git reported success but did not list ${ref}. Check SR_ARAGON_TRACK_BRANCH ` +
          `(currently '${options.trackBranch}') against the remote's branches.`,
      };
    }
    return {
      ok: true,
      channel,
      ref,
      commit: parsed.commit,
      detected_at: detectedAt,
      source: result.command,
    };
  }

  const result = await runGit(["ls-remote", "--tags", options.repoUrl], options.timeoutMs);
  if (!result.ok) {
    return failure(options, channel, result, "Could not list tags on the remote.");
  }
  const candidates = parseTagRefs(result.stdout);
  if (candidates.length === 0) {
    return {
      ok: false,
      channel,
      ref: null,
      commit: null,
      detected_at: detectedAt,
      source: result.command,
      code: "no_semver_tags",
      error:
        "The remote has no semver tags in any form this build understands " +
        "(vX.Y.Z, pkg-vX.Y.Z). Switch SR_ARAGON_UPGRADE_CHANNEL to 'branch' to " +
        "track a branch head instead.",
    };
  }
  const top = selectEngineTag(candidates, options.enginePackage);
  if (!top) {
    return {
      ok: false,
      channel,
      ref: null,
      commit: null,
      detected_at: detectedAt,
      source: result.command,
      code: "no_engine_tag",
      error: `No tag belongs to ${options.enginePackage}. Tags found: ${candidates
        .slice(0, 5)
        .map((tag) => tag.ref)
        .join(", ")}.`,
    };
  }
  return {
    ok: true,
    channel,
    ref: top.ref,
    version: top.version,
    package: top.package ?? null,
    commit: top.commit,
    detected_at: detectedAt,
    source: result.command,
    candidates: candidates.slice(0, 10),
  };
}

function failure(
  options: ReleaseCheckOptions,
  channel: string,
  result: GitResult,
  summary: string,
): ReleaseCheckResult {
  const detail = result.timed_out
    ? `timed out after ${options.timeoutMs} ms`
    : `exit code ${result.exit_code ?? "unknown"}`;
  const stderr = result.stderr.trim().split(/\r?\n/).slice(0, 3).join(" ").slice(0, 400);
  return {
    ok: false,
    channel,
    ref: null,
    commit: null,
    detected_at: now(),
    source: result.command,
    code: result.timed_out ? "timeout" : "git_failed",
    error:
      `${summary} \`${result.command}\` ${detail}` +
      (stderr ? `: ${stderr}` : "") +
      ". No fallback is applied — a release row must reflect what the remote actually said.",
  };
}
