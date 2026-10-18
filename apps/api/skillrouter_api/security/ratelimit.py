"""限流：登录固定窗口 + 通用速率/并发/配额计数器（§7.3 / §7.10）。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..db import db_session, db_write, now_iso, uuid_pk
from ..errors import ApiError, Code
from ..models import LoginAttempt, QuotaCounter, QuotaLimit, RateCounter
from ..settings import settings

WINDOW_SECONDS = 60


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# ── 登录限流 ──────────────────────────────────────────────────


def record_attempt(username: str, ip: str | None, success: bool) -> None:
    with db_write() as s:
        s.add(
            LoginAttempt(
                id=uuid_pk("lat"),
                username=username[:64],
                ip=ip,
                at=now_iso(),
                success=1 if success else 0,
            )
        )


def check_login_allowed(username: str, ip: str | None) -> None:
    """超限抛 LOGIN_THROTTLED（带 retry_after）。

    窗口按 **用户名 + IP** 组合统计（需求「5 次/5 分钟/IP+用户名」）：
    同一 IP 的其它账号、同一账号的其它 IP 都不受影响，避免误锁全员。
    """
    window_start = datetime.now(timezone.utc) - timedelta(seconds=settings.login_window_seconds)
    with db_session() as s:
        stmt = select(LoginAttempt).where(
            LoginAttempt.username == username, LoginAttempt.success == 0
        )
        if ip:
            stmt = stmt.where(LoginAttempt.ip == ip)
        rows = list(s.scalars(stmt.order_by(LoginAttempt.at.desc()).limit(50)))
    recent = [r for r in rows if (_dt(r.at) or window_start) >= window_start]
    if len(recent) < settings.login_max_failures:
        return
    last = _dt(recent[0].at)
    lock_until = (last or window_start) + timedelta(seconds=settings.login_lock_seconds)
    now = datetime.now(timezone.utc)
    if lock_until > now:
        retry = int((lock_until - now).total_seconds()) + 1
        raise ApiError(
            Code.LOGIN_THROTTLED,
            message="登录尝试过多",
            hint=f"登录失败 {len(recent)} 次，请等待 {retry} 秒后重试",
            details={"retry_after": retry},
        )


def clear_login_window(username: str) -> None:
    """成功登录后清空窗口（标记 success 记录）。"""
    with db_write() as s:
        rows = list(
            s.scalars(
                select(LoginAttempt).where(
                    LoginAttempt.username == username, LoginAttempt.success == 0
                )
            )
        )
        for row in rows:
            row.success = 1


# ── 速率 / 并发 / 配额 ────────────────────────────────────────


def _window_start(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    epoch = int(now.timestamp())
    start = epoch - (epoch % WINDOW_SECONDS)
    return datetime.fromtimestamp(start, tz=timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _limits_for(scope_type: str, scope_id: str) -> QuotaLimit | None:
    with db_session() as s:
        row = s.scalar(
            select(QuotaLimit).where(
                QuotaLimit.scope_type == scope_type, QuotaLimit.scope_id == scope_id
            )
        )
        if row:
            return row
        return s.scalar(
            select(QuotaLimit).where(
                QuotaLimit.scope_type == scope_type, QuotaLimit.scope_id == "*"
            )
        )


def check_and_count(
    *,
    scope_type: str,
    scope_id: str,
    rpm: int | None = None,
    concurrency: int | None = None,
    daily_calls: int | None = None,
) -> None:
    """检查并累加一个调用。四类 scope：token / capability / provider / global。

    检查顺序：令牌 → 能力 → 提供商 → 全局（调用方按此顺序逐个调用本函数）。
    """
    limit = _limits_for(scope_type, scope_id)
    eff_rpm = rpm if rpm is not None else (limit.rpm if limit else None)
    eff_conc = concurrency if concurrency is not None else (limit.concurrency if limit else None)
    eff_daily = daily_calls if daily_calls is not None else (limit.daily_calls if limit else None)
    window = _window_start()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with db_write() as s:
        counter_id = hashlib.sha256(
            f"{scope_type}|{scope_id}|{window}|{WINDOW_SECONDS}".encode()
        ).hexdigest()[:32]
        counter = s.get(RateCounter, counter_id)
        if counter is None:
            counter = RateCounter(
                id=counter_id,
                scope_type=scope_type,
                scope_id=scope_id,
                window_start=window,
                window_seconds=WINDOW_SECONDS,
                calls=0,
                concurrent=0,
                updated_at=now_iso(),
            )
            s.add(counter)
        if eff_rpm is not None and counter.calls >= eff_rpm:
            raise ApiError(
                Code.QUOTA_EXCEEDED,
                message=f"{scope_type} 级速率超限（{eff_rpm} 次/分钟）",
                hint="调整配额或等待窗口重置",
                details={"scope": scope_type, "retry_after": WINDOW_SECONDS},
            )
        if eff_conc is not None and counter.concurrent >= eff_conc:
            raise ApiError(
                Code.QUOTA_EXCEEDED,
                message=f"{scope_type} 级并发超限（{eff_conc}）",
                hint="等待在途请求结束后重试",
                details={"scope": scope_type, "retry_after": 2},
            )
        counter.calls += 1
        counter.concurrent += 1
        counter.updated_at = now_iso()

        qc = s.scalar(
            select(QuotaCounter).where(
                QuotaCounter.scope_type == scope_type,
                QuotaCounter.scope_id == scope_id,
                QuotaCounter.period == today,
            )
        )
        if qc is None:
            qc = QuotaCounter(
                id=uuid_pk("qcn"),
                scope_type=scope_type,
                scope_id=scope_id,
                period=today,
                calls=0,
                cost_usd=0.0,
                updated_at=now_iso(),
            )
            s.add(qc)
        if eff_daily is not None and qc.calls >= eff_daily:
            raise ApiError(
                Code.QUOTA_EXCEEDED,
                message=f"{scope_type} 级日调用配额已用尽（{eff_daily} 次/天）",
                details={"scope": scope_type, "retry_after": 3600},
            )
        qc.calls += 1
        qc.updated_at = now_iso()


def release_concurrency(*, scope_type: str, scope_id: str) -> None:
    window = _window_start()
    counter_id = hashlib.sha256(
        f"{scope_type}|{scope_id}|{window}|{WINDOW_SECONDS}".encode()
    ).hexdigest()[:32]
    with db_write() as s:
        counter = s.get(RateCounter, counter_id)
        if counter and counter.concurrent > 0:
            counter.concurrent -= 1
            counter.updated_at = now_iso()


def record_cost(*, scope_type: str, scope_id: str, cost_usd: float) -> None:
    if cost_usd <= 0:
        return
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    month = today[:7]
    with db_write() as s:
        for period in (today, month):
            qc = s.scalar(
                select(QuotaCounter).where(
                    QuotaCounter.scope_type == scope_type,
                    QuotaCounter.scope_id == scope_id,
                    QuotaCounter.period == period,
                )
            )
            if qc is None:
                qc = QuotaCounter(
                    id=uuid_pk("qcn"),
                    scope_type=scope_type,
                    scope_id=scope_id,
                    period=period,
                    calls=0,
                    cost_usd=0.0,
                    updated_at=now_iso(),
                )
                s.add(qc)
            qc.cost_usd += cost_usd
            qc.updated_at = now_iso()


def check_budget(*, scope_type: str, scope_id: str, max_cost_usd: float | None) -> None:
    if not max_cost_usd or max_cost_usd <= 0:
        return
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    with db_session() as s:
        qc = s.scalar(
            select(QuotaCounter).where(
                QuotaCounter.scope_type == scope_type,
                QuotaCounter.scope_id == scope_id,
                QuotaCounter.period == month,
            )
        )
        spent = qc.cost_usd if qc else 0.0
    if spent >= max_cost_usd:
        raise ApiError(
            Code.BUDGET_EXCEEDED,
            message=f"预算已用尽（已用 ${spent:.4f} / 上限 ${max_cost_usd:.4f}）",
            hint="请在系统设置中调整月预算",
            details={"spent_usd": round(spent, 6), "budget_usd": max_cost_usd},
        )


def quota_snapshot(scope_type: str, scope_id: str) -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    month = today[:7]
    with db_session() as s:
        daily = s.scalar(
            select(QuotaCounter).where(
                QuotaCounter.scope_type == scope_type,
                QuotaCounter.scope_id == scope_id,
                QuotaCounter.period == today,
            )
        )
        monthly = s.scalar(
            select(QuotaCounter).where(
                QuotaCounter.scope_type == scope_type,
                QuotaCounter.scope_id == scope_id,
                QuotaCounter.period == month,
            )
        )
        minute = s.scalar(
            select(RateCounter).where(
                RateCounter.scope_type == scope_type,
                RateCounter.scope_id == scope_id,
                RateCounter.window_start == _window_start(),
            )
        )
    return {
        "calls_today": daily.calls if daily else 0,
        "cost_month_usd": round(monthly.cost_usd, 6) if monthly else 0.0,
        "calls_this_minute": minute.calls if minute else 0,
        "concurrent": minute.concurrent if minute else 0,
    }
