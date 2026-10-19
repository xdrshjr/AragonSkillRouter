"""路由健康分（近 5 分钟错误率，§7.7 排序依据）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CallLog

WINDOW_MINUTES = 5


def route_health(session: Session, route_id: str) -> dict:
    since = (
        datetime.now(UTC) - timedelta(minutes=WINDOW_MINUTES)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    rows = list(
        session.scalars(
            select(CallLog).where(CallLog.created_at >= since).order_by(CallLog.created_at.desc()).limit(1000)
        )
    )
    matched = [r for r in rows if (r.request_fingerprint or "").find(route_id) >= 0]
    if not matched:
        return {"score": 1.0, "samples": 0, "error_rate": 0.0, "p95_ms": None}
    ok = sum(1 for r in matched if r.status == "success")
    latencies = sorted(r.latency_ms or 0 for r in matched)
    p95 = latencies[int(len(latencies) * 0.95) - 1] if latencies else None
    error_rate = round(1 - ok / len(matched), 4)
    return {"score": round(max(0.0, 1 - error_rate), 4), "samples": len(matched), "error_rate": error_rate, "p95_ms": p95}


def summarize(session: Session) -> dict:
    since = (
        datetime.now(UTC) - timedelta(minutes=WINDOW_MINUTES)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    rows = list(session.scalars(select(CallLog).where(CallLog.created_at >= since)).all())
    total = len(rows)
    ok = sum(1 for r in rows if r.status == "success")
    latencies = sorted(r.latency_ms or 0 for r in rows)
    return {
        "window_minutes": WINDOW_MINUTES,
        "calls": total,
        "success_rate": round(ok / total, 4) if total else None,
        "p95_ms": latencies[int(len(latencies) * 0.95) - 1] if latencies else None,
    }
