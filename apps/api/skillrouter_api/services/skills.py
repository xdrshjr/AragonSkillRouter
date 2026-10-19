"""SKILL 服务：CRUD / 版本 / 导出 / 执行计划 / 调用测试（§7.9）。"""

from __future__ import annotations

import io
import json
import re
import zipfile

from sqlalchemy import desc, select

from ..db import db_session, db_write, now_iso, uuid_pk
from ..errors import ApiError, Code
from ..logging import get_logger, redact
from ..models import Capability, Skill, SkillExport, SkillFile, SkillVersion
from ..schemas.common import paginate, sort_items
from ..settings import settings
from . import audit, files, versions

log = get_logger("skillrouter.run")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
SCRIPT_SUFFIXES = (".sh", ".py", ".js", ".mjs", ".ps1", ".bat")


# ── 导出契约（§7.9）─────────────────────────────────────────


def invoke_helper_source(public_base_url: str | None = None) -> str:
    """随包安装的零依赖 `invoke.mjs`（由后端下发给 CLI 与 bundle）。"""
    return f"""#!/usr/bin/env node
// Skill Router 调用助手（零依赖，Node >= 18）
// 用法：
//   node ./invoke.mjs capabilities.list [--refresh]
//   node ./invoke.mjs capabilities.get <key>
//   node ./invoke.mjs invoke --capability <key> [--version <v>] [--mode sync|async|stream] --input '<json>'
//   node ./invoke.mjs task.status <task_id>
//   node ./invoke.mjs task.cancel <task_id>
//   node ./invoke.mjs skills.list | skills.read <name>
//   node ./invoke.mjs health
// 令牌从 ~/.skillrouter/credentials.json 解析，绝不出现在命令行参数里。
import {{ readFileSync, existsSync, statSync }} from 'node:fs';
import {{ homedir }} from 'node:os';
import {{ join, dirname }} from 'node:path';
import {{ fileURLToPath }} from 'node:url';

const DEFAULT_BASE_URL = {json.dumps(public_base_url or settings.public_base_url)};
const here = dirname(fileURLToPath(import.meta.url));

function fail(code, message, extra) {{
  process.stdout.write(JSON.stringify({{ ok: false, error: {{ code, message, ...(extra || {{}}) }} }}) + '\\n');
  process.exit(1);
}}

function readConfig() {{
  const path = join(here, 'skillrouter.config.json');
  if (!existsSync(path)) fail('CONFIG_MISSING', '缺少 skillrouter.config.json，请重新运行 skillrouter install');
  try {{ return JSON.parse(readFileSync(path, 'utf8')); }}
  catch (e) {{ fail('CONFIG_INVALID', 'skillrouter.config.json 不是合法 JSON'); }}
}}

function resolveToken(config) {{
  if (process.env.SKILLROUTER_TOKEN) return process.env.SKILLROUTER_TOKEN;
  const home = process.env.SKILLROUTER_HOME || join(homedir(), '.skillrouter');
  const path = join(home, 'credentials.json');
  if (!existsSync(path)) fail('TOKEN_MISSING', '未找到平台令牌，请运行 skillrouter install --token-stdin');
  try {{
    const raw = JSON.parse(readFileSync(path, 'utf8'));
    const ref = config.token_ref || 'default';
    const value = raw[ref] || raw.token || raw.default;
    if (!value) fail('TOKEN_MISSING', '凭据文件中没有该 token_ref 的记录');
    return value;
  }} catch (e) {{ fail('TOKEN_UNREADABLE', '无法读取凭据文件'); }}
}}

function baseUrl(config) {{
  return (config.base_url || DEFAULT_BASE_URL).replace(/\\/$/, '');
}}

function parseArgs(argv) {{
  const out = {{ _: [] }};
  for (let i = 0; i < argv.length; i++) {{
    const arg = argv[i];
    if (arg.startsWith('--')) {{
      const key = arg.slice(2);
      const next = argv[i + 1];
      if (next === undefined || next.startsWith('--')) {{ out[key] = true; }}
      else {{ out[key] = next; i++; }}
    }} else {{ out._.push(arg); }}
  }}
  return out;
}}

async function call(config, token, path, options = {{}}) {{
  const url = baseUrl(config) + path;
  const response = await fetch(url, {{
    method: options.method || 'GET',
    headers: {{ Authorization: 'Bearer ' + token, 'Content-Type': 'application/json', ...(options.headers || {{}}) }},
    body: options.body ? JSON.stringify(options.body) : undefined,
  }});
  const text = await response.text();
  let body = null;
  try {{ body = text ? JSON.parse(text) : null; }} catch {{ body = {{ raw: text }}; }}
  if (!response.ok) {{
    const err = (body && body.error) || {{ code: 'HTTP_' + response.status, message: text }};
    fail(err.code || 'HTTP_ERROR', err.message || '请求失败', {{ hint: err.hint, request_id: err.request_id, status: response.status }});
  }}
  return body;
}}

async function streamInvoke(config, token, key, input, version) {{
  const response = await fetch(baseUrl(config) + '/api/v1/capabilities/' + encodeURIComponent(key) + '/invoke', {{
    method: 'POST',
    headers: {{ Authorization: 'Bearer ' + token, 'Content-Type': 'application/json', Accept: 'text/event-stream' }},
    body: JSON.stringify({{ capability_version: version, input, options: {{ mode: 'stream' }} }}),
  }});
  if (!response.ok || !response.body) {{
    const text = await response.text();
    fail('STREAM_FAILED', text || ('HTTP ' + response.status));
  }}
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {{
    const {{ done, value }} = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, {{ stream: true }});
    let index;
    while ((index = buffer.indexOf('\\n\\n')) >= 0) {{
      const frame = buffer.slice(0, index);
      buffer = buffer.slice(index + 2);
      for (const line of frame.split('\\n')) {{
        if (!line.startsWith('data:')) continue;
        const raw = line.slice(5).trim();
        if (!raw) continue;
        process.stdout.write(raw + '\\n');
      }}
    }}
  }}
}}

async function main() {{
  const args = parseArgs(process.argv.slice(2));
  const cmd = args._[0];
  const config = readConfig();
  if (cmd === 'health') {{
    const response = await fetch(baseUrl(config) + '/api/v1/health');
    process.stdout.write(JSON.stringify(await response.json()) + '\\n');
    return;
  }}
  const token = resolveToken(config);
  if (cmd === 'capabilities.list') {{
    const suffix = args.refresh ? '?refresh=1' : '';
    process.stdout.write(JSON.stringify(await call(config, token, '/api/v1/capabilities' + suffix)) + '\\n');
  }} else if (cmd === 'capabilities.get') {{
    const key = args._[1];
    if (!key) fail('USAGE', '用法：capabilities.get <key>');
    process.stdout.write(JSON.stringify(await call(config, token, '/api/v1/capabilities/' + encodeURIComponent(key))) + '\\n');
  }} else if (cmd === 'invoke') {{
    const key = args.capability;
    if (!key) fail('USAGE', "用法：invoke --capability <key> --input '<json>'");
    let input = {{}};
    if (typeof args.input === 'string') {{
      try {{ input = JSON.parse(args.input); }} catch (e) {{ fail('USAGE', '--input 必须是合法 JSON'); }}
    }}
    const mode = args.mode || 'sync';
    if (mode === 'stream') {{ await streamInvoke(config, token, key, input, args.version); return; }}
    const body = await call(config, token, '/api/v1/capabilities/' + encodeURIComponent(key) + '/invoke', {{
      method: 'POST',
      body: {{ capability_version: args.version, input, options: {{ mode }} }},
    }});
    process.stdout.write(JSON.stringify(body) + '\\n');
  }} else if (cmd === 'task.status') {{
    const id = args._[1];
    if (!id) fail('USAGE', '用法：task.status <task_id>');
    process.stdout.write(JSON.stringify(await call(config, token, '/api/v1/tasks/' + encodeURIComponent(id))) + '\\n');
  }} else if (cmd === 'task.cancel') {{
    const id = args._[1];
    if (!id) fail('USAGE', '用法：task.cancel <task_id>');
    process.stdout.write(JSON.stringify(await call(config, token, '/api/v1/tasks/' + encodeURIComponent(id) + '/cancel', {{ method: 'POST', body: {{}} }})) + '\\n');
  }} else if (cmd === 'skills.list') {{
    process.stdout.write(JSON.stringify(await call(config, token, '/api/v1/skills')) + '\\n');
  }} else if (cmd === 'skills.read') {{
    const name = args._[1];
    if (!name) fail('USAGE', '用法：skills.read <name>');
    process.stdout.write(JSON.stringify(await call(config, token, '/api/v1/skills/' + encodeURIComponent(name))) + '\\n');
  }} else {{
    fail('USAGE', '未知命令：' + (cmd || '(空)'));
  }}
}}

main().catch((err) => fail('UNEXPECTED', String(err && err.message ? err.message : err)));
"""


