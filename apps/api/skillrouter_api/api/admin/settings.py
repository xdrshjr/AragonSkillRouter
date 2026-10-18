"""设置、主密钥轮换、审计查询、备份触发、文件下载（§9.2）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile

from ...crypto.keyring import rotation_status, rotate_master_key
from ...deps import page_params, require_password_changed
from ...errors import ApiError, Code
from ...schemas.engine import SettingsUpdate
from ...security.sessions import SessionContext
from ...services import audit, backup_job, files, tasks
from ...services import settings as settings_service
from ...upgrade import detect

router = APIRouter(tags=["settings"])


@router.get("/settings")
async def get_settings(ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return settings_service.get_all()


@router.patch("/settings")
async def patch_settings(payload: SettingsUpdate, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    data = payload.model_dump(exclude_none=True)
    if "fetch_allowlist" in data and isinstance(data["fetch_allowlist"], list):
        data["fetch_allowlist"] = ",".join(data["fetch_allowlist"])
    result = settings_service.update(data, actor=ctx.admin_id)
    audit.write("settings.updated", object_type="settings", summary={"keys": list(data)}, actor_id=ctx.admin_id)
    return result


@router.post("/settings/rotate-master-key")
async def rotate(payload: dict | None = None, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    result = rotate_master_key(triggered_by=ctx.admin_id)
    audit.write("master_key.rotated", object_type="master_key", summary=result, actor_id=ctx.admin_id)
    return {"status": result["status"], **(rotation_status(result["rotation_id"]))}


@router.get("/settings/rotate-master-key/{rotation_id}")
async def rotate_status(rotation_id: str, ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return rotation_status(rotation_id)


@router.get("/settings/audit")
async def list_audit(
    action: str | None = None,
    actor: str | None = None,
    object_type: str | None = None,
    object_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    paging: dict = Depends(page_params),
    ctx: SessionContext = Depends(require_password_changed),
) -> dict:
    return audit.list_logs(
        action=action,
        actor=actor,
        object_type=object_type,
        object_id=object_id,
        date_from=date_from,
        date_to=date_to,
        q=paging["q"],
        page=paging["page"],
        page_size=paging["page_size"],
    )


@router.get("/settings/audit/actions")
async def audit_actions(ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return {"items": audit.AUDIT_ACTIONS}


@router.post("/settings/backup", status_code=202)
async def trigger_backup(ctx: SessionContext = Depends(require_password_changed)) -> dict:
    queue_id = tasks.enqueue("backup", {"quick": False}, priority=8, max_attempts=1)
    return {"queued": True, "queue_id": queue_id, "note": "备份在 worker 中执行，完成后写入 file_objects 可下载"}


@router.get("/settings/backup/now")
async def backup_now(ctx: SessionContext = Depends(require_password_changed)) -> dict:
    result = backup_job.build_backup(quick=False)
    audit.write(
        "backup.created",
        object_type="backup",
        object_id=result["file_id"],
        summary={"sha256": result["sha256"]},
        actor_id=ctx.admin_id,
    )
    return {
        "file_id": result["file_id"],
        "sha256": result["sha256"],
        "path": result["file"],
        "manifest": result["manifest"],
        "download_url": f"/api/admin/files/{result['file_id']}",
    }


@router.get("/settings/egress")
async def egress(ctx: SessionContext = Depends(require_password_changed)) -> dict:
    return {
        "egress_policy": detect.settings_note()["egress_policy"],
        "engine_channel": detect.settings_note()["engine_channel"],
        "note": "egress=deny 时无法自动检测上游版本，只能手动上传 tarball 升级",
    }


@router.post("/files", status_code=201)
async def upload_file(
    file: UploadFile = File(...), ctx: SessionContext = Depends(require_password_changed)
) -> dict:
    """管理端附件上传（会话 + CSRF）。

    Agent 面板的拖拽/粘贴附件走这条路由：控制台只有会话 Cookie，没有平台令牌，
    因此不能复用 `/api/v1/files`（那条要求 `Authorization: Bearer srk_...`）。
    """
    data = await file.read()
    saved = files.save_bytes(
        data,
        mime=file.content_type,
        filename=file.filename,
        kind="upload",
        owner_type="admin",
        owner_id=ctx.admin_id,
    )
    return {**saved, "download_url": f"/api/admin/files/{saved['file_id']}"}


@router.get("/files/{file_id}")
async def download_file(file_id: str, ctx: SessionContext = Depends(require_password_changed)) -> Response:
    data, record = files.read_bytes(file_id)
    return Response(
        content=data,
        media_type=record.mime or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{record.filename or record.id}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
