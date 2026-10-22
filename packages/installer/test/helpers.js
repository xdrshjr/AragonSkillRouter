/**
 * Helpers shared by the test files: run the real CLI as a child process.
 *
 * A child process (rather than importing `run()` directly) is deliberate: the
 * assertions then cover the launcher, the exit code and the stream separation
 * (`stdout` = report, `stderr` = errors), which is what an operator actually sees.
 */

import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const BIN = path.join(HERE, "..", "bin", "skill-router-connect.js");

/**
 * @param {string[]} args
 * @param {{cwd?: string, env?: Record<string, string>}} [options]
 * @returns {Promise<{code: number, stdout: string, stderr: string}>}
 */
export function runCli(args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [BIN, ...args], {
      cwd: options.cwd || process.cwd(),
      env: { ...process.env, NO_COLOR: "1", ...(options.env || {}) },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });
    child.on("error", reject);
    child.on("close", (code) => resolve({ code: code === null ? -1 : code, stdout, stderr }));
  });
}

/**
 * Assert-free helper: the full token must never appear in any output.
 * @param {string} token
 * @param {...string} outputs
 * @returns {string[]} the outputs that leaked it
 */
export function leakedIn(token, ...outputs) {
  return outputs.filter((output) => typeof output === "string" && output.includes(token));
}
