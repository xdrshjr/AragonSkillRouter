"""系统状态与默认设置种子（§7.1 / §10.1）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.base import now_iso
from ..models import EngineCapabilityStatus, Setting, SystemState
from ..settings import settings

DEFAULT_STATE = {
    "admin_initialized": "0",
    "schema_version": "1",
    "engine_pin": settings.engine_pin,
    "engine_previous_pin": "",
    "install_mode": settings.install_mode,
    "master_key_id": settings.master_key_id,
}

DEFAULT_SETTINGS: dict[str, dict] = {
    "payload_policy": {"value": settings.payload_policy},
    "retention_days": {"value": settings.retention_days},
    "fetch_allowlist": {"value": settings.allowlist},
    "ssrf_deny": {"value": settings.ssrf_deny_list},
    "engine_policy": {"value": settings.engine_policy},
    "default_budget_usd": {"value": settings.default_budget_usd},
    "agent_model": {
        "value": {
            "provider": "anthropic",
            "model": "claude-sonnet-4-5",
            "base_url": None,
            "temperature": 0.2,
            "max_tokens": 8192,
            "credential_id": None,
            "allowed_providers": [],
            "max_test_calls_per_flow": 10,
            "max_cost_usd_per_flow": 1.0,
            "allowed_window_hours": 24,
        }
    },
}

#: §12.2 能力状态初始值（升级验证后由 verify.py 重算）
DEFAULT_ENGINE_CAPABILITIES: list[tuple[str, str, str, int]] = [
    ("多轮对话与流式输出", "available", "网关 subscribe() → SSE assistant.delta", 1),
    ("工具调用与进度", "available", "映射为 tool.started / tool.finished", 2),
    ("停止（abort）", "available", "停止报告列出已停止步骤与外部待确认操作", 3),
    ("TODO 规划", "available", "上游 core 0.2.34 未产出 todo 事件，映射已就绪", 4),
    ("子代理团队（team）", "needs_adapter", "事件可读但平台暂不展示团队视图", 5),
    ("快速模型评审（fast_review）", "needs_adapter", "需要单独模型配置与费用归属，本阶段不开启", 6),
    ("上下文自动压缩", "available", "由上游 contextManager 驱动", 7),
    ("计划模式（plan mode）", "permission_limited", "仅在接入流程中以只读模式启用", 8),
    ("文件读写 / bash 内置工具", "permission_limited", "网关注册工具集不含它们（本平台不支持，非沙箱）", 9),
    ("skill_install / skill_create 内置工具", "permission_limited", "平台用自身 SKILL 导入管线替代", 10),
    ("沙箱 CodeAct（isolated-vm）", "unsupported", "可选依赖，本阶段不安装", 11),
    ("会话持久化", "needs_adapter", "上游无持久化，平台自建 agent_sessions/agent_turns", 12),
]


def get_state(session: Session, key: str, default: str = "") -> str:
    row = session.get(SystemState, key)
    return row.value if row else default


def set_state(session: Session, key: str, value: str) -> None:
    row = session.get(SystemState, key)
    if row is None:
        session.add(SystemState(key=key, value=value, updated_at=now_iso()))
    else:
        row.value = value
        row.updated_at = now_iso()


def seed_system_state(session: Session) -> None:
    for key, value in DEFAULT_STATE.items():
        if session.get(SystemState, key) is None:
            session.add(SystemState(key=key, value=value, updated_at=now_iso()))
    for key, value in DEFAULT_SETTINGS.items():
        if session.get(Setting, key) is None:
            session.add(
                Setting(key=key, value=value, scope="global", updated_at=now_iso())
            )
    existing = {
        row.capability_key for row in session.scalars(select(EngineCapabilityStatus)).all()
    }
    for label, state, detail, ordinal in DEFAULT_ENGINE_CAPABILITIES:
        if label in existing:
            continue
        session.add(
            EngineCapabilityStatus(
                id=f"ecs_{ordinal:04d}",
                capability_key=label,
                state=state,
                detail=detail,
                since_version=settings.gateway_pin_version,
                updated_at=now_iso(),
                ordinal=ordinal,
            )
        )
    session.commit()
