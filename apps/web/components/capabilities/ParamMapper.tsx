'use client';

import { Plus, Trash2 } from 'lucide-react';
import * as React from 'react';
import { Button } from '@/components/ui/button';
import { cn } from '@/components/ui/cn';
import { Select } from '@/components/ui/select';
import type { Json, ParamMapEntry, ParamTransform } from '@/lib/types';

/**
 * 参数映射可视化编辑（spec §8.4 / §7.6-5）。
 * 每行 = 上游参数名 ← 来源（input.* / route.*）+ 转换 + 默认值。
 * 来源命名空间受限于 input_schema.properties 与该 route 的声明字段。
 */

const TRANSFORMS: { value: ParamTransform; label: string }[] = [
  { value: 'passthrough', label: '原样传递' },
  { value: 'int', label: '转整数' },
  { value: 'float', label: '转小数' },
  { value: 'string', label: '转字符串' },
  { value: 'bool', label: '转布尔' },
  { value: 'json', label: '序列化为 JSON' },
  { value: 'join', label: '拼接数组' },
  { value: 'base64', label: 'Base64 编码' },
];

const ROUTE_FIELDS = ['route.model_hint', 'route.id', 'route.provider_id'];

export interface ParamMapperProps {
  id: string;
  value: Record<string, ParamMapEntry>;
  onChange: (value: Record<string, ParamMapEntry>) => void;
  /** input_schema.properties 的键 */
  inputFields: string[];
  disabled?: boolean;
  className?: string;
}

interface Row {
  target: string;
  from: string;
  transform: ParamTransform;
  fallback: string;
}

function toRows(value: Record<string, ParamMapEntry>): Row[] {
  return Object.entries(value).map(([target, entry]) => {
    const transform =
      typeof entry.transform === 'string' &&
      TRANSFORMS.some((item) => item.value === entry.transform)
        ? (entry.transform as ParamTransform)
        : 'passthrough';
    const fallback =
      entry.default === undefined || entry.default === null ? '' : String(entry.default as Json);
    return { target, from: entry.from, transform, fallback };
  });
}

function fromRows(rows: Row[]): Record<string, ParamMapEntry> {
  const result: Record<string, ParamMapEntry> = {};
  for (const row of rows) {
    if (!row.target.trim()) continue;
    const entry: ParamMapEntry = { from: row.from, transform: row.transform };
    if (row.fallback !== '') entry.default = row.fallback;
    result[row.target.trim()] = entry;
  }
  return result;
}

