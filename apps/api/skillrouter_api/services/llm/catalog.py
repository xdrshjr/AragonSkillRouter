"""模型目录：归一化 → 分类 → TTL 缓存 → 失败降级（spec §3.5）。

「稳健」在这里的具体含义是**上游抖动时不让用户看到「一个模型都没有」**：

```
refresh=True            → 直打上游 → 成功写库 → source="upstream"
refresh=False, 未过期   → 读库     → source="cache"
refresh=False, 已过期   → 打上游 → 成功写库 → source="upstream"
                          ↘ 失败：库里还有 <24h 的旧值 → 200 + source="cache-stale" + warning
                          ↘ 失败且无旧值             → 503 UPSTREAM_UNAVAILABLE
```

另一个必须做对的地方是**命中校验**（spec §3.5 / R-11）：读缓存前重算 `dedupe_key`
并核对命中的那一行，键不匹配（地址或凭证换了）就当作未命中 —— 否则会把「别人的清单」
当成新地址的清单展示出来。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select

from ...db import db_session, db_write, now_iso
from ...errors import ApiError, Code
from ...logging import get_logger, redact
from ...models import LlmModelCatalog
from ...settings import settings
from . import protocols
from .protocols import ModelEntry

log = get_logger("skillrouter.run")

#: 一次最多返回多少条（spec §3.5：`limit` 上限 500）
MAX_LIMIT = 500
#: 上游抖动时的降级窗口：库里 24 小时内的旧值仍然可展示
STALE_WINDOW_HOURS = 24
#: anthropic 的续页上限（避免一次点击打爆上游）
MAX_PAGES = 3
#: TTL 的合法区间（spec §6.1：60–86400）
TTL_MIN, TTL_MAX = 60, 86400

#: 分类正则（按顺序匹配，命中即止）
_KIND_RULES: tuple[tuple[str, str], ...] = (
    (r"embed", "embedding"),
    (r"rerank", "rerank"),
    (r"whisper|transcrib|tts|speech|audio|realtime", "audio"),
    (r"dall-e|image|flux|sd-?3|imagen", "image"),
    (
        r"chat|instruct|gpt|claude|gemini|qwen|deepseek|glm|llama|mistral|kimi|doubao|o1|o3|o4",
        "chat",
    ),
)

#: 「推荐」白名单（ADR-8：正则而不是外部 yaml，误判只影响排序）
_RECOMMENDED = re.compile(
    r"gpt-4|gpt-5|o3|o4|claude-(3|4|sonnet|opus|haiku)|gemini-(1\.5|2|3)|qwen[23]"
    r"|deepseek-(v3|r1|chat)|glm-4|llama-3\.[1-3]|mistral-large|kimi-k2",
    re.IGNORECASE,
)


class UpstreamFetchError(Exception):
    """上游取清单失败：由探测/目录两条路径共享同一个失败描述。"""

    def __init__(
        self,
        error_code: str,
        *,
        http_status: int | None = None,
        detail: str = "",
        hint: str | None = None,
    ) -> None:
        super().__init__(detail or error_code)
        self.error_code = error_code
        self.http_status = http_status
        self.detail = detail
        self.hint = hint


@dataclass(slots=True)
class FetchResult:
    """一次「打上游取清单」的结果（成功路径）。"""

    entries: list[ModelEntry] = field(default_factory=list)
    truncated: bool = False
    notes: list[str] = field(default_factory=list)
    http_status: int | None = None


def classify(model_id: str) -> str:
    """模型用途分类：embedding / rerank / audio / image / chat / other。"""
    needle = (model_id or "").lower()
    for pattern, kind in _KIND_RULES:
        if re.search(pattern, needle):
            return kind
    return "other"


def is_recommended(model_id: str) -> bool:
    return bool(_RECOMMENDED.search(model_id or ""))


def normalize(payload: dict, spec: protocols.ProtocolSpec) -> tuple[list[ModelEntry], bool]:
    """归一化一页响应：返回 `(entries, has_more)`（spec §3.5 的四形状 + 兜底）。"""
    entries = protocols.parse_models(spec, payload)
    for entry in entries:
        entry.kind = classify(entry.id)
        entry.recommended = is_recommended(entry.id)
    has_more, _cursor = protocols.models_has_more(spec, payload)
    return entries, has_more


def ttl_seconds() -> int:
    value = int(getattr(settings, "llm_catalog_ttl_s", 600) or 600)
    return max(TTL_MIN, min(TTL_MAX, value))


def dedupe_key(identity) -> str:  # noqa: ANN001
    """`sha256(protocol|wire|base_url|credential_id)[:32]`（spec §3.5）。"""
    raw = "|".join(
        [
            str(getattr(identity, "protocol", "") or ""),
            str(getattr(identity, "wire", "") or ""),
            str(getattr(identity, "base_url", "") or ""),
            str(getattr(identity, "credential_id", "") or ""),
        ]
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def target_key(protocol: str, wire: str, base_url: str, credential_id: str | None = None) -> str:
    """给 `invalidate_by_target()` 用的同口径键（不构造 identity）。"""

    target = SimpleNamespace(
        protocol=protocol, wire=wire, base_url=base_url, credential_id=credential_id
    )
    return dedupe_key(target)


def _iso(delta_seconds: int = 0) -> str:
    return (
        (datetime.now(UTC) + timedelta(seconds=delta_seconds))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _sort_key(entry: ModelEntry) -> tuple:
    return (0 if entry.recommended else 1, 0 if entry.kind == "chat" else 1, entry.id)


def shape_items(
    entries: list[ModelEntry], *, include_non_llm: bool, limit: int
) -> tuple[list[dict], bool]:
    """过滤 + 排序 + 截断，返回 `(items, truncated)`。"""
    rows = list(entries)
    if not include_non_llm:
        rows = [r for r in rows if r.kind in ("chat", "other")]
    rows.sort(key=_sort_key)
    cap = max(1, min(int(limit or MAX_LIMIT), MAX_LIMIT))
    truncated = len(rows) > cap
    return [r.as_dict() for r in rows[:cap]], truncated


async def fetch_from_upstream(identity) -> FetchResult:  # noqa: ANN001
    """真实打上游把清单取回来（含 anthropic 的续页），失败抛 `UpstreamFetchError`。"""
    from . import probe as probe_engine

    spec = protocols.get(getattr(identity, "protocol", ""))
    wire = protocols.wire_of(spec.key, getattr(identity, "wire", None))
    method, url = protocols.build_models_request(spec, identity)
    entries: list[ModelEntry] = []
    notes: list[str] = []
    truncated = False
    http_status: int | None = None
    for _page in range(MAX_PAGES):
        try:
            response = await probe_engine.request(
                identity,
                method,
                url,
                timeout_s=settings.llm_probe_timeout_s,
                stage="auth",
            )
        except UpstreamFetchError:
            raise
        http_status = response.status_code
        if response.status_code in (401, 403):
            raise UpstreamFetchError(
                Code.UPSTREAM_AUTH,
                http_status=response.status_code,
                detail=_safe_detail(response, identity),
                hint="上游拒绝鉴权：检查 API key 是否属于该项目、是否已启用对应 API",
            )
        if response.status_code == 429:
            raise UpstreamFetchError(
                Code.UPSTREAM_RATE_LIMIT,
                http_status=response.status_code,
                detail=_safe_detail(response, identity),
                hint="上游限流：稍后重试，或降低刷新频率",
            )
        if response.status_code >= 400:
            raise UpstreamFetchError(
                Code.UPSTREAM_ERROR,
                http_status=response.status_code,
                detail=_safe_detail(response, identity),
                hint="上游返回错误：检查地址与路径是否指向该协议的模型清单端点",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamFetchError(
                Code.UPSTREAM_INVALID_RESPONSE,
                http_status=response.status_code,
                detail=_safe_detail(response, identity) or "上游 200 但不是 JSON",
                hint="该地址可能不是该协议的 API 端点",
            ) from exc
        try:
            page_entries, has_more = normalize(payload, spec)
        except ApiError as exc:
            raise UpstreamFetchError(
                exc.code,
                http_status=response.status_code,
                detail=_safe_detail(response, identity),
                hint=exc.hint,
            ) from exc
        entries.extend(page_entries)
        if not has_more:
            break
        cursor = payload.get("last_id") if isinstance(payload, dict) else None
        if not cursor:
            notes.append("上游声明还有更多模型，但没有给出续页游标：本次只取到一页")
            break
        query = protocols.models_query(identity)
        query["after_id"] = str(cursor)
        url = protocols.compose_url(
            getattr(identity, "base_url", ""),
            protocols.effective_paths(spec.key, wire, getattr(identity, "paths", None)).models
            or "",
            query,
        )
    else:
        notes.append(f"模型清单超过 {MAX_PAGES} 页，本次只取前 {MAX_PAGES} 页")
    if spec.paginate == "google_next_page_token":
        notes.append("Google 只取第一页（pageSize=1000 已覆盖全部候选），不续 pageToken")
    if not entries:
        notes.append("上游返回了空的模型清单：该网关可能不实现 /models，请手填模型名")
    return FetchResult(entries=entries, truncated=truncated, notes=notes, http_status=http_status)


def _safe_detail(response, identity) -> str:  # noqa: ANN001
    """上游响应摘要：先 `redact()`，再把本次密钥原文显式换成 `***`。

    实测 `logging._PATTERNS` 只覆盖 `sk-…` / `srk_…` / `Bearer …` 等形态，
    自建网关的 `gw-xxxx` 类自定义 key 不命中任何模式，必须补这一刀（spec §3.4）。
    """
    text = redact(response.text or "", max_len=400)
    secret = str(getattr(identity, "secret", "") or "")
    if secret and isinstance(text, str):
        text = text.replace(secret, "***")
    return text


def store(
    identity,  # noqa: ANN001 - identity 是 `ProbeIdentity`（延迟导入避免环）
    entries: list[ModelEntry],
    *,
    truncated: bool,
    fetched_at: str | None = None,
) -> dict:
    """把一次成功的上游结果 upsert 进缓存（按 `dedupe_key`），返回写入的行摘要。"""
    key = dedupe_key(identity)
    fetched = fetched_at or _iso()
    expires = _iso(ttl_seconds())
    rows, capped = shape_items(entries, include_non_llm=True, limit=MAX_LIMIT)
    payload = rows
    with db_write() as s:
        row = s.scalar(select(LlmModelCatalog).where(LlmModelCatalog.dedupe_key == key))
        if row is None:
            row = LlmModelCatalog(
                dedupe_key=key,
                credential_id=getattr(identity, "credential_id", None),
                provider_id=getattr(identity, "provider_id", None),
                protocol=str(getattr(identity, "protocol", "") or ""),
                wire=str(getattr(identity, "wire", "") or ""),
                base_url=str(getattr(identity, "base_url", "") or ""),
                models=payload,
                model_count=len(payload),
                truncated=1 if (truncated or capped) else 0,
                source="upstream",
                fetched_at=fetched,
                expires_at=expires,
                created_at=now_iso(),
                updated_at=now_iso(),
            )
            s.add(row)
        else:
            row.credential_id = getattr(identity, "credential_id", None)
            row.provider_id = getattr(identity, "provider_id", None)
            row.protocol = str(getattr(identity, "protocol", "") or "")
            row.wire = str(getattr(identity, "wire", "") or "")
            row.base_url = str(getattr(identity, "base_url", "") or "")
            row.models = payload
            row.model_count = len(payload)
            row.truncated = 1 if (truncated or capped) else 0
            row.source = "upstream"
            row.fetched_at = fetched
            row.expires_at = expires
            row.last_error_redacted = None
            row.updated_at = now_iso()
    log.info(
        "llm_catalog_refreshed",
        extra={
            "object": getattr(identity, "credential_id", None) or "draft",
            "detail": {
                "protocol": getattr(identity, "protocol", None),
                "wire": getattr(identity, "wire", None),
                "model_count": len(payload),
            },
        },
    )
    return {
        "fetched_at": fetched,
        "expires_at": expires,
        "model_count": len(payload),
        "truncated": bool(truncated or capped),
    }


def _read_row(identity) -> LlmModelCatalog | None:  # noqa: ANN001
    key = dedupe_key(identity)
    with db_session() as s:
        row = s.scalar(select(LlmModelCatalog).where(LlmModelCatalog.dedupe_key == key))
        if row is None:
            return None
        # 命中校验：键不匹配（地址/凭证换了）就当未命中，绝不用旧行凑一个 200
        if row.base_url != str(getattr(identity, "base_url", "") or ""):
            return None
        return row


def _rows_to_entries(row: LlmModelCatalog) -> list[ModelEntry]:
    out: list[ModelEntry] = []
    for item in row.models or []:
        if not isinstance(item, dict):
            continue
        out.append(
            ModelEntry(
                id=str(item.get("id") or ""),
                display_name=str(item.get("display_name") or item.get("id") or ""),
                kind=str(item.get("kind") or "other"),
                recommended=bool(item.get("recommended")),
                context_window=item.get("context_window"),
                methods=[str(m) for m in (item.get("methods") or [])],
                created_at=item.get("created_at"),
                owned_by=item.get("owned_by"),
            )
        )
    return [e for e in out if e.id]


#: 「上游不可用」类错误：无缓存可降级时统一成 503 `UPSTREAM_UNAVAILABLE`（spec §5.3）
_UNAVAILABLE_CODES = frozenset(
    {Code.UPSTREAM_UNAVAILABLE, Code.UPSTREAM_ERROR, Code.UPSTREAM_TIMEOUT}
)


def _degraded_code(error_code: str) -> str:
    """无缓存可降级时的对外错误码。

    可用性类失败（5xx / 超时 / 连不上）统一成 `UPSTREAM_UNAVAILABLE`(503)，
    语义是「这个端点现在给不出清单」；而鉴权 / 限流 / 响应形状这类**可处置**的失败
    保留原码，否则用户会把「key 错了」误读成「服务挂了」。
    """
    return Code.UPSTREAM_UNAVAILABLE if error_code in _UNAVAILABLE_CODES else error_code


def _within_stale_window(fetched_at: str | None) -> bool:
    if not fetched_at:
        return False
    horizon = _iso(-STALE_WINDOW_HOURS * 3600)
    return fetched_at >= horizon


def _mark_error(identity, detail: str) -> None:  # noqa: ANN001
    """把失败摘要记在缓存行的 `last_error_redacted`（没有行就跳过）。"""
    key = dedupe_key(identity)
    try:
        with db_write() as s:
            row = s.scalar(select(LlmModelCatalog).where(LlmModelCatalog.dedupe_key == key))
            if row is not None:
                row.last_error_redacted = redact(detail, max_len=400)
                row.updated_at = now_iso()
    except Exception as exc:  # noqa: BLE001 - 记录失败摘要不能影响主流程
        log.warning("llm_catalog_mark_failed", extra={"detail": redact(str(exc))})


async def list_models(
    identity: Any,
    *,
    refresh: bool = False,
    include_non_llm: bool = False,
    limit: int = MAX_LIMIT,
    record_probe: bool = True,
    actor: str | None = None,
    mode: str = "auth",
) -> dict:
    """取模型清单（缓存优先 + 失败降级），返回 spec §5.3 的响应形状。"""
    from . import probe as probe_engine

    row = _read_row(identity)
    fresh = bool(row and row.expires_at > _iso())
    notes: list[str] = []
    warning: str | None = None
    source = "cache"
    fetched_at = row.fetched_at if row else None
    expires_at = row.expires_at if row else None
    entries: list[ModelEntry] = []

    if row is not None and fresh and not refresh:
        entries = _rows_to_entries(row)
        notes.append("清单来自缓存（未过期）；需要重新拉取请勾选「强制刷新」")
    else:
        try:
            result = await fetch_from_upstream(identity)
        except UpstreamFetchError as exc:
            _mark_error(identity, exc.detail)
            if row is not None and _within_stale_window(row.fetched_at):
                entries = _rows_to_entries(row)
                source = "cache-stale"
                warning = (
                    f"上游暂时不可用，展示的是 {_human_age(row.fetched_at)}的清单"
                    f"（{row.fetched_at}）"
                )
                log.warning(
                    "llm_catalog_degraded",
                    extra={
                        "object": getattr(identity, "credential_id", None) or "draft",
                        "detail": {
                            "error_code": exc.error_code,
                            "http_status": exc.http_status,
                            "detail": exc.detail,
                        },
                    },
                )
            else:
                raise ApiError(
                    _degraded_code(exc.error_code),
                    message="取模型清单失败，且没有可用的历史缓存",
                    hint=exc.hint or "检查地址与凭证；也可以手填模型名",
                    details={
                        "upstream": {
                            "stage": "auth",
                            "http_status": exc.http_status,
                            "detail": exc.detail,
                            "error_code": exc.error_code,
                        }
                    },
                ) from exc
        else:
            entries = result.entries
            notes.extend(result.notes)
            stored = store(identity, entries, truncated=result.truncated)
            source = "upstream"
            fetched_at = stored["fetched_at"]
            expires_at = stored["expires_at"]

    items, truncated = shape_items(entries, include_non_llm=include_non_llm, limit=limit)
    probe_id = None
    if record_probe:
        probe_id = probe_engine.record_probe(
            identity,
            mode=mode,
            model=getattr(identity, "model", None),
            ok=True,
            stages=[
                {
                    "stage": "auth",
                    "ok": True,
                    "http_status": 200,
                    "latency_ms": 0,
                    "detail": f"识别到 {len(entries)} 个模型（source={source}）",
                    "model_count": len(entries),
                }
            ],
            http_status=200,
            latency_ms=0,
            error_code=None,
            detail_redacted=warning,
            actor=actor,
        )
    return {
        "items": items,
        "count": len(items),
        "source": source,
        "fetched_at": fetched_at,
        "expires_at": expires_at,
        "truncated": truncated,
        "probe_id": probe_id,
        "notes": notes,
        "warning": warning,
    }


def _human_age(iso_ts: str | None) -> str:
    if not iso_ts:
        return "未知时间"
    try:
        parsed = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return "若干分钟前"
    minutes = max(0, int((datetime.now(UTC) - parsed).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} 分钟前"
    return f"{minutes // 60} 小时前"


def invalidate(credential_id: str) -> int:
    """清掉某条凭证的全部缓存行（凭证被替换 / 轮换 / 停用 / 改协议时调用）。"""
    if not credential_id:
        return 0
    with db_write() as s:
        rows = list(
            s.scalars(select(LlmModelCatalog).where(LlmModelCatalog.credential_id == credential_id))
        )
        for row in rows:
            s.delete(row)
    if rows:
        log.info(
            "llm_catalog_invalidated",
            extra={"object": credential_id, "detail": {"rows": len(rows)}},
        )
    return len(rows)


def invalidate_by_target(protocol: str, wire: str, base_url: str) -> int:
    """清掉「协议 + wire + 地址」对应的缓存行（`PATCH /agent/config` 用，不依赖前端）。"""
    if not protocol or not base_url:
        return 0
    key = target_key(protocol, wire, base_url)
    with db_write() as s:
        repo = list(s.scalars(select(LlmModelCatalog).where(LlmModelCatalog.dedupe_key == key)))
        for row in repo:
            s.delete(row)
    return len(repo)


__all__ = [
    "MAX_LIMIT",
    "ModelEntry",
    "STALE_WINDOW_HOURS",
    "FetchResult",
    "UpstreamFetchError",
    "classify",
    "dedupe_key",
    "fetch_from_upstream",
    "invalidate",
    "invalidate_by_target",
    "is_recommended",
    "list_models",
    "normalize",
    "shape_items",
    "store",
    "target_key",
    "ttl_seconds",
]
