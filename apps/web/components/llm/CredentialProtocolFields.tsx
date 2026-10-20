'use client';

import { Eye, EyeOff, PlugZap } from 'lucide-react';
import * as React from 'react';
import { FormField } from '@/components/common/FormField';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/components/ui/cn';
import { Input } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import type { SelectOption } from '@/components/ui/select';
import { formatDuration } from '@/lib/format';
import type { LlmProbeResponse, LlmProtocolSpec } from '@/lib/types';
import { LLM_STAGE_LABELS, type LlmProbeTransportError } from './ConnectionTester';
import { ProtocolPicker } from './ProtocolPicker';

/**
 * 凭证抽屉里的「协议 + 地址 + 认证方式 + 头名 + 立即测试」（spec §3.8 ①②、§4.3）。
 *
 * **受控组件，不持有表单状态**：`/providers/{id}` 的抽屉已经是
 * `react-hook-form + zodResolver`（`credentialSchema`），那里必须用 `Controller`
 * 或 `setValue` 把 `protocol` / `base_url` / `auth_style` / `header_name` 注册进同一份
 * schema（spec R-19）。本组件只负责渲染 + 把变更回抛给 `onChange`，
 * 这样同一份 `secret` / `base_url` 不会出现两处真相。
 *
 * 请求体构造请直接用 `buildProbeTarget(values)`（`components/llm/useLlmDraft.ts` 的纯函数），
 * 它认 snake_case，正好可以传 `form.getValues()` 的结果。
 */

/** 受控值：字段名与后端 / RHF 的 snake_case 保持一致，避免翻译层漂移 */
export interface CredentialProtocolFieldsValue {
  protocol: string;
  /** 线协议：只有「协议自身不绑定 wire」（`spec.wire === null`）时才显示可编辑 */
  wire: string;
  base_url: string;
  auth_style: string;
  header_name: string;
  /** 路径覆盖（仅 `paths_inherit_from_wire` 的协议显示） */
  paths_models?: string;
  paths_chat?: string;
  /** 密钥：只有 `showSecret` 时才由本组件渲染，否则由调用方的表单持有 */
  secret?: string;
}

export type CredentialProtocolFieldName =
  | 'protocol'
  | 'wire'
  | 'base_url'
  | 'auth_style'
  | 'header_name'
  | 'paths_models'
  | 'paths_chat'
  | 'secret';

export interface CredentialProtocolFieldsProps {
  value: CredentialProtocolFieldsValue;
  /** 只回抛被改动的字段（合并交给调用方，通常是 `form.setValue`） */
  onChange: (patch: Partial<CredentialProtocolFieldsValue>) => void;
  /** `GET /api/admin/llm/protocols` 的 items（协议卡片与默认值都来自它） */
  protocols: LlmProtocolSpec[];
  protocolsLoading?: boolean;
  protocolsError?: { code: string; message: string; hint?: string | null } | null;
  onProtocolsRetry?: () => void;
  /** 协议切换后的回调（页面可据此 toast「地址已切换为新协议默认值」） */
  onProtocolChange?: (result: { protocol: string; baseUrlChanged: boolean }) => void;
  /** 「立即测试（草稿）」：不传则不渲染该按钮 */
  onTest?: () => void;
  testResult?: LlmProbeResponse | null;
  testing?: boolean;
  /** 传输层错误（`describeError()` 的形态），与 `testResult.ok === false` 分开显示 */
  error?: LlmProbeTransportError | null;
  /** 字段级错误（RHF `formState.errors`） */
  errors?: Partial<Record<CredentialProtocolFieldName, string>>;
  /** 是否由本组件渲染密钥输入（抽屉已有独立密钥字段时保持 false） */
  showSecret?: boolean;
  disabled?: boolean;
  /** 表单控件 id 前缀（抽屉里同一页可能有多个表单） */
  idPrefix?: string;
  className?: string;
}

/**
 * 认证方式的界面文案。**只做词表**：哪些认证方式可选由后端 `auth_styles` 决定，
 * 这里不判断「哪个协议用哪个头」（那是 INV-1 的后端职责）。
 */
const AUTH_STYLE_LABELS: Record<string, string> = {
  bearer: 'Bearer Token（Authorization: Bearer …）',
  api_key: 'API Key（放进指定请求头）',
  basic: 'Basic Auth（用户名:密码）',
  header: '自定义请求头',
  none: '无认证（内网免鉴权）',
};

/** 需要显式头名的认证方式（`header_name` 只对这两种有意义） */
const HEADER_NAME_STYLES: readonly string[] = ['api_key', 'header'];