def render_skill_md(skill: Skill, version: SkillVersion | None, *, public_base_url: str | None = None) -> str:
    definition = skill.draft_definition or {}
    frontmatter = (version.frontmatter if version else None) or definition.get("frontmatter") or {}
    name = skill.name
    description = skill.description or skill.title
    activation = frontmatter.get("activation") or definition.get("activation") or "auto"
    required = [
        dep.get("capability_key")
        for dep in ((version.dependencies if version else definition.get("dependencies")) or [])
        if dep.get("capability_key")
    ]
    base_url = public_base_url or settings.public_base_url
    body = (version.skill_md if version else None) or definition.get("instructions") or ""
    if not body.strip():
        body = _default_body(skill, required)
    header = [
        "---",
        f"name: {name}",
        f"description: {description}",
        f"version: {(version.version if version else skill.latest_version) or definition.get('version', '0.1.0')}",
        "license: proprietary",
        "allowed-tools: [bash]",
        f"activation: {activation}",
        "metadata:",
        "  skillrouter:",
        f"    base_url: {base_url}",
        "    helper: ./invoke.mjs",
        "    config: ./skillrouter.config.json",
        f"    required_capabilities: [{', '.join(required)}]",
        "---",
        "",
    ]
    return "\n".join(header) + body.lstrip("\n")


