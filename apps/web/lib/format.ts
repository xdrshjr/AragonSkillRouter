/**
 * 统一中文格式化（时间/耗时/字节/费用/比例/相对时间）。
 * 所有页面复用，避免各处文案不一致。
 */

const MS = 1000;
const MINUTE = 60 * MS;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

const pad = (n: number): string => String(n).padStart(2, '0');

function toDate(value: string | number | Date | null | undefined): Date | null {
  if (value === null || value === undefined || value === '') return null;
  const d = value instanceof Date ? value : new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** 2026-10-18 13:41 */
export function formatDateTime(value: string | number | Date | null | undefined): string {
  const d = toDate(value);
  if (!d) return '—';
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 2026-10-18 13:41:25 */
export function formatDateTimeSec(value: string | number | Date | null | undefined): string {
  const d = toDate(value);
  if (!d) return '—';
  return `${formatDateTime(d)}:${pad(d.getSeconds())}`;
}

/** 10-18 13:41（表格紧凑） */
export function formatShortTime(value: string | number | Date | null | undefined): string {
  const d = toDate(value);
  if (!d) return '—';
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 2026-10-18 */
export function formatDate(value: string | number | Date | null | undefined): string {
  const d = toDate(value);
  if (!d) return '—';
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** 3 分钟前 / 2 天后 */
export function formatRelative(
  value: string | number | Date | null | undefined,
  now: Date = new Date(),
): string {
  const d = toDate(value);
  if (!d) return '—';
  const diff = d.getTime() - now.getTime();
  const abs = Math.abs(diff);
  const future = diff > 0;
  const suffix = future ? '后' : '前';

  if (abs < 45 * MS) return '刚刚';
  if (abs < HOUR) {
    const m = Math.round(abs / MINUTE);
    return `${m} 分钟${suffix}`;
  }
  if (abs < DAY) {
    const h = Math.round(abs / HOUR);
    return `${h} 小时${suffix}`;
  }
  if (abs < 30 * DAY) {
    const dy = Math.round(abs / DAY);
    return `${dy} 天${suffix}`;
  }
  return formatDate(d);
}

/** 740ms / 1.2s / 2 分 3 秒 */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return '—';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60 * MS) {
    const s = ms / MS;
    return `${s < 10 ? s.toFixed(2) : s.toFixed(1)}s`;
  }
  if (ms < HOUR) {
    const total = Math.round(ms / MS);
    const m = Math.floor(total / 60);
    const s = total % 60;
    return s === 0 ? `${m} 分` : `${m} 分 ${s} 秒`;
  }
  const totalMin = Math.round(ms / MINUTE);
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return m === 0 ? `${h} 小时` : `${h} 小时 ${m} 分`;
}

/** 1.4 KB / 18.4 MB */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return '—';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'] as const;
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  const unit = units[unitIndex] ?? 'KB';
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${unit}`;
}

/** $0.0004 / $1.20；0 显示 $0 */
export function formatCost(usd: number | null | undefined): string {
  if (usd === null || usd === undefined || Number.isNaN(usd)) return '—';
  if (usd === 0) return '$0';
  if (usd < 0.01) return `$${usd.toFixed(4)}`;
  if (usd < 1) return `$${usd.toFixed(3)}`;
  return `$${usd.toFixed(2)}`;
}

/** 0.982 → 98.2% */
export function formatPercent(ratio: number | null | undefined, digits = 1): string {
  if (ratio === null || ratio === undefined || Number.isNaN(ratio)) return '—';
  return `${(ratio * 100).toFixed(digits)}%`;
}

/** 18324 → 1.8 万（指标卡用；阈值以下原样） */
export function formatCount(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n) || !Number.isFinite(n)) return '—';
  const abs = Math.abs(n);
  if (abs < 10_000) return n.toLocaleString('zh-CN');
  if (abs < 100_000_000) {
    const v = n / 10_000;
    return `${(abs < 100_000 ? v.toFixed(1) : Math.round(v)).toString().replace(/\.0$/, '')} 万`;
  }
  return `${(n / 100_000_000).toFixed(1).replace(/\.0$/, '')} 亿`;
}

/** 状态文本（中文） */
const STATUS_LABELS: Record<string, string> = {
  draft: '草稿',
  published: '已发布',
  deprecated: '已停用',
  failing: '异常',
  active: '启用',
  disabled: '停用',
  expired: '已过期',
  queued: '排队中',
  running: '进行中',
  succeeded: '成功',
  success: '成功',
  failed: '失败',
  canceling: '取消中',
  canceled: '已取消',
  status_unknown: '状态未知',
  stopping: '停止中',
  stopped: '已停止',
  pending: '待处理',
  passed: '通过',
  skipped: '已跳过',
  waiting_approval: '待审批',
  approved: '已批准',
  rejected: '已拒绝',
  available: '已可用',
  degraded: '降级',
  unavailable: '不可用',
  needs_adapter: '待适配',
  permission_limited: '受权限限制',
  unsupported: '不支持',
  verify_only: '仅验证',
  deploy: '部署',
  manual: '手动',
  auto: '自动',
  pinned: '固定版本',
  production: 'production',
  staging: 'staging',
};

export function statusLabel(status: string | null | undefined): string {
  if (!status) return '—';
  return STATUS_LABELS[status] ?? status;
}

/** 截断（用于预览文本） */
export function truncate(text: string | null | undefined, max = 120): string {
  if (!text) return '';
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

/** JSON 美化（失败时原样返回） */
export function prettyJson(value: unknown, maxChars = 20_000): string {
  try {
    const text = JSON.stringify(value, null, 2);
    if (!text) return '';
    return text.length > maxChars ? `${text.slice(0, maxChars)}\n…（已截断）` : text;
  } catch {
    return String(value);
  }
}

/** 复制到剪贴板（含非安全上下文的降级） */
export async function copyText(text: string): Promise<boolean> {
  if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      /* 降级 */
    }
  }
  if (typeof document === 'undefined') return false;
  try {
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.top = '-1000px';
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(area);
    return ok;
  } catch {
    return false;
  }
}

/** 数字或占位符 */
export function orDash(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—';
  return String(value);
}
