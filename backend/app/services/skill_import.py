"""Import a SKILL from a git repository, a web URL or an uploaded bundle.

Safety rules (they are the reason this module is not a convenience wrapper):

  * hard limits: at most 40 files, 1 MB in total, 256 KB per file, depth 12;
  * path traversal, absolute paths and symlinks are rejected, never written;
  * binary files are listed but not imported;
  * scripts and install hooks are listed as ``requires_review`` and are NEVER
    executed — the review payload says so explicitly;
  * git is cloned shallow into a temporary directory with a timeout and no
    hooks; web fetches go through ``network.guarded_request`` so the SSRF policy
    applies, and any additional fetch must stay on the same origin;
  * previews are persisted as ``skill_imports`` rows (status ``pending``) so a
    review survives a restart. Nothing is created until ``commit_import``.

Nothing here runs bundle content. The only process started is ``git`` itself.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import yaml

from app.core import db
from app.core.errors import ApiError, conflict, not_found, unprocessable
from app.security.redact import Redactor
from app.services import audit, files as file_service, network, skills

MAX_FILES = 40
MAX_TOTAL_BYTES = 1024 * 1024
MAX_FILE_BYTES = 256 * 1024
MAX_DEPTH = 12
MAX_RELATED_FETCHES = 3
GIT_TIMEOUT_S = 30
WEB_TIMEOUT_S = 20

_SCRIPT_SUFFIXES = {
    ".sh",
    ".bash",
    ".zsh",
    ".fish",
    ".ps1",
    ".psm1",
    ".bat",
    ".cmd",
    ".py",
    ".rb",
    ".pl",
    ".php",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
}
_INSTALL_HOOKS = {
    "install.sh",
    "install.ps1",
    "install.py",
    "setup.py",
    "setup.cfg",
    "pyproject.toml",
    "package.json",
    "makefile",
    "dockerfile",
    "postinstall.js",
    "preinstall.js",
}
_RELATIVE_REF = re.compile(r"^\.{0,2}/?[A-Za-z0-9._\-/]+\.(?:md|markdown|txt|json|yaml|yml)$")

Batch = tuple[
    list[dict[str, Any]],  # entries
    list[dict[str, str]],  # issues
    dict[str, Any],  # meta (truncated, total_bytes)
    dict[str, Any],  # resolved source facts
    dict[str, bytes],  # text contents keyed by path
]


# --------------------------------------------------------------------- helpers --
def _now() -> str:
    return db.iso()


def _issue(severity: str, field: str, message: str) -> dict[str, str]:
    return {"severity": severity, "field": field, "message": message}


def _is_binary(content: bytes) -> bool:
    if b"\x00" in content[:8192]:
        return True
    try:
        content[:8192].decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _classify(path: str, content: bytes) -> tuple[str, str | None]:
    """Return (kind, review_reason). kind ∈ text|binary|script|install_hook|link."""
    name = path.rsplit("/", 1)[-1].lower()
    suffix = ("." + name.rsplit(".", 1)[-1]) if "." in name else ""
    if name in _INSTALL_HOOKS:
        if name == "package.json":
            try:
                parsed = json.loads(content.decode("utf-8", errors="replace"))
            except ValueError:
                parsed = None
            if isinstance(parsed, dict) and parsed.get("scripts"):
                return "install_hook", "package.json declares lifecycle scripts; listed for review, never executed."
            return "text", None
        return "install_hook", "Install/build hook; listed for review and never executed."
    if suffix in _SCRIPT_SUFFIXES:
        return "script", "Script file; listed for review and never executed."
    if content.startswith(b"#!"):
        return "script", "Executable script (shebang); listed for review and never executed."
    if _is_binary(content):
        return "binary", "Binary file; not imported."
    return "text", None


def _unsafe_path(path: str) -> str | None:
    """Return a reason when the path must not be trusted."""
    cleaned = (path or "").replace("\\", "/")
    if not cleaned or cleaned in {".", "./"}:
        return "Empty path."
    if cleaned.startswith("/"):
        return "Absolute path."
    if re.match(r"^[A-Za-z]:", cleaned):
        return "Absolute path (drive letter)."
    if cleaned.startswith("~"):
        return "Home-relative path."
    parts = [p for p in cleaned.split("/") if p not in ("", ".")]
    if any(part == ".." for part in parts):
        return "Path traversal ('..')."
    if len(parts) > MAX_DEPTH:
        return f"Path deeper than {MAX_DEPTH} levels."
    return None


def _redact_text(value: str) -> str:
    return Redactor().text(value)


def _entry(path: str, content: bytes | None, *, size: int | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    if content is None:
        kind, reason = "link", "Symbolic link; not imported."
    else:
        kind, reason = _classify(path, content)
    item = {
        "path": path,
        "size": int(size if size is not None else len(content or b"")),
        "kind": kind,
        "requires_review": kind in {"script", "install_hook"},
        "review_reason": reason,
    }
    if extra:
        item.update(extra)
    return item


# ------------------------------------------------------------- frontmatter -----
def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse a leading YAML frontmatter block; returns (meta, body)."""
    stripped = text.lstrip("\ufeff")
    if not stripped.startswith("---"):
        return {}, stripped
    lines = stripped.splitlines()
    end = None
    for index in range(1, min(len(lines), 400)):
        if lines[index].strip() in {"---", "..."}:
            end = index
            break
    if end is None:
        return {}, stripped
    raw = "\n".join(lines[1:end])[:200_000]
    try:
        meta = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        return {}, stripped
    if not isinstance(meta, dict):
        return {}, stripped
    return {str(k): v for k, v in meta.items()}, "\n".join(lines[end + 1 :])


