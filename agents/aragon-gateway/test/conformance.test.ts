/**
 * Adapter conformance suite (spec §12.4-3).
 *
 * This is the check the upgrade pipeline runs against a candidate engine build:
 *
 * - an upstream export that is REMOVED or RENAMED fails the run;
 * - an added export is reported as `needs_adapter_review` (a warning here, because a
 *   new symbol never widens our permission surface — the tool list does that);
 * - the 13 platform event names and the 12 platform tools are frozen;
 * - the upstream tool contract is exercised for real (`defineTool` + `AragonAgent`
 *   construction) so a silent behavioural break is caught, not just a name diff.
 *
 * When `@aragon-agent/core` is not installed the upstream-facing tests are skipped
 * with an explicit notice rather than silently passing.
 */

import { describe, expect, it } from 'vitest';

import { PLATFORM_EVENT_TYPES } from '../src/types.js';
import { UPSTREAM_BUILTIN_TOOL_NAMES, PLATFORM_TOOL_NAMES, checkUpstreamToolList } from '../src/policy.js';
import { buildToolset, defineWithHost, registrableToolNames } from '../src/tools/index.js';
import { PlatformClient } from '../src/platform-client.js';
import { loadPolicy } from '../src/policy.js';
import type { CoreModule } from '../src/engine.js';

// ---------------------------------------------------------------------------
// Frozen expectations
// ---------------------------------------------------------------------------

/**
 * The 86 public exports of `@aragon-agent/core@0.2.34` (measured, sorted).
 * DO NOT edit this list to make a build pass: an upgrade that changes it needs
 * `needs_adapter_review` (addition) or a failed verification (removal), per §12.4-3.
 */
const EXPECTED_EXPORTS: readonly string[] = [
  'ABSOLUTE_MAX_OUTPUT_TOKENS',
  'ALWAYS_SKILLS_MAX_BYTES',
  'Agent',
  'AnthropicProvider',
  'AragonAgent',
  'CHARS_PER_TOKEN',
  'CONTEXT_SAFETY_MARGIN_TOKENS',
  'DEFAULT_MAX_OUTPUT_TOKENS',
  'DEFAULT_RETRY_POLICY',
  'GoogleProvider',
  'IdleWatchdog',
  'LLMError',
  'MIN_MAX_OUTPUT_TOKENS',
  'MessageManager',
  'MessageQueueManager',
  'ModelRegistry',
  'OpenAIProvider',
  'ProviderRegistry',
  'RETRY_LIMITS',
  'SAFE_FALLBACK_MAX_OUTPUT_TOKENS',
  'SKILL_BODY_MAX_BYTES',
  'SKILL_CATALOG_MAX_BYTES',
  'SKILL_DESC_LINE_MAX',
  'SKILL_FILES_MAX',
  'SKILL_FIND_MAX_BYTES',
  'SKILL_MD_MAX_BYTES',
  'SKILL_NAME_PATTERN',
  'SKILL_RESULT_MAX_BYTES',
  'SkillRegistry',
  'THINKING_BUDGET',
  'THINKING_HEADROOM_TOKENS',
  'ToolExecutor',
  'ToolParamValidator',
  'ToolRegistry',
  'ToolValidationError',
  'applySkillArguments',
  'classifyHttpError',
  'classifyOutputLimitFailure',
  'clearLearnedCeilings',
  'clearLearnedTokenFields',
  'completeLLM',
  'computeBackoffDelay',
  'computeToolPolicy',
  'consumeStream',
  'createSkillFindTool',
  'createSkillTool',
  'defineTool',
  'errorResult',
  'estimatePromptTokens',
  'evaluateToolCall',
  'findSafeCutIndices',
  'formatValidationErrors',
  'getLearnedCeiling',
  'getLearnedTokenField',
  'getModelRegistry',
  'getProviderRegistry',
  'imageResult',
  'initProviders',
  'isRetryableError',
  'learnModelCeiling',
  'learnTokenField',
  'multiResult',
  'parseFrontmatter',
  'parseRetryAfterMs',
  'parseSSEStream',
  'planCompaction',
  'rankCatalogRecords',
  'relieveTail',
  'renderSkillBody',
  'renderSkillCatalog',
  'renderSkillFindResults',
  'renderSkillInvocation',
  'resolveOutputTokens',
  'sanitizeForPromptBlock',
  'sendWithOutputLimitRecovery',
  'setModelRegistry',
  'setProviderRegistry',
  'staticCeilingFor',
  'streamLLM',
  'suggestSkillNames',
  'textResult',
  'validateHistory',
  'validateSkillFrontmatter',
  'validateStagedSkill',
  'withRetry',
  'wrapFetchError',
];

