"""SKILL 导入：Git / URL / 文件，预览 → 校验 → 落地（§7.9）。"""

from __future__ import annotations

import hashlib
import io
import re
import tarfile
import zipfile
from pathlib import PurePosixPath

import yaml
from sqlalchemy import select

from ..db import db_session, db_write, now_iso, uuid_pk
from ..errors import ApiError, Code
from ..logging import get_logger, redact
from ..models import Skill, SkillImport
from ..security.fetch_guard import fetch_text
from ..security.instruction_scanner import scan
from ..settings import settings
from . import audit
from . import skills as skill_service

log = get_logger("skillrouter.run")

MAX_ENTRIES = 200
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def _safe_path(path: str) -> str | None:
    """返回 None 表示拒绝。"""
    raw = path.replace("\\", "/").lstrip("./")
    if not raw:
        return None
    if raw.startswith("/") or ".." in raw.split("/"):
        return None
    parts = PurePosixPath(raw).parts
    for part in parts:
        if part.upper() in RESERVED_NAMES or part.startswith("."):
            return None
    return raw


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    match = FRONTMATTER.match(text)
    if not match:
        return {}, text
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(meta, dict):
        return {}, text
    return meta, match.group(2)


def _inspect_tree(entries: list[tuple[str, bytes]]) -> dict:
    warnings: list[dict] = []
    files: list[dict] = []
    requires_sandbox = False
    total = 0
    if len(entries) > MAX_ENTRIES:
        raise ApiError(
            Code.SKILL_IMPORT_INVALID,
            message=f"文件条目超过 {MAX_ENTRIES} 个",
            details={"count": len(entries)},
        )
    for path, content in entries:
        safe = _safe_path(path)
        if safe is None:
            raise ApiError(
                Code.SKILL_IMPORT_INVALID,
                message=f"非法路径：{path}",
                hint="不允许绝对路径、.. 穿越、符号链接、Windows 保留名与隐藏文件",
            )
        if len(content) > MAX_FILE_BYTES:
            raise ApiError(
                Code.SKILL_IMPORT_INVALID,
                message=f"单文件超过 2MB：{safe}",
                details={"path": safe, "size": len(content)},
            )
        total += len(content)
        if total > MAX_TOTAL_BYTES:
            raise ApiError(Code.SKILL_IMPORT_INVALID, message="导入包总大小超过 20MB")
        if skill_service.is_script(safe):
            requires_sandbox = True
        files.append(
            {
                "path": safe,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "type": "script" if skill_service.is_script(safe) else "text",
            }
        )
    if not any(f["path"].endswith("SKILL.md") for f in files):
        raise ApiError(
            Code.SKILL_IMPORT_INVALID,
            message="缺少 SKILL.md",
            hint="SKILL.md 必须位于导入包根目录（或唯一顶层目录下）",
        )
    if requires_sandbox:
        warnings.append(
            {
                "code": "SCRIPTS_PRESENT",
                "message": "包含脚本或安装钩子；本阶段不执行脚本，仅作静态展示",
            }
        )
    return {"files": files, "warnings": warnings, "requires_sandbox_review": requires_sandbox}


async def _from_url(url: str) -> tuple[list[tuple[str, bytes]], dict]:
    result = await fetch_text(url, purpose="skill_import")
    text = result.text
    meta, body = _parse_frontmatter(text)
    if not meta:
        meta = {"name": _guess_name(url), "description": body.strip().split("\n")[0][:160]}
        body = text
    entries = [("SKILL.md", text.encode())]
    return entries, {"source_url": result.url, "frontmatter": meta, "body": body, "truncated": result.truncated}


