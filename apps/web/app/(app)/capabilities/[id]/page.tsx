'use client';

import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { CheckCircle2, CircleAlert, Layers, Save, Send, ShieldOff } from 'lucide-react';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import * as React from 'react';
import { DangerConfirm } from '@/components/common/DangerConfirm';
import { EmptyState, RequestIdCopy } from '@/components/common/EmptyState';
import { PageHeader } from '@/components/common/PageHeader';
import { PublishStateBadges, StatusBadge } from '@/components/common/StatusBadge';
import { ParamMapper } from '@/components/capabilities/ParamMapper';
import { RouteEditor, type RouteEditorProvider } from '@/components/capabilities/RouteEditor';
import { SchemaEditor } from '@/components/capabilities/SchemaEditor';
import { TestPanel } from '@/components/capabilities/TestPanel';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Dialog, DialogClose, DialogContent } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Textarea } from '@/components/ui/textarea';
import { useToast } from '@/components/ui/toast';
import { apiGet, apiPatch, apiPost, describeError, testCapability } from '@/lib/api';
import { formatDateTime, prettyJson } from '@/lib/format';
import { queryKeys } from '@/lib/query';
import type {
  CallMode,
  Capability,
  CapabilityIssue,
  CapabilityRoute,
  CapabilityTestResult,
  CapabilityTestRun,
  CapabilityValidation,
  JsonObject,
  Paginated,
  Provider,
  ProviderDetail,
} from '@/lib/types';

/**
 * 能力详情（spec §13.2 `/capabilities/[id]`）：
 * 左：结构化编辑器 Tab（请求/路由/映射/Schema/限制/错误映射/示例）
 * 右：示例与测试面板 + 发布
 * 「已保存 / 测试通过 / 已发布 / 可调用」四态独立展示；
 * 路由 Tab 明确显示每条 route 的提供商/凭证/优先级/启用与「谁是当前主力」。
 */

interface DraftState {
  key: string;
  name: string;
  kind: string;
  modes: CallMode[];
  file_io: { input: string[]; output: string[] };
  input_schema: JsonObject;
  output_schema: JsonObject;
  examples: { title?: string; input: JsonObject }[];
  compatibility_group: string;
  failover: { enabled: boolean; max_switches: number };
  tags: string[];
  requires_agent_reasoning: boolean;
}

const DEFAULT_INPUT_SCHEMA: JsonObject = {
  $schema: 'https://json-schema.org/draft/2020-12/schema',
  type: 'object',
  properties: { prompt: { type: 'string' } },
  required: ['prompt'],
  additionalProperties: false,
};

/** 测试证据有效期：发布闸门只认 24h 内的成功测试（spec §A.3.1） */
const TEST_EVIDENCE_WINDOW_MS = 86_400_000;

/**
 * 「测试通过」的唯一判定：`test_runs[].ok === true` 且 `at` 在 24h 内。
 *
 * `ok` 是**布尔**（后端 `bool(t.ok)`）。写成 `=== 1` 会恒为 false，
 * 让发布按钮永久禁用（F-01 以另一种形式复发），所以这里只做 `=== true`。
 */
function hasFreshTestEvidence(runs: CapabilityTestRun[], now: number): boolean {
  return runs.some(
    (run) => run.ok === true && Number.isFinite(Date.parse(run.at)) && Date.parse(run.at) >= now - TEST_EVIDENCE_WINDOW_MS,
  );
}

function toDraft(capability: Capability): DraftState {
  // 后端详情接口把可编辑定义放在 `definition`（列表接口不返回），
  // 读错字段会退化成默认 schema，保存时把真实 schema/映射覆盖掉。
  const definition = capability.definition ?? {};
  const inputSchema =
    definition['input_schema'] && typeof definition['input_schema'] === 'object'
      ? (definition['input_schema'] as JsonObject)
      : DEFAULT_INPUT_SCHEMA;
  const outputSchema =
    definition['output_schema'] && typeof definition['output_schema'] === 'object'
      ? (definition['output_schema'] as JsonObject)
      : { type: 'object', additionalProperties: true };
  const rawExamples = Array.isArray(definition['examples']) ? definition['examples'] : [];
  const examples: { title?: string; input: JsonObject }[] = [];
  for (const item of rawExamples) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) continue;
    const record = item as Record<string, unknown>;
    const input = record['input'];
    if (!input || typeof input !== 'object' || Array.isArray(input)) continue;
    const title = typeof record['title'] === 'string' ? record['title'] : undefined;
    examples.push(title === undefined ? { input: input as JsonObject } : { title, input: input as JsonObject });
  }

  return {
    key: capability.key,
    name: capability.name,
    kind: capability.kind,
    modes: capability.modes ?? ['sync'],
    file_io: {
      input: Array.isArray(capability.file_io?.input) ? capability.file_io.input : [],
      output: Array.isArray(capability.file_io?.output) ? capability.file_io.output : [],
    },
    input_schema: inputSchema,
    output_schema: outputSchema,
    examples: examples.length > 0 ? examples : [{ title: '最小示例', input: {} }],
    compatibility_group: capability.compatibility_group ?? '',
    tags: capability.tags ?? [],
    requires_agent_reasoning: capability.requires_agent_reasoning,
    failover: (definition['failover'] as DraftState['failover'] | undefined) ?? { enabled: true, max_switches: 2 },
  };
}

