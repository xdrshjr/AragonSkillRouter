"""通用 Schema 与分页工具（§9.1）。"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ApiErrorBody(BaseModel):
    code: str
    message: str
    hint: str | None = None
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str


class Page(BaseModel, Generic[T]):
    items: list[T]
    page: int = 1
    page_size: int = 20
    total: int = 0


class IdResponse(BaseModel):
    id: str


class OkResponse(BaseModel):
    ok: bool = True
    request_id: str | None = None


class PageParams(BaseModel):
    page: int = 1
    page_size: int = 20
    sort: str | None = None
    q: str | None = None


def paginate(items: list[Any], page: int = 1, page_size: int = 20) -> dict:
    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    total = len(items)
    start = (page - 1) * page_size
    return {
        "items": items[start : start + page_size],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


def sort_items(items: list[dict], sort: str | None, default: str = "-created_at") -> list[dict]:
    key_expr = sort or default
    descending = key_expr.startswith("-")
    key = key_expr.lstrip("-+")

    def _key(item: dict) -> Any:
        value = item.get(key)
        return (value is None, value if value is not None else "")

    try:
        return sorted(items, key=_key, reverse=descending)
    except TypeError:
        return items
