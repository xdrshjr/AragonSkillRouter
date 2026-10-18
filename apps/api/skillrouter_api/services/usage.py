"""用量与费用（实际 vs 估算，§7.10）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, select

from ..db import db_session, db_write, now_iso, uuid_pk
from ..models import PriceBook, UsageRecord
from ..schemas.common import paginate

DEFAULT_PRICES = {
    "tokens": 0.000002,  # 每 token 估算价（USD），可在 price_book 覆盖
    "images": 0.02,
    "seconds": 0.01,
    "chars": 0.00001,
    "bytes": 0.000001,
}


def unit_price(provider_id: str | None, capability_key: str | None, unit: str) -> float:
    with db_session() as s:
        row = s.scalar(
            select(PriceBook)
            .where(
                PriceBook.provider_id == provider_id,
                PriceBook.unit == unit,
            )
            .order_by(desc(PriceBook.effective_from))
        )
        if row:
            return row.unit_price_usd
    return DEFAULT_PRICES.get(unit, 0.0)


def record(
    *,
    call_id: str | None,
    provider_id: str | None,
    capability_key: str | None,
    token_id: str | None,
    usage: dict | None,
    cost_usd: float | None = None,
) -> dict:
    usage = usage or {}
    unit = usage.get("unit") or "tokens"
    qty = float(usage.get("total") or usage.get("qty") or 0)
    source = usage.get("source") or "estimated"
    if cost_usd is None:
        cost_usd = round(qty * unit_price(provider_id, capability_key, unit), 6)
    with db_write() as s:
        s.add(
            UsageRecord(
                id=uuid_pk("usg"),
                call_id=call_id,
                provider_id=provider_id,
                capability_key=capability_key,
                token_id=token_id,
                unit=unit,
                qty=qty,
                cost_usd=cost_usd,
                source=source,
                at=now_iso(),
            )
        )
    return {"unit": unit, "qty": qty, "cost_usd": cost_usd, "source": source}


def summary(hours: int = 24) -> dict:
    since = (
        datetime.now(timezone.utc) - timedelta(hours=hours)
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
