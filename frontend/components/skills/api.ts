'use client';

/**
 * SKILL types and hooks (CONTRACT §5.7).
 *
 * `lib/types.ts` and `lib/queries.ts` belong to another owner and carry no SKILL
 * types yet, so this module owns its own shapes and hooks. Every field name comes
 * from `backend/app/services/skills.py` (`serialise`, `serialise_version`,
 * `dependency_impact`) or `skill_import.py`.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api } from '@/lib/api';
import type { ListEnvelope } from '@/lib/types';

/* -------------------------------------------------------------------- types -- */

export type SkillStatus = 'draft' | 'published' | 'disabled';
export type SkillVersionStatus = 'draft' | 'testing' | 'published' | 'deprecated';
export type PlanStepKind = 'capability' | 'agent' | 'transform';

export interface SkillIssue {
  severity: 'error' | 'warning';
  field: string;
  message: string;
}

export interface SkillDependency {
  capability: string;
  version?: string | number | null;
  required?: boolean;
}

export interface SkillPermissions {
  network?: string[];
  files?: string[];
  credentials?: string[];
  [key: string]: unknown;
}

export interface SkillPlanStepRaw {
  id?: string;
  kind?: PlanStepKind | string;
  capability?: string;
  version?: string | number | null;
  instruction?: string;
  input?: Record<string, unknown> | string;
  map?: Record<string, string>;
  set?: Record<string, string>;
  save_as?: string | null;
  on_error?: 'stop' | 'continue';
  note?: string | null;
}

export interface SkillPlanStepView {
  id: string;
  kind: PlanStepKind | null;
  on_error: string;
  save_as: string | null;
  note: string | null;
  requires_live_agent: boolean;
  capability?: string;
  version?: string | number | null;
  instruction?: string;
  map?: Record<string, string>;
}

export interface SkillManifest {
  name: string;
  description: string;
  when_to_use: string;
  inputs: Record<string, unknown>;
  outputs: Record<string, unknown>;
  dependencies: SkillDependency[];
  permissions: SkillPermissions;
  instructions: string;
  plan: SkillPlanStepRaw[];
  examples: Array<{ input: Record<string, unknown>; note?: string }>;
  sources: Array<{ type: string; uri?: string; commit?: string }>;
}

export interface SkillVersion {
  id: string;
  skill_id: string;
  version: string;
  status: SkillVersionStatus;
  checksum: string;
  created_at: string;
  published_at: string | null;
  immutable: boolean;
  dependencies: SkillDependency[];
  plan: SkillPlanStepView[];
  requires_live_agent: boolean;
  step_count: number;
  validation?: { issues: SkillIssue[]; blocking: boolean };
  can_publish?: boolean;
  manifest?: SkillManifest;
}

export interface DependencyImpactEntry {
  capability: string;
  capability_id: string | null;
  capability_name: string | null;
  capability_status: string | null;
  exists: boolean;
  requested_versions: string[];
  published_versions: Array<{ id: string; version: number; published_at: string | null }>;
  fields: string[];
  step_ids: string[];
  resolvable: boolean;
  used_by_published_skills: Array<{ id: string; slug: string; name: string }>;
  blocking: boolean;
}

export interface DependencyImpact {
  skill_id: string;
  skill_slug: string;
  skill_status: SkillStatus;
  version_id: string | null;
  version: string | null;
  version_status: SkillVersionStatus | null;
  impact: DependencyImpactEntry[];
  blocking: boolean;
  summary: string;
}

export interface Skill {
  id: string;
  slug: string;
  name: string;
  description: string;
  category: string;
  status: SkillStatus;
  source: { type: string | null; ref: string | null };
  current_version_id: string | null;
  current_version: SkillVersion | null;
  version_count: number;
  latest_published_version: SkillVersion | null;
  requires_live_agent: boolean;
  created_at: string;
  updated_at: string;
  versions?: SkillVersion[];
  dependency_impact?: DependencyImpact;
}

export interface SkillFilters {
  search?: string;
  category?: string;
  status?: SkillStatus | '';
  limit?: number;
  offset?: number;
}

export interface SkillCreateInput {
  name: string;
  slug?: string;
  description: string;
  category: string;
}

export interface SkillUpdateInput {
  name?: string;
  description?: string;
  category?: string;
  status?: SkillStatus;
}

export interface ImportFileEntry {
  path: string;
  size: number;
  kind: 'text' | 'binary' | 'script' | 'install_hook' | 'link';
  requires_review: boolean;
  review_reason: string | null;
}

