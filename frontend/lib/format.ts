/**
 * Presentation helpers. Everything is deterministic: timestamps are formatted in
 * UTC so server-rendered markup matches the client's first paint. Relative times
 * are computed by `<TimeAgo>` after mount, never during render.
 */

export type StatusTone = 'neutral' | 'accent' | 'success' | 'warning' | 'danger' | 'info';

const LABELS: Record<string, string> = {
  // capability / version
  draft: 'Draft',
  testing: 'Testing',
  published: 'Published',
  disabled: 'Disabled',
  deprecated: 'Deprecated',
  // credential
  active: 'Active',
  expired: 'Expired',
  // verification
  valid: 'Valid',
  invalid: 'Invalid',
  unreachable: 'Unreachable',
  rate_limited: 'Rate limited',
  unknown: 'Unknown',
  // route health
  healthy: 'Healthy',
  degraded: 'Degraded',
  unhealthy: 'Unhealthy',
  // invocation / task
  queued: 'Queued',
  running: 'Running',
  succeeded: 'Succeeded',
  failed: 'Failed',
  cancelled: 'Cancelled',
  cancel_requested: 'Cancel requested',
  uncertain: 'Uncertain',
  rejected: 'Rejected',
  // tests / engine / imports
  passed: 'Passed',
  blocked: 'Blocked',
  preview: 'Preview',
  pending: 'Pending',
  verified: 'Verified',
  imported: 'Imported',
  detected: 'Detected',
  validating: 'Validating',
  pending_approval: 'Pending approval',
  rolled_back: 'Rolled back',
  superseded: 'Superseded',
  not_configured: 'Not configured',
  // modes / env
  sync: 'Sync',
  async: 'Async',
  stream: 'Stream',
  production: 'Production',
  staging: 'Staging',
  development: 'Development',
  // severities
  error: 'Error',
  warning: 'Warning',
  info: 'Info',
  // provider categories / capability types
  llm: 'LLM',
  image: 'Image',
  video: 'Video',
  speech: 'Speech',
  transcription: 'Transcription',
  generic: 'Generic',
  // strategies / auth types
  priority: 'Priority',
  fixed: 'Fixed',
  health_first: 'Health first',
  api_key: 'API key',
  bearer: 'Bearer token',
  basic: 'Basic auth',
  custom_header: 'Custom header',
  // encodings
  json: 'JSON',
  form: 'Form',
  multipart: 'Multipart',
  none: 'None',
  base64: 'Base64',
  url: 'URL',
  sse: 'SSE',
  ndjson: 'NDJSON',
  // invocation surfaces / statuses
  mcp: 'MCP',
  http: 'HTTP',
  admin: 'Admin',
};

const TONES: Record<string, StatusTone> = {
  active: 'success',
  valid: 'success',
  healthy: 'success',
  succeeded: 'success',
  published: 'success',
  passed: 'success',
  verified: 'success',
  imported: 'success',
  detected: 'accent',
  draft: 'neutral',
  unknown: 'neutral',
  cancelled: 'neutral',
  disabled: 'neutral',
  deprecated: 'neutral',
  superseded: 'neutral',
  rolled_back: 'neutral',
  none: 'neutral',
  testing: 'accent',
  running: 'accent',
  queued: 'accent',
  validating: 'accent',
  pending: 'warning',
  degraded: 'warning',
  rate_limited: 'warning',
  uncertain: 'warning',
  cancel_requested: 'warning',
  blocked: 'warning',
  pending_approval: 'warning',
  not_configured: 'warning',
  warning: 'warning',
  invalid: 'danger',
  unhealthy: 'danger',
  failed: 'danger',
  rejected: 'danger',
  expired: 'danger',
  error: 'danger',
  unreachable: 'danger',
  info: 'info',
  preview: 'info',
};

/** Human label for any status string from CONTRACT §4. */
export function statusLabel(value: string | null | undefined): string {
  if (!value) return 'Unknown';
  const known = LABELS[value];
  if (known) return known;
  return value
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
}

/** Badge tone for any status string from CONTRACT §4. */
export function statusTone(value: string | null | undefined): StatusTone {
  if (!value) return 'neutral';
  return TONES[value] ?? 'neutral';
}

export function severityTone(severity: string): StatusTone {
  if (severity === 'error') return 'danger';
  if (severity === 'warning') return 'warning';
  return 'info';
}

const UTC_DATE = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'UTC',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
});

