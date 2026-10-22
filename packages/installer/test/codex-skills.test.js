/**
 * `codex-skills` target: the layout a Codex build with a skills directory reads.
 *
 * Codex 0.163 (and the Codex app) load skills from `~/.codex/skills/<name>/SKILL.md`,
 * the same shape Claude Code uses — NOT from AGENTS.md. The installer therefore has a
 * dedicated target, and `--user` must point at `~/.codex` rather than at `$HOME`,
 * because writing `~/AGENTS.md` would be silently ignored by that build.
 */

import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import path from "node:path";
import { after, afterEach, before, beforeEach, describe, it } from "node:test";

import { leakedIn, runCli } from "./helpers.js";
import { startMockPlatform, TEST_TOKEN } from "./mock-server.js";
import { MOCK_SKILL_NAME, skillDirFor, skillPathFor } from "./skill-paths.js";
import { buildArtifacts, expandRelPath, resolveTarget } from "../src/targets.js";

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
  workDir = await mkdtemp(path.join(tmpdir(), "sr-codex-"));
  platform.setCapabilities([{ slug: "text.summarize", name: "Summarize", version: 2 }]);
});

afterEach(async () => {
  await rm(workDir, { recursive: true, force: true });
});

describe("codex-skills target", () => {
  it("writes skills/<skill-name>/SKILL.md with a name that matches the directory", async () => {
    const result = await runCli(
      ["install", "--url", platform.url, "--token", TEST_TOKEN, "--target", "codex-skills"],
      { cwd: workDir },
    );
    assert.equal(result.code, 0, `stderr: ${result.stderr}`);

    const skillPath = skillPathFor(workDir, "codex-skills");
    assert.ok(existsSync(skillPath), `SKILL.md should exist at ${skillPath}`);
    const content = readFileSync(skillPath, "utf8");

    // Frontmatter `name` and the containing directory agree: Codex shows one and
    // resolves the other, and a mismatch makes the skill look broken in the list.
    const frontmatter = content.slice(0, content.indexOf("---", 4) + 3);
    assert.match(frontmatter, new RegExp(`\\nname: ${MOCK_SKILL_NAME}\\n`));
    assert.equal(path.basename(path.dirname(skillPath)), MOCK_SKILL_NAME);
    assert.match(frontmatter, /\ndescription: ".+"\n/);

    // The catalogue and the refresh procedure are present, exactly as for the
    // claude-code target — the document is shared, only the path differs.
    assert.match(content, /text\.summarize/);
    assert.match(content, /version_token/i);
    assert.match(content, /POST http:\/\/127\.0\.0\.1:\d+\/api\/v1\/invocations/);

    const config = JSON.parse(readFileSync(path.join(workDir, ".skill-router.json"), "utf8"));
    assert.equal(config.target, "codex-skills");
    assert.deepEqual(leakedIn(TEST_TOKEN, result.stdout, result.stderr, content), []);
  });

  it("is idempotent", async () => {
    const args = ["install", "--url", platform.url, "--token", TEST_TOKEN, "--target", "codex-skills"];
    assert.equal((await runCli(args, { cwd: workDir })).code, 0);
    const before = readFileSync(skillPathFor(workDir, "codex-skills"), "utf8");

    const second = await runCli([...args, "--json"], { cwd: workDir });
    assert.equal(second.code, 0, second.stderr);
    assert.equal(JSON.parse(second.stdout).files[0].action, "unchanged");
    assert.equal(readFileSync(skillPathFor(workDir, "codex-skills"), "utf8"), before);
  });

  it("removes the skill directory and the config on uninstall", async () => {
    assert.equal(
      (await runCli(["install", "--url", platform.url, "--token", TEST_TOKEN, "--target", "codex-skills"], { cwd: workDir })).code,
      0,
    );
    const skillDir = skillDirFor(workDir, "codex-skills");
    assert.ok(existsSync(skillDir));

    const removed = await runCli(["uninstall", "--json"], { cwd: workDir });
    assert.equal(removed.code, 0, removed.stderr);
    assert.equal(existsSync(skillDir), false, "the skill directory should be pruned");
    assert.equal(existsSync(path.join(workDir, ".skill-router.json")), false);
  });

  it("keeps a file the user added next to the skill", async () => {
    assert.equal(
      (await runCli(["install", "--url", platform.url, "--token", TEST_TOKEN, "--target", "codex-skills"], { cwd: workDir })).code,
      0,
    );
    const skillDir = skillDirFor(workDir, "codex-skills");
    const notes = path.join(skillDir, "notes.md");
    await readFile(skillPathFor(workDir, "codex-skills"), "utf8"); // ensure it exists
    await import("node:fs/promises").then((fs) => fs.writeFile(notes, "mine\n", "utf8"));

    assert.equal((await runCli(["uninstall"], { cwd: workDir })).code, 0);
    assert.ok(existsSync(notes), "a foreign file must survive uninstall");
    assert.equal(existsSync(path.join(skillDir, "SKILL.md")), false);
  });

  it("--user resolves to the target's user directory and verify finds it there", async () => {
    // A sandboxed home: `os.homedir()` follows USERPROFILE on Windows and HOME
    // elsewhere, so this exercises the real user-scope logic without writing into the
    // machine's own ~/.codex.
    const home = await mkdtemp(path.join(tmpdir(), "sr-home-"));
    const env = { USERPROFILE: home, HOME: home };
    try {
      const install = await runCli(
        ["install", "--url", platform.url, "--token", TEST_TOKEN, "--target", "codex-skills", "--user", "--json"],
        { cwd: workDir, env },
      );
      assert.equal(install.code, 0, install.stderr);
      const report = JSON.parse(install.stdout);
      assert.equal(report.base_dir, path.join(home, ".codex"));
      assert.ok(report.files[0].path.startsWith("skills"), report.files[0].path);

      const skillFile = path.join(home, ".codex", report.files[0].path);
      assert.ok(existsSync(skillFile), `SKILL.md should be at ${skillFile}`);
      assert.ok(existsSync(path.join(home, ".codex", ".skill-router.json")));

      // The regression: `verify --user` used to resolve the base dir to $HOME, miss the
      // config in ~/.codex, and fail with "No platform URL". It must find it, with no
      // --target flag to help it.
      const verify = await runCli(["verify", "--user", "--json"], { cwd: workDir, env });
      assert.equal(verify.code, 0, verify.stdout + verify.stderr);
      const verified = JSON.parse(verify.stdout);
      assert.equal(verified.agent.name, "mock-research-bot");
      assert.equal(verified.capabilities.count, 1);

      // update and uninstall follow the same resolution, and uninstall prunes the skill.
      assert.equal((await runCli(["update", "--user"], { cwd: workDir, env })).code, 0);
      const uninstall = await runCli(["uninstall", "--user"], { cwd: workDir, env });
      assert.equal(uninstall.code, 0, uninstall.stdout + uninstall.stderr);
      assert.equal(existsSync(path.dirname(skillFile)), false, "the skill directory should be pruned");
      assert.equal(existsSync(path.join(home, ".codex", ".skill-router.json")), false);
    } finally {
      await rm(home, { recursive: true, force: true });
    }
  });
});