def _default_body(skill: Skill, required: list[str]) -> str:
    lines = [
        f"# {skill.title}",
        "",
        skill.description or "",
        "",
        "## 使用说明",
        "1. 列出可用能力（平台新增能力会即时出现在结果里，无需重装）：",
        "   `node ./invoke.mjs capabilities.list`",
        "2. 查看能力参数（`input_schema` 即输入约束）：",
        "   `node ./invoke.mjs capabilities.get <key>`",
        "3. 调用能力：",
        "   `node ./invoke.mjs invoke --capability <key> --mode sync --input '{\"prompt\":\"你好\"}'`",
        "   若返回 `task_id`，用 `node ./invoke.mjs task.status <task_id>` 轮询。",
        "4. 错误体含 `code`/`hint`/`request_id`；把 `request_id` 报给管理员可定位问题。",
        "",
        "只用 `invoke.mjs` 访问平台；不要尝试读取或转述任何密钥——平台令牌已通过 `token_ref` 间接引用。",
    ]
    if required:
        lines += ["", f"依赖能力：{', '.join(required)}"]
    return "\n".join(lines) + "\n"


def config_json(skill: Skill) -> dict:
    return {
        "base_url": settings.public_base_url,
        "transport": "http",
        "token_ref": f"skill:{skill.name}",
        "installed_at": now_iso(),
        "cli_version": settings.cli_package_version,
        "server_version": settings.cli_package_version,
    }


# ── CRUD ─────────────────────────────────────────────────────