def _from_archive(data: bytes) -> list[tuple[str, bytes]]:
    entries: list[tuple[str, bytes]] = []
    if zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                if info.external_attr >> 16 & 0o170000 == 0o120000:
                    raise ApiError(Code.SKILL_IMPORT_INVALID, message=f"不允许符号链接：{info.filename}")
                entries.append((info.filename, zf.read(info)))
        return entries
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
            for member in tf.getmembers():
                if member.isdir():
                    continue
                if member.issym() or member.islnk():
                    raise ApiError(Code.SKILL_IMPORT_INVALID, message=f"不允许符号链接：{member.name}")
                handle = tf.extractfile(member)
                if handle is None:
                    continue
                entries.append((member.name, handle.read()))
        return entries
    except tarfile.TarError as exc:
        raise ApiError(
            Code.SKILL_IMPORT_INVALID,
            message="无法识别的压缩包格式（只支持 zip / tar）",
            details={"reason": str(exc)},
        ) from exc


def _guess_name(source: str) -> str:
    raw = source.rstrip("/").split("/")[-1] or "imported-skill"
    cleaned = re.sub(r"[^a-z0-9-]+", "-", raw.lower().replace("_", "-"))
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    if len(cleaned) < 2:
        cleaned = "imported-skill"
    return cleaned[:64]


async def preview(payload: dict) -> dict:
    kind = payload.get("kind") or "url"
    entries: list[tuple[str, bytes]] = []
    meta: dict = {}
    source = {}
    try:
        if kind == "url":
            if not payload.get("url"):
                raise ApiError(Code.VALIDATION_FAILED, "kind=url 时需要 url")
            entries, info = await _from_url(payload["url"])
            meta = info.get("frontmatter") or {}
            source = {"type": "url", "url": info["source_url"]}
        elif kind == "file":
            upload_id = payload.get("upload_id")
            if not upload_id:
                raise ApiError(Code.VALIDATION_FAILED, "kind=file 时需要 upload_id")
            from . import files as file_service

            data, record = file_service.read_bytes(upload_id)
            if record.mime in ("application/zip",) or record.filename and record.filename.endswith(
                (".zip", ".tar", ".tar.gz", ".tgz")
            ):
                entries = _from_archive(data)
            else:
                text = data.decode("utf-8", errors="replace")
                meta, _body = _parse_frontmatter(text)
                entries = [(record.filename or "SKILL.md", data)]
                if not (record.filename or "").endswith("SKILL.md"):
                    entries = [("SKILL.md", data)]
            source = {"type": "file", "upload_id": upload_id}
        elif kind == "git":
            if not payload.get("url"):
                raise ApiError(Code.VALIDATION_FAILED, "kind=git 时需要 url")
            raise ApiError(
                Code.SKILL_IMPORT_INVALID,
                message="Git 导入需要本机具备 git 且已配置出站策略",
                hint="在 SKILLROUTER_EGRESS_POLICY=allow 的环境下使用，或改用 zip/文件导入",
                details={"requested": payload["url"], "ref": payload.get("ref")},
            )
        else:
            raise ApiError(Code.VALIDATION_FAILED, f"不支持的导入类型：{kind}")
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ApiError(
            Code.SKILL_IMPORT_INVALID,
            message=f"资料读取失败：{redact(str(exc), max_len=200)}",
        ) from exc

    inspected = _inspect_tree(entries)
    skill_md = next(
        (content.decode("utf-8", errors="replace") for path, content in entries if path.endswith("SKILL.md")),
        "",
    )
    front_meta, body = _parse_frontmatter(skill_md)
    merged = {**front_meta, **meta}
    warnings = inspected["warnings"] + scan(skill_md, source=f"skill_import:{kind}")
    if merged.get("activation") == "always":
        warnings.append(
            {
                "code": "ACTIVATION_ALWAYS",
                "message": "导入内容请求 activation: always，已降级为 auto（未审查来源不允许常驻）",
            }
        )
    name = str(merged.get("name") or _guess_name(source.get("url") or "imported-skill"))
    if not re.match(r"^[a-z0-9][a-z0-9-]{1,63}$", name):
        name = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")[:64] or "imported-skill"
    description = str(merged.get("description") or body.strip().split("\n")[0][:200] or "")
    token = uuid_pk("svp")
    preview_payload = {
        "kind": kind,
        "skill_md": skill_md,
        "frontmatter": {**merged, "activation": "auto"},
        "body": body,
        "files": inspected["files"],
        "name": name,
        "description": description,
        "requires_sandbox_review": inspected["requires_sandbox_review"],
    }
    import_id = uuid_pk("ski")
    with db_write() as s:
        s.add(
            SkillImport(
                id=import_id,
                kind=kind,
                source_url=source.get("url"),
                source_ref=payload.get("ref"),
                preview_token=token,
                files=inspected["files"],
                warnings=warnings,
                requires_sandbox_review=1 if inspected["requires_sandbox_review"] else 0,
                status="previewed",
                created_at=now_iso(),
            )
        )
        s.flush()
    _STASH[token] = preview_payload
    dependencies = []
    for line in body.split("\n"):
        match = re.search(r"(?:required_capabilities|依赖能力)[:：]\s*(.+)", line)
        if match:
            dependencies = [
                {"capability_key": item.strip(), "version_range": "*"}
                for item in match.group(1).split(",")
                if item.strip()
            ]
    return {
        "preview_token": token,
        "import_id": import_id,
        "source": source,
        "ref": payload.get("ref"),
        "commit": None,
        "files": inspected["files"],
        "name": name,
        "description": description,
        "declared_permissions": {
            "allowed_tools": ["bash"],
            "needs_agent_reasoning": False,
            "network": True,
        },
        "dependencies": dependencies,
        "warnings": warnings,
        "requires_sandbox_review": inspected["requires_sandbox_review"],
        "note": "脚本不会被执行；activation 已降级为 auto",
    }


