/**
 * spec §6.1 `env.test.ts`：§2.6 环境表逐项断言。
 * 关键三条：`SKILLROUTER_MASTER_KEY` 必须**不存在**、`SKILLROUTER_INTERNAL_URL` 必须同时出现在
 * api 与 gateway 两份 env（P0-1）、继承的 `SKILLROUTER_*`/`AGENT_*`/`ENGINE_*`/`PYTHON*` 被剔除（P1-12）。
 */
import path from 'node:path';

import { describe, expect, it } from 'vitest';

import { composeEnv, stripInherited } from '../src/main/env';
import { DEFAULT_API_PORT, DEFAULT_GATEWAY_PORT, DEFAULT_WEB_PORT } from '../src/shared/constants';
import type { ComposeEnvOptions } from '../src/main/env';

const dataDir = path.resolve('C:/Users/u/AppData/Roaming/SkillRouter/data');
const base: ComposeEnvOptions = {
  dataDir,
  dbPath: path.join(dataDir, 'skillrouter.db'),
  apiPort: DEFAULT_API_PORT,
  webPort: DEFAULT_WEB_PORT,
  gatewayPort: DEFAULT_GATEWAY_PORT,
  gatewaySecret: 'x'.repeat(48),
  base: { PATH: 'C:\\Windows\\system32' },
};

describe('composeEnv / api', () => {
  it('SKILLROUTER_ENV 是 development（D7）', () => {
    expect(composeEnv('api', base).SKILLROUTER_ENV).toBe('development');
  });

  it('数据目录与库路径都是绝对路径', () => {
    const env = composeEnv('api', base);
    expect(path.isAbsolute(env.SKILLROUTER_DATA_DIR ?? '')).toBe(true);
    expect(path.isAbsolute(env.SKILLROUTER_DB_PATH ?? '')).toBe(true);
    expect(env.SKILLROUTER_DB_PATH).toBe(path.join(dataDir, 'skillrouter.db'));
  });

  it('绝不设置 SKILLROUTER_MASTER_KEY（密钥只落 <data>/credentials-master.key）', () => {
    for (const role of ['api', 'worker', 'gateway', 'web'] as const) {
      expect(composeEnv(role, base)).not.toHaveProperty('SKILLROUTER_MASTER_KEY');
    }
  });

  it('SKILLROUTER_INTERNAL_URL 在 api 与 gateway 两份 env 里都存在且都指向 api 端口（P0-1）', () => {
    const expected = `http://127.0.0.1:${DEFAULT_API_PORT}`;
    expect(composeEnv('api', base).SKILLROUTER_INTERNAL_URL).toBe(expected);
    expect(composeEnv('bootstrap', base).SKILLROUTER_INTERNAL_URL).toBe(expected);
    expect(composeEnv('gateway', base).SKILLROUTER_INTERNAL_URL).toBe(expected);
  });

  it('ENGINE_TOOLCHAIN 显式钉死为 gateway（P1-12）', () => {
    expect(composeEnv('api', base).SKILLROUTER_ENGINE_TOOLCHAIN).toBe('gateway');
  });

  it('admin 凭据与网关密钥只来自 desktop.json', () => {
    const env = composeEnv('api', base);
    expect(env.SKILLROUTER_ADMIN_USER).toBe('admin');
    expect(env.SKILLROUTER_ADMIN_PASSWORD).toBe('adminjr');
    expect(env.SKILLROUTER_AGENT_GATEWAY_SECRET).toBe('x'.repeat(48));
    expect(env.SKILLROUTER_AGENT_GATEWAY_BIND).toBe('127.0.0.1');
    expect(env.SKILLROUTER_AGENT_GATEWAY_URL).toBe(`http://127.0.0.1:${DEFAULT_GATEWAY_PORT}`);
    expect(env.SKILLROUTER_PUBLIC_BASE_URL).toBe(`http://127.0.0.1:${DEFAULT_WEB_PORT}`);
  });

  it('载荷 / 出站策略取桌面端的保守值', () => {
    const env = composeEnv('api', base);
    expect(env.SKILLROUTER_PAYLOAD_POLICY).toBe('redacted_preview');
    expect(env.SKILLROUTER_EGRESS_POLICY).toBe('deny');
    expect(env.SKILLROUTER_ENGINE_POLICY).toBe('notify');
  });
});