export interface SkillImportPreview {
  preview_id: string;
  status: string;
  source_type: 'git' | 'web' | 'file';
  source_uri: string;
  resolved: Record<string, unknown>;
  limits: {
    max_files: number;
    max_total_bytes: number;
    max_file_bytes: number;
    file_count: number;
    total_bytes: number;
    truncated: boolean;
  };
  files: ImportFileEntry[];
  requires_review: Array<{ path: string; kind: string; reason: string | null }>;
  scripts_executed: boolean;
  scripts_policy: string;
  detected_skill_document: string | null;
  frontmatter_keys: string[];
  dependencies: SkillDependency[];
  permissions: string[];
  requires_permissions_confirmation: boolean;
  proposed_manifest: SkillManifest;
  validation: { issues: SkillIssue[]; blocking: boolean };
  notes: string[];
}

export interface SkillImportRecord {
  id: string;
  skill_id: string | null;
  source_type: string;
  source_uri: string;
  resolved_commit: string | null;
  status: 'pending' | 'verified' | 'rejected' | 'failed' | 'imported';
  files: ImportFileEntry[];
  file_count: number;
  dependencies: SkillDependency[];
  permissions: string[];
  requires_review: Array<{ path: string; kind: string; reason: string | null }>;
  scripts_executed: boolean;
  detected_skill_document: string | null;
  validation: { issues: SkillIssue[]; blocking: boolean };
  requires_permissions_confirmation: boolean;
  created_at: string;
  finished_at: string | null;
}

export interface SkillImportCommitResult {
  import: SkillImportRecord;
  skill: Skill;
}

export interface SkillTestStepResult {
  id: string;
  kind: string;
  status: string;
  capability?: string;
  version?: number | null;
  invocation_id?: string;
  request_id?: string;
  result_preview?: string | null;
  duration_ms: number;
  error?: { code?: string; message?: string; action?: string } | null;
}

export interface SkillTestResult {
  skill: { id: string; slug: string; name: string };
  version: { id: string; version: string; status: SkillVersionStatus };
  status: string;
  steps: SkillTestStepResult[];
  output: unknown;
  outputs: Record<string, unknown>;
  usage: Record<string, unknown>;
  duration_ms: number;
  surface: string;
}

export interface SkillExportBundle {
  [key: string]: unknown;
}

export interface CapabilityOption {
  id: string;
  slug: string;
  name: string;
  status: string;
  published_versions: number[];
}

/* ---------------------------------------------------------------- endpoints -- */

export const skillEndpoints = {
  skills: '/api/admin/skills',
  skill: (id: string) => `/api/admin/skills/${encodeURIComponent(id)}`,
  versions: (id: string) => `/api/admin/skills/${encodeURIComponent(id)}/versions`,
  version: (id: string, versionId: string) =>
    `/api/admin/skills/${encodeURIComponent(id)}/versions/${encodeURIComponent(versionId)}`,
  validate: (id: string, versionId: string) =>
    `/api/admin/skills/${encodeURIComponent(id)}/versions/${encodeURIComponent(versionId)}/validate`,
  publish: (id: string, versionId: string) =>
    `/api/admin/skills/${encodeURIComponent(id)}/versions/${encodeURIComponent(versionId)}/publish`,
  test: (id: string) => `/api/admin/skills/${encodeURIComponent(id)}/test`,
  impact: (id: string) => `/api/admin/skills/${encodeURIComponent(id)}/impact`,
  export: (id: string, format: 'skill' | 'json') =>
    `/api/admin/skills/${encodeURIComponent(id)}/export?format=${format}`,
  importPreview: '/api/admin/skills/import/preview',
  importCommit: '/api/admin/skills/import',
  imports: '/api/admin/skills/imports',
} as const;

function clean(params: Record<string, unknown>): Record<string, string | number> {
  const out: Record<string, string | number> = {};
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue;
    if (typeof value === 'string' || typeof value === 'number') out[key] = value;
  }
  return out;
}

/* -------------------------------------------------------------------- hooks -- */

export const skillKeys = {
  list: (filters: SkillFilters) => ['skills', filters] as const,
  detail: (id: string) => ['skill', id] as const,
  impact: (id: string) => ['skill', id, 'impact'] as const,
  imports: ['skill-imports'] as const,
};

export function useSkillsQuery(filters: SkillFilters) {
  return useQuery<ListEnvelope<Skill>, Error>({
    queryKey: skillKeys.list(filters),
    queryFn: ({ signal }) =>
      api.get<ListEnvelope<Skill>>(skillEndpoints.skills, { query: clean({ ...filters }), signal }),
  });
}

export function useSkillQuery(id: string | undefined) {
  return useQuery<Skill, Error>({
    queryKey: skillKeys.detail(id ?? 'missing'),
    queryFn: ({ signal }) => api.get<Skill>(skillEndpoints.skill(id as string), { signal }),
    enabled: Boolean(id),
  });
}

export function useImportHistoryQuery(enabled: boolean) {
  return useQuery<ListEnvelope<SkillImportRecord>, Error>({
    queryKey: skillKeys.imports,
    queryFn: ({ signal }) =>
      api.get<ListEnvelope<SkillImportRecord>>(skillEndpoints.imports, { query: { limit: 20 }, signal }),
    enabled,
  });
}

