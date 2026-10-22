/**
 * Platform tool registration — THE PERMISSION CEILING.
 *
 * This file is the single place where the agent's capabilities are decided, and
 * the decision is an allowlist, not a denylist:
 *
 *   * ONLY the canonical names in `PLATFORM_TOOL_NAMES` can ever be registered.
 *     A session-create request that asks for anything else is rejected outright
 *     (400) before an Agent is constructed. That rejection is deliberate: the
 *     engine does not register a denied tool, so the model cannot see it or plan
 *     around it — a silently dropped tool would instead teach the model to try
 *     and fail.
 *   * `bash`, `read_file`, `write_file`, `edit_file`, `grep`, `glob`, web tools
 *     and the CodeAct sandbox are not merely unregistered — they are unnameable
 *     here. There is no code path that could add them, which is why the promise
 *     survives future edits to the request schema.
 *   * Every allowed tool is a thin HTTP POST back to the platform with a
 *     short-lived, run-scoped token. The engine never receives a platform
 *     credential, a database handle or a filesystem path, so a prompt-injected
 *     instruction inside a tool result has nothing to reach for: the worst it can
 *     do is ask the platform for another allowed tool call, which the platform
 *     audits and budget-checks.
 *
 * WIRE NAMES. Provider APIs only accept tool names matching `[A-Za-z0-9_-]{1,64}`
 * (Anthropic rejects a dot with a 400), while the platform's canonical names are
 * dotted (`platform.list_providers`). So the model sees
 * `platform_list_providers` and the callback carries the canonical dotted name.
 * The mapping is mechanical and asserted in `test/tools.test.mjs`.
 */

import { defineTool, textResult, errorResult, type AgentTool } from "@aragon-agent/core";

/** The complete set of tools the engine may ever hold. */
export const PLATFORM_TOOL_NAMES = [
  "platform.list_providers",
  "platform.list_credentials",
  "platform.list_capabilities",
  "platform.get_capability",
  "platform.create_capability_draft",
  "platform.update_capability_draft",
  "platform.validate_capability",
  "platform.test_capability",
  "platform.publish_capability",
  "platform.list_skills",
  "platform.save_skill_draft",
  "platform.get_test_evidence",
] as const;

export type PlatformToolName = (typeof PLATFORM_TOOL_NAMES)[number];

const ALLOWED = new Set<string>(PLATFORM_TOOL_NAMES);

/** Names that must never be reachable, listed so the refusal can quote them. */
export const FORBIDDEN_TOOL_HINTS = [
  "bash",
  "shell",
  "exec",
  "read_file",
  "write_file",
  "edit_file",
  "grep",
  "glob",
  "list_dir",
  "web_fetch",
  "web_search",
  "task",
  "code_exec",
] as const;

export interface PlatformToolSpec {
  /** Canonical dotted name, or its underscore spelling. */
  name: string;
  description?: string;
  /** JSON Schema for the arguments. The platform owns the real validation. */
  parameters?: Record<string, unknown>;
  label?: string;
}

export interface ToolDispatchRecord {
  tool: string;
  tool_call_id: string;
  /** Epoch ms the HTTP POST left this process. */
  dispatched_at: number;
  /** Epoch ms the platform answered (or the request failed locally). */
  completed_at: number | null;
  ok: boolean | null;
}

/**
 * Accept either spelling and return the canonical dotted name, or null when the
 * name is not in the ceiling. `platform_list_providers` and
 * `platform.list_providers` are the same tool; `bash` is not a tool at all.
 */
export function canonicalToolName(name: string): PlatformToolName | null {
  const trimmed = (name ?? "").trim();
  if (trimmed === "") return null;
  if (ALLOWED.has(trimmed)) return trimmed as PlatformToolName;
  // Provider-safe spelling of a dotted name: only the NAMESPACE separator is
  // replaced (`platform_list_providers`), because the tool's own words are
  // themselves underscored. Replacing every underscore would turn
  // `platform.save_skill_draft` into `platform.save.skill.draft`, which matches
  // nothing — a bug that silently made every multi-word tool unnameable.
  const separator = trimmed.indexOf("_");
  if (separator > 0) {
    const candidate = `${trimmed.slice(0, separator)}.${trimmed.slice(separator + 1)}`;
    if (ALLOWED.has(candidate)) return candidate as PlatformToolName;
  }
  return null;
}

