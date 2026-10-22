/**
 * The skill directory is now named after the skill (`skill-router-<agent>`), so a
 * test that hard-codes "skill-router" must derive it instead.
 */

import path from "node:path";

import { skillNameOf } from "../src/render.js";

/** The skill directory name the mock platform's agent produces. */
export const MOCK_AGENT_NAME = "mock-research-bot";
export const MOCK_SKILL_NAME = skillNameOf({ agent: { name: MOCK_AGENT_NAME } });

/**
 * Path of the generated SKILL.md for a target inside `baseDir`.
 * @param {string} baseDir
 * @param {"claude-code"|"codex-skills"|"generic"|"codex"} target
 * @returns {string}
 */
export function skillPathFor(baseDir, target) {
  if (target === "claude-code") return path.join(baseDir, ".claude", "skills", MOCK_SKILL_NAME, "SKILL.md");
  if (target === "codex-skills") return path.join(baseDir, "skills", MOCK_SKILL_NAME, "SKILL.md");
  if (target === "codex") return path.join(baseDir, "AGENTS.md");
  return path.join(baseDir, "SKILL.md");
}

/**
 * Directory the installer should prune on uninstall, for a directory-per-skill target.
 * @param {string} baseDir
 * @param {"claude-code"|"codex-skills"} target
 * @returns {string}
 */
export function skillDirFor(baseDir, target) {
  if (target === "claude-code") return path.join(baseDir, ".claude", "skills", MOCK_SKILL_NAME);
  return path.join(baseDir, "skills", MOCK_SKILL_NAME);
}