def _brief(row: Skill, version: SkillVersion | None) -> dict:
    definition = row.draft_definition or {}
    deps = (version.dependencies if version else definition.get("dependencies")) or []
    return {
        "id": row.id,
        "name": row.name,
        "title": row.title,
        "description": row.description,
        "category": row.category,
        "tags": row.tags or [],
        "status": row.status,
        "enabled": bool(row.enabled),
        "latest_version": row.latest_version,
        "published_version_id": row.published_version_id,
        "source_type": row.source_type,
        "source_ref": row.source_ref,
        "source_url": row.source_url,
        "dependency_count": len(deps),
        "dependencies": deps,
        "activation": definition.get("activation", "auto"),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def list_skills(
    *,
    q: str | None = None,
    status: str | None = None,
    category: str | None = None,
    enabled: bool | None = None,
    page: int = 1,
    page_size: int = 20,
    sort: str | None = None,
) -> dict:
    with db_session() as s:
        rows = list(s.scalars(select(Skill).where(Skill.deleted_at.is_(None))))
        versions_map = {
            v.id: v for v in s.scalars(select(SkillVersion)).all()
        }
    items = [_brief(row, versions_map.get(row.published_version_id or "")) for row in rows]
    if q:
        needle = q.lower()
        items = [i for i in items if needle in i["name"].lower() or needle in i["title"].lower()]
    if status:
        items = [i for i in items if i["status"] == status]
    if category:
        items = [i for i in items if i["category"] == category]
    if enabled is not None:
        items = [i for i in items if i["enabled"] is enabled]
    items = sort_items(items, sort, default="-updated_at")
    return paginate(items, page, page_size)


def get_skill(skill_id: str) -> dict:
    with db_session() as s:
        row = s.get(Skill, skill_id)
        if row is None or row.deleted_at:
            raise ApiError(Code.NOT_FOUND, "SKILL 不存在")
        published = s.get(SkillVersion, row.published_version_id) if row.published_version_id else None
        data = _brief(row, published)
        data["definition"] = row.draft_definition or {}
        data["skill_md"] = render_skill_md(row, published)
        data["published_skill_md"] = render_skill_md(row, published) if published else None
        data["versions"] = [
            {
                "id": v.id,
                "version": v.version,
                "status": v.status,
                "published_at": v.published_at,
                "is_current": v.id == row.published_version_id,
            }
            for v in s.scalars(
                select(SkillVersion).where(SkillVersion.skill_id == skill_id).order_by(desc(SkillVersion.created_at))
            )
        ]
        data["install_command"] = settings.install_command()
        missing = _missing_dependencies(data.get("dependencies") or [])
        data["missing_dependencies"] = missing
        data["installable"] = bool(row.status == "published" and row.enabled)
    return data


def _missing_dependencies(dependencies: list[dict]) -> list[dict]:
    missing = []
    with db_session() as s:
        for dep in dependencies:
            key = dep.get("capability_key")
            if not key:
                continue
            row = s.scalar(select(Capability).where(Capability.key == key, Capability.deleted_at.is_(None)))
            if row is None or row.status != "published" or not row.enabled:
                missing.append(
                    {
                        "capability_key": key,
                        "version_range": dep.get("version_range"),
                        "reason": "能力不存在" if row is None else f"状态为 {row.status}",
                    }
                )
                continue
            if not versions.in_range(row.latest_version or "0.0.0", dep.get("version_range")):
                missing.append(
                    {
                        "capability_key": key,
                        "version_range": dep.get("version_range"),
                        "reason": f"当前版本 {row.latest_version} 不满足依赖区间",
                    }
                )
    return missing


def create_skill(data: dict, *, actor: str | None = None) -> dict:
    if not NAME_RE.match(data["name"]):
        raise ApiError(
            Code.VALIDATION_FAILED,
            message=f"name 必须是 kebab-case（2-64 字符）：{data['name']}",
        )
    skill_id = uuid_pk("skl")
    definition = {
        "title": data["title"],
        "description": data.get("description") or "",
        "when_to_use": data.get("when_to_use"),
        "inputs": data.get("inputs") or [],
        "outputs": data.get("outputs") or [],
        "dependencies": data.get("dependencies") or [],
        "instructions": data.get("instructions") or "",
        "examples": data.get("examples") or [],
        "permissions": data.get("permissions") or {"allowed_tools": ["bash"], "needs_agent_reasoning": False, "network": True},
        "execution_plan": data.get("execution_plan") or {},
        "version": data.get("version") or "0.1.0",
        "activation": data.get("activation") or "auto",
        "frontmatter": {},
        "category": data.get("category"),
    }
    with db_write() as s:
        if s.scalar(select(Skill).where(Skill.name == data["name"])):
            raise ApiError(Code.CONFLICT, f"SKILL 名称已存在：{data['name']}")
        s.add(
            Skill(
                id=skill_id,
                name=data["name"],
                title=data["title"],
                description=definition["description"],
                category=data.get("category"),
                tags=data.get("tags") or [],
                status="draft",
                enabled=1,
                draft_definition=definition,
                source_type="manual",
                created_at=now_iso(),
                updated_at=now_iso(),
            )
        )
    audit.write("skill.created", object_type="skill", object_id=skill_id, summary={"name": data["name"]}, actor_id=actor)
    return get_skill(skill_id)


def update_skill(skill_id: str, patch: dict, *, actor: str | None = None) -> dict:
    with db_write() as s:
        row = s.get(Skill, skill_id)
        if row is None or row.deleted_at:
            raise ApiError(Code.NOT_FOUND, "SKILL 不存在")
        definition = dict(row.draft_definition or {})
        for key in (
            "when_to_use",
            "inputs",
            "outputs",
            "dependencies",
            "instructions",
            "examples",
            "permissions",
            "execution_plan",
            "activation",
        ):
            if patch.get(key) is not None:
                definition[key] = patch[key]
        row.draft_definition = definition
        if patch.get("title"):
            row.title = patch["title"]
            definition["title"] = patch["title"]
        if patch.get("description") is not None:
            row.description = patch["description"]
            definition["description"] = patch["description"]
        if patch.get("category") is not None:
            row.category = patch["category"]
        if patch.get("tags") is not None:
            row.tags = patch["tags"]
        row.updated_at = now_iso()
    return get_skill(skill_id)


def set_enabled(skill_id: str, enabled: bool, *, actor: str | None = None) -> dict:
    """启用/停用：独立于 status，不产生新版本（§7.9 / R17）。"""
    with db_write() as s:
        row = s.get(Skill, skill_id)
        if row is None or row.deleted_at:
            raise ApiError(Code.NOT_FOUND, "SKILL 不存在")
        row.enabled = 1 if enabled else 0
        row.updated_at = now_iso()
    audit.write(
        "skill.enabled" if enabled else "skill.disabled",
        object_type="skill",
        object_id=skill_id,
        summary={"enabled": enabled},
        actor_id=actor,
    )
    return get_skill(skill_id)


def publish(skill_id: str, *, version: str | None = None, changelog: str | None = None, actor: str | None = None) -> dict:
    data = get_skill(skill_id)
    missing = _missing_dependencies(data.get("dependencies") or [])
    if missing:
        raise ApiError(
            Code.SKILL_DEPENDENCY_MISSING,
            message="SKILL 依赖的能力版本缺失或未发布",
            hint="先发布依赖能力，或修改依赖区间",
            details={"missing": missing},
        )
    definition = data["definition"]
    target = version or versions.next_version(data.get("latest_version"))
    with db_write() as s:
        row = s.get(Skill, skill_id)
        if row is None:
            raise ApiError(Code.NOT_FOUND, "SKILL 不存在")
        if s.scalar(
            select(SkillVersion).where(SkillVersion.skill_id == skill_id, SkillVersion.version == target)
        ):
            raise ApiError(Code.CONFLICT, f"版本 {target} 已存在")
        provisional = SkillVersion(
            id="tmp",
            skill_id=skill_id,
            version=target,
            content_hash="",
            skill_md=definition.get("instructions") or "",
            frontmatter={},
            execution_plan=definition.get("execution_plan") or {},
            dependencies=definition.get("dependencies") or [],
            permissions=definition.get("permissions") or {},
            examples=definition.get("examples") or [],
            status="published",
            created_at=now_iso(),
        )
        skill_md = render_skill_md(row, provisional)
        version_id = versions.freeze_skill(
            s,
            skill_id=skill_id,
            version=target,
            skill_md=skill_md,
            frontmatter={"activation": definition.get("activation", "auto"), "allowed-tools": ["bash"]},
            execution_plan=definition.get("execution_plan") or {},
            dependencies=definition.get("dependencies") or [],
            permissions=definition.get("permissions") or {},
            examples=definition.get("examples") or [],
        )
        row.published_version_id = version_id
        row.latest_version = target
        row.status = "published"
        row.enabled = 1
        row.updated_at = now_iso()
    audit.write(
        "skill.published",
        object_type="skill",
        object_id=skill_id,
        summary={"version": target, "changelog": changelog},
        actor_id=actor,
    )
    return {"ok": True, "version": target, "version_id": version_id}


def deprecate(skill_id: str, *, actor: str | None = None) -> dict:
    with db_write() as s:
        row = s.get(Skill, skill_id)
        if row is None:
            raise ApiError(Code.NOT_FOUND, "SKILL 不存在")
        row.status = "deprecated"
        row.updated_at = now_iso()
    audit.write("skill.deprecated", object_type="skill", object_id=skill_id, actor_id=actor)
    return {"ok": True, "status": "deprecated"}


def delete(skill_id: str, *, actor: str | None = None) -> dict:
    with db_write() as s:
        row = s.get(Skill, skill_id)
        if row is None or row.deleted_at:
            raise ApiError(Code.NOT_FOUND, "SKILL 不存在")
        row.deleted_at = now_iso()
        row.enabled = 0
    audit.write("skill.deleted", object_type="skill", object_id=skill_id, actor_id=actor)
    return {"ok": True, "id": skill_id}


def list_versions(skill_id: str) -> list[dict]:
    with db_session() as s:
        row = s.get(Skill, skill_id)
        if row is None:
            raise ApiError(Code.NOT_FOUND, "SKILL 不存在")
        return [
            {
                "id": v.id,
                "version": v.version,
                "status": v.status,
                "published_at": v.published_at,
                "changelog": None,
                "content_hash": v.content_hash,
                "dependencies": v.dependencies or [],
                "is_current": v.id == row.published_version_id,
            }
            for v in s.scalars(
                select(SkillVersion).where(SkillVersion.skill_id == skill_id).order_by(desc(SkillVersion.created_at))
            )
        ]


def diff(skill_id: str, from_version: str, to_version: str) -> dict:
    with db_session() as s:
        left = s.scalar(
            select(SkillVersion).where(SkillVersion.skill_id == skill_id, SkillVersion.version == from_version)
        )
        right = s.scalar(
            select(SkillVersion).where(SkillVersion.skill_id == skill_id, SkillVersion.version == to_version)
        )
        if left is None or right is None:
            raise ApiError(Code.NOT_FOUND, "版本不存在")
        return {
            "from": from_version,
            "to": to_version,
            "changes": versions.diff(
                {"skill_md": left.skill_md, "dependencies": left.dependencies, "plan": left.execution_plan},
                {"skill_md": right.skill_md, "dependencies": right.dependencies, "plan": right.execution_plan},
            ),
        }


def dependencies_of(skill_id: str) -> dict:
    data = get_skill(skill_id)
    out = []
    with db_session() as s:
        for dep in data.get("dependencies") or []:
            key = dep.get("capability_key")
            row = s.scalar(select(Capability).where(Capability.key == key))
            out.append(
                {
                    "capability_key": key,
                    "version_range": dep.get("version_range"),
                    "exists": row is not None,
                    "status": row.status if row else "missing",
                    "current_version": row.latest_version if row else None,
                    "satisfied": bool(
                        row
                        and row.status == "published"
                        and versions.in_range(row.latest_version or "0.0.0", dep.get("version_range"))
                    ),
                }
            )
    return {"skill_id": skill_id, "dependencies": out}


# ── 导出 ─────────────────────────────────────────────────────


def build_bundle(skill_id: str, fmt: str = "zip") -> tuple[bytes, str, str]:
    data = get_skill(skill_id)  # 不存在会先抛 NOT_FOUND
    with db_session() as s:
        row = s.get(Skill, skill_id)
        if row is None:  # pragma: no cover - get_skill 已保证存在；防御并发删除
            raise ApiError(Code.NOT_FOUND, "SKILL 不存在")
        version = s.get(SkillVersion, row.published_version_id) if row.published_version_id else None
        skill_md = render_skill_md(row, version)
    if fmt == "skill_md":
        return skill_md.encode(), "SKILL.md", "text/markdown"
    files_map: dict[str, bytes] = {
        "SKILL.md": skill_md.encode(),
        "invoke.mjs": invoke_helper_source().encode(),
        "skillrouter.config.json": json.dumps(config_json(row), ensure_ascii=False, indent=2).encode(),
        "README.md": _bundle_readme(row, data).encode(),
    }
    _secret_scan(files_map, skill_id)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files_map.items():
            zf.writestr(f"{row.name}/{name}", content)
    payload = buffer.getvalue()
    with db_write() as s:
        s.add(
            SkillExport(
                id=uuid_pk("ske"),
                skill_id=skill_id,
                version=version.version if version else row.latest_version,
                format=fmt,
                sha256=__import__("hashlib").sha256(payload).hexdigest(),
                created_at=now_iso(),
                created_by=None,
            )
        )
    return payload, f"{row.name}.zip", "application/zip"


def _bundle_readme(skill: Skill, data: dict) -> str:
    return (
        f"# {skill.title}\n\n"
        f"{skill.description}\n\n"
        "## 安装\n\n"
        "```\n"
        f"{data.get('install_command')}\n"
        "```\n\n"
        "或在已安装目录内直接使用：\n\n"
        "```\n"
        "node ./invoke.mjs capabilities.list\n"
        "node ./invoke.mjs invoke --capability <key> --input '{\"prompt\":\"hi\"}'\n"
        "```\n\n"
        "## 要求\n\n"
        "- Node >= 18（`invoke.mjs` 使用内置 fetch）\n"
        "- 平台访问令牌（由 `skillrouter install` 写入 `~/.skillrouter/credentials.json`）\n\n"
        "## 说明\n\n"
        "- 本包不包含任何密钥；`skillrouter.config.json` 只保存 `base_url` 与 `token_ref`。\n"
        "- 能力目录由平台动态下发，新增能力无需重装本包。\n"
    )


def _secret_scan(files_map: dict[str, bytes], skill_id: str) -> None:
    patterns = [re.compile(rb"sk-[A-Za-z0-9_\-]{16,}"), re.compile(rb"srk_[A-Za-z0-9_\-]{20,}")]
    for name, content in files_map.items():
        for pattern in patterns:
            if pattern.search(content):
                raise ApiError(
                    Code.SKILL_EXPORT_FAILED,
                    message=f"导出包 {name} 中检测到疑似密钥，已拒绝导出",
                    details={"skill_id": skill_id, "file": name},
                )


def record_export(skill_id: str, fmt: str, data: bytes, actor: str | None = None) -> dict:
    saved = files.save_bytes(
        data,
        mime="application/zip" if fmt == "zip" else "text/markdown",
        filename=f"{skill_id}.{ 'zip' if fmt == 'zip' else 'md'}",
        kind="artifact",
        owner_type="skill",
        owner_id=skill_id,
    )
    audit.write("skill.exported", object_type="skill", object_id=skill_id, summary={"format": fmt}, actor_id=actor)
    return saved


# ── 执行计划与调用测试 ───────────────────────────────────────


def validate_execution_plan(plan: dict) -> list[dict]:
    issues: list[dict] = []
    steps = (plan or {}).get("steps") or []
    if not steps:
        issues.append({"level": "warn", "path": "execution_plan.steps", "message": "未定义执行计划，SKILL 只能作为说明文档使用"})
    seen: set[str] = set()
    for index, step in enumerate(steps):
        step_id = step.get("id")
        if not step_id:
            issues.append({"level": "error", "path": f"steps[{index}].id", "message": "步骤缺少 id"})
        elif step_id in seen:
            issues.append({"level": "error", "path": f"steps[{index}].id", "message": f"步骤 id 重复：{step_id}"})
        else:
            seen.add(step_id)
        step_type = step.get("type")
        if step_type not in ("capability", "agent_reasoning", "transform"):
            issues.append(
                {
                    "level": "error",
                    "path": f"steps[{index}].type",
                    "message": "type 必须是 capability / agent_reasoning / transform",
                }
            )
        if step_type == "capability":
            key = step.get("capability_key")
            if not key:
                issues.append({"level": "error", "path": f"steps[{index}].capability_key", "message": "capability 步骤需要 capability_key"})
            else:
                with db_session() as s:
                    row = s.scalar(select(Capability).where(Capability.key == key, Capability.deleted_at.is_(None)))
                if row is None:
                    issues.append(
                        {
                            "level": "error",
                            "path": f"steps[{index}].capability_key",
                            "message": f"能力不存在：{key}",
                        }
                    )
        on_error = step.get("on_error", "stop")
        if on_error not in ("stop", "continue", "fallback"):
            issues.append(
                {
                    "level": "error",
                    "path": f"steps[{index}].on_error",
                    "message": "on_error 必须是 stop / continue / fallback",
                }
            )
    return issues


async def test_skill(skill_id: str, payload: dict, *, actor: str | None = None) -> dict:
    from . import execution

    data = get_skill(skill_id)
    missing = data.get("missing_dependencies") or []
    if missing:
        raise ApiError(
            Code.SKILL_DEPENDENCY_MISSING,
            message="SKILL 依赖缺失，无法执行",
            details={"missing": missing},
        )
    plan = (data.get("definition") or {}).get("execution_plan") or {}
    steps = plan.get("steps") or []
    issues = validate_execution_plan(plan)
    if any(i["level"] == "error" for i in issues):
        raise ApiError(
            Code.VALIDATION_FAILED,
            message="执行计划存在错误",
            details={"issues": issues},
        )
    inputs = dict(payload.get("input") or {})
    results: list[dict] = []
    used_agent = False
    for step in steps:
        step_type = step.get("type")
        if step_type == "agent_reasoning":
            used_agent = True
            results.append(
                {
                    "step": step.get("id"),
                    "type": step_type,
                    "status": "skipped",
                    "detail": "该步骤需要实时 Agent 推理，本次测试未执行（需 Agent 引擎在线）",
                }
            )
            continue
        if step_type == "transform":
            results.append({"step": step.get("id"), "type": step_type, "status": "ok", "detail": "本地转换，无外部调用"})
            continue
        key = step.get("capability_key")
        mapping = step.get("inputs") or {}
        step_input = {}
        for target, source in mapping.items():
            if isinstance(source, str) and source.startswith("input."):
                step_input[target] = inputs.get(source[6:])
            elif isinstance(source, str) and source.startswith("steps."):
                parts = source.split(".")
                prev = next((r for r in results if r.get("step") == parts[1]), None)
                step_input[target] = (prev or {}).get("output")
            else:
                step_input[target] = source
        try:
            result = await execution.invoke(
                capability_key=key,
                input_payload=step_input,
                options={"mode": "sync"},
                caller={"type": "skill", "id": skill_id},
            )
            results.append(
                {
                    "step": step.get("id"),
                    "type": step_type,
                    "status": "ok" if result.get("status") == "succeeded" else "failed",
                    "output": redact(result.get("output"), max_len=1024),
                    "usage": result.get("usage"),
                    "request_id": result.get("request_id"),
                }
            )
            if result.get("status") != "succeeded" and step.get("on_error", "stop") == "stop":
                break
        except ApiError as exc:
            results.append(
                {
                    "step": step.get("id"),
                    "type": step_type,
                    "status": "failed",
                    "error": {"code": exc.code, "message": exc.message, "hint": exc.hint},
                }
            )
            if step.get("on_error", "stop") == "stop":
                break
    ok = all(r["status"] in ("ok", "skipped") for r in results) if results else False
    return {
        "ok": ok,
        "used_agent": used_agent,
        "steps": results,
        "note": "agent_reasoning 步骤需要 Agent 引擎在线，测试中标记 skipped" if used_agent else None,
    }


# ── 对外分发 ─────────────────────────────────────────────────


def catalog() -> list[dict]:
    with db_session() as s:
        rows = list(
            s.scalars(
                select(Skill).where(
                    Skill.deleted_at.is_(None), Skill.enabled == 1, Skill.status == "published"
                )
            )
        )
        out = []
        for row in rows:
            version = s.get(SkillVersion, row.published_version_id) if row.published_version_id else None
            deps = (version.dependencies if version else []) or []
            out.append(
                {
                    "name": row.name,
                    "title": row.title,
                    "description": row.description,
                    "version": row.latest_version,
                    "category": row.category,
                    "required_capabilities": [d.get("capability_key") for d in deps if d.get("capability_key")],
                }
            )
    return sorted(out, key=lambda item: item["name"])


def public_skill(name: str) -> dict:
    with db_session() as s:
        row = s.scalar(select(Skill).where(Skill.name == name, Skill.deleted_at.is_(None)))
        if row is None:
            raise ApiError(Code.NOT_FOUND, "SKILL 不存在")
        if not row.enabled:
            raise ApiError(Code.SKILL_DISABLED, "该 SKILL 已被停用")
        if row.status != "published":
            raise ApiError(Code.NOT_FOUND, "SKILL 尚未发布")
        version = s.get(SkillVersion, row.published_version_id) if row.published_version_id else None
        skill_md = render_skill_md(row, version)
        deps = (version.dependencies if version else []) or []
        return {
            "name": row.name,
            "title": row.title,
            "description": row.description,
            "version": row.latest_version,
            "skill_md": skill_md,
            "instructions": (version.skill_md if version else "") or "",
            "invoke_mjs": invoke_helper_source(),
            "execution_plan": version.execution_plan if version else {},
            "required_capabilities": [d.get("capability_key") for d in deps if d.get("capability_key")],
            "examples": version.examples if version else [],
            "permissions": version.permissions if version else {},
            "install_command": settings.install_command(),
        }


def files_for_version(version_id: str) -> list[dict]:
    with db_session() as s:
        return [
            {"path": f.path, "size": f.size, "sha256": f.sha256, "mime": f.mime, "executable": bool(f.executable)}
            for f in s.scalars(select(SkillFile).where(SkillFile.skill_version_id == version_id))
        ]


def is_script(path: str) -> bool:
    return path.lower().endswith(SCRIPT_SUFFIXES) or path.lower().startswith("hooks/")
