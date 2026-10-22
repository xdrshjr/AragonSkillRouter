export interface JsonParseSuccess {
  ok: true;
  value: unknown;
}

export interface JsonParseFailure {
  ok: false;
  error: string;
}

export type JsonParseResult = JsonParseSuccess | JsonParseFailure;

/** Parse JSON text, returning a readable error instead of throwing. */
export function parseJson(text: string): JsonParseResult {
  const trimmed = text.trim();
  if (!trimmed) return { ok: true, value: undefined };
  try {
    return { ok: true, value: JSON.parse(trimmed) };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : 'Invalid JSON.' };
  }
}

/** Parse JSON text that must be an object (capability config, test input). */
export function parseJsonObject(text: string): { ok: true; value: Record<string, unknown> } | JsonParseFailure {
  const parsed = parseJson(text);
  if (!parsed.ok) return parsed;
  if (parsed.value === undefined) return { ok: true, value: {} };
  if (parsed.value === null || typeof parsed.value !== 'object' || Array.isArray(parsed.value)) {
    return { ok: false, error: 'Expected a JSON object ({ … }).' };
  }
  return { ok: true, value: parsed.value as Record<string, unknown> };
}

export function formatJsonText(value: unknown): string {
  if (value === undefined) return '';
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Structural equality via canonical JSON — used for dirty tracking. */
export function jsonEqual(a: unknown, b: unknown): boolean {
  try {
    return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
  } catch {
    return false;
  }
}