# --------------------------------------------------------------- collectors ---
def _collect_from_directory(root: Path) -> Batch:
    """Walk a cloned repository with hard limits. Never follows symlinks."""
    entries: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []
    contents: dict[str, bytes] = {}
    total = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        depth = 0 if rel_dir == "." else rel_dir.count("/") + 1
        if depth >= MAX_DEPTH:
            dirnames[:] = []
            continue
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for name in sorted(filenames):
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            full = Path(dirpath) / name
            reason = _unsafe_path(rel)
            if reason:
                issues.append(_issue("error", f"files[{len(entries)}]", f"Skipped {rel}: {reason}"))
                continue
            if full.is_symlink():
                entries.append(_entry(rel, None, extra={"skipped": "symlink"}))
                issues.append(
                    _issue("error", f"files[{len(entries) - 1}]", f"Skipped {rel}: symbolic links are not imported.")
                )
                continue
            if len(entries) >= MAX_FILES:
                truncated = True
                break
            try:
                size = full.stat().st_size
            except OSError:
                continue
            if size > MAX_FILE_BYTES:
                entries.append(_entry(rel, b"", size=size, extra={"skipped": "too_large"}))
                issues.append(
                    _issue("warning", f"files[{len(entries) - 1}]", f"Skipped {rel}: larger than {MAX_FILE_BYTES} bytes.")
                )
                continue
            if total + size > MAX_TOTAL_BYTES:
                truncated = True
                break
            try:
                content = full.read_bytes()
            except OSError:
                continue
            total += size
            entry = _entry(rel, content)
            entries.append(entry)
            if entry["kind"] == "text":
                contents[rel] = content
        if truncated:
            break
    if truncated:
        issues.append(
            _issue(
                "error",
                "files",
                f"The bundle exceeds the import limits ({MAX_FILES} files / {MAX_TOTAL_BYTES} bytes); "
                "only the first part was reviewed.",
            )
        )
    return entries, issues, {"truncated": truncated, "total_bytes": total}, {}, contents


