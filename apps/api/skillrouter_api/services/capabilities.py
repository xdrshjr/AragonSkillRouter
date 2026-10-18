"""能力服务：定义校验、草稿/测试/发布/停用/回退、影响面（§7.6 / §7.7 / §15.5）。"""

from __future__ import annotations

import copy
import json
import re
import time
from datetime import datetime, timedelta, timezone

import jsonschema
from sqlalchemy import and_, case, desc, func, select
from sqlalchemy.orm import aliased

from ..db import db_session, db_write, now_iso, uuid_pk
from ..errors import ApiError, Code
from ..executors.http_executor import invoke_sync
from ..logging import get_logger, redact
from ..models import (
    CallLog,
    Capability,
    CapabilityRoute,
    CapabilityTestRun,
    CapabilityVersion,
    CompatibilityGroup,
    Credential,
    PlatformToken,
    Provider,
    Skill,
    Task,
)
from ..schemas.common import paginate, sort_items
from ..settings import settings
from . import audit, routing, usage, versions

log = get_logger("skillrouter.run")

KEY_RE = re.compile(r"^[a-z0-9]+(\.[a-z0-9][a-z0-9-]*)+$")
KINDS = (
    "text.generate",
    "image.generate",
    "video.generate",
    "audio.speech",
    "audio.transcribe",
    "generic.http",
)
MODES = ("sync", "async", "stream")
DEFAULT_STATUS = "draft"


# ── 定义工具 ──────────────────────────────────────────────────


def default_definition(key: str, name: str, kind: str) -> dict:
    return {
        "key": key,
        "name": name,
        "kind": kind,
        "modes": ["sync"],
        "file_io": {"input": [], "output": []},
        "input_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"prompt": {"type": "string", "description": "输入提示"}},
            "required": ["prompt"],
            "additionalProperties": False,
        },
        "output_schema": {"type": "object"},
        "examples": [],
        "compatibility_group": None,
        "failover": {"enabled": True, "max_switches": 2},
        "requires_agent_reasoning": False,
        "tags": [],
        "docs_url": None,
        "routes": [],
    }


def _definition_of(row: Capability) -> dict:
    return copy.deepcopy(row.draft_definition or {})


def _published_definition(row: Capability) -> dict | None:
    if not row.published_version_id:
        return None
    with db_session() as s:
        version = s.get(CapabilityVersion, row.published_version_id)
        return copy.deepcopy(version.definition) if version else None


def _sync_projection(session, row: Capability, definition: dict) -> None:  # noqa: ANN001
    """把 definition 的冗余列与 capability_routes 投影写入（同事务）。"""
    row.key = definition.get("key") or row.key
    row.name = definition.get("name") or row.name
    row.kind = definition.get("kind") or row.kind
    row.modes = definition.get("modes") or ["sync"]
    row.file_io = definition.get("file_io") or {}
    row.failover = definition.get("failover") or {"enabled": True, "max_switches": 2}
    row.tags = definition.get("tags") or []
    row.compatibility_group = definition.get("compatibility_group")
    row.requires_agent_reasoning = 1 if definition.get("requires_agent_reasoning") else 0
    row.draft_definition = definition
    row.draft_revision = int(row.draft_revision or 0) + 1
    row.updated_at = now_iso()

    existing = {
        route.route_key: route
        for route in session.scalars(
            select(CapabilityRoute).where(CapabilityRoute.capability_id == row.id)
        )
    }
    seen: set[str] = set()
    for route in definition.get("routes") or []:
        route_key = route.get("id") or route.get("route_key")
        if not route_key:
            continue
        if route_key in seen:
            # 重复 route id 由 validate_definition 报错；投影表保持唯一约束不被触发
            continue
        seen.add(route_key)
        target = existing.get(route_key)
        if target is None:
            target = CapabilityRoute(
                id=uuid_pk("crt"),
                capability_id=row.id,
                provider_id=route.get("provider_id") or "",
                credential_id=route.get("credential_id") or "",
                route_key=route_key,
                priority=int(route.get("priority", 0)),
                enabled=1 if route.get("enabled", True) else 0,
                model_hint=route.get("model_hint"),
                request=route.get("request") or {},
                param_map=route.get("param_map") or {},
                response_map=route.get("response_map") or {},
                stream=route.get("stream"),
                async_=route.get("async"),
                limits=route.get("limits") or {},
                errors=route.get("errors") or [],
                timeout_ms=int(route.get("timeout_ms") or 60000),
                retry=route.get("retry") or {},
                health={},
                created_at=now_iso(),
                updated_at=now_iso(),
            )
            session.add(target)
        else:
            target.provider_id = route.get("provider_id") or target.provider_id
            target.credential_id = route.get("credential_id") or target.credential_id
            target.priority = int(route.get("priority", 0))
            target.enabled = 1 if route.get("enabled", True) else 0
            target.model_hint = route.get("model_hint")
            target.request = route.get("request") or {}
            target.param_map = route.get("param_map") or {}
            target.response_map = route.get("response_map") or {}
            target.stream = route.get("stream")
            target.async_ = route.get("async")
            target.limits = route.get("limits") or {}
            target.errors = route.get("errors") or []
            target.timeout_ms = int(route.get("timeout_ms") or 60000)
            target.retry = route.get("retry") or {}
            target.updated_at = now_iso()
    for route_key, stale in existing.items():
        if route_key not in seen:
            session.delete(stale)


# ── 校验（12 条规则）────────────────────────────────────────


def _issue(level: str, path: str, code: str, message: str, hint: str = "") -> dict:
    return {"level": level, "path": path, "code": code, "message": message, "hint": hint}