describe("target helpers", () => {
  it("expands the skill-name placeholder everywhere it appears", () => {
    assert.equal(expandRelPath("skills/{skillName}/SKILL.md", "skill-router-bot"), "skills/skill-router-bot/SKILL.md");
    assert.equal(expandRelPath("AGENTS.md", "x"), "AGENTS.md");
    assert.equal(expandRelPath("{skillName}/{skillName}.md", "a"), "a/a.md");
  });

  it("builds artifacts with the skill name applied", () => {
    const spec = resolveTarget("codex-skills");
    const [artifact] = buildArtifacts(spec, "# doc\n", { skillName: "skill-router-demo" });
    assert.equal(artifact.relPath, "skills/skill-router-demo/SKILL.md");
    assert.equal(artifact.style, "replace");
    assert.equal(artifact.content, "# doc\n");
  });

  it("keeps the AGENTS.md target available for instruction-file agents", () => {
    const spec = resolveTarget("codex");
    const [artifact] = buildArtifacts(spec, "---\nname: x\ndescription: y\n---\n\nBody\n", { skillName: "x" });
    assert.equal(artifact.relPath, "AGENTS.md");
    assert.equal(artifact.style, "managed-block");
    assert.ok(!artifact.content.startsWith("---"), "a managed block cannot start with frontmatter");
  });
});
