import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  GENERIC_DIR_NAME,
  candidateSkillDirs,
  currentEnv,
  detectTarget,
  formatDetection,
  type Env,
} from '../src/detect.js';

let root: string;

async function makeDir(...segments: string[]): Promise<string> {
  const target = path.join(root, ...segments);
  await fs.mkdir(target, { recursive: true });
  return target;
}

function envFor(cwd: string, home: string, aragonHome?: string): Env {
  return {
    cwd,
    home,
    aragonHome,
    platform: 'win32',
    env: {},
  };
}

beforeEach(async () => {
  root = await fs.mkdtemp(path.join(os.tmpdir(), 'sr-detect-'));
});

afterEach(async () => {
  await fs.rm(root, { recursive: true, force: true });
});

describe('detectTarget', () => {
  it('always computes absolute paths from cwd/home', () => {
    const env = currentEnv({ cwd: '/tmp/work', home: '/home/u', aragonHome: undefined, env: {} });
    expect(env.cwd).toBe('/tmp/work');
    expect(env.home).toBe('/home/u');
    expect(env.aragonHome).toBeUndefined();
  });

  it('prefers --dir over every candidate', async () => {
    const cwd = await makeDir('cwd');
    const home = await makeDir('home');
    await makeDir('cwd', '.aragon');
    const explicit = await makeDir('custom-target');

    const detection = await detectTarget({ explicitDir: explicit, env: envFor(cwd, home) });

    expect(detection.candidate).toBe('--dir');
    expect(detection.dir).toBe(explicit);
    expect(detection.detected).toBe(true);
    expect(detection.reason).toContain('--dir');
  });

  it('resolves a relative --dir against cwd and infers the agent from the path', async () => {
    const cwd = await makeDir('cwd');
    const home = await makeDir('home');

    const detection = await detectTarget({ explicitDir: '.claude/skills', env: envFor(cwd, home) });

    expect(detection.dir).toBe(path.join(cwd, '.claude', 'skills'));
    expect(detection.agent).toBe('claude');
  });

  it('honours an explicit --agent label', async () => {
    const cwd = await makeDir('cwd');
    const home = await makeDir('home');
    const detection = await detectTarget({
      explicitDir: 'skills',
      agent: 'generic',
      env: envFor(cwd, home),
    });
    expect(detection.agent).toBe('generic');
  });

  it('detects .aragon/skills before .claude/skills', async () => {
    const cwd = await makeDir('cwd');
    const home = await makeDir('home');
    await makeDir('cwd', '.aragon');
    await makeDir('cwd', '.claude');

    const detection = await detectTarget({ env: envFor(cwd, home) });

    expect(detection.candidate).toBe('.aragon/skills');
    expect(detection.dir).toBe(path.join(cwd, '.aragon', 'skills'));
    expect(detection.agent).toBe('aragon');
    expect(detection.reason).toContain('.aragon/skills');
  });

  it('falls back to .claude/skills when only .claude exists', async () => {
    const cwd = await makeDir('cwd');
    const home = await makeDir('home');
    await makeDir('cwd', '.claude');

    const detection = await detectTarget({ env: envFor(cwd, home) });

    expect(detection.candidate).toBe('.claude/skills');
    expect(detection.dir).toBe(path.join(cwd, '.claude', 'skills'));
    expect(detection.agent).toBe('claude');
    expect(detection.reason).toContain('.claude/skills');
  });

  it('uses ARAGON_HOME when no local agent directory exists', async () => {
    const cwd = await makeDir('cwd');
    const home = await makeDir('home');
    const aragonHome = await makeDir('aragon-home');
    await makeDir('home', '.aragon-agent');

    const detection = await detectTarget({ env: envFor(cwd, home, aragonHome) });

    expect(detection.candidate).toBe('$ARAGON_HOME/skills');
    expect(detection.dir).toBe(path.join(aragonHome, 'skills'));
    expect(detection.agent).toBe('aragon');
  });

  it('uses ~/.aragon-agent/skills when ARAGON_HOME is unset', async () => {
    const cwd = await makeDir('cwd');
    const home = await makeDir('home');
    await makeDir('home', '.aragon-agent');

    const detection = await detectTarget({ env: envFor(cwd, home) });

    expect(detection.candidate).toBe('~/.aragon-agent/skills');
    expect(detection.dir).toBe(path.join(home, '.aragon-agent', 'skills'));
    expect(detection.detected).toBe(true);
  });

  it('falls back to ./skillrouter-skill and reports that no agent was detected', async () => {
    const cwd = await makeDir('cwd');
    const home = await makeDir('home');

    const detection = await detectTarget({ env: envFor(cwd, home) });

    expect(detection.candidate).toBe(`./${GENERIC_DIR_NAME}`);
    expect(detection.dir).toBe(path.join(cwd, GENERIC_DIR_NAME));
    expect(detection.detected).toBe(false);
    expect(detection.reason).toContain('未探测到');
  });

  it('lists every candidate it inspected, in order', async () => {
    const cwd = await makeDir('cwd');
    const home = await makeDir('home');
    const aragonHome = await makeDir('aragon-home');

    const detection = await detectTarget({ env: envFor(cwd, home, aragonHome) });

    expect(detection.candidates.map((item) => item.label)).toEqual([
      '.aragon/skills',
      '.claude/skills',
      '$ARAGON_HOME/skills',
      '~/.aragon-agent/skills',
      `./${GENERIC_DIR_NAME}`,
    ]);
    expect(detection.candidates.every((item) => item.exists === false)).toBe(true);
  });

  it('prints the final choice plus a human reason', async () => {
    const cwd = await makeDir('cwd');
    const home = await makeDir('home');
    await makeDir('cwd', '.claude');

    const detection = await detectTarget({ env: envFor(cwd, home) });
    const lines = formatDetection(detection);

    expect(lines).toHaveLength(2);
    expect(lines[0]).toContain(detection.dir);
    expect(lines[1]).toContain('理由:');
  });
});

describe('candidateSkillDirs', () => {
  it('returns the probe order used by update/uninstall/list', async () => {
    const cwd = await makeDir('cwd');
    const home = await makeDir('home');
    const dirs = candidateSkillDirs(envFor(cwd, home, 'M:/aragon-home'));

    expect(dirs).toEqual([
      path.join(cwd, '.aragon', 'skills'),
      path.join(cwd, '.claude', 'skills'),
      path.join('M:/aragon-home', 'skills'),
      path.join(home, '.aragon-agent', 'skills'),
      path.join(cwd, GENERIC_DIR_NAME),
    ]);
  });
});
