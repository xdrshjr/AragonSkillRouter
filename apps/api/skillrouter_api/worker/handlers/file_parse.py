"""文档解析：pdf 文本层 / txt / md / json（§11.3，不 OCR）。"""

from __future__ import annotations

import re

from ...logging import get_logger, redact
from ...services import files

log = get_logger("skillrouter.run")

MAX_PAGES = 50
MAX_CHARS = 120_000


def _pdf_text(data: bytes) -> tuple[str, int]:
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - 可选依赖
        return (
            "（未安装 pypdf，无法提取 PDF 文本层；请安装或改用 txt/md 上传）",
            0,
        )
    import io

    reader = PdfReader(io.BytesIO(data))
    pages = reader.pages[:MAX_PAGES]
    parts = []
    for index, page in enumerate(pages):
        try:
            parts.append(f"--- 第 {index + 1} 页 ---\n" + (page.extract_text() or ""))
        except Exception:  # noqa: BLE001
            parts.append(f"--- 第 {index + 1} 页（无法解析）---")
    return "\n".join(parts), len(reader.pages)


def summarize(text: str, *, limit: int = 4000) -> dict:
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    title = lines[0][:200] if lines else ""
    headings = [line for line in lines if re.match(r"^#{1,4}\s|^\d+\.\s", line)][:20]
    code_blocks = re.findall(r"```[\s\S]{0,2000}?```", text)[:5]
    return {
        "title": title,
        "line_count": len(lines),
        "char_count": len(text),
        "headings": headings,
        "code_blocks": [redact(block, max_len=800) for block in code_blocks],
        "excerpt": redact(text[:limit], max_len=limit),
        "truncated": len(text) > limit,
    }


async def handle(payload: dict) -> None:
    file_id = payload.get("file_id")
    if not file_id:
        return
    data, record = files.read_bytes(file_id)
    mime = record.mime or ""
    if mime == "application/pdf":
        text, pages = _pdf_text(data)
        truncated = pages > MAX_PAGES
    else:
        text = data.decode("utf-8", errors="replace")[:MAX_CHARS]
        pages = 1
        truncated = len(text) >= MAX_CHARS
    summary = summarize(text)
    summary["pages"] = pages
    summary["truncated"] = summary["truncated"] or truncated
    log.info("file_parsed", extra={"object": file_id, "detail": {"chars": summary["char_count"]}})
    from ...db import db_write, now_iso, uuid_pk
    from ...models import AgentArtifact

    with db_write() as s:
        s.add(
            AgentArtifact(
                id=uuid_pk("art"),
                turn_id=payload.get("turn_id"),
                kind="finding",
                payload={"kind": "document_summary", "file_id": file_id, **summary},
                status="pending_review",
                created_at=now_iso(),
            )
        )
