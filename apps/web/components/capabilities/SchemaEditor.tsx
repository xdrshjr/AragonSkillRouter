'use client';

import { CircleAlert, CircleCheck, Wand2 } from 'lucide-react';
import * as React from 'react';
import { Button } from '@/components/ui/button';
import { cn } from '@/components/ui/cn';
import type { Json, JsonObject } from '@/lib/types';

/**
 * JSON Schema 编辑器（spec §8.4）：
 * - 就地 JSON 解析错误（行/列）
 * - 「格式化」与「最小骨架」两个动作
 * - 结构校验：必须是 object、`additionalProperties` 必须为 false（§7.6-4）
 * - 字段路径错误（如 `properties.prompt.type`）
 */

export interface SchemaEditorProps {
  id: string;
  label: string;
  value: JsonObject;
  onChange: (value: JsonObject) => void;
  /** input（要求 properties）或 output */
  role?: 'input' | 'output';
  description?: string;
  rows?: number;
  disabled?: boolean;
  className?: string;
}

interface SchemaIssue {
  path: string;
  message: string;
}

function validateSchemaShape(schema: JsonObject, role: 'input' | 'output'): SchemaIssue[] {
  const issues: SchemaIssue[] = [];
  const type = schema['type'];
  if (type !== 'object') {
    issues.push({ path: 'type', message: '顶层 type 必须是 "object"' });
  }
  if (schema['additionalProperties'] !== false) {
    issues.push({
      path: 'additionalProperties',
      message: '必须显式设为 false（不支持的参数要显式报错）',
    });
  }
  const properties = schema['properties'];
  if (role === 'input') {
    if (!properties || typeof properties !== 'object' || Array.isArray(properties)) {
      issues.push({ path: 'properties', message: 'input_schema 必须有 properties 对象' });
    } else if (Object.keys(properties as Record<string, Json>).length === 0) {
      issues.push({ path: 'properties', message: 'input_schema.properties 不能为空' });
    }
    const required = schema['required'];
    if (required !== undefined) {
      if (!Array.isArray(required)) {
        issues.push({ path: 'required', message: 'required 必须是数组' });
      } else if (properties && typeof properties === 'object' && !Array.isArray(properties)) {
        const props = properties as Record<string, Json>;
        required.forEach((name) => {
          if (typeof name === 'string' && !(name in props)) {
            issues.push({ path: `required.${name}`, message: 'required 中的字段未在 properties 声明' });
          }
        });
      }
    }
  }
  return issues;
}

const SKELETON: JsonObject = {
  $schema: 'https://json-schema.org/draft/2020-12/schema',
  type: 'object',
  properties: {
    prompt: { type: 'string', description: '输入提示词' },
  },
  required: ['prompt'],
  additionalProperties: false,
};

export function SchemaEditor({
  id,
  label,
  value,
  onChange,
  role = 'input',
  description,
  rows = 14,
  disabled,
  className,
}: SchemaEditorProps) {
  const [text, setText] = React.useState<string>(() => JSON.stringify(value, null, 2));
  const [parseError, setParseError] = React.useState<string | null>(null);
  const [issues, setIssues] = React.useState<SchemaIssue[]>([]);
  const dirtyRef = React.useRef(false);

  // 外部值变化（如版本回滚）时同步文本
  React.useEffect(() => {
    if (dirtyRef.current) return;
    setText(JSON.stringify(value, null, 2));
  }, [value]);

  const apply = (raw: string): void => {
    dirtyRef.current = true;
    setText(raw);
    try {
      const parsed = JSON.parse(raw) as unknown;
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        setParseError('顶层必须是 JSON 对象');
        setIssues([]);
        return;
      }
      setParseError(null);
      const schema = parsed as JsonObject;
      setIssues(validateSchemaShape(schema, role));
      onChange(schema);
    } catch (cause) {
      setParseError(cause instanceof Error ? cause.message : 'JSON 解析失败');
      setIssues([]);
    }
  };

  const format = (): void => {
    try {
      const parsed = JSON.parse(text) as JsonObject;
      const formatted = JSON.stringify(parsed, null, 2);
      setText(formatted);
      apply(formatted);
    } catch {
      /* 保留现有错误 */
    }
  };

  const reset = (): void => {
    dirtyRef.current = false;
    setText(JSON.stringify(value, null, 2));
    setParseError(null);
    setIssues(validateSchemaShape(value, role));
  };

  const errorCount = issues.length + (parseError ? 1 : 0);

  return (
    <div className={cn('flex flex-col gap-1.5', className)}>
      <div className="flex items-center justify-between gap-2">
        <label htmlFor={id} className="text-13 font-medium text-content">
          {label}
        </label>
        <div className="flex items-center gap-1">
          <Button variant="ghost" size="sm" onClick={format} disabled={disabled} icon={<Wand2 size={14} strokeWidth={1.75} />}>
            格式化
          </Button>
          <Button variant="ghost" size="sm" onClick={() => apply(JSON.stringify(SKELETON, null, 2))} disabled={disabled}>
            插入骨架
          </Button>
          <Button variant="ghost" size="sm" onClick={reset} disabled={disabled}>
            还原
          </Button>
        </div>
      </div>
      {description ? <p className="text-12 text-content-muted">{description}</p> : null}
      <textarea
        id={id}
        value={text}
        rows={rows}
        spellCheck={false}
        disabled={disabled}
        aria-invalid={errorCount > 0 || undefined}
        aria-describedby={`${id}-issues`}
        onChange={(event) => apply(event.target.value)}
        className={cn(
          'w-full rounded border bg-surface px-3 py-2 text-12 sr-mono text-content',
          'focus-visible:outline-none focus-visible:shadow-[var(--sr-focus-ring)]',
          errorCount > 0 ? 'border-danger' : 'border-border',
        )}
      />
      <div id={`${id}-issues`} className="flex flex-col gap-1" aria-live="polite">
        {parseError ? (
          <p className="flex items-start gap-1 text-12 text-danger">
            <CircleAlert size={12} strokeWidth={1.75} className="mt-0.5 shrink-0" aria-hidden />
            JSON 解析失败：{parseError}
          </p>
        ) : null}
        {issues.map((issue) => (
          <p key={`${issue.path}-${issue.message}`} className="flex items-start gap-1 text-12 text-danger">
            <CircleAlert size={12} strokeWidth={1.75} className="mt-0.5 shrink-0" aria-hidden />
            <span>
              <code className="sr-mono">{issue.path}</code>：{issue.message}
            </span>
          </p>
        ))}
        {errorCount === 0 ? (
          <p className="flex items-center gap-1 text-12 text-ok">
            <CircleCheck size={12} strokeWidth={1.75} aria-hidden />
            Schema 结构合法（object + additionalProperties:false）
          </p>
        ) : null}
      </div>
    </div>
  );
}
