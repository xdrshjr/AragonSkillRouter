/**
 * `GET /healthz` — spec §9.5 shape (extended by §A.5.3):
 * `{ok, engine:{package,version,commit,schema_version}, tools:[], active_turns, uptime_s,
 *   pin_source, resolved_path, maintenance}`.
 *
 * `ok` is true only when the engine can actually serve a turn. A gateway whose SDK is
 * missing, or whose child process died, reports `ok:false` with a `detail` — that is
 * what §12.4-4's health gate ("GET /healthz 连续 3 次通过且 engine.version 匹配") and the
 * Docker healthcheck key off, so it must never be optimistic.
 *
 * `pin_source` + `resolved_path` are the "installed but inert" probe (§A.5.3): they say
 * which pointer produced the pin and which package directory the pin resolves to.
 */

import type { EngineBackend } from './engine.js';
import type { EngineHealth } from './types.js';

export interface HealthDeps {
  engine: EngineBackend;
  /** Pin the gateway was configured with (reported even when the engine is down). */
  pin: string;
  /** Where that pin came from: the pointer file or `ENGINE_PIN`. */
  pinSource?: 'pointer' | 'env';
  /** Absolute path the pin resolves to (`<homeRoot>/<version>/node_modules/@aragon-agent/core`). */
  resolvedPath?: string;
  /** Platform maintenance marker present (write freeze). */
  maintenance?: boolean;
  /** Extra tool names that exist but are disabled by policy. */
  disabledTools?: string[];
}

export function buildHealthReport(deps: HealthDeps): EngineHealth {
  const status = deps.engine.status();
  const identity = deps.engine.identity();
  const available = status.availability === 'available';
  return {
    ok: available,
    engine: {
      ...identity,
      version: identity.version || deps.pin,
      status: status.availability,
      ...(status.detail === undefined ? {} : { detail: status.detail }),
    },
    tools: deps.engine.toolNames(),
    active_turns: deps.engine.activeTurnCount(),
    uptime_s: deps.engine.uptimeSeconds(),
    pin_source: deps.pinSource ?? 'env',
    resolved_path: deps.resolvedPath ?? '',
    maintenance: deps.maintenance ?? false,
  };
}

/**
 * Summary used by the reload path and by `engine-host`'s `ready` frame: a stable,
 * ordered tool list so a version switch can assert the permission surface did not
 * change behind the platform's back (spec §12.4-3).
 */
export function toolSummary(names: readonly string[]): string {
  return names.slice().sort().join(',');
}

/** True when two health reports describe the same tool surface. */
export function sameToolSurface(a: readonly string[], b: readonly string[]): boolean {
  return toolSummary(a) === toolSummary(b);
}