#: 预览内容暂存（进程内，10 分钟有效由前端重新预览兜底）
_STASH: dict[str, dict] = {}


def commit(payload: dict, *, actor: str | None = None) -> dict:
    token = payload["preview_token"]
    stashed = _STASH.get(token)
    if not stashed:
        raise ApiError(
            Code.NOT_FOUND,
            message="预览已过期，请重新执行导入预览",
            details={"preview_token": token},
        )
    name = payload.get("name") or stashed["name"]
    with db_session() as s:
        existing = s.scalar(select(Skill).where(Skill.name == name, Skill.deleted_at.is_(None)))
    if existing:
        skill_id = existing.id
        with db_write() as s:
            row = s.get(Skill, skill_id)
            definition = dict(row.draft_definition or {})
            definition["instructions"] = stashed["body"]
            definition["frontmatter"] = stashed["frontmatter"]
            definition["activation"] = "auto"
            row.draft_definition = definition
            row.description = stashed["description"]
            row.updated_at = now_iso()
    else:
        created = skill_service.create_skill(
            {
                "name": name,
                "title": payload.get("title") or stashed["frontmatter"].get("name") or name,
                "description": stashed["description"],
                "instructions": stashed["body"],
                "activation": "auto",
            },
            actor=actor,
        )
        skill_id = created["id"]
        with db_write() as s:
            row = s.get(Skill, skill_id)
            definition = dict(row.draft_definition or {})
            definition["frontmatter"] = stashed["frontmatter"]
            definition["source_files"] = stashed["files"]
            row.draft_definition = definition
            row.source_type = stashed["kind"]
            row.updated_at = now_iso()
    with db_write() as s:
        record = s.scalar(select(SkillImport).where(SkillImport.preview_token == token))
        if record:
            record.status = "committed"
            record.skill_id = skill_id
    audit.write(
        "skill.imported",
        object_type="skill",
        object_id=skill_id,
        summary={"kind": stashed["kind"], "files": len(stashed["files"])},
        actor_id=actor,
    )
    _STASH.pop(token, None)
    return skill_service.get_skill(skill_id)


__all__ = ["preview", "commit", "settings"]