export function CredentialProtocolFields({
  value,
  onChange,
  protocols,
  protocolsLoading,
  protocolsError,
  onProtocolsRetry,
  onProtocolChange,
  onTest,
  testResult,
  testing,
  error,
  errors,
  showSecret = false,
  disabled,
  idPrefix = 'llm',
  className,
}: CredentialProtocolFieldsProps) {
  const [revealSecret, setRevealSecret] = React.useState(false);
  const spec = protocols.find((item) => item.key === value.protocol) ?? null;

  /** 可选 wire：从协议目录里取（`wire !== null` 的协议的 wire 集合），不硬编码三值 */
  const wireOptions = React.useMemo<SelectOption[]>(() => {
    const seen = new Set<string>();
    const options: SelectOption[] = [];
    for (const item of protocols) {
      if (item.wire === null || seen.has(item.wire)) continue;
      seen.add(item.wire);
      options.push({ value: item.wire, label: item.wire, hint: item.label_zh });
    }
    return options;
  }, [protocols]);

  const wireSelectable = spec?.wire === null;
  const inheritPaths = spec?.paths_inherit_from_wire === true;
  const needsBaseUrl = spec ? spec.default_base_url.trim() === '' : false;
  const headerNameEnabled = HEADER_NAME_STYLES.includes(value.auth_style);

  function handleProtocolChange(protocolKey: string): void {
    const next = protocols.find((item) => item.key === protocolKey) ?? null;
    const previous = protocols.find((item) => item.key === value.protocol) ?? null;
    const untouched =
      value.base_url.trim() === '' ||
      (previous ? value.base_url.trim() === previous.default_base_url.trim() : false);
    const nextBaseUrl = next && untouched ? next.default_base_url : value.base_url;
    onChange({
      protocol: protocolKey,
      wire: next?.wire ?? '',
      base_url: nextBaseUrl,
      auth_style: next?.default_auth_style ?? value.auth_style,
      // 换协议后路径覆盖与旧头名都不再成立
      header_name: '',
      paths_models: '',
      paths_chat: '',
    });
    onProtocolChange?.({ protocol: protocolKey, baseUrlChanged: nextBaseUrl !== value.base_url });
  }

  return (
    <div className={cn('flex flex-col gap-3', className)}>
      <ProtocolPicker
        protocols={protocols}
        value={value.protocol}
        onChange={handleProtocolChange}
        loading={protocolsLoading}
        error={protocolsError}
        onRetry={onProtocolsRetry}
        disabled={disabled}
      />

      <div className="grid grid-cols-1 gap-x-3 gap-y-1 md:grid-cols-2">
        {wireSelectable ? (
          <FormField
            id={`${idPrefix}-wire`}
            label="线协议"
            description="自定义地址仍按哪种规范发请求"
            error={errors?.wire}
          >
            <Select
              id={`${idPrefix}-wire`}
              value={value.wire || undefined}
              onValueChange={(next) => onChange({ wire: next })}
              options={wireOptions}
              placeholder="选择线协议"
              disabled={disabled}
              invalid={Boolean(errors?.wire)}
            />
          </FormField>
        ) : null}

        <FormField
          id={`${idPrefix}-base_url`}
          label="地址"
          required={needsBaseUrl || !value.protocol}
          hint={spec?.default_base_url ? '可改' : undefined}
          description={
            spec?.base_url_hint
              ? `填法：${spec.base_url_hint}${spec.default_base_url ? '；留空则用默认地址' : ''}`
              : undefined
          }
          error={errors?.base_url}
        >
          <Input
            id={`${idPrefix}-base_url`}
            value={value.base_url}
            onChange={(event) => onChange({ base_url: event.target.value })}
            placeholder={spec?.base_url_hint || 'https://your-gateway/v1'}
            autoComplete="off"
            spellCheck={false}
            inputMode="url"
            invalid={Boolean(errors?.base_url)}
            disabled={disabled}
          />
        </FormField>

        <FormField
          id={`${idPrefix}-auth_style`}
          label="认证方式"
          description={
            spec ? `可选：${spec.auth_styles.map((style) => AUTH_STYLE_LABELS[style] ?? style).join(' / ')}` : undefined
          }
          error={errors?.auth_style}
        >
          <Select
            id={`${idPrefix}-auth_style`}
            value={value.auth_style || undefined}
            onValueChange={(next) => onChange({ auth_style: next })}
            options={(spec?.auth_styles ?? []).map((style) => ({
              value: style,
              label: AUTH_STYLE_LABELS[style] ?? style,
            }))}
            placeholder="选择认证方式"
            disabled={disabled || !spec}
            invalid={Boolean(errors?.auth_style)}
          />
        </FormField>

        <FormField
          id={`${idPrefix}-header_name`}
          label="头名"
          hint={headerNameEnabled ? '如 x-api-key' : '当前认证方式不需要'}
          description={
            headerNameEnabled
              ? '留空则用该协议默认头名（由后端决定）'
              : '只有 API Key / 自定义请求头时才会用到'
          }
          error={errors?.header_name}
        >
          <Input
            id={`${idPrefix}-header_name`}
            value={value.header_name}
            onChange={(event) => onChange({ header_name: event.target.value })}
            placeholder={headerNameEnabled ? 'x-api-key' : '—'}
            autoComplete="off"
            spellCheck={false}
            disabled={disabled || !headerNameEnabled}
            invalid={Boolean(errors?.header_name)}
          />
        </FormField>

        {showSecret ? (
          <FormField
            id={`${idPrefix}-secret`}
            label="密钥"
            required
            hint="不会回显"
            description="只在本次请求的内存里使用；不会写库、不会进日志"
            error={errors?.secret}
            className="md:col-span-2"
            aside={
              <Button
                variant="ghost"
                size="sm"
                type="button"
                onClick={() => setRevealSecret((prev) => !prev)}
                aria-pressed={revealSecret}
                icon={
                  revealSecret ? (
                    <EyeOff size={14} strokeWidth={1.75} />
                  ) : (
                    <Eye size={14} strokeWidth={1.75} />
                  )
                }
              >
                {revealSecret ? '隐藏' : '显示'}
              </Button>
            }
          >
            <Input
              id={`${idPrefix}-secret`}
              type={revealSecret ? 'text' : 'password'}
              autoComplete="off"
              spellCheck={false}
              value={value.secret ?? ''}
              onChange={(event) => onChange({ secret: event.target.value })}
              placeholder="粘贴密钥"
              invalid={Boolean(errors?.secret)}
              disabled={disabled}
            />
          </FormField>
        ) : null}

        {inheritPaths ? (
          <>
            <FormField
              id={`${idPrefix}-paths_models`}
              label="取模型路径"
              hint="可留空"
              description="留空即继承所选线协议的默认路径（不是「没有模型清单」）"
            >
              <Input
                id={`${idPrefix}-paths_models`}
                value={value.paths_models ?? ''}
                onChange={(event) => onChange({ paths_models: event.target.value })}
                placeholder="继承所选线协议"
                autoComplete="off"
                spellCheck={false}
                className="sr-mono"
                disabled={disabled}
              />
            </FormField>
            <FormField
              id={`${idPrefix}-paths_chat`}
              label="对话路径"
              hint="可留空"
              description="留空即继承所选线协议的默认路径"
            >
              <Input
                id={`${idPrefix}-paths_chat`}
                value={value.paths_chat ?? ''}
                onChange={(event) => onChange({ paths_chat: event.target.value })}
                placeholder="继承所选线协议"
                autoComplete="off"
                spellCheck={false}
                className="sr-mono"
                disabled={disabled}
              />
            </FormField>
          </>
        ) : null}
      </div>

      <div className="flex flex-col gap-2 border-t border-border pt-3">
        <div className="flex flex-wrap items-center gap-2">
          {onTest ? (
            <Button
              variant="secondary"
              size="sm"
              type="button"
              loading={testing}
              disabled={disabled}
              onClick={onTest}
              icon={<PlugZap size={16} strokeWidth={1.75} />}
            >
              {testing ? '测试中…' : '立即测试'}
            </Button>
          ) : null}
          <span className="text-12 text-content-muted">
            测试用的是当前表单值，不需要先保存（鉴权阶段不计费）。
          </span>
        </div>

        {/* 抽屉内的紧凑结论：完整阶段时间线在 /settings/agent 的 ConnectionTester 里 */}
        <div aria-live="polite" className="flex flex-col gap-1 text-12">
          {error ? (
            <p className="flex flex-wrap items-center gap-2 text-content">
              <Badge tone="danger" mono>
                {error.code}
              </Badge>
              <span>{error.message}</span>
              {error.hint ? <span className="text-content-muted">· {error.hint}</span> : null}
            </p>
          ) : null}
          {testResult ? (
            <>
              <p className="flex flex-wrap items-center gap-2">
                <Badge tone={testResult.ok ? 'ok' : 'danger'} dot>
                  {testResult.ok ? '测试通过' : '测试失败'}
                </Badge>
                {testResult.error_code ? (
                  <Badge tone="danger" mono>
                    {testResult.error_code}
                  </Badge>
                ) : null}
                {testResult.stages.map((stage) => (
                  <span key={stage.stage} className="text-content-muted">
                    {LLM_STAGE_LABELS[stage.stage] ?? stage.stage}
                    {stage.ok ? ' ✓' : ' ✗'} HTTP {stage.http_status ?? '—'} ·{' '}
                    {formatDuration(stage.latency_ms)}
                  </span>
                ))}
              </p>
              {testResult.hint ? <p className="text-content-muted">下一步：{testResult.hint}</p> : null}
              {testResult.problems.length > 0 ? (
                <ul className="flex flex-col gap-0.5 text-content-muted">
                  {testResult.problems.map((problem) => (
                    <li key={problem}>· {problem}</li>
                  ))}
                </ul>
              ) : null}
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}