/** Canonical dotted name → the provider-safe tool name the model sees. */
export function wireToolName(canonical: string): string {
  return canonical.replace(/\./g, "_");
}

export interface ToolBuildResult {
  tools: AgentTool[];
  /** canonical name → wire name, echoed on /info and in the run record. */
  mapping: Record<string, string>;
}

export class ToolCeilingError extends Error {
  constructor(
    message: string,
    readonly rejected: string[],
  ) {
    super(message);
    this.name = "ToolCeilingError";
  }
}

export interface ToolDispatcherOptions {
  /** Platform base URL, e.g. http://127.0.0.1:8000 */
  baseUrl: string;
  /** Run-scoped, short-lived bearer token minted by the platform for this run. */
  token: string;
  sessionId: string;
  runId: string;
  timeoutMs: number;
}

/**
 * Builds the AgentTool list for one run and records every dispatch.
 *
 * The dispatch log is what makes `POST /sessions/{id}/stop` honest: a tool call
 * whose POST is already at the platform *may still complete there* even after the
 * local loop aborts, and reporting it as "stopped" would be a lie the operator
 * would discover by finding an invoked capability they thought they cancelled.
 */
export class ToolDispatcher {
  private readonly records = new Map<string, ToolDispatchRecord>();
  private readonly order: string[] = [];

  constructor(private readonly options: ToolDispatcherOptions) {}

  build(specs: PlatformToolSpec[]): ToolBuildResult {
    const rejected: string[] = [];
    const seen = new Set<string>();
    const mapping: Record<string, string> = {};
    const tools: AgentTool[] = [];

    for (const spec of specs) {
      const canonical = canonicalToolName(spec.name);
      if (canonical === null) {
        rejected.push(String(spec.name ?? "<missing>"));
        continue;
      }
      if (seen.has(canonical)) continue;
      seen.add(canonical);
      const wire = wireToolName(canonical);
      mapping[canonical] = wire;
      tools.push(this.buildOne(canonical, wire, spec));
    }

    if (rejected.length > 0) {
      const forbidden = rejected.filter((name) =>
        FORBIDDEN_TOOL_HINTS.some((hint) => name.toLowerCase().includes(hint)),
      );
      const extra =
        forbidden.length > 0
          ? ` Requests for shell, file, code-execution or web tools are always refused: ` +
            `${forbidden.join(", ")}. Register the work as a platform capability instead.`
          : "";
      throw new ToolCeilingError(
        `Tool(s) not in the platform permission ceiling: ${rejected.join(", ")}. ` +
          `Allowed names: ${PLATFORM_TOOL_NAMES.join(", ")}.${extra}`,
        rejected,
      );
    }

    return { tools, mapping };
  }

  private buildOne(canonical: PlatformToolName, wire: string, spec: PlatformToolSpec): AgentTool {
    const label = spec.label ?? canonical.split(".").slice(1).join(" ").replace(/_/g, " ");
    const description =
      spec.description ??
      `Call the Skill Router platform tool ${canonical}. Returns the platform's JSON result as text.`;
    const parameters =
      (spec.parameters as never) ??
      ({ type: "object", properties: {}, additionalProperties: true } as never);
    // `execute` is called by the engine detached from this object, so the
    // dispatcher is captured explicitly rather than reached through `this`.
    const dispatcher = this;

    return defineTool({
      name: wire,
      label,
      description,
      parameters,
      tags: ["platform"],
      async execute(toolCallId: string, params: Record<string, unknown>, context) {
        return dispatcher.dispatch(canonical, toolCallId, params, context.signal);
      },
    }) as AgentTool;
  }

