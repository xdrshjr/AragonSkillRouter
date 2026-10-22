/**
 * `uninstall` — removes exactly what `install` wrote and nothing else.
 *
 * Safety cases: a foreign file with the same name, a user file next to the skill,
 * a hand-written AGENTS.md, a config pointing outside the base dir, and running it
 * twice.
 */

import assert from "node:assert/strict";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { after, afterEach, before, beforeEach, describe, it } from "node:test";

import { runCli } from "./helpers.js";
import { startMockPlatform, TEST_TOKEN } from "./mock-server.js";
import { skillDirFor, skillPathFor } from "./skill-paths.js";

/** @type {import("./mock-server.js").MockPlatform} */
let platform;
/** @type {string} */
let workDir;

before(async () => {
  platform = await startMockPlatform();
});

after(async () => {
  await platform.stop();
});

beforeEach(async () => {
  workDir = await mkdtemp(path.join(tmpdir(), "sr-uninstall-"));
  platform.setCapabilities([{ slug: "text.summarize", name: "Summarize", version: 2 }]);
});

afterEach(async () => {
  await rm(workDir, { recursive: true, force: true });
});

describe("uninstall", () => {
  it("removes the skill file and the config, and prunes the directories it created", async () => {
    assert.equal((await runCli(["install", "--url", platform.url, "--token", TEST_TOKEN, "--target", "claude-code"], { cwd: workDir })).code, 0);
    const skillPath = skillPathFor(workDir, "claude-code");
    assert.ok(existsSync(skillPath));

    const result = await runCli(["uninstall", "--json"], { cwd: workDir });
    assert.equal(result.code, 0, result.stderr);
    const report = JSON.parse(result.stdout);
    assert.equal(report.config_removed, true);
    assert.equal(existsSync(skillPath), false);
    assert.equal(existsSync(path.join(workDir, ".skill-router.json")), false);
    // .claude/skills/skill-router was created by the installer and is now empty.
    assert.equal(existsSync(skillDirFor(workDir, "claude-code")), false);
  });

  it("keeps unrelated files, including a directory that is no longer empty", async () => {
    assert.equal((await runCli(["install", "--url", platform.url, "--token", TEST_TOKEN, "--target", "claude-code"], { cwd: workDir })).code, 0);
    const skillDir = skillDirFor(workDir, "claude-code");
    writeFileSync(path.join(skillDir, "notes.md"), "my own notes\n", "utf8");
    writeFileSync(path.join(workDir, "unrelated.txt"), "keep me\n", "utf8");

    const result = await runCli(["uninstall"], { cwd: workDir });
    assert.equal(result.code, 0, result.stderr);
    assert.equal(existsSync(path.join(skillDir, "SKILL.md")), false);
    assert.equal(existsSync(path.join(skillDir, "notes.md")), true, "a file the tool did not write must survive");
    assert.equal(readFileSync(path.join(workDir, "unrelated.txt"), "utf8"), "keep me\n");
  });

  it("removes only the managed block from AGENTS.md and never deletes a hand-written file", async () => {
    writeFileSync(path.join(workDir, "AGENTS.md"), "# House rules\n\nRun the tests.\n", "utf8");
    assert.equal((await runCli(["install", "--url", platform.url, "--token", TEST_TOKEN, "--target", "codex"], { cwd: workDir })).code, 0);

    const result = await runCli(["uninstall"], { cwd: workDir });
    assert.equal(result.code, 0, result.stderr);
    const content = readFileSync(path.join(workDir, "AGENTS.md"), "utf8");
    assert.match(content, /# House rules/);
    assert.equal(content.includes("<!-- skill-router:begin -->"), false);
    assert.match(content, /Run the tests\./);
  });

  it("deletes AGENTS.md when the managed block was all it contained", async () => {
    assert.equal((await runCli(["install", "--url", platform.url, "--token", TEST_TOKEN, "--target", "codex"], { cwd: workDir })).code, 0);
    assert.equal((await runCli(["uninstall"], { cwd: workDir })).code, 0);
    assert.equal(existsSync(path.join(workDir, "AGENTS.md")), false);
  });

  it("is a no-op when nothing was installed", async () => {
    const result = await runCli(["uninstall", "--json"], { cwd: workDir });
    assert.equal(result.code, 0, result.stderr);
    assert.match(result.stdout, /"note": "not installed"/);
    assert.match(JSON.parse(result.stdout).removed.length.toString(), /^0$/);
  });

  it("is safe to run twice", async () => {
    assert.equal((await runCli(["install", "--url", platform.url, "--token", TEST_TOKEN], { cwd: workDir })).code, 0);
    assert.equal((await runCli(["uninstall"], { cwd: workDir })).code, 0);
    const second = await runCli(["uninstall"], { cwd: workDir });
    assert.equal(second.code, 0, second.stderr);
    assert.match(second.stdout, /Nothing to uninstall/);
  });

  it("refuses to delete a SKILL.md it did not write", async () => {
    writeFileSync(path.join(workDir, "SKILL.md"), "# Somebody else's skill\n", "utf8");
    writeFileSync(
      path.join(workDir, ".skill-router.json"),
      JSON.stringify(
        {
          schema: "skill-router.connection/v1",
          url: platform.url,
          token: TEST_TOKEN,
          target: "generic",
          base_dir: workDir,
          files: [{ path: "SKILL.md", style: "replace" }],
          created_dirs: [],
        },
        null,
        2,
      ),
      "utf8",
    );
    const result = await runCli(["uninstall"], { cwd: workDir });
    assert.equal(result.code, 0, result.stderr);
    assert.equal(existsSync(path.join(workDir, "SKILL.md")), true, "a foreign file must not be deleted");
    assert.match(result.stdout, /not written by this tool/);
  });

  it("refuses a config entry that points outside the base directory", async () => {
    const outside = path.join(workDir, "..", `sr-outside-${Date.now()}.md`);
    writeFileSync(outside, "# outside\n", "utf8");
    mkdirSync(path.join(workDir, "inner"), { recursive: true });
    writeFileSync(
      path.join(workDir, "inner", ".skill-router.json"),
      JSON.stringify(
        {
          schema: "skill-router.connection/v1",
          url: platform.url,
          token: TEST_TOKEN,
          target: "generic",
          base_dir: path.join(workDir, "inner"),
          files: [{ path: path.relative(path.join(workDir, "inner"), outside), style: "replace" }],
          created_dirs: [],
        },
        null,
        2,
      ),
      "utf8",
    );
    // Use --dir so the config in inner/ is the one that is read.
    const result = await runCli(["uninstall", "--dir", path.join(workDir, "inner")], { cwd: workDir });
    assert.equal(result.code, 1);
    assert.match(result.stderr, /outside/);
    assert.equal(existsSync(outside), true, "the outside file must still exist");
    try {
      await rm(outside, { force: true });
    } catch {
      // best effort
    }
  });
});