def _read_tar_member(archive: tarfile.TarFile, member: tarfile.TarInfo) -> bytes | None:
    handle = archive.extractfile(member)
    return handle.read() if handle else None


def _collect_from_archive(content: bytes) -> Batch:
    entries: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []
    contents: dict[str, bytes] = {}
    state = {"total": 0, "truncated": False}

    def take(name: str, reader) -> None:  # type: ignore[no-untyped-def]
        reason = _unsafe_path(name)
        if reason:
            issues.append(_issue("error", f"files[{len(entries)}]", f"Skipped {name}: {reason}"))
            return
        if len(entries) >= MAX_FILES or state["truncated"]:
            state["truncated"] = True
            return
        payload = reader()
        if payload is None:
            issues.append(_issue("error", f"files[{len(entries)}]", f"Skipped {name}: unreadable archive member."))
            return
        if len(payload) > MAX_FILE_BYTES:
            entries.append(_entry(name, b"", size=len(payload), extra={"skipped": "too_large"}))
            issues.append(
                _issue("warning", f"files[{len(entries) - 1}]", f"Skipped {name}: larger than {MAX_FILE_BYTES} bytes.")
            )
            return
        if state["total"] + len(payload) > MAX_TOTAL_BYTES:
            state["truncated"] = True
            return
        state["total"] += len(payload)
        entry = _entry(name, payload)
        entries.append(entry)
        if entry["kind"] == "text":
            contents[name] = payload

    if zipfile.is_zipfile(io.BytesIO(content)):
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                take(info.filename, lambda info=info: archive.read(info))
    else:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:*") as archive:
            for member in archive.getmembers():
                if member.isdir():
                    continue
                if member.issym() or member.islnk():
                    entries.append(_entry(member.name, None, extra={"skipped": "link"}))
                    issues.append(
                        _issue(
                            "error",
                            f"files[{len(entries) - 1}]",
                            f"Skipped {member.name}: links are not imported.",
                        )
                    )
                    continue
                if not member.isfile():
                    continue
                take(member.name, lambda member=member: _read_tar_member(archive, member))
    if state["truncated"]:
        issues.append(
            _issue(
                "error",
                "files",
                f"The bundle exceeds the import limits ({MAX_FILES} files / {MAX_TOTAL_BYTES} bytes).",
            )
        )
    return (
        entries,
        issues,
        {"truncated": state["truncated"], "total_bytes": state["total"]},
        {},
        contents,
    )


