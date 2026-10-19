"""设置读写（含 engine_policy / payload_policy / 白名单 / Agent 模型配置，§9.2 / §A.3.9）。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from ..db import db_session, db_write, now_iso
from ..errors import ApiError, Code
from ..logging import get_logger, redact
from ..models import Setting
from ..settings import settings
from . import settings_runtime

log = get_logger("skillrouter.run")

EDITABLE = {
    "payload_policy",
    "retention_days",
    "fetch_allowlist",
    "ssrf_deny",
    "engine_policy",
    "default_budget_usd",
    "budget_usd",
    "agent_model",
}

#: 部署事实：可以读，但**永不从 DB 覆盖**，因此列在 `POST /settings` 的 `readonly` 里
READONLY_KEYS = (
    "master_key_path",
    "agent_gateway_secret",
    "db_path",
    "data_dir",
    "agent_engine_home_root",
    "engine_toolchain",
    "cancel_reconcile_minutes",
)


def get_all() -> dict:
    with db_session() as s:
        rows = list(s.scalars(select(Setting)))
        out = {row.key: row.value.get("value") if isinstance(row.value, dict) else row.value for row in rows}
    out.setdefault("payload_policy", settings.payload_policy)
    out.setdefault("retention_days", settings.retention_days)
    out.setdefault("engine_policy", settings.engine_policy)
    out.setdefault("fetch_allowlist", settings.allowlist)
    out.setdefault("default_budget_usd", settings.default_budget_usd)
    out.setdefault("ssrf_deny", settings.ssrf_deny_list)
    # `fetch_allowlist` 在 DB 里可能以逗号分隔字符串存在（历史行 + PATCH 的 `",".join`），
    # 前端类型是 `string[]`：不归一会让设置页 `.join()` 抛错整页白屏（§A.3.9）
    out["fetch_allowlist"] = _as_list(out.get("fetch_allowlist"))
    out["ssrf_deny"] = _as_list(out.get("ssrf_deny"))
    out["env"] = settings.env
    out["install_mode"] = settings.install_mode
    out["install_command"] = settings.install_command()
    out["public_base_url"] = settings.public_base_url
    out["max_upload_mb"] = settings.max_upload_mb
    out["egress_policy"] = settings.egress_policy
    out["retention_days_max"] = 3650
    return redact(out)


def _as_list(value) -> list[str]:  # noqa: ANN001
    """把 DB/环境里「字符串或列表」的取值统一成 `list[str]`（GET /settings 的契约）。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [x.strip() for x in value.split(",") if x.strip()]
    return [str(x).strip() for x in value if str(x).strip()]


def get_value(key: str, default: Any = None) -> Any:  # noqa: ANN401
    with db_session() as s:
        row = s.get(Setting, key)
        if row is None:
            return default
        return row.value.get("value") if isinstance(row.value, dict) else row.value


def update(patch: dict, *, actor: str | None = None) -> dict:
    unknown = set(patch) - EDITABLE
    if unknown:
        raise ApiError(
            Code.VALIDATION_FAILED,
            message=f"不可修改的设置项：{', '.join(sorted(unknown))}",
            hint="只允许修改 payload_policy / retention_days / fetch_allowlist / engine_policy / budget / agent_model",
        )
    with db_write() as s:
        for key, value in patch.items():
            row = s.get(Setting, key)
            if row is None:
                s.add(
                    Setting(
                        key=key,
                        value={"value": value},
                        scope="global",
                        updated_by=actor,
                        updated_at=now_iso(),
                    )
                )
            else:
                row.value = {"value": value}
                row.updated_by = actor
                row.updated_at = now_iso()
    write_agent_policy()
    # 同进程立即生效（跨进程最长 TTL_SECONDS 秒，§A.3.9）
    settings_runtime.invalidate()
    result = get_all()
    result["saved_at"] = now_iso()
    result["effective"] = settings_runtime.effective_scopes()
    result["readonly"] = list(READONLY_KEYS)
    return result


def agent_config() -> dict:
    value = get_value("agent_model", {}) or {}
    return redact(value)


def update_agent_config(patch: dict, *, actor: str | None = None) -> dict:
    current = dict(get_value("agent_model", {}) or {})
    current.update({k: v for k, v in patch.items() if v is not None})
    with db_write() as s:
        row = s.get(Setting, "agent_model")
        if row is None:
            s.add(
                Setting(
                    key="agent_model",
                    value={"value": current},
                    scope="global",
                    updated_by=actor,
                    updated_at=now_iso(),
                )
            )
        else:
            row.value = {"value": current}
            row.updated_by = actor
            row.updated_at = now_iso()
    write_agent_policy()
    settings_runtime.invalidate()
    return redact(current)


def write_agent_policy() -> dict:
    """把平台工具白名单写入网关策略文件（网关只注册这些工具）。"""
    policy = {
        "version": 1,
        "allowed_tools": [
            "sr_capabilities_list",
            "sr_capability_get",
            "sr_fetch_source",
            "sr_parse_document",
            "sr_propose_capability_draft",
            "sr_update_capability_draft",
            "sr_test_capability",
            "sr_sample_invoke",
            "sr_create_skill_draft",
            "sr_validate_skill",
            "sr_record_finding",
            "sr_request_approval",
        ],
        "denied_upstream_tools": [
            "bash",
            "write_file",
            "read_file",
            "web",
            "skill_install",
            "skill_create",
            "code_execution",
        ],
        "plan_mode": "read_only",
        "updated_at": now_iso(),
    }
    path = settings.agent_policy_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8")
    return policy


def read_agent_policy() -> dict:
    path = settings.agent_policy_path
    if not path.exists():
        return write_agent_policy()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return write_agent_policy()