describe('composeEnv / gateway', () => {
  it('ENGINE_PIN 是纯版本号（去掉 @commit），SKILLROUTER_ENGINE_PIN 带 commit', () => {
    const env = composeEnv('gateway', base);
    expect(env.ENGINE_PIN).toBe('0.2.34');
    expect(env.ENGINE_PIN).not.toContain('@');
    expect(composeEnv('api', base).SKILLROUTER_ENGINE_PIN).toContain('@');
  });

  it('AGENT_* 路径全在 data 目录下且为绝对路径', () => {
    const env = composeEnv('gateway', base);
    expect(env.AGENT_ENGINE_HOME_ROOT).toBe(path.join(dataDir, 'aragon-home', 'engines'));
    expect(env.AGENT_ENGINE_RUNTIME_DIR).toBe(path.join(dataDir, 'engine-runtime'));
    expect(env.AGENT_ENGINE_BUILDS_DIR).toBe(path.join(dataDir, 'engine-builds'));
    expect(env.AGENT_POLICY_PATH).toBe(path.join(dataDir, 'agent-policy.json'));
    expect(env.AGENT_SESSION_DIR).toBe(path.join(dataDir, 'agent-sessions'));
    for (const key of [
      'AGENT_ENGINE_HOME_ROOT',
      'AGENT_ENGINE_RUNTIME_DIR',
      'AGENT_ENGINE_BUILDS_DIR',
      'AGENT_POLICY_PATH',
      'AGENT_SESSION_DIR',
    ]) {
      expect(path.isAbsolute(env[key] ?? '')).toBe(true);
    }
  });

  it('网关密钥与 api 侧同源，端口来自运行时扫描结果', () => {
    const env = composeEnv('gateway', { ...base, gatewayPort: 18105 });
    expect(env.AGENT_GATEWAY_SHARED_SECRET).toBe('x'.repeat(48));
    expect(env.AGENT_GATEWAY_PORT).toBe('18105');
    expect(env.AGENT_GATEWAY_BIND).toBe('127.0.0.1');
    expect(env.AGENT_GATEWAY_ENGINE_MODE).toBe('auto');
    expect(env.NODE_ENV).toBe('production');
    expect(env.ELECTRON_RUN_AS_NODE).toBe('1');
  });
});

describe('composeEnv / web', () => {
  it('PORT 与 HOSTNAME 只出现在 web 的 env 里', () => {
    const web = composeEnv('web', base);
    expect(web.PORT).toBe(String(DEFAULT_WEB_PORT));
    expect(web.HOSTNAME).toBe('127.0.0.1');
    expect(web.NEXT_TELEMETRY_DISABLED).toBe('1');
    for (const role of ['api', 'worker', 'gateway'] as const) {
      const env = composeEnv(role, base);
      expect(env).not.toHaveProperty('PORT');
      expect(env).not.toHaveProperty('HOSTNAME');
    }
  });
});

describe('继承环境剔除（P1-12）', () => {
  it('SKILLROUTER_* / AGENT_* / ENGINE_* / PYTHON* / NODE_OPTIONS / ELECTRON_RUN_AS_NODE 全部被删', () => {
    const dirty = {
      PATH: 'C:\\Windows\\system32',
      SKILLROUTER_ENV: 'production',
      SKILLROUTER_DATA_DIR: 'C:\\dev\\var',
      SKILLROUTER_ENGINE_TOOLCHAIN: 'local',
      AGENT_GATEWAY_PORT: '8500',
      AGENT_MODEL_ID: 'leak',
      ENGINE_PIN: '9.9.9',
      PYTHONHOME: 'C:\\conda',
      PYTHONPATH: 'C:\\conda\\lib',
      PYTHONUTF8: '0',
      NODE_OPTIONS: '--max-old-space-size=64',
      ELECTRON_RUN_AS_NODE: '1',
    };
    const clean = stripInherited(dirty);
    expect(clean.PATH).toBe('C:\\Windows\\system32');
    expect(Object.keys(clean).filter((k) => k !== 'PATH')).toHaveLength(0);

    const env = composeEnv('api', { ...base, base: dirty });
    // 我们的值赢回，继承值不得生效
    expect(env.SKILLROUTER_ENV).toBe('development');
    expect(env.SKILLROUTER_ENGINE_TOOLCHAIN).toBe('gateway');
    expect(env.SKILLROUTER_DATA_DIR).toBe(dataDir);
    expect(env.PYTHONUTF8).toBe('1');
    expect(env.NODE_OPTIONS).toBeUndefined();
    expect(env.AGENT_MODEL_ID).toBeUndefined();
    expect(env.PYTHONHOME).toBeUndefined();
  });

  it('worker 拿到同一套 Python 侧基础环境，但没有 admin 凭据', () => {
    const env = composeEnv('worker', base);
    expect(env.SKILLROUTER_DATA_DIR).toBe(dataDir);
    expect(env.SKILLROUTER_PAYLOAD_POLICY).toBe('redacted_preview');
    expect(env.SKILLROUTER_ADMIN_PASSWORD).toBeUndefined();
    expect(env.PYTHONUNBUFFERED).toBe('1');
    expect(env.PYTHONIOENCODING).toBe('utf-8');
  });
});