def validate_definition(
    definition: dict,
    *,
    for_publish: bool = False,
    capability_id: str | None = None,
) -> list[dict]:
    issues: list[dict] = []
    key = definition.get("key") or ""
    if not KEY_RE.match(key):
        issues.append(
            _issue(
                "error",
                "key",
                "VALIDATION_FAILED",
                "key 必须是小写点分标识（如 text.generate.gpt4o-mini）",
                "只允许 a-z0-9 与 . / -",
            )
        )
    with db_session() as s:
        clash = s.scalar(select(Capability).where(Capability.key == key))
        if clash and clash.id != capability_id and not clash.deleted_at:
            issues.append(_issue("error", "key", "CONFLICT", f"key 已被占用：{key}"))
    if definition.get("kind") not in KINDS:
        issues.append(
            _issue("error", "kind", "VALIDATION_FAILED", f"kind 必须是 {', '.join(KINDS)} 之一")
        )

    modes = definition.get("modes") or []
    if not isinstance(modes, list) or not modes:
        issues.append(_issue("error", "modes", "VALIDATION_FAILED", "modes 必须是非空集合"))
    else:
        if len(set(modes)) != len(modes):
            issues.append(_issue("error", "modes", "VALIDATION_FAILED", "modes 不能重复"))
        for mode in modes:
            if mode not in MODES:
                issues.append(
                    _issue("error", "modes", "UNSUPPORTED_MODE", f"未知模式 {mode}（只允许 sync/async/stream）")
                )
    file_io = definition.get("file_io") or {}
    mime_re = re.compile(r"^(\*/\*|[a-z]+/[a-z0-9.+\-]+)$")
    for field in ("input", "output"):
        for mime in file_io.get(field) or []:
            if not mime_re.match(str(mime)):
                issues.append(
                    _issue("error", f"file_io.{field}", "VALIDATION_FAILED", f"非法 MIME：{mime}")
                )

    routes = definition.get("routes") or []
    if not routes:
        issues.append(_issue("error", "routes", "VALIDATION_FAILED", "至少需要 1 条 route"))
    enabled_routes = [r for r in routes if r.get("enabled", True)]
    if routes and not enabled_routes:
        issues.append(_issue("error", "routes", "VALIDATION_FAILED", "至少需要 1 条启用的 route"))

    seen_ids: set[str] = set()
    with db_session() as s:
        for index, route in enumerate(routes):
            path = f"routes[{index}]"
            route_id = route.get("id") or route.get("route_key")
            if not route_id:
                issues.append(_issue("error", path, "VALIDATION_FAILED", "route 缺少 id"))
            elif route_id in seen_ids:
                issues.append(_issue("error", path, "VALIDATION_FAILED", f"route id 重复：{route_id}"))
            else:
                seen_ids.add(route_id)
            request = route.get("request") or {}
            url_template = str(request.get("url_template") or "")
            if not url_template:
                issues.append(_issue("error", f"{path}.request", "VALIDATION_FAILED", "缺少 url_template"))
            else:
                probe = url_template.replace("{base_url}", "https://example.invalid")
                probe = re.sub(r"\{[a-z_]+\}", "x", probe)
                if not probe.startswith(("http://", "https://")):
                    issues.append(
                        _issue(
                            "error",
                            f"{path}.request.url_template",
                            "VALIDATION_FAILED",
                            "url_template 只允许 http/https，且必须包含 {base_url} 或为绝对 URL",
                        )
                    )
            encoding = (request.get("encoding") or "json").lower()
            if encoding == "multipart" and not (file_io.get("input") or []):
                issues.append(
                    _issue(
                        "error",
                        f"{path}.request.encoding",
                        "VALIDATION_FAILED",
                        "encoding=multipart 时 file_io.input 不能为空",
                    )
                )
            credential_id = route.get("credential_id")
            provider_id = route.get("provider_id")
            cred = s.get(Credential, credential_id) if credential_id else None
            provider = s.get(Provider, provider_id) if provider_id else None
            if cred is None or cred.deleted_at:
                level = "error" if for_publish else "warn"
                issues.append(
                    _issue(level, f"{path}.credential_id", "CREDENTIAL_IN_USE", "引用的凭证不存在")
                )
            elif cred.status != "active":
                level = "error" if for_publish else "warn"
                issues.append(
                    _issue(
                        level,
                        f"{path}.credential_id",
                        "CREDENTIAL_DISABLED",
                        f"凭证 {cred.name} 状态为 {cred.status}",
                        "启用或替换该凭证",
                    )
                )
            if provider is None or provider.deleted_at:
                level = "error" if for_publish else "warn"
                issues.append(_issue(level, f"{path}.provider_id", "NOT_FOUND", "引用的提供商不存在"))
            limits = route.get("limits") or {}
            if int(limits.get("max_file_mb") or 0) > settings.max_upload_mb:
                issues.append(
                    _issue(
                        "error",
                        f"{path}.limits.max_file_mb",
                        "VALIDATION_FAILED",
                        f"max_file_mb 不能超过平台上限 {settings.max_upload_mb}",
                    )
                )
            allowed = set(limits.get("allowed_mime") or [])
            if allowed and not allowed <= set(file_io.get("input") or []):
                issues.append(
                    _issue(
                        "error",
                        f"{path}.limits.allowed_mime",
                        "VALIDATION_FAILED",
                        "allowed_mime 必须是 file_io.input 的子集",
                    )
                )
            param_map = route.get("param_map") or {}
            properties = set((definition.get("input_schema") or {}).get("properties") or {})
            for target, item in param_map.items():
                source = (item or {}).get("from") if isinstance(item, dict) else None
                if not source:
                    continue
                namespace, _, field_name = source.partition(".")
                if namespace == "input" and field_name.split(".")[0] not in properties:
                    issues.append(
                        _issue(
                            "error",
                            f"{path}.param_map.{target}",
                            "PARAM_MAP_UNRESOLVED",
                            f"input.{field_name} 未在 input_schema.properties 中声明",
                        )
                    )
                elif namespace == "route" and field_name not in ("model_hint", "id", "provider_id", "route_key"):
                    issues.append(
                        _issue(
                            "error",
                            f"{path}.param_map.{target}",
                            "PARAM_MAP_UNRESOLVED",
                            f"route.{field_name} 不在允许的声明字段中",
                        )
                    )
                elif namespace not in ("input", "route", "options", "const"):
                    issues.append(
                        _issue(
                            "error",
                            f"{path}.param_map.{target}",
                            "PARAM_MAP_UNRESOLVED",
                            f"未知命名空间 {namespace}",
                        )
                    )
            for rule in route.get("errors") or []:
                match = (rule or {}).get("match") or {}
                status = match.get("status")
                if status is not None:
                    try:
                        int(status)
                    except (TypeError, ValueError):
                        issues.append(
                            _issue(
                                "error",
                                f"{path}.errors",
                                "VALIDATION_FAILED",
                                f"errors.match.status 必须是整数：{status}",
                            )
                        )
                elif not match.get("json"):
                    issues.append(
                        _issue(
                            "error",
                            f"{path}.errors",
                            "VALIDATION_FAILED",
                            "errors.match 需要 status 或 json",
                        )
                    )
            statuses = {
                int((rule or {}).get("match", {}).get("status"))
                for rule in (route.get("errors") or [])
                if str((rule or {}).get("match", {}).get("status", "")).isdigit()
            }
            if 429 not in statuses:
                issues.append(
                    _issue(
                        "warn",
                        f"{path}.errors",
                        "VALIDATION_FAILED",
                        "未配置 429 错误映射，限流时只能走兜底映射",
                    )
                )
            if "async" in (definition.get("modes") or []):
                async_conf = route.get("async") or {}
                if not (async_conf.get("submit") or {}).get("task_id_path") or not (
                    async_conf.get("query") or {}
                ).get("url_template"):
                    # 只对该 route 是 warn；「至少一条 enabled route 支持」在循环外判定为 error
                    issues.append(
                        _issue(
                            "warn",
                            f"{path}.async",
                            "ASYNC_QUERY_MISSING",
                            "该 route 不完整支持异步（需要 async.submit.task_id_path 与 async.query.url_template）",
                        )
                    )
            if "stream" in (definition.get("modes") or []):
                stream_conf = route.get("stream") or {}
                if stream_conf.get("protocol") not in ("sse", "ndjson"):
                    issues.append(
                        _issue(
                            "warn",
                            f"{path}.stream",
                            "VALIDATION_FAILED",
                            "该 route 未提供 stream.protocol，流式调用时会被过滤",
                        )
                    )
    if "stream" in (definition.get("modes") or []) and not any(
        (r.get("stream") or {}).get("protocol") in ("sse", "ndjson") for r in enabled_routes
    ):
        issues.append(
            _issue(
                "error",
                "modes",
                "UNSUPPORTED_MODE",
                "modes 含 stream，但没有任何启用的 route 提供 stream.protocol",
            )
        )
    if "async" in (definition.get("modes") or []) and enabled_routes:
        any_async = any(
            ((r.get("async") or {}).get("submit") or {}).get("task_id_path")
            and ((r.get("async") or {}).get("query") or {}).get("url_template")
            for r in enabled_routes
        )
        if not any_async:
            issues.append(
                _issue(
                    "error",
                    "modes",
                    "ASYNC_QUERY_MISSING",
                    "modes 含 async，但没有任何启用的 route 同时提供 async.submit.task_id_path 与 async.query.url_template",
                )
            )

    for schema_field in ("input_schema", "output_schema"):
        schema = definition.get(schema_field)
        if not isinstance(schema, dict) or not schema:
            issues.append(_issue("error", schema_field, "INPUT_SCHEMA_INVALID", f"{schema_field} 缺失"))
            continue
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
        except jsonschema.SchemaError as exc:
            issues.append(
                _issue("error", schema_field, "INPUT_SCHEMA_INVALID", f"不是合法 JSON Schema：{exc.message}")
            )
            continue
        if schema.get("additionalProperties") is not False:
            issues.append(
                _issue(
                    "error",
                    schema_field,
                    "INPUT_SCHEMA_INVALID",
                    "additionalProperties 必须为 false（不支持的参数必须显式报错）",
                )
            )

    examples = definition.get("examples") or []
    if not examples:
        issues.append(
            _issue("error", "examples", "VALIDATION_FAILED", "至少需要 1 条调用示例")
        )
    else:
        schema = definition.get("input_schema") or {}
        validator = jsonschema.Draft202012Validator(schema) if schema else None
        for index, example in enumerate(examples):
            if validator is not None:
                try:
                    validator.validate(example.get("input") or {})
                except jsonschema.ValidationError as exc:
                    issues.append(
                        _issue(
                            "error",
                            f"examples[{index}].input",
                            "INPUT_SCHEMA_INVALID",
                            f"示例不满足 input_schema：{exc.message}",
                        )
                    )

    group = definition.get("compatibility_group")
    if group:
        with db_session() as s:
            row = s.scalar(select(CompatibilityGroup).where(CompatibilityGroup.slug == group))
            if row is None:
                issues.append(
                    _issue(
                        "error",
                        "compatibility_group",
                        "VALIDATION_FAILED",
                        f"compatibility_group {group} 不存在",
                        "必须先通过 POST /capabilities/{id}/compatibility 显式建组",
                    )
                )
            elif row.kind != definition.get("kind"):
                issues.append(
                    _issue(
                        "error",
                        "compatibility_group",
                        "VALIDATION_FAILED",
                        f"组内 kind 不一致（组为 {row.kind}，能力为 {definition.get('kind')}）",
                    )
                )
    if for_publish:
        errors = [i for i in issues if i["level"] == "error"]
        if errors:
            return issues
    return issues


