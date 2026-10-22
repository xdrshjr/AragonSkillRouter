/**
 * `update` — refreshes the SKILL from `GET /api/v1/skill`.
 *
 * The mock platform's `/api/v1/skill` body carries a distinctive sentence, so the
 * test proves the file content really comes from that endpoint and not from local
 * re-rendering.
 */

import assert from "node:assert/strict";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { after, afterEach, before, beforeEach, describe, it } from "node:test";

import { leakedIn, runCli } from "./helpers.js";
import { startMockPlatform, TEST_TOKEN } from "./mock-server.js";

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
  workDir = await mkdtemp(path.join(tmpdir(), "sr-update-"));
  platform.setCapabilities([{ slug: "text.summarize", name: "Summarize", version: 2 }]);
});

afterEach(async () => {
  await rm(workDir, { recursive: true, force: true });
});

describe("update", () => {
  it("rewrites the SKILL with the server document and keeps our frontmatter", async () => {
    assert.equal((await runCli(["install", "--url", platform.url, "--token", TEST_TOKEN], { cwd: workDir })).code, 0);
    const before = readFileSync(path.join(workDir, "SKILL.md"), "utf8");
    assert.equal(before.includes("SERVER-RENDERED-BODY"), false);

    const result = await runCli(["update", "--json"], { cwd: workDir });
    assert.equal(result.code, 0, result.stderr);
    const report = JSON.parse(result.stdout);
    assert.equal(report.files[0].action, "written");
    assert.equal(report.server_sha256, "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef");

    const content = readFileSync(path.join(workDir, "SKILL.md"), "utf8");
    assert.match(content, /SERVER-RENDERED-BODY/, "body must come from GET /api/v1/skill");
    assert.match(content, /^---\nname: skill-router-mock-research-bot\n/);
    assert.match(content, /\nallowed-tools: /, "our frontmatter is added because the server cannot know the local tool set");
    assert.match(content, /catalogue version token: vt-text\.summarize/i);
    // The server's own frontmatter is not duplicated.
    assert.equal(content.match(/^---$/gm).length, 2);
    assert.deepEqual(leakedIn(TEST_TOKEN, result.stdout, result.stderr, content), []);
  });

  it("is idempotent on a second run", async () => {
    await runCli(["install", "--url", platform.url, "--token", TEST_TOKEN], { cwd: workDir });
    assert.equal((await runCli(["update"], { cwd: workDir })).code, 0);
    const afterFirst = readFileSync(path.join(workDir, "SKILL.md"), "utf8");
    const second = await runCli(["update", "--json"], { cwd: workDir });
    assert.equal(second.code, 0, second.stderr);
    assert.equal(JSON.parse(second.stdout).files[0].action, "unchanged");
    assert.equal(readFileSync(path.join(workDir, "SKILL.md"), "utf8"), afterFirst);
  });

  it("refuses to update when nothing was installed", async () => {
    const result = await runCli(["update", "--url", platform.url, "--token", TEST_TOKEN], { cwd: workDir });
    assert.equal(result.code, 1);
    assert.match(result.stderr, /Nothing to update/);
    assert.equal(existsSync(path.join(workDir, "SKILL.md")), false);
  });

  it("does not overwrite a SKILL.md it did not write", async () => {
    assert.equal((await runCli(["install", "--url", platform.url, "--token", TEST_TOKEN], { cwd: workDir })).code, 0);
    // Something replaced our file with a hand-written one.
    writeFileSync(path.join(workDir, "SKILL.md"), "# Hand written, not managed\n", "utf8");
    const result = await runCli(["update"], { cwd: workDir });
    assert.equal(result.code, 1);
    assert.match(result.stderr, /refusing_to_overwrite|already exists and differs/);
    assert.match(readFileSync(path.join(workDir, "SKILL.md"), "utf8"), /Hand written/);
  });

  it("refreshes the codex block in place and leaves the rest of AGENTS.md alone", async () => {
    writeFileSync(path.join(workDir, "AGENTS.md"), "# Rules\n\nDo not break prod.\n", "utf8");
    assert.equal((await runCli(["install", "--url", platform.url, "--token", TEST_TOKEN, "--target", "codex"], { cwd: workDir })).code, 0);
    const updated = await runCli(["update"], { cwd: workDir });
    assert.equal(updated.code, 0, updated.stderr);
    const content = readFileSync(path.join(workDir, "AGENTS.md"), "utf8");
    assert.match(content, /^# Rules\n\nDo not break prod\./);
    assert.match(content, /SERVER-RENDERED-BODY/);
    assert.equal(content.match(/<!-- skill-router:begin -->/g).length, 1);
  });
});