  private async dispatch(
    canonical: PlatformToolName,
    toolCallId: string,
    params: Record<string, unknown>,
    signal: AbortSignal | undefined,
  ): Promise<ReturnType<typeof textResult>> {
    const record: ToolDispatchRecord = {
      tool: canonical,
      tool_call_id: toolCallId,
      dispatched_at: Date.now(),
      completed_at: null,
      ok: null,
    };
    this.records.set(toolCallId, record);
    this.order.push(toolCallId);

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(new Error("tool timeout")), this.options.timeoutMs);
    const onAbort = () => controller.abort(new Error("run aborted"));
    if (signal) {
      if (signal.aborted) onAbort();
      else signal.addEventListener("abort", onAbort, { once: true });
    }

    const url = `${this.options.baseUrl.replace(/\/+$/, "")}/api/admin/agent/tools/${canonical}`;
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          authorization: `Bearer ${this.options.token}`,
          accept: "application/json",
        },
        body: JSON.stringify({
          run_id: this.options.runId,
          session_id: this.options.sessionId,
          tool: canonical,
          tool_call_id: toolCallId,
          arguments: params ?? {},
        }),
        signal: controller.signal,
      });

      const raw = await response.text();
      let payload: Record<string, unknown> = {};
      try {
        payload = raw ? (JSON.parse(raw) as Record<string, unknown>) : {};
      } catch {
        payload = { ok: false, message: `platform returned non-JSON (${response.status}): ${raw.slice(0, 400)}` };
      }

      const ok = response.ok && payload.ok !== false;
      record.completed_at = Date.now();
      record.ok = ok;

      if (!ok) {
        const code = typeof payload.code === "string" ? payload.code : `http_${response.status}`;
        const message = typeof payload.message === "string" ? payload.message : raw.slice(0, 400);
        const actions = Array.isArray(payload.actions) ? payload.actions.filter((a) => typeof a === "string") : [];
        const advice = actions.length > 0 ? ` Next: ${actions.join(" ")}` : "";
        return errorResult(`${code}: ${message}${advice}`) as never;
      }

      const text =
        typeof payload.text === "string" && payload.text.length > 0
          ? payload.text
          : JSON.stringify(payload.structured ?? payload.result ?? payload, null, 2);
      return textResult(text) as never;
    } catch (error) {
      record.completed_at = Date.now();
      record.ok = false;
      const reason = error instanceof Error ? error.message : String(error);
      const aborted = signal?.aborted || controller.signal.aborted;
      return errorResult(
        aborted
          ? `platform tool ${canonical} was interrupted before the platform answered. ` +
              `It may still have run server-side; check the invocation records before retrying.`
          : `platform tool ${canonical} could not reach the platform at ${url}: ${reason}`,
      ) as never;
    } finally {
      clearTimeout(timer);
      if (signal) signal.removeEventListener("abort", onAbort);
    }
  }

  /**
   * Split dispatches into "finished" and "may still complete upstream".
   *
   * A call is "may still complete" when the POST was sent but no answer was
   * observed. Calls that already returned successfully are done — including any
   * that returned *after* the stop request, which is precisely the case a naive
   * implementation would hide.
   */
  report(sinceMs: number | null): {
    in_flight: ToolDispatchRecord[];
    completed_after_stop: ToolDispatchRecord[];
  } {
    const inFlight: ToolDispatchRecord[] = [];
    const completedAfter: ToolDispatchRecord[] = [];
    for (const id of this.order) {
      const record = this.records.get(id);
      if (!record) continue;
      if (record.completed_at === null) {
        inFlight.push({ ...record });
      } else if (sinceMs !== null && record.completed_at >= sinceMs) {
        completedAfter.push({ ...record });
      }
    }
    return { in_flight: inFlight, completed_after_stop: completedAfter };
  }

  snapshot(): ToolDispatchRecord[] {
    return this.order
      .map((id) => this.records.get(id))
      .filter((record): record is ToolDispatchRecord => Boolean(record))
      .map((record) => ({ ...record }));
  }
}