# ── CRUD ─────────────────────────────────────────────────────


# ── 列表指标（4 个，批量聚合，禁止 N+1）─────────────────────────


def _route_unit_price(selection) -> float | None:  # noqa: ANN001
    """route 显式单价（定价阶梯第一级，§A.3.4-3）；没有则交给价目表/默认价。"""
    chosen = getattr(selection, "chosen", None) or {}
    raw = (chosen.get("price") or {}).get("unit_price_usd")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _metrics_for(caps: list[tuple[str, str]]) -> dict[str, dict]:
    """一次 `GROUP BY` + 一次窗口查询，批量算 4 个指标（§A.3.1-3）。

    `caps` 是 `[(capability_key, capability_id)]`，返回 `{capability_key: {...}}`：
    `last_test_ok` / `last_test_at` / `success_rate_24h`（无调用时 **None，不是 0**）/ `call_count_24h`。
    """
    # 成功率分子必须来自唯一映射常量（不变式 I7）：字面写 'succeeded' 会让成功恒为 0
    from .execution import LOGGED_STATUS  # noqa: PLC0415 - execution 依赖本模块，只能延迟导入

    ok_status = LOGGED_STATUS["succeeded"]
    keys = [key for key, _cid in caps if key]
    ids = [cid for _key, cid in caps if cid]
    metrics: dict[str, dict] = {
        key: {
            "last_test_ok": None,
            "last_test_at": None,
            "success_rate_24h": None,
            "call_count_24h": 0,
        }
        for key in keys
    }
    if not keys:
        return metrics
    since = (
        (datetime.now(timezone.utc) - timedelta(hours=24))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    with db_session() as s:
        rows = s.execute(
            select(
                CallLog.capability_key,
                func.count(),
                func.sum(case((CallLog.status == ok_status, 1), else_=0)),
            )
            .where(
                CallLog.created_at >= since,
                CallLog.capability_key.in_(keys),
                # 分母只算终态：异步调用的记录在提交时以 `queued` 落库，
                # 把它算进分母会让成功率永远偏低（§A.3.1-2 / I7）
                CallLog.status.in_(tuple(LOGGED_STATUS.values())),
            )
            .group_by(CallLog.capability_key)
        ).all()
        for key, total, ok in rows:
            total = int(total or 0)
            entry = metrics.setdefault(key, {})
            entry["call_count_24h"] = total
            entry["success_rate_24h"] = round(float(ok or 0) / total, 4) if total else None
        if ids:
            aliased_run = aliased(CapabilityTestRun)
            latest = (
                select(
                    aliased_run.capability_id.label("capability_id"),
                    func.max(aliased_run.at).label("max_at"),
                )
                .where(aliased_run.capability_id.in_(ids))
                .group_by(aliased_run.capability_id)
                .subquery()
            )
            test_rows = s.execute(
                select(
                    CapabilityTestRun.capability_id,
                    CapabilityTestRun.ok,
                    CapabilityTestRun.at,
                ).join(
                    latest,
                    and_(
                        CapabilityTestRun.capability_id == latest.c.capability_id,
                        CapabilityTestRun.at == latest.c.max_at,
                    ),
                )
            ).all()
            by_id = {cid: key for key, cid in caps if cid}
            for capability_id, ok, at in test_rows:
                key = by_id.get(capability_id)
                if key is None:
                    continue
                entry = metrics.setdefault(key, {})
                entry["last_test_ok"] = bool(ok)
                entry["last_test_at"] = at
    return metrics


def _brief(row: Capability, definition: dict, metrics: dict | None = None) -> dict:
    routes = definition.get("routes") or []
    primary = None
    candidates = [r for r in routes if r.get("enabled", True)]
    if candidates:
        primary = sorted(candidates, key=lambda r: int(r.get("priority", 0)))[0]
    metrics = metrics or {}
    return {
        "id": row.id,
        "key": row.key,
        "name": row.name,
        "kind": row.kind,
        "modes": row.modes or [],
        "file_io": row.file_io or {},
        "status": row.status,
        "enabled": bool(row.enabled),
        "latest_version": row.latest_version,
        "published_version_id": row.published_version_id,
        "compatibility_group": row.compatibility_group,
        "requires_agent_reasoning": bool(row.requires_agent_reasoning),
        "tags": row.tags or [],
        "route_count": len(routes),
        "enabled_route_count": len(candidates),
        # 4 个列表指标：全部为「无数据 → null / 0」，不是占位 0 成功率
        "last_test_ok": metrics.get("last_test_ok"),
        "last_test_at": metrics.get("last_test_at"),
        "success_rate_24h": metrics.get("success_rate_24h"),
        "call_count_24h": int(metrics.get("call_count_24h") or 0),
        "primary_route": (
            {
                "route_id": primary.get("id"),
                "provider_id": primary.get("provider_id"),
                "credential_id": primary.get("credential_id"),
                "priority": primary.get("priority", 0),
            }
            if primary
            else None
        ),
        "draft_revision": row.draft_revision,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def list_capabilities(
    *,
    q: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    provider: str | None = None,
    mode: str | None = None,
    page: int = 1,
    page_size: int = 20,
    sort: str | None = None,
) -> dict:
    with db_session() as s:
        rows = list(s.scalars(select(Capability).where(Capability.deleted_at.is_(None))))
        refs = [(row.key, row.id) for row in rows]
        items = [_brief(row, row.draft_definition or {}) for row in rows]
    metrics = _metrics_for(refs)
    for item in items:
        item.update(metrics.get(item["key"]) or {})
    if provider:
        with db_session() as s:
            route_rows = list(s.scalars(select(CapabilityRoute).where(CapabilityRoute.provider_id == provider)))
        allowed = {r.capability_id for r in route_rows}
        items = [i for i in items if i["id"] in allowed]
    if q:
        needle = q.lower()
        items = [i for i in items if needle in i["key"].lower() or needle in i["name"].lower()]
    if kind:
        items = [i for i in items if i["kind"] == kind]
    if status:
        items = [i for i in items if i["status"] == status]
    if mode:
        items = [i for i in items if mode in (i["modes"] or [])]
    items = sort_items(items, sort, default="-updated_at")
    return paginate(items, page, page_size)


def get_capability(capability_id: str) -> dict:
    with db_session() as s:
        row = s.get(Capability, capability_id)
        if row is None or row.deleted_at:
            raise ApiError(Code.NOT_FOUND, "能力不存在")
        definition = _definition_of(row)
        data = _brief(row, definition, _metrics_for([(row.key, row.id)]).get(row.key))
        data["definition"] = definition
        data["issues"] = validate_definition(definition, capability_id=row.id)
        data["credential_status"] = routing.credential_status_map(definition)
        published = _published_definition(row)
        data["published_definition"] = published
        data["has_unpublished_changes"] = bool(
            published is not None and versions.content_hash(published) != versions.content_hash(definition)
        )
        data["versions"] = [
            {"id": v.id, "version": v.version, "status": v.status, "published_at": v.published_at}
            for v in s.scalars(
                select(CapabilityVersion)
                .where(CapabilityVersion.capability_id == row.id)
                .order_by(desc(CapabilityVersion.created_at))
            )
        ]
        data["test_runs"] = [
            {
                "id": t.id,
                "ok": bool(t.ok),
                "mode": t.mode,
                "route_id": t.route_id,
                "http_status": t.http_status,
                "latency_ms": t.latency_ms,
                "error_code": t.error_code,
                "at": t.at,
            }
            for t in s.scalars(
                select(CapabilityTestRun)
                .where(CapabilityTestRun.capability_id == row.id)
                .order_by(desc(CapabilityTestRun.at))
                .limit(10)
            )
        ]
        skills = []
        for skill in s.scalars(select(Skill).where(Skill.deleted_at.is_(None))):
            deps = (skill.draft_definition or {}).get("dependencies") or []
            if any(d.get("capability_key") == row.key for d in deps):
                skills.append({"id": skill.id, "name": skill.name, "version": skill.latest_version})
        data["dependent_skills"] = skills
    return data


def create_capability(data: dict, *, actor: str | None = None) -> dict:
    key = data["key"]
    if not KEY_RE.match(key):
        raise ApiError(
            Code.VALIDATION_FAILED,
            message=f"key 不合法：{key}",
            hint="形如 text.generate.gpt4o-mini（小写点分）",
        )
    definition = data.get("definition") or default_definition(
        key, data["name"], data.get("kind", "generic.http")
    )
    definition.setdefault("key", key)
    definition.setdefault("name", data["name"])
    definition.setdefault("kind", data.get("kind", "generic.http"))
    definition["key"] = key
    definition["name"] = data["name"]
    definition["kind"] = data.get("kind", definition.get("kind", "generic.http"))
    if data.get("tags"):
        definition["tags"] = data["tags"]
    definition["requires_agent_reasoning"] = bool(data.get("requires_agent_reasoning", False))
    capability_id = uuid_pk("cap")
    with db_write() as s:
        if s.scalar(select(Capability).where(Capability.key == key)):
            raise ApiError(Code.CONFLICT, f"能力 key 已存在：{key}")
        row = Capability(
            id=capability_id,
            key=key,
            name=data["name"],
            kind=definition["kind"],
            modes=definition.get("modes") or ["sync"],
            file_io=definition.get("file_io") or {},
            failover=definition.get("failover") or {"enabled": True, "max_switches": 2},
            enabled=1,
            draft_definition=definition,
            draft_revision=1,
            status=DEFAULT_STATUS,
            requires_agent_reasoning=1 if definition.get("requires_agent_reasoning") else 0,
            tags=definition.get("tags") or [],
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        s.add(row)
        s.flush()
        _sync_projection(s, row, definition)
    audit.write(
        "capability.created",
        object_type="capability",
        object_id=capability_id,
        summary={"key": key},
        actor_id=actor,
    )
    return get_capability(capability_id)


def update_capability(capability_id: str, patch: dict, *, actor: str | None = None) -> dict:
    with db_write() as s:
        row = s.get(Capability, capability_id)
        if row is None or row.deleted_at:
            raise ApiError(Code.NOT_FOUND, "能力不存在")
        definition = copy.deepcopy(patch.get("definition") or row.draft_definition or {})
        if patch.get("name"):
            definition["name"] = patch["name"]
        if patch.get("tags") is not None:
            definition["tags"] = patch["tags"]
        if patch.get("requires_agent_reasoning") is not None:
            definition["requires_agent_reasoning"] = bool(patch["requires_agent_reasoning"])
        if patch.get("enabled") is not None:
            row.enabled = 1 if patch["enabled"] else 0
        _sync_projection(s, row, definition)
    if patch.get("definition") is not None or patch.get("name"):
        audit.write(
            "capability.updated",
            object_type="capability",
            object_id=capability_id,
            summary={"revision": patch.get("definition") is not None},
            actor_id=actor,
        )
    return get_capability(capability_id)


def validate(capability_id: str) -> dict:
    data = get_capability(capability_id)
    issues = validate_definition(data["definition"], capability_id=capability_id)
    errors = [i for i in issues if i["level"] == "error"]
    with db_write() as s:
        row = s.get(Capability, capability_id)
        if row and not errors and row.status == "draft":
            row.status = "testing"
            row.updated_at = now_iso()
    return {
        "ok": not errors,
        "issues": issues,
        "error_count": len(errors),
        "warn_count": len(issues) - len(errors),
    }


async def test_capability(capability_id: str, payload: dict, *, actor: str | None = None) -> dict:
    data = get_capability(capability_id)
    definition = data["definition"]
    mode = payload.get("mode") or (definition.get("modes") or ["sync"])[0]
    input_payload = payload.get("input") or {}
    schema = definition.get("input_schema") or {}
    try:
        jsonschema.Draft202012Validator(schema).validate(input_payload)
    except jsonschema.ValidationError as exc:
        raise ApiError(
            Code.INPUT_SCHEMA_INVALID,
            message=f"输入不满足 input_schema：{exc.message}",
            details={"fields": [".".join(str(p) for p in exc.path)]},
        ) from exc
    options = {"mode": mode}
    if payload.get("route_id"):
        options["preferred_route"] = payload["route_id"]
    selection, material, base_url = routing.resolve(
        capability_id=capability_id, definition=definition, mode=mode, options=options
    )
    if payload.get("route_id"):
        picked = next(
            (c for c in selection.candidates if c["route_id"] == payload["route_id"]), None
        )
        if picked is None:
            raise ApiError(
                Code.PROVIDER_NOT_AVAILABLE,
                message=f"指定的 route {payload['route_id']} 不可用",
                details={"dropped": selection.dropped},
            )
        selection.chosen = picked
    if mode != "sync":
        raise ApiError(
            Code.UNSUPPORTED_MODE,
            message="测试面板目前只执行 sync 模式（async/stream 请在调用记录中验证）",
            details={"supported": ["sync"]},
        )
    request_id = uuid_pk("req")
    result = await invoke_sync(
        request_id=request_id,
        capability=definition,
        route=selection.chosen or {},
        input_payload=input_payload,
        material=material,
        base_url=base_url,
        timeout_ms=int((selection.chosen or {}).get("timeout_ms") or 60000),
        options=options,
        retry_conf=(selection.chosen or {}).get("retry"),
    )
    with db_write() as s:
        run = CapabilityTestRun(
            id=uuid_pk("ctr"),
            capability_id=capability_id,
            capability_version_id=data.get("published_version_id"),
            route_id=(selection.chosen or {}).get("route_id"),
            version=data.get("latest_version"),
            mode=mode,
            ok=1 if result.status == "succeeded" else 0,
            http_status=result.http_status,
            latency_ms=(result.timings or {}).get("total_ms"),
            request_preview_redacted=result.request_preview,
            response_preview_redacted=result.response_preview,
            error_code=(result.error or {}).get("code"),
            error_detail_redacted=redact(json.dumps(result.error or {}, ensure_ascii=False), max_len=1024),
            credential_id=(selection.chosen or {}).get("credential_id"),
            route={k: selection.chosen.get(k) for k in ("route_id", "provider_id", "credential_id", "priority")}
            if selection.chosen
            else {},
            at=now_iso(),
        )
        s.add(run)
        row = s.get(Capability, capability_id)
        if row and row.status == "draft":
            row.status = "testing"
            row.updated_at = now_iso()
        test_run_id = run.id
    # 测试面板同样是「真实触达上游」的调用，必须计费（token/agent 作用域为空，§A.3.4）
    usage.account(
        call_id=None,
        provider_id=(selection.chosen or {}).get("provider_id"),
        capability_key=data["key"],
        token_id=None,
        agent_id=None,
        unit=(result.usage or {}).get("unit") or "tokens",
        qty=(result.usage or {}).get("total") or (result.usage or {}).get("qty") or 0,
        source=(result.usage or {}).get("source") or "estimated",
        unit_price_usd=_route_unit_price(selection),
    )
    return {
        "test_run_id": test_run_id,
        "status": result.status,
        "http_status": result.http_status,
        "latency_ms": (result.timings or {}).get("total_ms"),
        "response_preview": result.response_preview,
        "output": result.output,
        "error": result.error,
        "used_route": {
            "route_id": (selection.chosen or {}).get("route_id"),
            "provider_id": (selection.chosen or {}).get("provider_id"),
            "credential_id": (selection.chosen or {}).get("credential_id"),
        },
        "dropped": selection.dropped,
    }


def publish(
    capability_id: str, *, version: str | None = None, changelog: str | None = None, actor: str | None = None
) -> dict:
    data = get_capability(capability_id)
    definition = data["definition"]
    issues = validate_definition(definition, for_publish=True, capability_id=capability_id)
    errors = [i for i in issues if i["level"] == "error"]
    if errors:
        raise ApiError(
            Code.VALIDATION_FAILED,
            message="能力定义仍有错误，无法发布",
            hint="修正 issues 中 level=error 的项后重试",
            details={"issues": errors},
        )
    since = (
        datetime.now(timezone.utc) - timedelta(hours=24)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    with db_session() as s:
        runs = list(
            s.scalars(
                select(CapabilityTestRun).where(
                    CapabilityTestRun.capability_id == capability_id,
                    CapabilityTestRun.ok == 1,
                    CapabilityTestRun.at >= since,
                )
            )
        )
    if not runs:
        raise ApiError(
            Code.PUBLISH_EVIDENCE_MISSING,
            message="缺少 24 小时内的成功测试记录",
            hint="先在测试面板跑一次通过的调用（真实上游响应），再发布",
        )
    target_version = version or versions.next_version(data.get("latest_version"))
    with db_write() as s:
        row = s.get(Capability, capability_id)
        if row is None:
            raise ApiError(Code.NOT_FOUND, "能力不存在")
        existing = s.scalar(
            select(CapabilityVersion).where(
                CapabilityVersion.capability_id == capability_id,
                CapabilityVersion.version == target_version,
            )
        )
        if existing:
            raise ApiError(Code.CONFLICT, f"版本 {target_version} 已存在")
        version_id = versions.freeze_capability(
            s,
            capability_id=capability_id,
            version=target_version,
            definition=definition,
            changelog=changelog,
            actor=actor,
            test_run_ids=[r.id for r in runs[:5]],
        )
        row.published_version_id = version_id
        row.latest_version = target_version
        row.status = "published"
        row.enabled = 1
        row.updated_at = now_iso()
        _sync_projection(s, row, definition)
    audit.write(
        "capability.published",
        object_type="capability",
        object_id=capability_id,
        summary={"version": target_version, "evidence": [r.id for r in runs[:5]]},
        actor_id=actor,
    )
    return {"ok": True, "version": target_version, "version_id": version_id}


def deprecate(capability_id: str, *, actor: str | None = None) -> dict:
    with db_write() as s:
        row = s.get(Capability, capability_id)
        if row is None or row.deleted_at:
            raise ApiError(Code.NOT_FOUND, "能力不存在")
        row.status = "deprecated"
        row.enabled = 0
        row.updated_at = now_iso()
        if row.published_version_id:
            version = s.get(CapabilityVersion, row.published_version_id)
            if version:
                version.status = "deprecated"
    audit.write("capability.deprecated", object_type="capability", object_id=capability_id, actor_id=actor)
    return {"ok": True, "id": capability_id, "status": "deprecated"}


def rollback(capability_id: str, to_version: str, *, actor: str | None = None) -> dict:
    with db_write() as s:
        row = s.get(Capability, capability_id)
        if row is None:
            raise ApiError(Code.NOT_FOUND, "能力不存在")
        target = s.scalar(
            select(CapabilityVersion).where(
                CapabilityVersion.capability_id == capability_id,
                CapabilityVersion.version == to_version,
            )
        )
        if target is None:
            raise ApiError(Code.NOT_FOUND, f"版本 {to_version} 不存在")
        target.status = "published"
        row.published_version_id = target.id
        row.latest_version = target.version
        row.status = "published"
        row.updated_at = now_iso()
    audit.write(
        "capability.rolled_back",
        object_type="capability",
        object_id=capability_id,
        summary={"to_version": to_version},
        actor_id=actor,
    )
    return {"ok": True, "published_version": to_version}


def list_versions(capability_id: str) -> list[dict]:
    with db_session() as s:
        rows = list(
            s.scalars(
                select(CapabilityVersion)
                .where(CapabilityVersion.capability_id == capability_id)
                .order_by(desc(CapabilityVersion.created_at))
            )
        )
        current = s.get(Capability, capability_id)
        return [
            {
                "id": r.id,
                "version": r.version,
                "status": r.status,
                "published_at": r.published_at,
                "changelog": r.changelog,
                "content_hash": r.content_hash,
                "test_run_ids": r.test_run_ids or [],
                "is_current": bool(current and current.published_version_id == r.id),
                "created_at": r.created_at,
            }
            for r in rows
        ]


def version_detail(capability_id: str, version: str) -> dict:
    with db_session() as s:
        row = s.scalar(
            select(CapabilityVersion).where(
                CapabilityVersion.capability_id == capability_id,
                CapabilityVersion.version == version,
            )
        )
        if row is None:
            raise ApiError(Code.NOT_FOUND, f"版本 {version} 不存在")
        return {
            "id": row.id,
            "version": row.version,
            "status": row.status,
            "definition": row.definition,
            "content_hash": row.content_hash,
            "published_at": row.published_at,
            "published_by": row.published_by,
            "changelog": row.changelog,
            "test_run_ids": row.test_run_ids or [],
        }


def diff_versions(capability_id: str, from_version: str, to_version: str) -> dict:
    left = version_detail(capability_id, from_version)
    right = version_detail(capability_id, to_version)
    return {
        "from": from_version,
        "to": to_version,
        "changes": versions.diff(left["definition"], right["definition"]),
    }


def confirm_compatibility(capability_id: str, group: str, *, notes: str | None = None, actor: str | None = None) -> dict:
    data = get_capability(capability_id)
    with db_write() as s:
        row = s.get(Capability, capability_id)
        if row is None:
            raise ApiError(Code.NOT_FOUND, "能力不存在")
        existing = s.scalar(select(CompatibilityGroup).where(CompatibilityGroup.slug == group))
        if existing is None:
            s.add(
                CompatibilityGroup(
                    id=uuid_pk("cmg"),
                    slug=group,
                    kind=data["kind"],
                    notes=notes,
                    confirmed_by=actor,
                    confirmed_at=now_iso(),
                )
            )
        elif existing.kind != data["kind"]:
            raise ApiError(
                Code.CONFLICT,
                f"组 {group} 的 kind 为 {existing.kind}，与能力 {data['kind']} 不一致",
            )
        definition = copy.deepcopy(row.draft_definition or {})
        definition["compatibility_group"] = group
        _sync_projection(s, row, definition)
    audit.write(
        "capability.compatibility_confirmed",
        object_type="capability",
        object_id=capability_id,
        summary={"group": group},
        actor_id=actor,
    )
    return {"ok": True, "compatibility_group": group}


def impact(capability_id: str) -> dict:
    data = get_capability(capability_id)
    with db_session() as s:
        tokens = list(
            s.scalars(select(PlatformToken).where(PlatformToken.revoked_at.is_(None)))
        )
        matching_tokens = []
        for token in tokens:
            scopes = token.scopes or []
            if "*" in scopes or data["key"] in scopes or any(
                isinstance(scope, str) and scope.startswith("tag:") and scope[4:] in (data["tags"] or [])
                for scope in scopes
            ):
                matching_tokens.append({"id": token.id, "name": token.name, "prefix": token.prefix})
        inflight = list(
            s.scalars(
                select(Task).where(
                    Task.capability_key == data["key"],
                    Task.status.in_(("queued", "running", "canceling", "status_unknown")),
                )
            )
        )
        skills = data.get("dependent_skills") or []
        route_creds = []
        for route in (data["definition"].get("routes") or []):
            cred = s.get(Credential, route.get("credential_id"))
            route_creds.append(
                {
                    "credential_id": route.get("credential_id"),
                    "name": cred.name if cred else None,
                    "status": cred.status if cred else "missing",
                    "route_id": route.get("id"),
                }
            )
    return {
        "capability": {"id": data["id"], "key": data["key"], "status": data["status"]},
        "skills": skills,
        "tokens": matching_tokens,
        "published_versions": [
            {"version": v["version"], "status": v["status"]} for v in data.get("versions", [])
        ],
        "inflight_tasks": len(inflight),
        "route_credentials": route_creds,
        "blocking": bool(skills or matching_tokens or inflight),
    }


def delete(capability_id: str, *, actor: str | None = None) -> dict:
    data = impact(capability_id)
    if data["blocking"]:
        raise ApiError(
            Code.CAPABILITY_IN_USE,
            message="能力仍被 SKILL / 令牌 / 在途任务引用，无法删除",
            hint="改用停用（deprecate），或先解除依赖",
            details=data,
        )
    with db_write() as s:
        row = s.get(Capability, capability_id)
        if row is None or row.deleted_at:
            raise ApiError(Code.NOT_FOUND, "能力不存在")
        if row.status == "published":
            raise ApiError(
                Code.CAPABILITY_IN_USE,
                message="已发布能力禁止删除，只能停用",
                hint="POST /capabilities/{id}/deprecate",
            )
        row.deleted_at = now_iso()
        row.enabled = 0
        for route in s.scalars(select(CapabilityRoute).where(CapabilityRoute.capability_id == capability_id)):
            s.delete(route)
    audit.write("capability.deleted", object_type="capability", object_id=capability_id, actor_id=actor)
    return {"ok": True, "id": capability_id}


def describe(key: str) -> dict:
    """对外统一描述（HTTP / MCP / SKILL / CLI 共用同一份，§9.7）。"""
    with db_session() as s:
        row = s.scalar(
            select(Capability).where(Capability.key == key, Capability.deleted_at.is_(None))
        )
        if row is None:
            raise ApiError(Code.NOT_FOUND, "能力不存在")
        definition = (
            copy.deepcopy(s.get(CapabilityVersion, row.published_version_id).definition)
            if row.published_version_id and s.get(CapabilityVersion, row.published_version_id)
            else copy.deepcopy(row.draft_definition or {})
        )
        version = row.latest_version
        status = row.status
        enabled = bool(row.enabled)
        credentials_map = {}
        for route in definition.get("routes") or []:
            cred = s.get(Credential, route.get("credential_id"))
            credentials_map[route.get("credential_id")] = cred.status if cred else "missing"
        providers = {}
        for provider in s.scalars(select(Provider)).all():
            providers[provider.id] = provider.name
    routes_summary = []
    for route in definition.get("routes") or []:
        if not route.get("enabled", True):
            continue
        routes_summary.append(
            {
                "route_id": route.get("id"),
                "provider": providers.get(route.get("provider_id"), route.get("provider_id")),
                "priority": route.get("priority", 0),
                "available": credentials_map.get(route.get("credential_id")) == "active",
            }
        )
    return {
        "key": definition.get("key"),
        "name": definition.get("name"),
        "kind": definition.get("kind"),
        "version": version,
        "status": status,
        "enabled": enabled,
        "modes": definition.get("modes") or [],
        "file_io": definition.get("file_io") or {},
        "input_schema": definition.get("input_schema"),
        "output_schema": definition.get("output_schema"),
        "examples": definition.get("examples") or [],
        "limits": (definition.get("routes") or [{}])[0].get("limits") or {},
        "tags": definition.get("tags") or [],
        "docs_url": definition.get("docs_url"),
        "compatibility_group": definition.get("compatibility_group"),
        "requires_agent_reasoning": bool(definition.get("requires_agent_reasoning")),
        "routes_summary": routes_summary,
    }


def catalog(token_ctx=None, *, tags: list[str] | None = None) -> list[dict]:
    """令牌可见的能力目录（仅已发布 + 启用）。"""
    from ..security.tokens import scope_allows

    with db_session() as s:
        rows = list(
            s.scalars(
                select(Capability).where(
                    Capability.deleted_at.is_(None),
                    Capability.status == "published",
                    Capability.enabled == 1,
                )
            )
        )
    out = []
    for row in rows:
        if token_ctx is not None and not scope_allows(token_ctx, row.key, row.tags or []):
            continue
        out.append(
            {
                "key": row.key,
                "name": row.name,
                "kind": row.kind,
                "version": row.latest_version,
                "modes": row.modes or [],
                "file_io": row.file_io or {},
                "tags": row.tags or [],
                "requires_agent_reasoning": bool(row.requires_agent_reasoning),
            }
        )
    return sorted(out, key=lambda item: item["key"])


def lookup(key: str) -> dict:
    """返回已发布能力的定义与版本元信息（执行路径使用）。"""
    with db_session() as s:
        row = s.scalar(
            select(Capability).where(Capability.key == key, Capability.deleted_at.is_(None))
        )
        if row is None:
            raise ApiError(Code.NOT_FOUND, "能力不存在")
        if row.status != "published" or not row.enabled:
            raise ApiError(
                Code.CAPABILITY_NOT_CALLABLE,
                message="能力未发布或已停用",
                details={"status": row.status, "enabled": bool(row.enabled)},
            )
        version_row = s.get(CapabilityVersion, row.published_version_id) if row.published_version_id else None
        if version_row is None:
            raise ApiError(Code.CAPABILITY_NOT_CALLABLE, "能力缺少已发布版本")
        return {
            "id": row.id,
            "key": row.key,
            "name": row.name,
            "tags": row.tags or [],
            "status": row.status,
            "version_id": version_row.id,
            "version": version_row.version,
            "definition": copy.deepcopy(version_row.definition),
            "requires_agent_reasoning": bool(row.requires_agent_reasoning),
        }


# ── route 子资源 ─────────────────────────────────────────────


def list_routes(capability_id: str) -> list[dict]:
    data = get_capability(capability_id)
    health = routing._health_from_projection(capability_id)  # noqa: SLF001
    out = []
    for index, route in enumerate(data["definition"].get("routes") or []):
        key = route.get("id") or route.get("route_key")
        out.append(
            {
                **route,
                "route_id": key,
                "health": health.get(key, {}),
                "is_primary": index == 0,
            }
        )
    out.sort(key=lambda r: int(r.get("priority", 0)))
    for index, route in enumerate(out):
        route["is_primary"] = index == 0
    return out


def upsert_route(capability_id: str, payload: dict, *, actor: str | None = None) -> dict:
    data = get_capability(capability_id)
    definition = data["definition"]
    routes = [r for r in (definition.get("routes") or [])]
    route_id = payload.get("route_id") or payload.get("id") or f"rt_{len(routes) + 1}"
    existing = next((r for r in routes if (r.get("id") or r.get("route_key")) == route_id), None)
    patch = {
        "id": route_id,
        "provider_id": payload.get("provider_id"),
        "credential_id": payload.get("credential_id"),
        "priority": payload.get("priority", 0),
        "enabled": payload.get("enabled", True),
        "model_hint": payload.get("model_hint"),
        "request": payload.get("request") or {},
        "param_map": payload.get("param_map") or {},
        "response_map": payload.get("response_map") or {},
        "stream": payload.get("stream"),
        "async": payload.get("async"),
        "limits": payload.get("limits") or {},
        "errors": payload.get("errors") or [],
        "timeout_ms": payload.get("timeout_ms") or 60000,
        "retry": payload.get("retry") or {},
    }
    if existing is None:
        routes.append(patch)
    else:
        existing.update({k: v for k, v in patch.items() if v is not None})
    definition["routes"] = routes
    update_capability(capability_id, {"definition": definition}, actor=actor)
    audit.write(
        "capability.route_changed",
        object_type="capability",
        object_id=capability_id,
        summary={"route_id": route_id, "action": "created" if existing is None else "updated"},
        actor_id=actor,
    )
    return {"ok": True, "route_id": route_id}


def delete_route(capability_id: str, route_id: str, *, actor: str | None = None) -> dict:
    data = get_capability(capability_id)
    definition = data["definition"]
    routes = [r for r in (definition.get("routes") or []) if (r.get("id") or r.get("route_key")) != route_id]
    if len(routes) == len(definition.get("routes") or []):
        raise ApiError(Code.NOT_FOUND, f"route {route_id} 不存在")
    definition["routes"] = routes
    update_capability(capability_id, {"definition": definition}, actor=actor)
    audit.write(
        "capability.route_changed",
        object_type="capability",
        object_id=capability_id,
        summary={"route_id": route_id, "action": "deleted"},
        actor_id=actor,
    )
    return {"ok": True}


__all__ = [
    "default_definition",
    "validate_definition",
    "list_capabilities",
    "get_capability",
    "create_capability",
    "update_capability",
    "validate",
    "test_capability",
    "publish",
    "deprecate",
    "rollback",
    "list_versions",
    "version_detail",
    "diff_versions",
    "confirm_compatibility",
    "impact",
    "delete",
    "describe",
    "catalog",
    "lookup",
    "list_routes",
    "upsert_route",
    "delete_route",
    "KINDS",
    "MODES",
    "KEY_RE",
]