/** spec §7.12 / §9.4 — the 13 event names, frozen. */
const EXPECTED_EVENT_TYPES: readonly string[] = [
  'session.started',
  'turn.started',
  'assistant.delta',
  'thinking.delta',
  'tool.started',
  'tool.progress',
  'tool.finished',
  'todo.updated',
  'turn.queued',
  'turn.finished',
  'turn.stopped',
  'turn.failed',
  'engine.notice',
];

/** Symbols the adapter is allowed to depend on (aragonagent-upstream.md §3). */
const ADAPTER_DEPENDENCY_ALLOWLIST = [
  'AragonAgent',
  'Agent',
  'defineTool',
  'textResult',
  'errorResult',
  'initProviders',
  'getProviderRegistry',
];

const core = await tryImportCore();

async function tryImportCore(): Promise<CoreModule | null> {
  try {
    return (await import('@aragon-agent/core')) as unknown as CoreModule;
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn(
      `[conformance] @aragon-agent/core is not importable (${(err as Error).message}); ` +
        'upstream-facing checks are SKIPPED. Run `npm install --include=dev` in agents/aragon-gateway.',
    );
    return null;
  }
}

// ---------------------------------------------------------------------------
// Frozen platform contract (always runs)
// ---------------------------------------------------------------------------

describe('platform contract (frozen)', () => {
  it('exposes exactly the 13 platform event names from §7.12', () => {
    expect([...PLATFORM_EVENT_TYPES]).toEqual(EXPECTED_EVENT_TYPES);
    expect(new Set(PLATFORM_EVENT_TYPES).size).toBe(PLATFORM_EVENT_TYPES.length);
  });

  it('registers exactly the 12 platform tools from §12.3, in spec order', () => {
    const names = registrableToolNames();
    expect(names).toEqual([
      'sr_capabilities_list',
      'sr_capability_get',
      'sr_fetch_source',
      'sr_parse_document',
      'sr_propose_capability_draft',
      'sr_update_capability_draft',
      'sr_test_capability',
      'sr_sample_invoke',
      'sr_create_skill_draft',
      'sr_validate_skill',
      'sr_record_finding',
      'sr_request_approval',
    ]);
    expect(names).toEqual([...PLATFORM_TOOL_NAMES]);
  });

  it('never exposes an upstream builtin tool name in the platform surface', () => {
    for (const builtin of UPSTREAM_BUILTIN_TOOL_NAMES) {
      expect(PLATFORM_TOOL_NAMES as readonly string[]).not.toContain(builtin);
    }
  });

  it('builds toolsets whose parameter schemas forbid undeclared arguments', () => {
    const deps = {
      platform: new PlatformClient({ internalUrl: 'http://127.0.0.1:8000', sharedSecret: 'x'.repeat(32), timeoutMs: 1000 }),
      policy: loadPolicy('definitely-missing-policy.json'),
    };
    const { tools } = buildToolset(deps);
    expect(tools).toHaveLength(12);
    for (const tool of tools) {
      expect(tool.parameters.type).toBe('object');
      expect(tool.parameters.additionalProperties).toBe(false);
      expect(typeof tool.execute).toBe('function');
    }
  });

  it('reports new upstream tools as default-off (never auto-registrable)', () => {
    const check = checkUpstreamToolList(['bash', 'read_file', 'brand_new_tool']);
    expect(check.added).toEqual(['brand_new_tool']);
    expect(check.leaked).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Upstream-facing contract
// ---------------------------------------------------------------------------

describe.skipIf(core === null)('upstream SDK conformance', () => {
  it('exports every frozen symbol (removal or rename fails the verification)', () => {
    const actual = new Set(Object.keys(core ?? {}));
    const missing = EXPECTED_EXPORTS.filter((name) => !actual.has(name));
    expect(missing, `removed/renamed public exports: ${missing.join(', ')}`).toEqual([]);
  });

  it('reports added exports as needs_adapter_review without failing', () => {
    const actual = Object.keys(core ?? {}).sort();
    const expected = new Set(EXPECTED_EXPORTS);
    const added = actual.filter((name) => !expected.has(name));
    if (added.length > 0) {
      // eslint-disable-next-line no-console
      console.warn(
        `[conformance] needs_adapter_review: upstream added ${added.length} export(s) — ` +
          `${added.join(', ')}. Record this in engine_candidates.verification_report.`,
      );
    }
    // An addition is not a failure: it cannot widen the platform tool surface (§12.4-3).
    expect(actual.length).toBeGreaterThanOrEqual(EXPECTED_EXPORTS.length);
  });

  it('keeps the adapter dependency allowlist present', () => {
    const actual = new Set(Object.keys(core ?? {}));
    for (const name of ADAPTER_DEPENDENCY_ALLOWLIST) {
      expect(actual.has(name), `adapter depends on ${name}`).toBe(true);
    }
  });

  it('keeps Agent and AragonAgent the same class', () => {
    const module = core as CoreModule & { Agent?: unknown };
    expect(module.AragonAgent).toBe(module.Agent);
  });

  it('accepts all 12 platform tools through defineTool()', () => {
    const module = core as CoreModule;
    const deps = {
      platform: new PlatformClient({ internalUrl: 'http://127.0.0.1:8000', sharedSecret: 'x'.repeat(32), timeoutMs: 1000 }),
      policy: loadPolicy('definitely-missing-policy.json'),
    };
    const { tools } = buildToolset(deps);
    const defined = defineWithHost(module, tools);
    expect(defined).toHaveLength(12);
    for (let i = 0; i < defined.length; i += 1) {
      expect(defined[i]?.name).toBe(tools[i]?.name);
      expect(defined[i]?.parameters).toEqual(tools[i]?.parameters);
    }
  });

  it('constructs an Agent with the platform tool array and exposes the documented API', async () => {
    const module = core as CoreModule;
    const deps = {
      platform: new PlatformClient({ internalUrl: 'http://127.0.0.1:8000', sharedSecret: 'x'.repeat(32), timeoutMs: 1000 }),
      policy: loadPolicy('definitely-missing-policy.json'),
    };
    const { tools } = buildToolset(deps);
    const registry = module.initProviders();
    const agent = new module.AragonAgent({
      systemPrompt: 'conformance check',
      model: { providerId: 'anthropic', modelId: 'conformance-no-network' },
      tools: defineWithHost(module, tools),
      providerRegistry: registry,
      getApiKey: () => undefined,
      thinkingLevel: 'off',
    });

    // The adapter uses exactly these members (engine.ts); assert they all exist.
    for (const method of ['prompt', 'continue', 'abort', 'waitForIdle', 'subscribe', 'steer', 'setModel', 'setSystemPrompt', 'setTools', 'replaceMessages'] as const) {
      expect(typeof (agent as unknown as Record<string, unknown>)[method], `${method}() is part of the adapter contract`).toBe('function');
    }
    expect(agent.state.tools).toHaveLength(12);
    expect(agent.state.isRunning).toBe(false);

    const unsubscribe = agent.subscribe(() => undefined);
    expect(typeof unsubscribe).toBe('function');
    unsubscribe();
    agent.abort();
    await agent.waitForIdle();
  });
});
