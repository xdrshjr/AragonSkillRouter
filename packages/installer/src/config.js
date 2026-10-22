/**
 * Connection config (`.skill-router.json`).
 *
 * This is the only file that ever holds the token, and it is written with mode
 * 0600. On Windows `chmod` maps onto the read-only flag only, so the file is
 * protected by the user profile's ACL instead - `verify` reports the real mode it
 * sees rather than claiming 0600 everywhere.
 */

import { chmodSync, existsSync, mkdirSync, readFileSync, statSync, unlinkSync, writeFileSync } from "node:fs";
import path from "node:path";

import { InstallerError } from "./errors.js";
import { maskToken } from "./token.js";

export const CONFIG_FILENAME = ".skill-router.json";
export const CONFIG_SCHEMA = "skill-router.connection/v1";

/**
 * @param {string} baseDir
 * @returns {string}
 */
export function configPath(baseDir) {
  return path.join(baseDir, CONFIG_FILENAME);
}

/**
 * @param {string} baseDir
 * @returns {any | null}
 */
export function readConfig(baseDir) {
  const file = configPath(baseDir);
  if (!existsSync(file)) return null;
  let raw;
  try {
    raw = readFileSync(file, "utf8");
  } catch (error) {
    throw new InstallerError(`Cannot read ${file}: ${error instanceof Error ? error.message : String(error)}`, {
      code: "config_unreadable",
      actions: ["Check file permissions, or delete the file and run `install` again."],
    });
  }
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") throw new Error("not an object");
    return parsed;
  } catch {
    throw new InstallerError(`${file} is not valid JSON.`, {
      code: "config_invalid",
      actions: ["Delete the file and run `install` again; the token is re-read from --token."],
    });
  }
}

/**
 * Write the config with 0600 where the platform supports it.
 *
 * On Windows `chmod` only toggles the read-only flag, so the mode read back is
 * meaningless (it reports 666). Rather than print a 0600 that does not exist, the
 * result says `windows-acl` there: the file is protected by the profile's ACL.
 *
 * @param {string} baseDir
 * @param {object} config
 * @returns {{path: string, mode: string, mode_note: string}}
 */
export function writeConfig(baseDir, config) {
  mkdirSync(baseDir, { recursive: true });
  const file = configPath(baseDir);
  writeFileSync(file, `${JSON.stringify(config, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
  if (process.platform === "win32") {
    return {
      path: file,
      mode: "windows-acl",
      mode_note: "POSIX 0600 cannot be set on Windows; the file is protected by the user profile ACL. Treat it as a secret.",
    };
  }
  let mode = "unknown";
  try {
    chmodSync(file, 0o600);
    mode = (statSync(file).mode & 0o777).toString(8).padStart(3, "0");
  } catch {
    mode = "default";
  }
  return { path: file, mode, mode_note: mode === "600" ? "mode 0600 (owner read/write only)" : `mode ${mode} - tighten with chmod 600 ${file}` };
}

/**
 * @param {string} baseDir
 * @returns {boolean}
 */
export function removeConfig(baseDir) {
  const file = configPath(baseDir);
  if (!existsSync(file)) return false;
  unlinkSync(file);
  return true;
}

/**
 * The config as it may be printed: the token is replaced by its mask.
 * @param {any} config
 */
export function redactedConfig(config) {
  if (!config || typeof config !== "object") return config;
  const clone = { ...config };
  if (typeof clone.token === "string") {
    clone.token_masked = maskToken(clone.token);
    clone.token = "***redacted***";
  }
  return clone;
}

/**
 * Path that must stay inside the base dir. A config written by another tool (or
 * edited by hand) cannot make `uninstall` delete something outside the target.
 * @param {string} baseDir
 * @param {string} relPath
 * @returns {string}
 */
export function assertInside(baseDir, relPath) {
  const root = path.resolve(baseDir);
  const full = path.resolve(root, relPath);
  const relative = path.relative(root, full);
  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new InstallerError(`Refusing to touch ${full}: it is outside ${root}.`, {
      code: "path_outside_target",
      actions: ["Delete the entry from .skill-router.json by hand if you really want that file removed."],
    });
  }
  return full;
}
