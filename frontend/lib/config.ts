export type ConfigPath = string;

/** Immutable deep set for `a.b.c` paths (objects only; arrays are handled by callers). */
export function setConfigPath<T extends Record<string, unknown>>(source: T, path: ConfigPath, value: unknown): T {
  const keys = path.split('.');

  const build = (node: unknown, index: number): Record<string, unknown> => {
    const container: Record<string, unknown> =
      node && typeof node === 'object' && !Array.isArray(node) ? { ...(node as Record<string, unknown>) } : {};
    const key = keys[index];
    if (!key) return container;
    if (index === keys.length - 1) {
      if (value === undefined) delete container[key];
      else container[key] = value;
      return container;
    }
    container[key] = build(container[key], index + 1);
    return container;
  };

  return build(source, 0) as T;
}

export function getConfigPath(source: unknown, path: ConfigPath): unknown {
  let node: unknown = source;
  for (const key of path.split('.')) {
    if (!node || typeof node !== 'object') return undefined;
    node = (node as Record<string, unknown>)[key];
  }
  return node;
}

export function configString(source: unknown, path: ConfigPath): string {
  const value = getConfigPath(source, path);
  if (value === undefined || value === null) return '';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return '';
  }
}

export function configNumber(source: unknown, path: ConfigPath): number | null {
  const value = getConfigPath(source, path);
  if (typeof value === 'number' && !Number.isNaN(value)) return value;
  if (typeof value === 'string' && value.trim() !== '' && !Number.isNaN(Number(value))) return Number(value);
  return null;
}

export function configBoolean(source: unknown, path: ConfigPath): boolean {
  return Boolean(getConfigPath(source, path));
}

export function configRecord(source: unknown, path: ConfigPath): Record<string, string> {
  const value = getConfigPath(source, path);
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  const out: Record<string, string> = {};
  for (const [key, entry] of Object.entries(value as Record<string, unknown>)) {
    out[key] = typeof entry === 'string' ? entry : JSON.stringify(entry);
  }
  return out;
}

export function configArray<T>(source: unknown, path: ConfigPath): T[] {
  const value = getConfigPath(source, path);
  return Array.isArray(value) ? (value as T[]) : [];
}

/** Parse a numeric input, keeping `null` for an empty field. */
export function numberOrNull(value: string): number | null {
  if (value.trim() === '') return null;
  const parsed = Number(value);
  return Number.isNaN(parsed) ? null : parsed;
}

/** Split comma/newline separated text into a trimmed, de-duplicated list. */
export function splitList(value: string): string[] {
  return Array.from(
    new Set(
      value
        .split(/[,\n]/)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  );
}
