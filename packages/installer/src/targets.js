/**
 * Target layout.
 *
 * Only targets that were actually exercised (see test/) are listed, and each one
 * states *what kind of proof* exists for it:
 *
 *   codex-skills `skills/<skill-name>/SKILL.md` (user scope: `~/.codex/skills/...`)
 *                Codex CLI / ChatGPT Codex reads skills from a directory of
 *                `<name>/SKILL.md` files with `name`/`description` frontmatter —
 *                the same shape Claude Code uses. This is the target to use for a
 *                Codex build that ships a `skills/` directory.
 *   claude-code  `.claude/skills/skill-router/SKILL.md`
 *   codex        `AGENTS.md` with a managed block
 *                (older Codex builds and plain instruction-file agents: the SKILL
 *                 lives inside a delimited block, everything around it preserved)
 *   generic      `SKILL.md` next to `.skill-router.json`
 *
 * The SKILL *content* is verified end to end against a real platform; whether a
 * given vendor binary loads it is a property of that binary, and the notes for each
 * target say which layout it expects.
 */

import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";

import { UsageError } from "./errors.js";

export const SKILL_DIR_NAME = "skill-router";
export const MARKER = "<!-- managed by skill-router-connect -->";
export const BLOCK_BEGIN = "<!-- skill-router:begin -->";
export const BLOCK_END = "<!-- skill-router:end -->";

/** Placeholder replaced with the skill name (`skill-router-<agent>`) at write time. */
export const SKILL_NAME_TOKEN = "{skillName}";

/**
 * @typedef {object} TargetFile
 * @property {string} relPath        path relative to the base dir
 * @property {"replace"|"managed-block"} style
 */

/**
 * @typedef {object} TargetSpec
 * @property {string} id
 * @property {string} label
 * @property {string} verified       what was proven, in one line
 * @property {TargetFile[]} files
 * @property {string[]} notes
 * @property {string} [userSubdir]   where `--user` points when it is not the home dir
 */

/** @type {Record<string, TargetSpec>} */
export const TARGETS = {
  "codex-skills": {
    id: "codex-skills",
    label: "Codex CLI / Codex app (skills directory)",
    verified: "layout, generated SKILL.md and re-install idempotency asserted by test/install.test.js; content verified against a live platform",
    files: [{ relPath: `skills/${SKILL_NAME_TOKEN}/SKILL.md`, style: "replace" }],
    userSubdir: ".codex",
    notes: [
      "--user writes ~/.codex/skills/<name>/SKILL.md, which is where Codex reads user-level skills.",
      "--project writes skills/<name>/SKILL.md under the current directory.",
      "Codex has no tool allowlist to write; the skill asks it to use its shell tool for curl.",
    ],
  },
  "claude-code": {
    id: "claude-code",
    label: "Claude Code",
    verified: "layout + generated SKILL.md asserted by test/install.test.js against a mock server",
    files: [{ relPath: `.claude/skills/${SKILL_NAME_TOKEN}/SKILL.md`, style: "replace" }],
    userSubdir: ".claude",
    notes: ["Skill directories under .claude/skills/<name>/ are project-scoped; --user writes to ~/.claude/skills/<name>/."],
  },
  codex: {
    id: "codex",
    label: "AGENTS.md instruction file",
    verified: "AGENTS.md managed block written, merged and removed by test/install.test.js",
    files: [{ relPath: "AGENTS.md", style: "managed-block" }],
    notes: [
      "For agents that read AGENTS.md instruction files: the SKILL is written as a delimited block.",
      "Text outside the markers is never touched, and `uninstall` removes only the block.",
      "Codex builds that ship a skills/ directory should use --target codex-skills instead.",
    ],
  },
  generic: {
    id: "generic",
    label: "Generic agent",
    verified: "plain SKILL.md + .skill-router.json written and removed by test/install.test.js",
    files: [{ relPath: "SKILL.md", style: "replace" }],
    notes: ["Use this when the agent can load a markdown file and read a JSON config of your choosing."],
  },
};

export const TARGET_IDS = Object.keys(TARGETS);

/**
 * @param {string} id
 * @param {string[]} [known]
 * @returns {TargetSpec}
 */
export function resolveTarget(id, known = TARGET_IDS) {
  const spec = TARGETS[id];
  if (!spec) {
    throw new UsageError(`Unknown target ${JSON.stringify(id)}. Supported targets: ${known.join(", ")}.`, {
      actions: [`Use --target ${known.join(" | ")}`],
    });
  }
  return spec;
}

/**
 * Expand `{skillName}` in a target path.
 * @param {string} relPath
 * @param {string} skillName
 * @returns {string}
 */
export function expandRelPath(relPath, skillName) {
  return relPath.split(SKILL_NAME_TOKEN).join(skillName);
}

/**
 * @param {TargetSpec} spec
 * @param {string} [skillName]
 * @returns {string[]}
 */
