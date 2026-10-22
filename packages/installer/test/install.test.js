/**
 * `install` — file layout per target, idempotency, --force, and the rule that the
 * token is stored but never printed.
 */

import assert from "node:assert/strict";
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { after, afterEach, before, beforeEach, describe, it } from "node:test";

import { leakedIn, runCli } from "./helpers.js";
import { startMockPlatform, TEST_TOKEN } from "./mock-server.js";
import { skillPathFor } from "./skill-paths.js";

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
  workDir = await mkdtemp(path.join(tmpdir(), "sr-install-"));
  platform.setCapabilities([{ slug: "text.summarize", name: "Summarize", version: 2 }]);
});

afterEach(async () => {
  await rm(workDir, { recursive: true, force: true });
});

describe("install (claude-code target)", () => {
  it("writes .claude/skills/<skill-name>/SKILL.md with the required frontmatter and catalogue", async () => {
    const result = await runCli(["install", "--url", platform.url, "--token", TEST_TOKEN, "--target", "claude-code"], { cwd: workDir });
    assert.equal(result.code, 0, `stderr: ${result.stderr}`);

    const skillPath = skillPathFor(workDir, "claude-code");
    assert.ok(existsSync(skillPath), "SKILL.md should exist at the Claude Code skill path");
    const content = readFileSync(skillPath, "utf8");

    const frontmatter = content.slice(0, content.indexOf("---", 4) + 3);
    assert.match(frontmatter, /^---/);
    assert.match(frontmatter, /\nname: skill-router-mock-research-bot\n/);
    assert.match(frontmatter, /\ndescription: ".+"\n/, "description must be a quoted YAML scalar (it contains ':' and quotes)");
    assert.match(frontmatter, /\nallowed-tools: .+/);

    // The catalogue: the granted capability, its schema and a call example.
    assert.match(content, /text\.summarize/);
    assert.match(content, /"prompt"/);
    assert.match(content, /POST http:\/\/127\.0\.0\.1:\d+\/api\/v1\/invocations/);
    // The refresh procedure and the MCP tool name.
    assert.match(content, /version_token/i);
    assert.match(content, /text__summarize/);
    assert.match(content, /## 2\. Refresh the catalogue/);

    // Connection config: url + token, nothing else secret, 0600 where supported.
    const configPath = path.join(workDir, ".skill-router.json");
    const config = JSON.parse(readFileSync(configPath, "utf8"));
    assert.equal(config.url, platform.url);
    assert.equal(config.token, TEST_TOKEN);
    assert.equal(config.target, "claude-code");
    if (process.platform !== "win32") {
      assert.equal(statSync(configPath).mode & 0o777, 0o600, "config must be 0600");
    }

    // The token is nowhere in the output, and the SKILL itself carries no token.
    assert.deepEqual(leakedIn(TEST_TOKEN, result.stdout, result.stderr, content), []);
    assert.match(result.stdout, /sr_live_\(\.\.\.\)|sr_live_\.\.\./);
  });

  it("is idempotent: a second install writes nothing and says so", async () => {
    const first = await runCli(["install", "--url", platform.url, "--token", TEST_TOKEN, "--target", "claude-code"], { cwd: workDir });
    assert.equal(first.code, 0, first.stderr);
    const skillPath = skillPathFor(workDir, "claude-code");
    const before = readFileSync(skillPath, "utf8");

    const second = await runCli(["install", "--url", platform.url, "--token", TEST_TOKEN, "--target", "claude-code", "--json"], { cwd: workDir });
    assert.equal(second.code, 0, second.stderr);
    const report = JSON.parse(second.stdout);
    assert.equal(report.files[0].action, "unchanged");
    assert.equal(readFileSync(skillPath, "utf8"), before);
    assert.equal(leakedIn(TEST_TOKEN, second.stdout, second.stderr).length, 0);
  });

  it("refuses to overwrite a changed SKILL without --force, then replaces it with --force", async () => {
    assert.equal((await runCli(["install", "--url", platform.url, "--token", TEST_TOKEN, "--target", "claude-code"], { cwd: workDir })).code, 0);

    platform.setCapabilities([
      { slug: "text.summarize", name: "Summarize", version: 2 },
      { slug: "image.generate", name: "Generate image", version: 1 },
    ]);

    const refused = await runCli(["install", "--url", platform.url, "--token", TEST_TOKEN, "--target", "claude-code"], { cwd: workDir });
    assert.equal(refused.code, 1, "a differing file must fail without --force");
    assert.match(refused.stderr, /refusing_to_overwrite|already exists and differs/);
    assert.match(refused.stderr, /--force/);

    const forced = await runCli(["install", "--url", platform.url, "--token", TEST_TOKEN, "--target", "claude-code", "--force", "--json"], { cwd: workDir });
    assert.equal(forced.code, 0, forced.stderr);
    const report = JSON.parse(forced.stdout);
    assert.equal(report.files[0].action, "written");
    const content = readFileSync(skillPathFor(workDir, "claude-code"), "utf8");
    assert.match(content, /image\.generate/);
  });

  it("rejects an unknown target with the supported list and exit 2", async () => {
    const result = await runCli(["install", "--url", platform.url, "--token", TEST_TOKEN, "--target", "cursor"], { cwd: workDir });
    assert.equal(result.code, 2);
    assert.match(result.stderr, /cursor/);
    assert.match(result.stderr, /claude-code/);
  });

  it("fails with exit 1 and the platform's advice when the token is rejected", async () => {
    const result = await runCli(["install", "--url", platform.url, "--token", "sr_live_wrongtoken0000000000000000000000"], { cwd: workDir });
    assert.equal(result.code, 1);
    assert.match(result.stderr, /invalid_token/);
    assert.match(result.stderr, /Access -> Tokens/);
    assert.equal(existsSync(path.join(workDir, ".skill-router.json")), false, "nothing is written on failure");
  });
});

describe("install (codex target)", () => {
  it("writes a managed block into AGENTS.md and preserves existing content", async () => {
    writeFileSync(path.join(workDir, "AGENTS.md"), "# My project rules\n\nAlways run the tests.\n", "utf8");
    const result = await runCli(["install", "--url", platform.url, "--token", TEST_TOKEN, "--target", "codex"], { cwd: workDir });
    assert.equal(result.code, 0, result.stderr);

    const content = readFileSync(path.join(workDir, "AGENTS.md"), "utf8");
    assert.match(content, /^# My project rules\n\nAlways run the tests\./);
    assert.match(content, /<!-- skill-router:begin -->/);
    assert.match(content, /<!-- skill-router:end -->/);
    assert.match(content, /text\.summarize/);
    // Frontmatter becomes bullet facts inside a block, never a stray `---` rule.
    assert.equal(content.includes("---\nname:"), false);
    assert.match(content, /\*\*name\*\*: skill-router-mock-research-bot/);
    assert.deepEqual(leakedIn(TEST_TOKEN, result.stdout, result.stderr, content), []);
  });

  it("appends the block only once", async () => {
    await runCli(["install", "--url", platform.url, "--token", TEST_TOKEN, "--target", "codex"], { cwd: workDir });
    const afterFirst = readFileSync(path.join(workDir, "AGENTS.md"), "utf8");
    const second = await runCli(["install", "--url", platform.url, "--token", TEST_TOKEN, "--target", "codex", "--json"], { cwd: workDir });
    assert.equal(second.code, 0, second.stderr);
    const afterSecond = readFileSync(path.join(workDir, "AGENTS.md"), "utf8");
    assert.equal(afterSecond, afterFirst);
    assert.equal(afterSecond.match(/<!-- skill-router:begin -->/g).length, 1);
  });
});

describe("install (generic target)", () => {
  it("writes SKILL.md next to .skill-router.json", async () => {
    const result = await runCli(["install", "--url", platform.url, "--token", TEST_TOKEN], { cwd: workDir });
    assert.equal(result.code, 0, result.stderr);
    assert.ok(existsSync(path.join(workDir, "SKILL.md")));
    assert.ok(existsSync(path.join(workDir, ".skill-router.json")));
    assert.equal(JSON.parse(readFileSync(path.join(workDir, ".skill-router.json"), "utf8")).target, "generic");
  });

  it("honours --dir and does not touch the cwd", async () => {
    const target = path.join(workDir, "nested", "agent");
    mkdirSync(target, { recursive: true });
    const result = await runCli(["install", "--url", platform.url, "--token", TEST_TOKEN, "--dir", target], { cwd: workDir });
    assert.equal(result.code, 0, result.stderr);
    assert.ok(existsSync(path.join(target, "SKILL.md")));
    assert.equal(existsSync(path.join(workDir, "SKILL.md")), false);
  });

  it("normalises a URL that already carries /api/v1", async () => {
    const result = await runCli(["install", "--url", `${platform.url}/api/v1`, "--token", TEST_TOKEN, "--json"], { cwd: workDir });
    assert.equal(result.code, 0, result.stderr);
    const config = JSON.parse(readFileSync(path.join(workDir, ".skill-router.json"), "utf8"));
    assert.equal(config.url, platform.url);
  });
});

describe("install (usage errors)", () => {
  it("requires --url when nothing is stored", async () => {
    const result = await runCli(["install", "--token", TEST_TOKEN], { cwd: workDir });
    assert.equal(result.code, 2);
    assert.match(result.stderr, /No platform URL/);
  });

  it("requires --token when nothing is stored", async () => {
    const result = await runCli(["install", "--url", platform.url], { cwd: workDir });
    assert.equal(result.code, 2);
    assert.match(result.stderr, /No agent token/);
  });

  it("rejects an unknown flag with exit 2", async () => {
    const result = await runCli(["install", "--nope"], { cwd: workDir });
    assert.equal(result.code, 2);
    assert.match(result.stderr, /Unknown argument/);
  });

  it("prints help and version without touching the filesystem", async () => {
    const help = await runCli(["--help"], { cwd: workDir });
    assert.equal(help.code, 0);
    assert.match(help.stdout, /Usage:/);
    for (const command of ["install", "verify", "update", "uninstall"]) {
      assert.match(help.stdout, new RegExp(command));
    }
    const version = await runCli(["--version"], { cwd: workDir });
    assert.equal(version.code, 0);
    assert.match(version.stdout.trim(), /^\d+\.\d+\.\d+$/);
    assert.equal(existsSync(path.join(workDir, ".skill-router.json")), false);
  });
});