export function ParamMapper({ id, value, onChange, inputFields, disabled, className }: ParamMapperProps) {
  const rows = React.useMemo(() => toRows(value), [value]);

  const update = (index: number, patch: Partial<Row>): void => {
    const next = rows.map((row, rowIndex) => (rowIndex === index ? { ...row, ...patch } : row));
    onChange(fromRows(next));
  };

  const remove = (index: number): void => {
    onChange(fromRows(rows.filter((_, rowIndex) => rowIndex !== index)));
  };

  const add = (): void => {
    const firstField = inputFields[0];
    const target = `param_${rows.length + 1}`;
    onChange(
      fromRows([
        ...rows,
        {
          target,
          from: firstField ? `input.${firstField}` : 'route.model_hint',
          transform: 'passthrough',
          fallback: '',
        },
      ]),
    );
  };

  const sourceOptions = React.useMemo(
    () => [
      ...inputFields.map((field) => ({ value: `input.${field}`, label: `input.${field}` })),
      ...ROUTE_FIELDS.map((field) => ({ value: field, label: field })),
    ],
    [inputFields],
  );

  const unresolved = rows.filter((row) => {
    if (row.from.startsWith('input.')) {
      const name = row.from.slice('input.'.length);
      return !inputFields.includes(name);
    }
    return !ROUTE_FIELDS.includes(row.from);
  });

  return (
    <div className={cn('flex flex-col gap-2', className)} id={id}>
      <div className="flex items-center justify-between">
        <h3 className="text-13 font-medium text-content">参数映射</h3>
        <Button
          variant="secondary"
          size="sm"
          onClick={add}
          disabled={disabled}
          icon={<Plus size={14} strokeWidth={1.75} />}
        >
          新增映射
        </Button>
      </div>

      {rows.length === 0 ? (
        <p className="rounded border border-dashed border-border px-3 py-3 text-12 text-content-muted">
          还没有映射。上游请求体里的每个字段都应显式声明来源，否则发布前校验会报 PARAM_MAP_UNRESOLVED。
        </p>
      ) : null}

      {rows.length > 0 ? (
        <div className="overflow-x-auto sr-scroll">
          <table className="w-full text-13">
            <caption className="px-1 py-1 text-left text-12 text-content-muted">
              上游参数 ← 输入来源（未声明来源会阻断发布）
            </caption>
            <thead>
              <tr className="border-b border-border">
                <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">
                  上游参数
                </th>
                <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">
                  来源
                </th>
                <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">
                  转换
                </th>
                <th scope="col" className="px-2 py-1.5 text-left text-12 font-medium text-content-muted">
                  默认值
                </th>
                <th scope="col" className="px-2 py-1.5 text-right text-12 font-medium text-content-muted">
                  操作
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--sr-border)]">
              {rows.map((row, index) => (
                <tr key={`${row.target}-${index}`}>
                  <td className="px-2 py-1.5">
                    <input
                      value={row.target}
                      disabled={disabled}
                      aria-label={`第 ${index + 1} 行上游参数名`}
                      onChange={(event) => update(index, { target: event.target.value })}
                      className="h-8 w-full rounded border border-border bg-surface px-2 text-13 sr-mono focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
                    />
                  </td>
                  <td className="px-2 py-1.5">
                    <input
                      value={row.from}
                      disabled={disabled}
                      list={`${id}-sources`}
                      aria-label={`第 ${index + 1} 行来源`}
                      onChange={(event) => update(index, { from: event.target.value })}
                      className={cn(
                        'h-8 w-full rounded border bg-surface px-2 text-13 sr-mono focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]',
                        unresolved.includes(row) ? 'border-danger' : 'border-border',
                      )}
                    />
                  </td>
                  <td className="px-2 py-1.5">
                    <Select
                      size="sm"
                      aria-label={`第 ${index + 1} 行转换`}
                      value={row.transform}
                      onValueChange={(next) => update(index, { transform: next as ParamTransform })}
                      options={TRANSFORMS}
                      disabled={disabled}
                    />
                  </td>
                  <td className="px-2 py-1.5">
                    <input
                      value={row.fallback}
                      disabled={disabled}
                      aria-label={`第 ${index + 1} 行默认值`}
                      onChange={(event) => update(index, { fallback: event.target.value })}
                      className="h-8 w-full rounded border border-border bg-surface px-2 text-13 sr-mono focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]"
                    />
                  </td>
                  <td className="px-2 py-1.5 text-right">
                    <Button
                      variant="ghost"
                      size="icon"
                      aria-label={`删除第 ${index + 1} 行映射`}
                      onClick={() => remove(index)}
                      disabled={disabled}
                      icon={<Trash2 size={16} strokeWidth={1.75} />}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <datalist id={`${id}-sources`}>
            {sourceOptions.map((option) => (
              <option key={option.value} value={option.value} />
            ))}
          </datalist>
        </div>
      ) : null}

      {unresolved.length > 0 ? (
        <ul className="flex flex-col gap-1" aria-live="polite">
          {unresolved.map((row, index) => (
            <li key={`${row.from}-${index}`} className="text-12 text-danger">
              <code className="sr-mono">{row.from}</code> 未在 input_schema 或 route 声明中定义（PARAM_MAP_UNRESOLVED）
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