function useInvalidateSkill(id?: string) {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: ['skills'] });
    if (id) void queryClient.invalidateQueries({ queryKey: skillKeys.detail(id) });
    void queryClient.invalidateQueries({ queryKey: ['overview'] });
  };
}

export function useCreateSkillMutation() {
  const invalidate = useInvalidateSkill();
  return useMutation<Skill, Error, SkillCreateInput>({
    mutationFn: (body) => api.post<Skill>(skillEndpoints.skills, body),
    onSuccess: invalidate,
  });
}

export function useUpdateSkillMutation(id: string) {
  const queryClient = useQueryClient();
  const invalidate = useInvalidateSkill(id);
  return useMutation<Skill, Error, SkillUpdateInput>({
    mutationFn: (body) => api.patch<Skill>(skillEndpoints.skill(id), body),
    onSuccess: (updated) => {
      queryClient.setQueryData(skillKeys.detail(id), updated);
      invalidate();
    },
  });
}

export function useDeleteSkillMutation(id: string) {
  const queryClient = useQueryClient();
  return useMutation<{ deleted?: boolean; id?: string; skill_id?: string }, Error, void>({
    mutationFn: () => api.del(skillEndpoints.skill(id)),
    onSuccess: () => {
      queryClient.removeQueries({ queryKey: skillKeys.detail(id) });
      void queryClient.invalidateQueries({ queryKey: ['skills'] });
      void queryClient.invalidateQueries({ queryKey: ['overview'] });
    },
  });
}

export function useCreateVersionMutation(id: string) {
  const invalidate = useInvalidateSkill(id);
  return useMutation<SkillVersion, Error, { manifest: SkillManifest; version?: string }>({
    mutationFn: (body) => api.post<SkillVersion>(skillEndpoints.versions(id), body),
    onSuccess: invalidate,
  });
}

export function useUpdateVersionMutation(id: string) {
  const invalidate = useInvalidateSkill(id);
  return useMutation<SkillVersion, Error, { versionId: string; manifest: SkillManifest }>({
    mutationFn: ({ versionId, manifest }) =>
      api.patch<SkillVersion>(skillEndpoints.version(id, versionId), { manifest }),
    onSuccess: invalidate,
  });
}

export function useValidateSkillVersionMutation(id: string) {
  const queryClient = useQueryClient();
  return useMutation<{ issues: SkillIssue[]; blocking: boolean }, Error, string>({
    mutationFn: (versionId) =>
      api.post<{ issues: SkillIssue[]; blocking: boolean }>(skillEndpoints.validate(id, versionId)),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: skillKeys.detail(id) });
    },
  });
}

export function usePublishSkillVersionMutation(id: string) {
  const invalidate = useInvalidateSkill(id);
  return useMutation<Skill, Error, string>({
    mutationFn: (versionId) => api.post<Skill>(skillEndpoints.publish(id, versionId)),
    onSuccess: invalidate,
  });
}

export function useTestSkillMutation(id: string) {
  return useMutation<SkillTestResult, Error, { version_id?: string; input: Record<string, unknown> }>({
    mutationFn: (body) => api.post<SkillTestResult>(skillEndpoints.test(id), body),
  });
}

export function useImportPreviewMutation() {
  return useMutation<
    SkillImportPreview,
    Error,
    { source_type: 'git' | 'web' | 'file'; uri?: string; file_id?: string }
  >({
    mutationFn: (body) => api.post<SkillImportPreview>(skillEndpoints.importPreview, body),
  });
}

export function useImportCommitMutation() {
  const invalidate = useInvalidateSkill();
  return useMutation<SkillImportCommitResult, Error, { preview_id: string; confirm_permissions: boolean }>({
    mutationFn: (body) => api.post<SkillImportCommitResult>(skillEndpoints.importCommit, body),
    onSuccess: invalidate,
  });
}

/** Capability picker options: published versions per capability, from the list endpoint. */
export function useCapabilityOptionsQuery() {
  return useQuery<CapabilityOption[], Error>({
    queryKey: ['skill-capability-options'],
    queryFn: async ({ signal }) => {
      const payload = await api.get<ListEnvelope<{
        id: string;
        slug: string;
        name: string;
        status: string;
        current_version?: { version: number | string; status: string } | null;
        published_version?: number | null;
        counts?: { versions?: number };
      }>>('/api/admin/capabilities', { query: { limit: 200 }, signal });
      return payload.items.map((item) => {
        const published = item.published_version;
        const versions = typeof published === 'number' ? [published] : [];
        return {
          id: item.id,
          slug: item.slug,
          name: item.name,
          status: item.status,
          published_versions: versions,
        };
      });
    },
    staleTime: 30_000,
  });
}