export default function CapabilityDetailPage() {
  const params = useParams<{ id: string }>();
  const capabilityId = params?.id ?? '';
  const queryClient = useQueryClient();
  const { toast } = useToast();

  const [draft, setDraft] = React.useState<DraftState | null>(null);
  const [routes, setRoutes] = React.useState<CapabilityRoute[]>([]);
  const [saved, setSaved] = React.useState(true);
  const [issues, setIssues] = React.useState<CapabilityIssue[] | null>(null);
  const [publishOpen, setPublishOpen] = React.useState(false);
  const [deprecateOpen, setDeprecateOpen] = React.useState(false);
  const [publishVersion, setPublishVersion] = React.useState('');
  const [publishChangelog, setPublishChangelog] = React.useState('');
  const [activeTab, setActiveTab] = React.useState('request');

  const capability = useQuery<Capability>({
    queryKey: queryKeys.capabilities.detail(capabilityId),
    queryFn: () => apiGet<Capability>(`/admin/capabilities/${capabilityId}`),
    enabled: Boolean(capabilityId),
  });

  const routeList = useQuery<CapabilityRoute[] | { items: CapabilityRoute[] }>({
    queryKey: queryKeys.capabilities.routes(capabilityId),
    queryFn: () => apiGet<CapabilityRoute[] | { items: CapabilityRoute[] }>(`/admin/capabilities/${capabilityId}/routes`),
    enabled: Boolean(capabilityId),
  });

  const providers = useQuery<Paginated<Provider>>({
    queryKey: queryKeys.providers.list({ page: 1, page_size: 100, sort: 'name' }),
    queryFn: () => apiGet<Paginated<Provider>>('/admin/providers', { page: 1, page_size: 100, sort: 'name' }),
  });

  const providerIds = React.useMemo(() => (providers.data?.items ?? []).map((p) => p.id), [providers.data]);
  const providerDetails = useQueries({
    queries: providerIds.map((id) => ({
      queryKey: queryKeys.providers.detail(id),
      queryFn: () => apiGet<ProviderDetail>(`/admin/providers/${id}`),
      staleTime: 60_000,
    })),
  });

  const routeProviders: RouteEditorProvider[] = React.useMemo(
    () =>
      providerDetails
        .map((query) => query.data)
        .filter((item): item is ProviderDetail => Boolean(item))
        .map((provider) => ({
          id: provider.id,
          name: provider.name,
          credentials: (provider.credentials ?? []).map((credential) => ({
            id: credential.id,
            name: credential.name,
            status: credential.status,
          })),
        })),
    [providerDetails],
  );

  // 初始化本地草稿
  React.useEffect(() => {
    if (capability.data && !draft) {
      setDraft(toDraft(capability.data));
    }
  }, [capability.data, draft]);

  /**
   * 「测试通过」徽标与发布闸门**同源**：都读落库的 `test_runs`（`ok === true` 且 24h 内）。
   * 这样刷新页面后按钮仍可用，而不是依赖本次会话里的测试响应。
   */
  const testRuns = React.useMemo<CapabilityTestRun[]>(
    () => capability.data?.test_runs ?? [],
    [capability.data],
  );
  const [testedOk, setTestedOk] = React.useState(false);
  React.useEffect(() => {
    setTestedOk(hasFreshTestEvidence(testRuns, Date.now()));
  }, [testRuns]);

  React.useEffect(() => {
    const data = routeList.data;
    if (!data) return;
    const items = Array.isArray(data) ? data : data.items;
    setRoutes(items);
    setSaved(true);
  }, [routeList.data]);

  // 未保存提示
  const dirty = !saved;
  React.useEffect(() => {
    if (!dirty) return;
    const handler = (event: BeforeUnloadEvent): void => {
      event.preventDefault();
      event.returnValue = '有未保存的变更，确定离开？';
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [dirty]);

  const patchDraft = (changes: Partial<DraftState>): void => {
    setDraft((prev) => (prev ? { ...prev, ...changes } : prev));
    setSaved(false);
  };

  const updateRoutes = (next: CapabilityRoute[]): void => {
    setRoutes(next);
    setSaved(false);
  };

  const definition = React.useMemo<JsonObject>(() => {
    if (!draft) return {};
    const base = capability.data?.definition ?? {};
    return {
      ...base,
      key: draft.key,
      name: draft.name,
      kind: draft.kind,
      modes: draft.modes,
      file_io: { input: draft.file_io.input, output: draft.file_io.output },
      input_schema: draft.input_schema,
      output_schema: draft.output_schema,
      examples: draft.examples.map((example) => ({
        title: example.title ?? null,
        input: example.input,
      })),
      compatibility_group: draft.compatibility_group || null,
      failover: draft.failover,
      tags: draft.tags,
      requires_agent_reasoning: draft.requires_agent_reasoning,
      routes: routes.map((route) => ({
        ...route,
        request: route.request,
        param_map: route.param_map,
        response_map: route.response_map,
        limits: route.limits,
        errors: route.errors,
      })),
    };
  }, [capability.data, draft, routes]);

  const save = useMutation({
    mutationFn: async () =>
      apiPatch<Capability>(`/admin/capabilities/${capabilityId}`, { definition }),
    onSuccess: async () => {
      setSaved(true);
      toast({ title: '配置已保存', description: '已保存 ≠ 已发布；仍需测试与手动发布。', tone: 'ok' });
      await queryClient.invalidateQueries({ queryKey: queryKeys.capabilities.detail(capabilityId) });
      await queryClient.invalidateQueries({ queryKey: queryKeys.capabilities.routes(capabilityId) });
    },
    onError: (cause) => {
      const info = describeError(cause);
      toast({
        title: '保存失败',
        description: info.hint ? `${info.message}（${info.hint}）` : info.message,
        tone: 'danger',
        requestId: info.requestId,
      });
    },
  });

  const validate = useMutation({
    mutationFn: async () => apiPost<CapabilityValidation>(`/admin/capabilities/${capabilityId}/validate`, {}),
    onSuccess: (data) => {
      setIssues(data.issues ?? []);
      toast({
        title: data.ok ? '校验通过' : `发现 ${data.issues?.length ?? 0} 个问题`,
        tone: data.ok ? 'ok' : 'warn',
      });
    },
    onError: (cause) => {
      const info = describeError(cause);
      toast({ title: '校验请求失败', description: info.message, tone: 'danger', requestId: info.requestId });
    },
  });

  const runTest = async (payload: {
    input: JsonObject;
    route_id?: string;
    mode?: CallMode;
    save_payload: boolean;
  }): Promise<CapabilityTestResult> => {
    const result = await testCapability(capabilityId, {
      input: payload.input,
      route_id: payload.route_id ?? null,
      mode: payload.mode ?? null,
      save_payload: payload.save_payload,
    });
    // 通过判定只认 `status === 'succeeded'`（返回体里没有 `ok`）；
    // 证据本身来自落库记录，所以这里重新拉详情：徽标与发布闸门读同一份 `test_runs`。
    await queryClient.invalidateQueries({ queryKey: queryKeys.capabilities.detail(capabilityId) });
    await queryClient.invalidateQueries({ queryKey: queryKeys.capabilities.all });
    if (result.status === 'succeeded') {
      toast({ title: '测试通过', description: '证据有效期 24 小时，之后需重新测试。', tone: 'ok' });
    } else if (result.status === 'status_unknown') {
      toast({
        title: '测试结果不确定',
        description: '上游可能已执行但未确认；重复测试可能产生双份费用。',
        tone: 'warn',
        requestId: result.error?.request_id ?? null,
      });
    } else {
      toast({
        title: '测试未通过',
        description: result.error?.message ?? '查看响应与错误映射',
        tone: 'danger',
        requestId: result.error?.request_id ?? null,
      });
    }
    return result;
  };

  const publish = useMutation({
    mutationFn: async () =>
      apiPost<Capability>(`/admin/capabilities/${capabilityId}/publish`, {
        version: publishVersion,
        changelog: publishChangelog,
      }),
    onSuccess: async () => {
      toast({ title: '已发布', description: `版本 ${publishVersion} 已冻结（不可修改）。`, tone: 'ok' });
      setPublishOpen(false);
      await queryClient.invalidateQueries({ queryKey: queryKeys.capabilities.detail(capabilityId) });
      await queryClient.invalidateQueries({ queryKey: queryKeys.capabilities.all });
    },
    onError: (cause) => {
      const info = describeError(cause);
      toast({
        title: info.code === 'PUBLISH_EVIDENCE_MISSING' ? '缺少有效测试证据' : '发布失败',
        description: info.hint ? `${info.message}（${info.hint}）` : info.message,
        tone: 'danger',
        requestId: info.requestId,
      });
    },
  });

  const deprecate = useMutation({
    mutationFn: async () => apiPost<Capability>(`/admin/capabilities/${capabilityId}/deprecate`, {}),
    onSuccess: async () => {
      toast({ title: '已停用', description: '不再参与路由，历史调用仍可查。', tone: 'ok' });
      setDeprecateOpen(false);
      await queryClient.invalidateQueries({ queryKey: queryKeys.capabilities.detail(capabilityId) });
    },
    onError: (cause) => {
      const info = describeError(cause);
      toast({ title: '停用失败', description: info.message, tone: 'danger', requestId: info.requestId });
    },
  });

  if (capability.isLoading || !draft) {
    return (
      <div className="flex flex-col gap-4" aria-busy="true">
        <Skeleton height={72} rounded="md" />
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
          <Skeleton height={420} rounded="md" className="lg:col-span-2" />
          <Skeleton height={420} rounded="md" />
        </div>
      </div>
    );
  }

  if (capability.isError) {
    const info = describeError(capability.error);
    return (
      <EmptyState
        kind={info.code === 'NOT_FOUND' ? 'empty' : 'unavailable'}
        title={info.code === 'NOT_FOUND' ? '能力不存在或已删除' : info.message}
        description={info.hint ?? undefined}
        requestId={info.requestId}
        actionLabel="返回列表"
        onAction={() => window.location.assign('/capabilities')}
      />
    );
  }

  // 三态徽标语义固定（spec §A.3.1）：
  // 「已发布」= published_version_id 非空（不是只看 status）；
  // 「可调用」= 已发布 + 能力 status === 'published' + 所有启用 route 的凭证都是 active。
  const published = Boolean(capability.data?.published_version_id);
  const enabled = capability.data?.enabled ?? true;
  const primaryRoute = [...routes].filter((route) => route.enabled).sort((a, b) => a.priority - b.priority)[0];
  const credentialStatus = new Map<string, string>();
  for (const provider of routeProviders) {
    for (const credential of provider.credentials) {
      credentialStatus.set(credential.id, credential.status);
    }
  }
  const liveRoutes = routes.filter((route) => route.enabled);
  const routesCredentialsActive =
    liveRoutes.length > 0 && liveRoutes.every((route) => credentialStatus.get(route.credential_id) === 'active');
  const callable =
    published && capability.data?.status === 'published' && enabled && routesCredentialsActive;
  const lastTestRun = testRuns[0] ?? null;
  const inputFields = Object.keys(
    (draft.input_schema['properties'] as Record<string, unknown> | undefined) ?? {},
  );
  const nextVersion = suggestVersion(capability.data?.latest_version ?? null);

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={draft.name || capability.data?.key || '能力'}
        breadcrumbs={[{ label: '能力', href: '/capabilities' }]}
        description={draft.key}
        status={
          <div className="flex flex-wrap items-center gap-2">
            <PublishStateBadges saved={saved} testedOk={testedOk} published={published} callable={callable} enabled={enabled} />
            <Link href={`/capabilities/${capabilityId}/versions`} className="text-12 text-accent hover:underline">
              版本历史 →
            </Link>
          </div>
        }
        actions={
          <>
            <Button variant="secondary" size="sm" loading={validate.isPending} onClick={() => validate.mutate()}>
              校验配置
            </Button>
            <Button
              variant="secondary"
              size="sm"
              loading={save.isPending}
              disabled={saved}
              onClick={() => save.mutate()}
              icon={<Save size={16} strokeWidth={1.75} />}
            >
              {saved ? '已保存' : '保存配置'}
            </Button>
            <Button
              variant="primary"
              size="sm"
              // 「已发布」是独立徽标，不是发布的拦路条件：改完定义、重新测试通过后
              // 必须还能发新版本（发布永远创建新版本，旧版本保持不可变）
              disabled={!testedOk || !saved}
              onClick={() => {
                setPublishVersion(nextVersion);
                setPublishOpen(true);
              }}
              icon={<Send size={16} strokeWidth={1.75} />}
            >
              发布
            </Button>
            <Button
              variant="danger"
              size="sm"
              disabled={capability.data?.status === 'deprecated'}
              onClick={() => setDeprecateOpen(true)}
              icon={<ShieldOff size={16} strokeWidth={1.75} />}
            >
              停用
            </Button>
          </>
        }
      />

      {/* 校验结果 */}
      {issues ? (
        <section aria-label="校验结果" className="sr-card p-3">
          <div className="flex items-center justify-between">
            <h2 className="flex items-center gap-1.5 text-13 font-medium text-content">
              {issues.length === 0 ? (
                <>
                  <CheckCircle2 size={16} strokeWidth={1.75} className="text-ok" aria-hidden />
                  校验通过，没有发现问题
                </>
              ) : (
                <>
                  <CircleAlert size={16} strokeWidth={1.75} className="text-danger" aria-hidden />
                  发现 {issues.length} 个问题
                </>
              )}
            </h2>
            <Button variant="ghost" size="sm" onClick={() => setIssues(null)}>
              收起
            </Button>
          </div>
          {issues.length > 0 ? (
            <ul className="mt-2 divide-y divide-[var(--sr-border)]">
              {issues.map((issue, index) => (
                <li key={`${issue.code}-${index}`} className="py-1.5">
                  <p className="flex flex-wrap items-center gap-1.5 text-13">
                    <Badge tone={(issue.level ?? issue.severity) === 'warn' ? 'warn' : 'danger'}>
                      {(issue.level ?? issue.severity) === 'warn' ? '警告' : '错误'}
                    </Badge>
                    <code className="sr-mono text-12 text-content-muted">{issue.path}</code>
                    <span className="text-content">{issue.message}</span>
                  </p>
                  <p className="mt-0.5 flex flex-wrap gap-2 text-12 text-content-muted">
                    <code className="sr-mono">{issue.code}</code>
                    {issue.hint ? <span>{issue.hint}</span> : null}
                  </p>
                </li>
              ))}
            </ul>
          ) : null}
        </section>
      ) : null}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        {/* 左：结构化编辑器 */}
        <div className="xl:col-span-2">
          <Tabs value={activeTab} onValueChange={setActiveTab}>
            <TabsList>
              <TabsTrigger value="request">请求</TabsTrigger>
              <TabsTrigger value="routes">路由</TabsTrigger>
              <TabsTrigger value="mapping">映射</TabsTrigger>
              <TabsTrigger value="schema">Schema</TabsTrigger>
              <TabsTrigger value="limits">限制</TabsTrigger>
              <TabsTrigger value="errors">错误映射</TabsTrigger>
              <TabsTrigger value="examples">示例</TabsTrigger>
            </TabsList>

            <TabsContent value="request">
              <section className="sr-card flex flex-col gap-3 p-4">
                <h2 className="text-16 font-semibold text-content">请求（当前主力 route）</h2>
                {!primaryRoute ? (
                  <p className="text-13 text-warn">还没有启用的 route。请到「路由」Tab 添加提供商与凭证。</p>
                ) : (
                  <>
                    <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                      <label className="flex flex-col gap-1">
                        <span className="text-12 text-content-muted">HTTP 方法</span>
                        <Select
                          value={primaryRoute.request.method}
                          onValueChange={(value) =>
                            updateRoutes(
                              routes.map((route) =>
                                route.id === primaryRoute.id
                                  ? { ...route, request: { ...route.request, method: value } }
                                  : route,
                              ),
                            )
                          }
                          options={['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].map((value) => ({ value, label: value }))}
                        />
                      </label>
                      <label className="flex flex-col gap-1">
                        <span className="text-12 text-content-muted">编码</span>
                        <Select
                          value={primaryRoute.request.encoding ?? 'json'}
                          onValueChange={(value) =>
                            updateRoutes(
                              routes.map((route) =>
                                route.id === primaryRoute.id
                                  ? {
                                      ...route,
                                      request: {
                                        ...route.request,
                                        encoding: value as 'json' | 'form' | 'multipart',
                                      },
                                    }
                                  : route,
                              ),
                            )
                          }
                          options={[
                            { value: 'json', label: 'json' },
                            { value: 'form', label: 'form' },
                            { value: 'multipart', label: 'multipart' },
                          ]}
                        />
                      </label>
                    </div>
                    <label className="flex flex-col gap-1">
                      <span className="text-12 text-content-muted">url_template</span>
                      <Input
                        value={primaryRoute.request.url_template}
                        onChange={(event) =>
                          updateRoutes(
                            routes.map((route) =>
                              route.id === primaryRoute.id
                                ? { ...route, request: { ...route.request, url_template: event.target.value } }
                                : route,
                            ),
                          )
                        }
                        className="sr-mono"
                      />
                    </label>
                    <label className="flex flex-col gap-1">
                      <span className="text-12 text-content-muted">body_template（JSON）</span>
                      <Textarea
                        rows={8}
                        className="sr-mono text-12"
                        value={prettyJson(primaryRoute.request.body_template ?? {})}
                        onChange={(event) => {
                          try {
                            const parsed = JSON.parse(event.target.value) as JsonObject;
                            updateRoutes(
                              routes.map((route) =>
                                route.id === primaryRoute.id
                                  ? { ...route, request: { ...route.request, body_template: parsed } }
                                  : route,
                              ),
                            );
                          } catch {
                            /* 保留上次合法值，错误由 SchemaEditor/校验提示 */
                          }
                        }}
                      />
                    </label>
                    <p className="text-12 text-content-muted">
                      多次改动的 route 需要保存后才生效；发布前请确认主力 route 的凭证仍是 active。
                    </p>
                  </>
                )}
              </section>
            </TabsContent>

            <TabsContent value="routes">
              <section className="sr-card p-4">
                <RouteEditor routes={routes} providers={routeProviders} onChange={updateRoutes} />
              </section>
            </TabsContent>

            <TabsContent value="mapping">
              <section className="sr-card flex flex-col gap-4 p-4">
                {primaryRoute ? (
                  <>
                    <ParamMapper
                      id="param-map"
                      value={primaryRoute.param_map}
                      onChange={(value) =>
                        updateRoutes(
                          routes.map((route) => (route.id === primaryRoute.id ? { ...route, param_map: value } : route)),
                        )
                      }
                      inputFields={inputFields}
                    />
                    <label className="flex flex-col gap-1">
                      <span className="text-12 text-content-muted">response_map（JSON）</span>
                      <Textarea
                        rows={8}
                        className="sr-mono text-12"
                        value={prettyJson(primaryRoute.response_map)}
                        onChange={(event) => {
                          try {
                            const parsed = JSON.parse(event.target.value) as JsonObject;
                            updateRoutes(
                              routes.map((route) =>
                                route.id === primaryRoute.id ? { ...route, response_map: parsed } : route,
                              ),
                            );
                          } catch {
                            /* ignore until valid */
                          }
                        }}
                      />
                    </label>
                  </>
                ) : (
                  <p className="text-13 text-warn">先配置一条启用的 route。</p>
                )}
              </section>
            </TabsContent>

            <TabsContent value="schema">
              <section className="sr-card flex flex-col gap-4 p-4">
                <SchemaEditor
                  id="input-schema"
                  label="input_schema"
                  role="input"
                  value={draft.input_schema}
                  onChange={(value) => patchDraft({ input_schema: value })}
                  description="对外契约：additionalProperties 必须为 false，未声明的参数会显式报错。"
                />
                <SchemaEditor
                  id="output-schema"
                  label="output_schema"
                  role="output"
                  rows={10}
                  value={draft.output_schema}
                  onChange={(value) => patchDraft({ output_schema: value })}
                />
              </section>
            </TabsContent>

            <TabsContent value="limits">
              <section className="sr-card grid grid-cols-1 gap-3 p-4 md:grid-cols-2">
                <h2 className="text-16 font-semibold text-content md:col-span-2">限制与故障切换</h2>
                {primaryRoute ? (
                  <>
                    {(
                      [
                        ['max_input_bytes', '最大输入字节'],
                        ['max_output_bytes', '最大输出字节'],
                        ['max_duration_ms', '最大时长（ms）'],
                        ['max_file_mb', '单文件上限（MB）'],
                      ] as const
                    ).map(([field, label]) => (
                      <label key={field} className="flex flex-col gap-1">
                        <span className="text-12 text-content-muted">{label}</span>
                        <Input
                          type="number"
                          value={String(primaryRoute.limits[field] ?? '')}
                          onChange={(event) =>
                            updateRoutes(
                              routes.map((route) =>
                                route.id === primaryRoute.id
                                  ? {
                                      ...route,
                                      limits: {
                                        ...route.limits,
                                        [field]: event.target.value === '' ? null : Number(event.target.value),
                                      },
                                    }
                                  : route,
                              ),
                            )
                          }
                        />
                      </label>
                    ))}
                    <label className="flex flex-col gap-1 md:col-span-2">
                      <span className="text-12 text-content-muted">allowed_mime（逗号分隔）</span>
                      <Input
                        value={(primaryRoute.limits.allowed_mime ?? []).join(', ')}
                        onChange={(event) =>
                          updateRoutes(
                            routes.map((route) =>
                              route.id === primaryRoute.id
                                ? {
                                    ...route,
                                    limits: {
                                      ...route.limits,
                                      allowed_mime: event.target.value
                                        .split(',')
                                        .map((item) => item.trim())
                                        .filter(Boolean),
                                    },
                                  }
                                : route,
                            ),
                          )
                        }
                        className="sr-mono"
                      />
                    </label>
                  </>
                ) : null}
                <div className="md:col-span-2 grid grid-cols-1 gap-3 md:grid-cols-3">
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={draft.failover.enabled}
                      onChange={(event) =>
                        patchDraft({ failover: { ...draft.failover, enabled: event.target.checked } })
                      }
                      className="h-4 w-4"
                    />
                    <span className="text-13 text-content">启用故障切换</span>
                  </label>
                  <label className="flex flex-col gap-1">
                    <span className="text-12 text-content-muted">最大切换次数</span>
                    <Input
                      type="number"
                      value={draft.failover.max_switches}
                      onChange={(event) =>
                        patchDraft({ failover: { ...draft.failover, max_switches: Number(event.target.value) } })
                      }
                    />
                  </label>
                </div>
              </section>
            </TabsContent>

            <TabsContent value="errors">
              <section className="sr-card flex flex-col gap-3 p-4">
                <h2 className="text-16 font-semibold text-content">错误映射</h2>
                <p className="text-12 text-content-muted">
                  至少覆盖 401 / 403 / 429（缺失 429 时校验给警告）。未声明的上游错误归入 UPSTREAM_ERROR。
                </p>
                {primaryRoute ? (
                  <ul className="flex flex-col gap-2">
                    {primaryRoute.errors.map((mapping, index) => (
                      <li key={index} className="grid grid-cols-1 gap-2 rounded border border-border p-2 md:grid-cols-5">
                        <label className="flex flex-col gap-1">
                          <span className="text-12 text-content-muted">匹配状态码</span>
                          <Input
                            type="number"
                            value={mapping.match.status ?? ''}
                            onChange={(event) =>
                              updateRoutes(
                                routes.map((route) =>
                                  route.id !== primaryRoute.id
                                    ? route
                                    : {
                                        ...route,
                                        errors: route.errors.map((item, itemIndex) =>
                                          itemIndex === index
                                            ? {
                                                ...item,
                                                match: {
                                                  ...item.match,
                                                  status:
                                                    event.target.value === '' ? null : Number(event.target.value),
                                                },
                                              }
                                            : item,
                                        ),
                                      },
                                ),
                              )
                            }
                          />
                        </label>
                        <label className="flex flex-col gap-1">
                          <span className="text-12 text-content-muted">平台错误码</span>
                          <Input
                            value={mapping.code}
                            onChange={(event) =>
                              updateRoutes(
                                routes.map((route) =>
                                  route.id !== primaryRoute.id
                                    ? route
                                    : {
                                        ...route,
                                        errors: route.errors.map((item, itemIndex) =>
                                          itemIndex === index ? { ...item, code: event.target.value } : item,
                                        ),
                                      },
                                ),
                              )
                            }
                            className="sr-mono"
                          />
                        </label>
                        <label className="flex flex-col gap-1">
                          <span className="text-12 text-content-muted">可重试</span>
                          <Select
                            value={mapping.retryable ? 'true' : 'false'}
                            onValueChange={(value) =>
                              updateRoutes(
                                routes.map((route) =>
                                  route.id !== primaryRoute.id
                                    ? route
                                    : {
                                        ...route,
                                        errors: route.errors.map((item, itemIndex) =>
                                          itemIndex === index ? { ...item, retryable: value === 'true' } : item,
                                        ),
                                      },
                                ),
                              )
                            }
                            options={[
                              { value: 'true', label: '可重试' },
                              { value: 'false', label: '不可重试' },
                            ]}
                          />
                        </label>
                        <label className="flex flex-col gap-1 md:col-span-2">
                          <span className="text-12 text-content-muted">提示</span>
                          <Input
                            value={mapping.hint ?? ''}
                            onChange={(event) =>
                              updateRoutes(
                                routes.map((route) =>
                                  route.id !== primaryRoute.id
                                    ? route
                                    : {
                                        ...route,
                                        errors: route.errors.map((item, itemIndex) =>
                                          itemIndex === index ? { ...item, hint: event.target.value } : item,
                                        ),
                                      },
                                ),
                              )
                            }
                          />
                        </label>
                      </li>
                    ))}
                    <li>
                      <Button
                        variant="secondary"
                        size="sm"
                        onClick={() =>
                          updateRoutes(
                            routes.map((route) =>
                              route.id === primaryRoute.id
                                ? {
                                    ...route,
                                    errors: [
                                      ...route.errors,
                                      { match: { status: 429 }, code: 'UPSTREAM_RATE_LIMIT', retryable: true, hint: '降低并发或稍后重试' },
                                    ],
                                  }
                                : route,
                            ),
                          )
                        }
                      >
                        新增错误映射
                      </Button>
                    </li>
                  </ul>
                ) : (
                  <p className="text-13 text-warn">先配置一条启用的 route。</p>
                )}
              </section>
            </TabsContent>

            <TabsContent value="examples">
              <section className="sr-card flex flex-col gap-3 p-4">
                <h2 className="text-16 font-semibold text-content">示例（发布需至少一条且测试通过）</h2>
                {draft.examples.map((example, index) => (
                  <div key={index} className="rounded border border-border p-2">
                    <label className="flex flex-col gap-1">
                      <span className="text-12 text-content-muted">标题</span>
                      <Input
                        value={example.title ?? ''}
                        onChange={(event) =>
                          patchDraft({
                            examples: draft.examples.map((item, itemIndex) =>
                              itemIndex === index ? { ...item, title: event.target.value } : item,
                            ),
                          })
                        }
                      />
                    </label>
                    <label className="mt-2 flex flex-col gap-1">
                      <span className="text-12 text-content-muted">input（JSON）</span>
                      <Textarea
                        rows={5}
                        className="sr-mono text-12"
                        value={prettyJson(example.input)}
                        onChange={(event) => {
                          try {
                            const parsed = JSON.parse(event.target.value) as JsonObject;
                            patchDraft({
                              examples: draft.examples.map((item, itemIndex) =>
                                itemIndex === index ? { ...item, input: parsed } : item,
                              ),
                            });
                          } catch {
                            /* 等合法 JSON */
                          }
                        }}
                      />
                    </label>
                  </div>
                ))}
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => patchDraft({ examples: [...draft.examples, { title: `示例 ${draft.examples.length + 1}`, input: {} }] })}
                >
                  新增示例
                </Button>
              </section>
            </TabsContent>
          </Tabs>
        </div>

        {/* 右：测试 + 发布 */}
        <aside className="flex flex-col gap-4">
          <section className="sr-card p-4">
            <TestPanel
              capabilityId={capabilityId}
              modes={draft.modes}
              routes={routes}
              examples={draft.examples}
              onRun={runTest}
              history={testRuns}
              disabled={!saved}
            />
            {!saved ? (
              <p className="mt-2 text-12 text-warn">有未保存的改动；测试会使用已保存的配置，请先保存。</p>
            ) : null}
          </section>

          <section className="sr-card p-4" aria-label="发布状态">
            <h2 className="flex items-center gap-1.5 text-16 font-semibold text-content">
              <Layers size={16} strokeWidth={1.75} aria-hidden />
              发布
            </h2>
            <dl className="mt-2 flex flex-col gap-1 text-13">
              <div className="flex justify-between">
                <dt className="text-content-muted">已发布版本</dt>
                <dd className="sr-mono text-content">{capability.data?.latest_version ?? '未发布'}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-content-muted">草稿修订</dt>
                <dd className="sr-mono text-content">#{capability.data?.draft_revision ?? 1}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-content-muted">状态</dt>
                <dd>
                  <StatusBadge kind="capability" status={capability.data?.status ?? 'draft'} />
                </dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-content-muted">最近测试</dt>
                <dd className="text-content">
                  {lastTestRun ? (
                    <span className="flex items-center gap-1">
                      <Badge tone={lastTestRun.ok ? 'ok' : 'danger'} dot>
                        {lastTestRun.ok ? '通过' : '未通过'}
                      </Badge>
                      <span className="text-12 text-content-muted">{formatDateTime(lastTestRun.at)}</span>
                    </span>
                  ) : (
                    <span className="text-12 text-content-muted">从未测试</span>
                  )}
                </dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-content-muted">主力 route</dt>
                <dd className="truncate text-content">
                  {primaryRoute
                    ? routeProviders.find((provider) => provider.id === primaryRoute.provider_id)?.name ??
                      primaryRoute.provider_id
                    : '无'}
                </dd>
              </div>
            </dl>
            <p className="mt-2 text-12 text-content-muted">
              发布要求：最近 24 小时内有<b>一条落库的</b>通过测试（读 <code className="sr-mono">test_runs</code>，与顶部「测试通过」徽标同源），且配置已保存。
            </p>
            {published && !callable ? (
              <p className="mt-1 text-12 text-warn">
                {routesCredentialsActive
                  ? '能力状态不是 published 或已停用，因此对外不可调用。'
                  : '有启用 route 的凭证不是 active：已发布，但当前不可调用。'}
              </p>
            ) : null}
          </section>
        </aside>
      </div>

      {/* 发布对话框 */}
      <Dialog open={publishOpen} onOpenChange={setPublishOpen}>
        <DialogContent
          title="发布能力"
          description="发布后该版本不可修改；变更必须通过新版本。"
          width="sm"
          footer={
            <>
              <DialogClose asChild>
                <Button variant="secondary" size="sm">
                  取消
                </Button>
              </DialogClose>
              <Button
                variant="primary"
                size="sm"
                loading={publish.isPending}
                disabled={!publishVersion.trim()}
                onClick={() => publish.mutate()}
              >
                发布 {publishVersion}
              </Button>
            </>
          }
        >
          <div className="flex flex-col gap-3">
            <label className="flex flex-col gap-1">
              <span className="text-13 text-content">版本号（语义化）</span>
              <Input value={publishVersion} onChange={(event) => setPublishVersion(event.target.value)} className="sr-mono" />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-13 text-content">变更说明</span>
              <Textarea
                rows={4}
                value={publishChangelog}
                onChange={(event) => setPublishChangelog(event.target.value)}
                placeholder="例如：新增 gpt-4o-mini 主 route，参数映射补齐 max_tokens"
              />
            </label>
            {publish.isError ? (
              <>
                <p className="text-12 text-danger">{describeError(publish.error).message}</p>
                <RequestIdCopy requestId={describeError(publish.error).requestId ?? ''} />
              </>
            ) : null}
            <p className="text-12 text-content-muted">
              已保存 {saved ? '是' : '否'} · 测试通过 {testedOk ? '是' : '否'} · 上一次测试时间{' '}
              {lastTestRun ? formatDateTime(lastTestRun.at) : '从未测试'}
            </p>
          </div>
        </DialogContent>
      </Dialog>

      <DangerConfirm
        open={deprecateOpen}
        onOpenChange={setDeprecateOpen}
        title="停用能力"
        action="停用"
        description="停用后不再参与路由；历史调用与版本仍可查，可重新发布新版本启用。"
        impacts={[
          { id: capabilityId, label: `能力 ${draft.key}`, detail: `主力 route ${primaryRoute?.route_key ?? '无'}` },
        ]}
        loading={deprecate.isPending}
        onConfirm={() => deprecate.mutateAsync()}
      />
    </div>
  );
}

function suggestVersion(latest: string | null): string {
  if (!latest) return '1.0.0';
  const match = /^(\d+)\.(\d+)\.(\d+)$/.exec(latest);
  if (!match) return '1.0.0';
  const major = Number(match[1] ?? 0);
  const minor = Number(match[2] ?? 0);
  return `${major}.${minor + 1}.0`;
}
