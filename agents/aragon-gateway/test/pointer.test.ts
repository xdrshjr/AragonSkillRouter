/**
 * T-47 (spec §A.7.1 / §A.3.7-4): the engine pointer beats the environment, a broken
 * pointer falls back to it *loudly*, and `/healthz.pin_source` reports the real source.
 *
 * These are behaviour assertions, not implementation ones: every case drives
 * `loadConfig()` off a temporary `<runtime_dir>/current.json` and then asks the same
 * question an operator would ask — "which version is actually going to run, and where
 * did that decision come from?"
 */

import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

import { engineResolvedPath, loadConfig, readEnginePointer } from '../src/config.js';
import { ALL_EVENT_NAMES } from '../src/engine.js';
import { buildHealthReport } from '../src/health.js';
import type { Logger } from '../src/types.js';

interface CapturedLog {
  level: 'debug' | 'info' | 'warn' | 'error';
  event: string;
  fields?: Record<string, unknown>;
}

function recordingLogger(sink: CapturedLog[]): Logger {
  const write = (level: CapturedLog['level']) => (event: string, fields?: Record<string, unknown>): void => {
    sink.push({ level, event, ...(fields === undefined ? {} : { fields }) });
  };
  const logger: Logger = {
    debug: write('debug'),
    info: write('info'),
    warn: write('warn'),
    error: write('error'),
    child: () => logger,
  };
  return logger;
}

function workspace(): { runtimeDir: string; homeRoot: string } {
  const root = mkdtempSync(join(tmpdir(), 'gateway-pointer-'));
  return { runtimeDir: join(root, 'engine-runtime'), homeRoot: join(root, 'aragon-home', 'engines') };
}

function baseEnv(env: { runtimeDir: string; homeRoot: string }, pin = '0.2.34'): NodeJS.ProcessEnv {
  return {
    AGENT_GATEWAY_SHARED_SECRET: 'x'.repeat(40),
    SKILLROUTER_INTERNAL_URL: 'http://127.0.0.1:8000',
    ENGINE_PIN: pin,
    AGENT_ENGINE_RUNTIME_DIR: env.runtimeDir,
    AGENT_ENGINE_HOME_ROOT: env.homeRoot,
  };
}

function installHome(homeRoot: string, version: string): string {
  const home = join(homeRoot, version);
  mkdirSync(join(home, 'node_modules', '@aragon-agent', 'core', 'dist'), { recursive: true });
  writeFileSync(
    join(home, 'node_modules', '@aragon-agent', 'core', 'package.json'),
    JSON.stringify({ name: '@aragon-agent/core', version }),
  );
  writeFileSync(join(home, 'node_modules', '@aragon-agent', 'core', 'dist', 'index.js'), 'export const x = 1;\n');
  return home;
}

function writePointer(runtimeDir: string, payload: unknown): void {
  mkdirSync(runtimeDir, { recursive: true });
  const body = typeof payload === 'string' ? payload : JSON.stringify(payload);
  writeFileSync(join(runtimeDir, 'current.json'), body);
}

describe('engine pointer (AGENT_ENGINE_RUNTIME_DIR/current.json)', () => {
  it('overrides ENGINE_PIN and reports pin_source=pointer', () => {
    const env = workspace();
    installHome(env.homeRoot, '0.2.38');
    writePointer(env.runtimeDir, { version: '0.2.38', pin: '0.2.38@abc123', schema_version: 1 });

    const config = loadConfig(baseEnv(env), recordingLogger([]));
    expect(config.enginePin).toBe('0.2.38');
    expect(config.enginePinEnv).toBe('0.2.34');
    expect(config.pinSource).toBe('pointer');
    expect(config.pointerError).toBeUndefined();
    expect(engineResolvedPath(config, config.enginePin).replace(/\\/g, '/')).toBe(
      `${resolve(env.homeRoot, '0.2.38').replace(/\\/g, '/')}/node_modules/@aragon-agent/core`,
    );
  });

  it('falls back to ENGINE_PIN when the pointer is broken JSON, and says so', () => {
    const env = workspace();
    installHome(env.homeRoot, '0.2.38');
    writePointer(env.runtimeDir, '{ this is not json');

    const logs: CapturedLog[] = [];
    const config = loadConfig(baseEnv(env), recordingLogger(logs));
    expect(config.enginePin).toBe('0.2.34');
    expect(config.pinSource).toBe('env');
    expect(config.pointerError).toContain('not valid JSON');
    const events = logs.filter((entry) => entry.event === 'engine_pointer_invalid');
    expect(events).toHaveLength(1);
    expect(events[0]?.level).toBe('error');
  });

  it('falls back to ENGINE_PIN when the pointed-at version is not installed', () => {
    const env = workspace();
    writePointer(env.runtimeDir, { version: '0.9.99', schema_version: 1 });

    const logs: CapturedLog[] = [];
    const config = loadConfig(baseEnv(env), recordingLogger(logs));
    expect(config.enginePin).toBe('0.2.34');
    expect(config.pinSource).toBe('env');
    expect(config.pointerError).toContain('0.9.99');
    expect(logs.some((entry) => entry.event === 'engine_pointer_invalid')).toBe(true);
  });

  it('ignores a pointer without a usable version', () => {
    const env = workspace();
    installHome(env.homeRoot, '0.2.38');
    writePointer(env.runtimeDir, { version: 'not-a-semver' });
    const read = readEnginePointer(env.runtimeDir, env.homeRoot);
    expect(read.pointer).toBeNull();
    expect(read.error).toContain('no usable version');
  });

  it('keeps /healthz.pin_source consistent with how the pin was resolved', () => {
    const env = workspace();
    installHome(env.homeRoot, '0.2.38');
    writePointer(env.runtimeDir, { version: '0.2.38', schema_version: 1 });
    const config = loadConfig(baseEnv(env), recordingLogger([]));

    const engine = {
      status: () => ({ availability: 'available' as const }),
      identity: () => ({
        package: '@aragon-agent/core',
        version: config.enginePin,
        commit: null,
        schema_version: 1,
      }),
      toolNames: () => ['sr_capabilities_list'],
      activeTurnCount: () => 0,
      uptimeSeconds: () => 1,
    };
    const report = buildHealthReport({
      engine: engine as never,
      pin: config.enginePin,
      pinSource: config.pinSource,
      resolvedPath: engineResolvedPath(config, config.enginePin),
    });
    expect(report.engine.version).toBe('0.2.38');
    expect(report.pin_source).toBe('pointer');
    expect(report.resolved_path).toBe(engineResolvedPath(config, '0.2.38'));
    expect(report.maintenance).toBe(false);
  });

  it('publishes a non-empty event contract derived from the gateway mapping', () => {
    // Never a hardcoded count: the Python side compares "candidate ⊇ this set".
    expect(ALL_EVENT_NAMES.length).toBeGreaterThan(0);
    expect(ALL_EVENT_NAMES).toContain('message_update');
    expect(new Set(ALL_EVENT_NAMES).size).toBe(ALL_EVENT_NAMES.length);
  });
});