async def _collect_git(uri: str) -> Batch:
    try:
        network.validate_url(uri)
    except network.NetworkPolicyError as exc:
        raise ApiError(
            "network_blocked",
            f"The repository URL is not allowed: {exc}",
            status_code=403,
            actions=["Use a public https git URL."],
        ) from exc
    temp_dir = Path(tempfile.mkdtemp(prefix="sr-skill-import-"))
    target = temp_dir / "repo"
    try:
        command = [
            "git",
            "clone",
            "--depth",
            "1",
            "--filter=blob:none",
            "--no-tags",
            "--quiet",
            uri,
            str(target),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=GIT_TIMEOUT_S,
                check=False,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        except FileNotFoundError as exc:
            raise ApiError(
                "git_unavailable",
                "git is not installed on the server, so git imports are unavailable.",
                status_code=503,
                actions=["Use a web URL or upload a bundle instead."],
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ApiError(
                "import_timeout",
                f"Cloning took longer than {GIT_TIMEOUT_S}s and was stopped.",
                status_code=504,
                actions=["Try a smaller repository.", "Or upload a bundle instead."],
            ) from exc
        if completed.returncode != 0:
            detail = _redact_text((completed.stderr or b"").decode("utf-8", errors="replace"))[:400]
            raise ApiError(
                "git_clone_failed",
                "The repository could not be cloned.",
                status_code=422,
                details={"stderr": detail},
                actions=["Check the URL and that the repository is public."],
            )
        commit = ""
        try:
            commit = (
                subprocess.run(
                    ["git", "-C", str(target), "rev-parse", "HEAD"],
                    capture_output=True,
                    timeout=GIT_TIMEOUT_S,
                    check=False,
                )
                .stdout.decode("utf-8", errors="replace")
                .strip()
            )
        except (OSError, subprocess.SubprocessError):
            commit = ""
        entries, issues, meta, _unused, contents = _collect_from_directory(target)
        return entries, issues, meta, {"type": "git", "commit": commit or None}, contents
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _same_origin(base: str, candidate: str) -> str | None:
    target = urljoin(base, candidate)
    base_parts = urlparse(base)
    target_parts = urlparse(target)
    if (target_parts.scheme, target_parts.netloc) != (base_parts.scheme, base_parts.netloc):
        return None
    return target


async def _collect_web(uri: str) -> Batch:
    try:
        response = await network.guarded_request("GET", uri, timeout_s=WEB_TIMEOUT_S, max_bytes=MAX_TOTAL_BYTES)
    except network.NetworkPolicyError as exc:
        raise ApiError(
            "network_blocked",
            f"The URL is not allowed: {exc}",
            status_code=403,
            actions=["Use a public https URL."],
        ) from exc
    if response.status_code >= 400:
        raise ApiError(
            "import_fetch_failed",
            f"The URL answered {response.status_code}.",
            status_code=422,
            details={"status": response.status_code, "url": response.url},
            actions=["Check the URL is publicly readable."],
        )
    content = response.content
    if len(content) > MAX_TOTAL_BYTES:
        raise unprocessable(
            "bundle_too_large",
            f"The downloaded bundle is larger than {MAX_TOTAL_BYTES} bytes.",
        )
    resolved = {
        "type": "web",
        "etag": response.headers.get("etag"),
        "content_type": response.content_type,
        "url": response.url,
        "bytes": len(content),
    }
    if zipfile.is_zipfile(io.BytesIO(content)) or content[:2] == b"\x1f\x8b":
        entries, issues, meta, _unused, contents = _collect_from_archive(content)
        return entries, issues, meta, resolved, contents

    name = (urlparse(response.url).path.rsplit("/", 1)[-1] or "SKILL.md").strip() or "SKILL.md"
    entry = _entry(name, content)
    entries = [entry]
    contents = {name: content} if entry["kind"] == "text" else {}
    issues: list[dict[str, str]] = []
    meta = {"truncated": False, "total_bytes": len(content)}

    frontmatter, _body = split_frontmatter(content.decode("utf-8", errors="replace"))
    refs: list[str] = []
    for key in ("sources", "references", "files", "examples", "related"):
        value = frontmatter.get(key)
        if isinstance(value, list):
            refs.extend(str(item) for item in value if isinstance(item, str))
    fetched = 0
    for ref in refs:
        if fetched >= MAX_RELATED_FETCHES:
            issues.append(_issue("warning", "files", "Only the first 3 same-origin references were fetched."))
            break
        candidate = ref.strip()
        if not _RELATIVE_REF.match(candidate):
            continue
        target = _same_origin(response.url, candidate)
        if target is None:
            issues.append(
                _issue("warning", "files", f"Reference {candidate!r} points outside the source origin and was not fetched.")
            )
            continue
        try:
            related = await network.guarded_request("GET", target, timeout_s=WEB_TIMEOUT_S, max_bytes=MAX_FILE_BYTES)
        except network.NetworkPolicyError as exc:
            issues.append(_issue("warning", "files", f"Reference {candidate!r} was blocked: {exc}"))
            continue
        if related.status_code >= 400 or _is_binary(related.content):
            continue
        if meta["total_bytes"] + len(related.content) > MAX_TOTAL_BYTES or len(entries) >= MAX_FILES:
            break
        rel_path = candidate.lstrip("./")
        entry = _entry(rel_path, related.content)
        entries.append(entry)
        meta["total_bytes"] += len(related.content)
        if entry["kind"] == "text":
            contents[rel_path] = related.content
        fetched += 1
    return entries, issues, meta, resolved, contents


def _collect_file(file_id: str) -> Batch:
    row, content = file_service.read_bytes(file_id)
    if len(content) > MAX_TOTAL_BYTES:
        raise unprocessable(
            "bundle_too_large",
            f"The uploaded bundle is {len(content)} bytes; the limit is {MAX_TOTAL_BYTES} bytes.",
            details={"size": len(content), "limit": MAX_TOTAL_BYTES},
        )
    resolved = {
        "type": "file",
        "file_id": row["id"],
        "filename": row["filename"],
        "sha256": row["sha256"],
        "content_type": row["content_type"],
        "bytes": len(content),
    }
    if zipfile.is_zipfile(io.BytesIO(content)) or content[:2] == b"\x1f\x8b":
        entries, issues, meta, _unused, contents = _collect_from_archive(content)
        return entries, issues, meta, resolved, contents
    name = row["filename"] or "SKILL.md"
    entry = _entry(name, content)
    contents = {name: content} if entry["kind"] == "text" else {}
    return [entry], [], {"truncated": False, "total_bytes": len(content)}, resolved, contents


# ------------------------------------------------------------------ staging ---
def _normalise_permissions(raw: Any) -> dict[str, list[str]]:
    permissions: dict[str, list[str]] = {"network": [], "files": [], "credentials": []}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, list):
                permissions[str(key)] = [str(item)[:200] for item in value]
            elif value not in (None, ""):
                permissions[str(key)] = [str(value)[:200]]
    elif isinstance(raw, list):
        permissions["files"] = [str(item)[:200] for item in raw]
    return permissions


def _permission_list(raw: Any) -> list[str]:
    permissions = _normalise_permissions(raw)
    return sorted({f"{key}:{item}" for key, values in permissions.items() for item in values})


def _pick_skill_document(entries: list[dict[str, Any]], contents: dict[str, bytes]) -> str | None:
    candidates = [
        entry["path"]
        for entry in entries
        if entry["kind"] == "text" and entry["path"].rsplit("/", 1)[-1].lower() == "skill.md"
    ]
    if candidates:
        return sorted(candidates, key=lambda p: (p.count("/"), len(p)))[0]
    for entry in entries:
        if entry["kind"] != "text" or not entry["path"].lower().endswith((".md", ".markdown")):
            continue
        meta, _ = split_frontmatter(contents.get(entry["path"], b"").decode("utf-8", errors="replace"))
        if "plan" in meta or "dependencies" in meta:
            return entry["path"]
    return None


def _build_manifest(
    *,
    frontmatter: dict[str, Any],
    instructions: str,
    fallback_name: str,
    source_type: str,
    source_uri: str,
    resolved: dict[str, Any],
) -> dict[str, Any]:
    raw_plan = frontmatter.get("plan")
    plan = raw_plan if isinstance(raw_plan, list) else []
    dependencies = frontmatter.get("dependencies")
    if not isinstance(dependencies, list):
        dependencies = []
        for step in plan:
            if isinstance(step, dict) and step.get("kind") == "capability" and step.get("capability"):
                entry: dict[str, Any] = {"capability": step["capability"], "required": True}
                if step.get("version") not in (None, ""):
                    entry["version"] = step["version"]
                dependencies.append(entry)
    name = str(frontmatter.get("name") or fallback_name or "Imported SKILL").strip()[:160]
    description = str(frontmatter.get("description") or "").strip()
    if not description:
        for line in instructions.splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                description = stripped[:280]
                break
    manifest: dict[str, Any] = {
        "name": name,
        "description": description,
        "instructions": instructions,
        "plan": plan,
        "when_to_use": str(frontmatter.get("when_to_use") or ""),
        "inputs": frontmatter.get("inputs") if isinstance(frontmatter.get("inputs"), dict) else {"type": "object", "properties": {}},
        "outputs": frontmatter.get("outputs") if isinstance(frontmatter.get("outputs"), dict) else {"type": "object", "properties": {}},
        "dependencies": dependencies,
        "permissions": _normalise_permissions(frontmatter.get("permissions")),
        "examples": frontmatter.get("examples") if isinstance(frontmatter.get("examples"), list) else [],
        "sources": [
            {
                "type": source_type,
                "uri": source_uri,
                **({"commit": resolved["commit"]} if resolved.get("commit") else {}),
            }
        ],
        "imported": True,
    }
    for extra in ("category", "tags", "license", "author"):
        if frontmatter.get(extra) is not None:
            manifest[extra] = frontmatter.get(extra)
    return manifest


def _stage(
    *,
    source_type: str,
    source_uri: str,
    resolved: dict[str, Any],
    entries: list[dict[str, Any]],
    source_issues: list[dict[str, str]],
    meta: dict[str, Any],
    contents: dict[str, bytes],
) -> dict[str, Any]:
    issues = list(source_issues)
    detected = _pick_skill_document(entries, contents)
    fallback_name = source_uri.rstrip("/").rsplit("/", 1)[-1].replace(".git", "") or "Imported SKILL"
    if detected:
        text = contents.get(detected, b"").decode("utf-8", errors="replace")
        frontmatter, body = split_frontmatter(text)
    else:
        frontmatter, body = {}, ""
        issues.append(
            _issue(
                "warning",
                "files",
                "No SKILL.md was found; the bundle is imported as a draft without instructions.",
            )
        )
    instructions = _redact_text(body.strip())[:20000]
    manifest = _build_manifest(
        frontmatter=frontmatter,
        instructions=instructions,
        fallback_name=fallback_name,
        source_type=source_type,
        source_uri=source_uri,
        resolved=resolved,
    )
    manifest_validation = skills.validate_manifest(manifest)
    issues.extend(manifest_validation["issues"])

    permissions = _permission_list(manifest.get("permissions"))
    review = [entry for entry in entries if entry["requires_review"]]
    if review:
        issues.append(
            _issue(
                "warning",
                "files",
                f"{len(review)} script/install-hook file(s) need review before use; they were not executed.",
            )
        )
    blocking = any(issue["severity"] == "error" for issue in issues)
    return {
        "status": "pending",
        "source_type": source_type,
        "source_uri": source_uri,
        "resolved": resolved,
        "limits": {
            "max_files": MAX_FILES,
            "max_total_bytes": MAX_TOTAL_BYTES,
            "max_file_bytes": MAX_FILE_BYTES,
            "file_count": len(entries),
            "total_bytes": int(meta.get("total_bytes") or 0),
            "truncated": bool(meta.get("truncated")),
        },
        "files": entries,
        "requires_review": [
            {"path": item["path"], "kind": item["kind"], "reason": item["review_reason"]} for item in review
        ],
        "scripts_executed": False,
        "scripts_policy": (
            "Scripts and install hooks found in an imported bundle are listed for review and are "
            "never executed by Skill Router."
        ),
        "detected_skill_document": detected,
        "frontmatter_keys": sorted(frontmatter.keys()),
        "dependencies": manifest.get("dependencies") or [],
        "permissions": permissions,
        "requires_permissions_confirmation": bool(permissions),
        "proposed_manifest": manifest,
        "validation": {"issues": issues, "blocking": blocking},
        "notes": [
            "Review the file list and permissions before importing.",
            "Importing creates a draft version; nothing is published automatically.",
        ],
    }


# -------------------------------------------------------------------- public --
async def preview_import(
    *,
    source_type: str,
    uri: str | None = None,
    file_id: str | None = None,
    actor_id: str | None = None,
    ip: str | None = None,
) -> dict[str, Any]:
    """Stage an import for review. Nothing is created outside the review row."""
    source_type = (source_type or "").strip().lower()
    if source_type not in {"git", "web", "file"}:
        raise unprocessable(
            "validation_failed",
            f"Unknown import source type {source_type!r}.",
            details=[{"field": "source_type", "message": "Use git, web or file."}],
        )
    if source_type in {"git", "web"}:
        if not uri or not str(uri).strip():
            raise unprocessable(
                "validation_failed",
                "A URI is required for git and web imports.",
                details=[{"field": "uri", "message": "Required."}],
            )
        source_uri = str(uri).strip()
        if source_type == "git":
            entries, issues, meta, resolved, contents = await _collect_git(source_uri)
        else:
            entries, issues, meta, resolved, contents = await _collect_web(source_uri)
    else:
        if not file_id:
            raise unprocessable(
                "validation_failed",
                "A file_id is required for file imports.",
                details=[{"field": "file_id", "message": "Upload the bundle first."}],
            )
        entries, issues, meta, resolved, contents = _collect_file(str(file_id))
        source_uri = f"file:{file_id}"

    staged = _stage(
        source_type=source_type,
        source_uri=source_uri,
        resolved=resolved,
        entries=entries,
        source_issues=issues,
        meta=meta,
        contents=contents,
    )

    import_id = db.new_id("imp")
    now = _now()
    log = [{"stage": "preview", "at": now, "actor_id": actor_id, "payload": staged}]
    db.insert(
        "skill_imports",
        {
            "id": import_id,
            "skill_id": None,
            "source_type": source_type,
            "source_uri": source_uri[:1000],
            "resolved_commit": resolved.get("commit") or resolved.get("etag"),
            "file_list": db.dumps(staged["files"]),
            "dependencies": db.dumps(staged["dependencies"]),
            "permissions": db.dumps(staged["permissions"]),
            "status": "pending",
            "log": db.dumps(log),
            "created_at": now,
            "finished_at": None,
        },
    )
    audit.record(
        actor_type="user",
        action="skill.import_preview",
        object_type="skill_import",
        object_id=import_id,
        actor_id=actor_id,
        summary=f"staged {source_type} import review",
        metadata={
            "source_uri": source_uri,
            "files": len(staged["files"]),
            "requires_review": len(staged["requires_review"]),
            "blocking": staged["validation"]["blocking"],
        },
        ip=ip,
    )
    payload = dict(staged)
    payload["preview_id"] = import_id
    return payload


def get_import(preview_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM skill_imports WHERE id = ?", (preview_id,))
    if not row:
        raise not_found("Import preview")
    return row


def _staged_payload(row: dict[str, Any]) -> dict[str, Any]:
    log = db.loads(row.get("log"), [])
    if isinstance(log, dict):
        log = [log]
    for entry in reversed(log if isinstance(log, list) else []):
        if isinstance(entry, dict) and entry.get("stage") == "preview":
            payload = entry.get("payload")
            if isinstance(payload, dict):
                return payload
    return {}


def serialise_import(row: dict[str, Any], *, include_payload: bool = False) -> dict[str, Any]:
    payload = _staged_payload(row)
    file_list = db.loads(row["file_list"], []) or []
    permissions = db.loads(row["permissions"], []) or []
    out = {
        "id": row["id"],
        "skill_id": row["skill_id"],
        "source_type": row["source_type"],
        "source_uri": row["source_uri"],
        "resolved_commit": row["resolved_commit"],
        "status": row["status"],
        "files": file_list,
        "file_count": len(file_list),
        "dependencies": db.loads(row["dependencies"], []) or [],
        "permissions": permissions,
        "requires_review": payload.get("requires_review", []),
        "scripts_executed": False,
        "detected_skill_document": payload.get("detected_skill_document"),
        "validation": payload.get("validation", {"issues": [], "blocking": False}),
        "requires_permissions_confirmation": bool(permissions),
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
    }
    if include_payload:
        out["staged"] = payload
    return out


def list_imports(*, status: str | None = None, limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    where = ["1 = 1"]
    params: list[Any] = []
    if status:
        where.append("status = ?")
        params.append(status)
    clause = " AND ".join(where)
    total = db.query_scalar(f"SELECT COUNT(*) FROM skill_imports WHERE {clause}", params) or 0
    rows = db.query_all(
        f"SELECT * FROM skill_imports WHERE {clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    return rows, int(total)


def commit_import(
    preview_id: str,
    confirm_permissions: bool = False,
    *,
    actor_id: str | None = None,
    ip: str | None = None,
) -> dict[str, Any]:
    row = get_import(preview_id)
    if row["status"] != "pending":
        raise conflict(
            "import_not_pending",
            f"This import is already {row['status']}.",
            details={"status": row["status"], "skill_id": row["skill_id"]},
            actions=["Start a new preview to import again."],
        )
    staged = _staged_payload(row)
    permissions = db.loads(row["permissions"], []) or []
    if permissions and not confirm_permissions:
        raise conflict(
            "permissions_not_confirmed",
            "This bundle requests permissions; confirm them before importing.",
            details={
                "permissions": permissions,
                "requires_review": staged.get("requires_review", []),
                "scripts_executed": False,
            },
            actions=[
                "Review the requested permissions and the flagged files.",
                "Repeat the import with confirm_permissions: true.",
            ],
        )
    validation = staged.get("validation") or {}
    if validation.get("blocking"):
        raise unprocessable(
            "validation_failed",
            "The staged import has blocking issues and cannot be committed.",
            details={"issues": validation.get("issues", []), "blocking": True},
            actions=["Fix the bundle and preview it again."],
        )
    manifest = staged.get("proposed_manifest") or {}
    if not isinstance(manifest, dict) or not manifest:
        raise unprocessable("validation_failed", "The staged preview has no manifest to import.")
    sources = manifest.get("sources")
    if isinstance(sources, list) and sources and isinstance(sources[0], dict):
        sources[0]["uri"] = row["source_uri"]
        if row["resolved_commit"]:
            sources[0].setdefault("commit", row["resolved_commit"])
    skill = skills.create_skill(
        name=str(manifest.get("name") or "Imported SKILL"),
        description=str(manifest.get("description") or ""),
        category=str(manifest.get("category") or "").strip() or "imported",
        source_type=row["source_type"],
        source_ref=row["source_uri"],
        manifest=manifest,
        actor_id=actor_id,
        ip=ip,
    )
    now = _now()
    log = db.loads(row["log"], [])
    if not isinstance(log, list):
        log = []
    log.append(
        {
            "stage": "commit",
            "at": now,
            "actor_id": actor_id,
            "skill_id": skill["id"],
            "confirm_permissions": bool(confirm_permissions),
            "permissions": permissions,
            "requires_review": [item.get("path") for item in staged.get("requires_review", [])],
            "scripts_executed": False,
        }
    )
    db.update(
        "skill_imports",
        row["id"],
        {"skill_id": skill["id"], "status": "imported", "finished_at": now, "log": db.dumps(log)},
    )
    audit.record(
        actor_type="user",
        action="skill.import",
        object_type="skill_import",
        object_id=row["id"],
        actor_id=actor_id,
        summary=f"imported {row['source_type']} bundle as a draft SKILL",
        metadata={
            "skill_id": skill["id"],
            "slug": skill["slug"],
            "permissions": permissions,
            "files": len(db.loads(row["file_list"], []) or []),
            "scripts_executed": False,
        },
        ip=ip,
    )
    return {
        "import": serialise_import(get_import(row["id"])),
        "skill": skills.serialise(skill["id"]),
        "scripts_executed": False,
    }