const UTC_DATE_SHORT = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'UTC',
  month: 'short',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
});

function parse(value: string | null | undefined): Date | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** `2026-10-08 14:02 UTC` — stable between server and client. */
export function formatDateTime(value: string | null | undefined): string {
  const date = parse(value);
  if (!date) return '—';
  return `${UTC_DATE.format(date)} UTC`;
}

/** `Oct 08, 14:02 UTC` — compact form for dense tables. */
export function formatDateTimeShort(value: string | null | undefined): string {
  const date = parse(value);
  if (!date) return '—';
  return `${UTC_DATE_SHORT.format(date)} UTC`;
}

/** Absolute ISO date without a clock (`2026-10-08`). */
export function formatDate(value: string | null | undefined): string {
  const date = parse(value);
  if (!date) return '—';
  return date.toISOString().slice(0, 10);
}

const RELATIVE_UNITS: Array<[Intl.RelativeTimeFormatUnit, number]> = [
  ['year', 365 * 24 * 3600],
  ['month', 30 * 24 * 3600],
  ['day', 24 * 3600],
  ['hour', 3600],
  ['minute', 60],
  ['second', 1],
];

/** `4 minutes ago` / `in 3 days`. Only call this after mount. */
export function formatRelativeTime(value: string | null | undefined, now: number = Date.now()): string {
  const date = parse(value);
  if (!date) return '—';
  const deltaSeconds = Math.round((date.getTime() - now) / 1000);
  const abs = Math.abs(deltaSeconds);
  if (abs < 5) return 'just now';
  const formatter = new Intl.RelativeTimeFormat('en', { numeric: 'auto' });
  for (const [unit, size] of RELATIVE_UNITS) {
    if (abs >= size || unit === 'second') {
      return formatter.format(Math.round(deltaSeconds / size), unit);
    }
  }
  return 'just now';
}

/** Latency / test duration in milliseconds. */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return '—';
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return `${minutes}m ${seconds}s`;
}

export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return new Intl.NumberFormat('en-US').format(value);
}

/** `0.9713 → 97.1%`; `null` stays an honest em dash instead of 0%. */
export function formatPercent(ratio: number | null | undefined, digits = 1): string {
  if (ratio === null || ratio === undefined || Number.isNaN(ratio)) return '—';
  return `${(ratio * 100).toFixed(digits)}%`;
}

export function formatCurrency(amount: number | null | undefined, currency = 'USD'): string {
  if (amount === null || amount === undefined || Number.isNaN(amount)) return '—';
  const digits = Math.abs(amount) > 0 && Math.abs(amount) < 0.01 ? 6 : 2;
  return new Intl.NumberFormat('en-US', { style: 'currency', currency, maximumFractionDigits: digits }).format(amount);
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = Math.abs(bytes);
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  const sign = bytes < 0 ? '-' : '';
  return `${sign}${value.toFixed(value >= 10 || index === 0 ? 0 : 1)} ${units[index]}`;
}

/** Trim long identifiers for table cells without hiding the tail. */
export function truncateMiddle(value: string, head = 10, tail = 6): string {
  if (value.length <= head + tail + 1) return value;
  return `${value.slice(0, head)}…${value.slice(-tail)}`;
}

export function pluralize(count: number, singular: string, plural?: string): string {
  return count === 1 ? singular : (plural ?? `${singular}s`);
}

/** Render an arbitrary API value as a short, safe one-liner. */
export function previewValue(value: unknown, maxLength = 120): string {
  if (value === null || value === undefined) return '—';
  let text: string;
  if (typeof value === 'string') text = value;
  else if (typeof value === 'number' || typeof value === 'boolean') text = String(value);
  else {
    try {
      text = JSON.stringify(value);
    } catch {
      text = String(value);
    }
  }
  if (text.length <= maxLength) return text;
  return `${text.slice(0, maxLength)}…`;
}

/** `2026-10-08T14:02:11.123Z` → `2026-10-08T14:02` for `datetime-local` inputs. */
export function toDateTimeLocalValue(iso: string | null | undefined): string {
  const date = parse(iso);
  if (!date) return '';
  return date.toISOString().slice(0, 16);
}

/** `datetime-local` value → ISO-8601 UTC, or `null` when empty/invalid. */
export function fromDateTimeLocalValue(value: string | undefined | null): string | null {
  if (!value) return null;
  const date = new Date(`${value}Z`);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}
