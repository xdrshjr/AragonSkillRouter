"""概览聚合（§9.6）：六项指标 + 待办，全部可跳转。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from ..db import db_session, now_iso
from ..models import (
    AgentApproval,
    CallLog,
    Capability,
    CapabilityRoute,
    Credential,
    EngineCandidate,
    Provider,
    Skill,
    SystemState,
    Task,
    TaskQueue,
)
from ..router.health import summarize
from ..settings import settings


def _since(hours: int) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def overview() -> dict:
    since24 = _since(24)
    with db_session() as s:
        caps = list(s.scalars(select(Capability).where(Capability.deleted_at.is_(None))))
        providers = list(s.scalars(select(Provider).where(Provider.deleted_at.is_(None))))
        creds = list(s.scalars(select(Credential).where(Credential.deleted_at.is_(None))))
        calls = list(s.scalars(select(CallLog).where(CallLog.created_at >= since24)))
        tasks_failed = list(
            s.scalars(select(Task).where(Task.status == "failed", Task.updated_at >= since24))
        )
        tasks_unknown = list(s.scalars(select(Task).where(Task.status == "status_unknown")))
        queue_depth = len(
            list(s.scalars(select(TaskQueue).where(TaskQueue.status.in_(("queued", "leased")))))
        )
        skills = list(s.scalars(select(Skill).where(Skill.deleted_at.is_(None))))
        routes = list(s.scalars(select(CapabilityRoute)))
        candidates = list(
            s.scalars(select(EngineCandidate).where(EngineCandidate.verification_status != "failed"))
        )
        approvals = list(s.scalars(select(AgentApproval).where(AgentApproval.status == "pending")))
        pin_row = s.get(SystemState, "engine_pin")
        policy_row = s.get(SystemState, "engine_policy")
        engine_pin = pin_row.value if pin_row else settings.engine_pin
        # 不变式 I6：`needs_reconcile=1` 的行必须能被发现（不受 24h 窗口限制）
        needs_reconcile = int(
            s.scalar(select(func.count()).select_from(CallLog).where(CallLog.needs_reconcile == 1)) or 0
        )

    published = [c for c in caps if c.status == "published"]
    draft = [c for c in caps if c.status == "draft"]
    credits_active = [c for c in creds if c.status == "active"]
    routes_by_cap: dict[str, list[CapabilityRoute]] = {}
    for route in routes:
        routes_by_cap.setdefault(route.capability_id, []).append(route)

    failing_keys: dict[str, int] = {}
    for call in calls:
        if call.status in ("failed", "status_unknown"):
            failing_keys[call.capability_key] = failing_keys.get(call.capability_key, 0) + 1

    ok_calls = sum(1 for c in calls if c.status == "success")
    latencies = sorted(c.latency_ms or 0 for c in calls if c.latency_ms)
    p50 = latencies[int(len(latencies) * 0.5) - 1] if latencies else None
    p95 = latencies[int(len(latencies) * 0.95) - 1] if latencies else None

    attention: list[dict] = []
    now_dt = datetime.now(timezone.utc)
    for cred in creds:
        if cred.expires_at:
            try:
                exp = datetime.fromisoformat(cred.expires_at.replace("Z", "+00:00"))
            except ValueError:
                continue
            days = (exp - now_dt).days
            if days <= 7:
                attention.append(
                    {
                        "code": "CREDENTIAL_EXPIRING",
                        "severity": "warning" if days >= 0 else "error",
                        "title": f"{cred.name} {'已过期' if days < 0 else f'{days} 天后过期'}",
                        "target": f"/providers/{cred.provider_id}/credentials/{cred.id}",
                    }
                )
        if cred.status == "disabled":
            attention.append(
                {
                    "code": "CREDENTIAL_DISABLED",
                    "severity": "warning",
                    "title": f"凭证 {cred.name} 已停用，引用它的能力将无法调用",
                    "target": f"/providers/{cred.provider_id}/credentials/{cred.id}",
                }
            )
    for cap in caps:
        if cap.status == "published" and cap.enabled:
            usable = [
                r
                for r in routes_by_cap.get(cap.id, [])
                if r.enabled and any(c.id == r.credential_id and c.status == "active" for c in creds)
            ]
            if not usable:
                attention.append(
                    {
                        "code": "CAPABILITY_NO_ROUTE",
                        "severity": "error",
                        "title": f"{cap.key} 没有可用路由（凭证停用或 route 全禁用）",
                        "target": f"/capabilities/{cap.id}",
                    }
                )
        if failing_keys.get(cap.key, 0) >= 3:
            attention.append(
                {
                    "code": "CAPABILITY_FAILING",
                    "severity": "error",
                    "title": f"{cap.key} 近 24h 失败 {failing_keys[cap.key]} 次",
                    "target": f"/capabilities/{cap.id}",
                }
            )
        if cap.status == "draft":
            attention.append(
                {
                    "code": "CAPABILITY_DRAFT",
                    "severity": "info",
                    "title": f"能力 {cap.key} 仍是草稿，未对外提供",
                    "target": f"/capabilities/{cap.id}",
                }
            )
    for cand in candidates[:3]:
        attention.append(
            {
                "code": "ENGINE_CANDIDATE",
                "severity": "info",
                "title": f"发现引擎候选版本 {cand.version}（{cand.provenance}）",
                "target": "/settings/engine",
            }
        )
    for approval in approvals:
        attention.append(
            {
                "code": "APPROVAL_PENDING",
                "severity": "warning",
                "title": f"待审批：{approval.action}（{approval.target or '-'}）",
                "target": "/agent",
            }
        )
    if tasks_unknown:
        attention.append(
            {
                "code": "TASK_STATUS_UNKNOWN",
                "severity": "warning",
                "title": f"{len(tasks_unknown)} 个任务无法判定上游是否受理，需对账",
                "target": "/tasks?status=unknown",
            }
        )
    if needs_reconcile:
        attention.append(
            {
                "code": "CALL_NEEDS_RECONCILE",
                "severity": "warning",
                "title": f"{needs_reconcile} 次调用无法判定上游是否受理，需对账",
                "target": "/calls?needs_reconcile=1",
                "href": "/calls?needs_reconcile=1",
            }
        )
    for provider in providers:
        has = any(c.provider_id == provider.id and c.status == "active" for c in creds)
        if not has:
            attention.append(
                {
                    "code": "PROVIDER_NO_CREDENTIAL",
                    "severity": "warning",
                    "title": f"提供商 {provider.name} 没有可用凭证",
                    "target": f"/providers/{provider.id}",
                }
            )

    health = summarize_stats(calls)
    return {
        "capabilities": {
            "published": len(published),
            "draft": len(draft),
            "failing": len(failing_keys),
            "callable": len([c for c in published if c.enabled]),
            "total": len(caps),
        },
        "providers": {
            "total": len(providers),
            "with_active_credential": len({c.provider_id for c in credits_active}),
        },
        "skills": {
            "total": len(skills),
            "published": len([s for s in skills if s.status == "published" and s.enabled]),
            "disabled": len([s for s in skills if not s.enabled]),
        },
        "success_rate_24h": round(ok_calls / len(calls), 4) if calls else None,
        "calls_24h": len(calls),
        "needs_reconcile": needs_reconcile,
        "latency": {"p50_ms": p50, "p95_ms": p95},
        "failed_tasks": len(tasks_failed),
        "queue_depth": queue_depth,
        "engine": {
            "version": engine_pin.split("@")[0],
            "pin": engine_pin,
            "status": "available",
            "candidate": candidates[0].version if candidates else None,
            "policy": (policy_row.value if policy_row else settings.engine_policy),
        },
        "attention": attention[:20],
        "pipeline": _pipeline(caps, creds, skills),
        "recent_window": health,
        "generated_at": now_iso(),
    }


def _pipeline(caps: list[Capability], creds: list[Credential], skills: list[Skill]) -> list[dict]:
    has_credential = any(c.status == "active" for c in creds)
    has_draft = any(c.status in ("draft", "testing") for c in caps)
    has_published = any(c.status == "published" for c in caps)
    has_skill = any(s.status == "published" for s in skills)
    return [
        {"step": "credential", "label": "配置凭证", "done": has_credential, "target": "/providers"},
        {"step": "capability", "label": "接入能力", "done": has_draft or has_published, "target": "/capabilities/new"},
        {"step": "publish", "label": "验证并发布", "done": has_published, "target": "/capabilities"},
        {"step": "skill", "label": "发布 SKILL", "done": has_skill, "target": "/skills"},
        {"step": "agent", "label": "外部 Agent 接入", "done": has_skill, "target": "/access"},
    ]


def summarize_stats(calls: list[CallLog]) -> dict:
    total = len(calls)
    ok = sum(1 for c in calls if c.status == "success")
    return {
        "calls": total,
        "success_rate": round(ok / total, 4) if total else None,
        "avg_latency_ms": int(sum(c.latency_ms or 0 for c in calls) / total) if total else None,
    }


__all__ = ["overview", "summarize"]