export function targetRelPaths(spec, skillName = SKILL_DIR_NAME) {
  return spec.files.map((file) => expandRelPath(file.relPath, skillName));
}

/**
 * Detect which agent is installed on this machine, so `install-self` needs no flags.
 *
 * An agent asked to "install this skill for yourself" cannot be expected to know the
 * installer's target vocabulary; it runs one command and reads the result. Checking for
 * the two known skill directories is enough — anything else falls back to a plain
 * SKILL.md in the working directory, which the agent can move itself.
 *
 * @param {(candidate: string) => boolean} [exists]
 * @returns {{id: string, baseDir: string, reason: string}}
 */
export function detectInstalledAgent(exists = existsSync) {
  const home = homedir();
  const candidates = [
    { id: "codex-skills", dir: path.join(home, ".codex"), label: "Codex" },
    { id: "claude-code", dir: path.join(home, ".claude"), label: "Claude Code" },
  ];
  for (const candidate of candidates) {
    if (exists(candidate.dir)) {
      return {
        id: candidate.id,
        baseDir: candidate.dir,
        reason: `found ${candidate.dir} (${candidate.label})`,
      };
    }
  }
  return {
    id: "generic",
    baseDir: process.cwd(),
    reason: "no known agent directory found; SKILL.md goes to the current directory",
  };
}

/**
 * Read one target file, or null when it does not exist.
 * @param {string} baseDir
 * @param {string} relPath
 * @returns {string | null}
 */
export function readArtifact(baseDir, relPath) {
  const full = path.join(baseDir, relPath);
  if (!existsSync(full)) return null;
  return readFileSync(full, "utf8");
}

/**
 * Merge our managed block into an existing document, leaving everything outside
 * the markers byte-identical.
 * @param {string | null} existing
 * @param {string} block   the block *without* markers
 * @returns {string}
 */
export function mergeManagedBlock(existing, block) {
  const wrapped = `${BLOCK_BEGIN}\n${block.trim()}\n${BLOCK_END}`;
  if (!existing) return `${wrapped}\n`;
  const begin = existing.indexOf(BLOCK_BEGIN);
  const end = existing.indexOf(BLOCK_END);
  if (begin !== -1 && end !== -1 && end > begin) {
    const before = existing.slice(0, begin);
    const after = existing.slice(end + BLOCK_END.length);
    return `${before}${wrapped}${after}`;
  }
  const trimmed = existing.replace(/\s+$/, "");
  return `${trimmed}\n\n${wrapped}\n`;
}

/**
 * Remove our managed block. Returns `{ content, found }`; `content` is `null` when
 * nothing but whitespace would remain, which tells the caller to delete the file.
 * @param {string} existing
 * @returns {{content: string | null, found: boolean}}
 */
export function stripManagedBlock(existing) {
  const begin = existing.indexOf(BLOCK_BEGIN);
  const end = existing.indexOf(BLOCK_END);
  if (begin === -1 || end === -1 || end <= begin) return { content: existing, found: false };
  let before = existing.slice(0, begin).replace(/\s+$/, "");
  const after = existing.slice(end + BLOCK_END.length).replace(/^\s+/, "");
  const joined = [before, after].filter((part) => part.trim() !== "").join("\n\n");
  return { content: joined.trim() === "" ? null : `${joined}\n`, found: true };
}

/**
 * The textual form that lands in the target file for one target.
 * @param {TargetSpec} spec
 * @param {string} document  the generated SKILL markdown (with frontmatter)
 * @returns {{relPath: string, style: TargetFile["style"], content: string}[]}
 */
export function buildArtifacts(spec, document, options = {}) {
  const skillName = options.skillName || SKILL_DIR_NAME;
  return spec.files.map((file) => ({
    relPath: expandRelPath(file.relPath, skillName),
    style: file.style,
    // A managed block cannot start with YAML frontmatter (it would be mid-file),
    // so the block variant carries the same facts as plain markdown.
    content: file.style === "managed-block" ? frontmatterToBlock(document) : document,
  }));
}

/**
 * Turn `---\nkey: value\n---\n\nbody` into `**name:** ...` lines plus the body, so
 * the same document can live inside AGENTS.md without a stray `---` rule.
 * @param {string} document
 * @returns {string}
 */
export function frontmatterToBlock(document) {
  const match = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(document);
  if (!match) return document.trim();
  const header = match[1]
    .split(/\r?\n/)
    .filter((line) => line.trim() !== "")
    .map((line) => {
      const index = line.indexOf(":");
      if (index === -1) return `- ${line.trim()}`;
      const key = line.slice(0, index).trim();
      const value = line.slice(index + 1).trim().replace(/^["']|["']$/g, "");
      return `- **${key}**: ${value}`;
    });
  const body = document.slice(match[0].length).trim();
  return ["## Skill Router connection", "", "<!-- skill-router frontmatter -->", ...header, "", body].join("\n").trim();
}
