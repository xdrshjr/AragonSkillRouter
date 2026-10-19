"""用量与费用（实际 vs 估算，§7.10 / §A.3.4）。

定价阶梯（严格按 §A.3.4-3）：调用/route 显式 `unit_price_usd` → `price_book` 当前有效行 →
`DEFAULT_PRICES`（记为 `estimated`）→ 都没有才 `cost_usd=0` / `source='unknown'`。
成本只累加到 `quota_counters.cost_usd`（日 + 月两行，由 `security.ratelimit.record_cost` 负责），
五个作用域统一走 `account()`。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import desc, select

from ..db import db_session, db_write, now_iso, uuid_pk
from ..logging import get_logger
from ..models import PlatformToken, PriceBook, UsageRecord
from ..models.access import SCOPE_TYPES
from ..schemas.common import paginate
from ..security.ratelimit import record_cost

log = get_logger("skillrouter.run")

DEFAULT_PRICES = {
    "tokens": 0.000002,  # 每 token 估算价（USD），可在 price_book 覆盖
    "images": 0.02,
    "seconds": 0.01,
    "chars": 0.00001,
    "bytes": 0.000001,
}

#: `usage_records.source` 的取值集合（§10.1 枚举）：实际用量 / 估算 / 未知 / 凭证探测
SOURCES = ("provider", "estimated", "unknown", "probe")


def _now_iso() -> str:
    return now_iso()


def _price_book_row(provider_id: str | None, capability_key: str | None, unit: str) -> PriceBook | None:
    """取 `price_book` 当前有效行：能力专属价 → 提供商通用价（`capability_key` 为空）。

    “当前有效”= `effective_from <= now`；同一层级有多行时取 `effective_from` 最新的一行。
    """
    if not unit:
        return None
    now = _now_iso()
    with db_session() as s:
        rows = list(
            s.scalars(
                select(PriceBook)
                .where(PriceBook.unit == unit, PriceBook.effective_from <= now)
                .order_by(desc(PriceBook.effective_from))
            )
        )
    if not rows:
        return None
    for row in rows:
        if capability_key and row.provider_id == provider_id and row.capability_key == capability_key:
            return row
    for row in rows:
        if row.provider_id == provider_id and not row.capability_key:
            return row
    return None


def resolve_price(
    provider_id: str | None, capability_key: str | None, unit: str
) -> tuple[float | None, str]:
    """返回 `(unit_price_usd, price_source)`；`price_source ∈ explicit/price_book/estimated/unknown`。"""
    row = _price_book_row(provider_id, capability_key, unit)
    if row is not None and row.unit_price_usd is not None:
        return float(row.unit_price_usd), "price_book"
    if unit in DEFAULT_PRICES:
        return float(DEFAULT_PRICES[unit]), "estimated"
    return None, "unknown"


def unit_price(provider_id: str | None, capability_key: str | None, unit: str) -> float:
    """单价的向后兼容入口（原签名返回 float）；需要知道取价来源请用 `resolve_price()`。"""
    price, _source = resolve_price(provider_id, capability_key, unit)
    return float(price or 0.0)


def _agent_of(token_id: str | None) -> str | None:
    """没有直接给 `agent_id` 时按 `token_id` 反查（worker 等只持有 token_id 的调用点）。"""
    if not token_id:
        return None
    try:
        with db_session() as s:
            row = s.get(PlatformToken, token_id)
            return row.agent_id if row is not None else None
    except Exception:  # noqa: BLE001 - 记账辅助，绝不阻断
        return None


def account(
    *,
    call_id: str | None,
    provider_id: str | None,
    capability_key: str | None,
    token_id: str | None,
    agent_id: str | None,
    unit: str,
    qty: float | int | None,
    source: str | None,
    unit_price_usd: float | None = None,
    currency: str = "USD",
) -> dict:
    """写 `usage_records` + 推进 `quota_counters.cost_usd`（token/agent/capability/provider/global 五层）。

    返回 `{unit, qty, cost_usd, source, price_source}`；**不抛错、不阻断调用**
    （记账失败只写一条日志，调用结果照常返回）。
    """
    unit = (unit or "tokens").strip() or "tokens"
    if not agent_id and token_id:
        agent_id = _agent_of(token_id)
    try:
        quantity = float(qty or 0)
    except (TypeError, ValueError):
        quantity = 0.0
    source = (source or "estimated").strip() or "estimated"
    if source not in SOURCES:
        source = "estimated"
    if unit_price_usd is not None:
        price: float | None = float(unit_price_usd)
        price_source = "explicit"
    else:
        price, price_source = resolve_price(provider_id, capability_key, unit)
    if price is None:
        # 既没有显式价、价目表、也没有默认价 → 只能记账为「未知价」，绝不阻断调用
        cost_usd = 0.0
        source = "unknown"
    else:
        cost_usd = round(quantity * price, 6)
    result = {
        "unit": unit,
        "qty": quantity,
        "cost_usd": cost_usd,
        "source": source,
        "price_source": price_source,
        "currency": currency,
    }
    scopes = (
        ("token", token_id),
        ("agent", agent_id),
        ("capability", capability_key),
        ("provider", provider_id),
        ("global", "*"),
    )
    try:
        with db_write() as s:
            s.add(
                UsageRecord(
                    id=uuid_pk("usg"),
                    call_id=call_id,
                    provider_id=provider_id,
                    capability_key=capability_key,
                    token_id=token_id,
                    unit=unit,
                    qty=quantity,
                    cost_usd=cost_usd,
                    source=source,
                    at=_now_iso(),
                )
            )
        for scope_type, scope_id in scopes:
            if scope_type not in SCOPE_TYPES or not scope_id:
                continue
            record_cost(scope_type=scope_type, scope_id=scope_id, cost_usd=cost_usd)
    except Exception:  # noqa: BLE001 - 记账绝不能阻断调用（§A.3.4）
        log.warning(
            "usage_account_failed",
            extra={
                "call_id": call_id,
                "capability_key": capability_key,
                "provider_id": provider_id,
                "unit": unit,
                "qty": quantity,
            },
        )
    return result


def record(
    *,
    call_id: str | None,
    provider_id: str | None,
    capability_key: str | None,
    token_id: str | None,
    usage: dict | None,
    cost_usd: float | None = None,
    agent_id: str | None = None,
) -> dict:
    """`account()` 的薄封装（向后兼容既有调用点）：把 `usage` 字典拆成 account 的入参。

    显式传入 `cost_usd` 时折算成单价（`qty=0` 时按 0 记），其余情况走定价阶梯。
    """
    usage = usage or {}
    unit = usage.get("unit") or "tokens"
    raw_qty = usage.get("total")
    if raw_qty is None:
        raw_qty = usage.get("qty")
    try:
        qty = float(raw_qty or 0)
    except (TypeError, ValueError):
        qty = 0.0
    source = usage.get("source") or "estimated"
    explicit_price = None
    if cost_usd is not None and qty:
        explicit_price = float(cost_usd) / qty
    return account(
        call_id=call_id,
        provider_id=provider_id,
        capability_key=capability_key,
        token_id=token_id,
        agent_id=agent_id,
        unit=unit,
        qty=qty,
        source=source,
        unit_price_usd=explicit_price,
    )


def summary(hours: int = 24) -> dict:
    since = (
        datetime.now(UTC) - timedelta(hours=hours)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    with db_session() as s:
        rows = list(s.scalars(select(UsageRecord).where(UsageRecord.at >= since)))
    total_cost = round(sum(r.cost_usd for r in rows), 6)
    by_provider: dict[str, dict] = {}
    for r in rows:
        key = r.provider_id or "unknown"
        entry = by_provider.setdefault(key, {"provider_id": key, "qty": 0.0, "cost_usd": 0.0, "calls": 0})
        entry["qty"] += r.qty
        entry["cost_usd"] = round(entry["cost_usd"] + r.cost_usd, 6)
        entry["calls"] += 1
    return {
        "window_hours": hours,
        "records": len(rows),
        "cost_usd": total_cost,
        "by_provider": list(by_provider.values()),
        "provider_actual": sum(1 for r in rows if r.source == "provider"),
        "estimated": sum(1 for r in rows if r.source != "provider"),
    }


def list_records(page: int = 1, page_size: int = 20, provider_id: str | None = None) -> dict:
    with db_session() as s:
        stmt = select(UsageRecord).order_by(desc(UsageRecord.at))
        if provider_id:
            stmt = stmt.where(UsageRecord.provider_id == provider_id)
        rows = list(s.scalars(stmt.limit(1000)))
    items = [
        {
            "id": r.id,
            "call_id": r.call_id,
            "provider_id": r.provider_id,
            "capability_key": r.capability_key,
            "unit": r.unit,
            "qty": r.qty,
            "cost_usd": r.cost_usd,
            "source": r.source,
            "at": r.at,
        }
        for r in rows
    ]
    return paginate(items, page, page_size)


def set_price(provider_id: str, unit: str, unit_price_usd: float, capability_key: str | None = None) -> dict:
    with db_write() as s:
        row = PriceBook(
            id=uuid_pk("prc"),
            provider_id=provider_id,
            capability_key=capability_key,
            unit=unit,
            unit_price_usd=unit_price_usd,
            currency="USD",
            effective_from=now_iso(),
            updated_at=now_iso(),
        )
        s.add(row)
    return {"id": row.id, "unit_price_usd": unit_price_usd}


__all__ = [
    "DEFAULT_PRICES",
    "SOURCES",
    "account",
    "record",
    "resolve_price",
    "unit_price",
    "summary",
    "list_records",
    "set_price",
]
