'use client';

import * as React from 'react';
import {
  useTableState as useBaseTableState,
  type UseTableStateOptions,
  type UseTableStateResult,
} from '@/components/common/DataTable';

/**
 * 列表状态（spec §A.3.11 / §13.5）——在 `DataTable` 的通用实现上加三件事：
 *
 * 1. `sortWhitelist`：排序字段白名单。后端对 `sort` 做白名单校验（越界 → 400），
 *    前端只允许发出后端真的支持的字段——宁可不可排序，也不要「点了表头但顺序没变」的假排序。
 * 2. `dateKeys`：`date_from` / `date_to` 命名的时间筛选。`<input type="datetime-local">`
 *    给出的是**本地无时区**字符串，而后端按 ISO8601 字符串比较 `created_at`，
 *    因此这里统一规范成 `YYYY-MM-DDTHH:mm:ssZ`（`_to` 键取当日 23:59:59Z）。
 * 3. `booleanKeys`：`needs_reconcile=1` 这类布尔筛选，查询串用 `1/0`，请求时转成真正的布尔。
 *
 * URL 状态同步、300ms 防抖、分页与筛选 chip 的交互全部沿用 `DataTable` 的实现（单一来源）。
 */

export interface UseTableStateExtOptions extends UseTableStateOptions {
  /** 允许排序的字段（不带 `-` 前缀）；空数组 = 服务端不需要排序 */
  sortWhitelist?: readonly string[];
  /** 时间筛选键（如 `['date_from', 'date_to']`），请求前规范成 ISO8601 UTC */
  dateKeys?: readonly string[];
  /** 布尔筛选键（如 `['needs_reconcile']`），请求前由 `'1'/'0'` 转成 `true/false` */
  booleanKeys?: readonly string[];
  /** `defaultSort` 不在白名单时使用的兜底排序（默认 `''`） */
  fallbackSort?: string;
}

export type UseTableStateExtResult = UseTableStateResult;

const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;

/** 本地时间 → 后端可比较的 ISO8601（秒级 UTC）；无法解析时原样返回，交给后端报 400。 */
export function toIsoInstant(value: string, endOfDay = false): string {
  if (!value) return '';
  if (DATE_ONLY.test(value)) {
    return `${value}T${endOfDay ? '23:59:59' : '00:00:00'}Z`;
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toISOString().replace(/\.\d{3}Z$/, 'Z');
}

/** 白名单过滤排序表达式：保留合法字段，丢弃越界字段（含 `-` 前缀形式）。 */
export function normalizeSort(sort: string, whitelist?: readonly string[]): string {
  if (!whitelist) return sort;
  const tokens = sort
    .split(',')
    .map((token) => token.trim())
    .filter(Boolean);
  const kept = tokens.filter((token) => whitelist.includes(token.replace(/^[-+]/, '')));
  return kept.join(',');
}

export function useTableState({
  sortWhitelist,
  dateKeys = [],
  booleanKeys = [],
  fallbackSort = '',
  defaultSort,
  ...rest
}: UseTableStateExtOptions): UseTableStateExtResult {
  const whitelistSig = sortWhitelist ? sortWhitelist.join('|') : '';
  const dateSig = dateKeys.join('|');
  const boolSig = booleanKeys.join('|');

  const whitelist = React.useMemo(
    () => (whitelistSig ? whitelistSig.split('|') : undefined),
    [whitelistSig],
  );
  const dateKeyList = React.useMemo(() => (dateSig ? dateSig.split('|') : []), [dateSig]);
  const boolKeyList = React.useMemo(() => (boolSig ? boolSig.split('|') : []), [boolSig]);

  const safeDefaultSort = React.useMemo(() => {
    const normalized = normalizeSort(defaultSort, whitelist);
    if (whitelist && whitelist.length > 0 && !normalized) {
      return normalizeSort(fallbackSort, whitelist) || defaultSort;
    }
    return normalized || defaultSort;
  }, [defaultSort, fallbackSort, whitelist]);

  const base = useBaseTableState({ ...rest, defaultSort: safeDefaultSort });

  const setSort = React.useCallback(
    (sort: string) => base.setSort(normalizeSort(sort, whitelist)),
    [base, whitelist],
  );

  const requestParams = React.useMemo(() => {
    const params: Record<string, string | number | boolean | undefined> = { ...base.requestParams };
    for (const key of dateKeyList) {
      const raw = params[key];
      if (typeof raw === 'string' && raw) {
        params[key] = toIsoInstant(raw, key.endsWith('_to'));
      }
    }
    for (const key of boolKeyList) {
      const raw = params[key];
      if (raw === '1' || raw === 'true') params[key] = true;
      else if (raw === '0' || raw === 'false') params[key] = false;
      else delete params[key];
    }
    return params as UseTableStateExtResult['requestParams'];
  }, [base.requestParams, dateKeyList, boolKeyList]);

  return { ...base, setSort, requestParams };
}
