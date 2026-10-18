"""
主密钥轮换：分批重加密（§7.4）。

不变式：轮换完成后，**所有未删除的凭证密文都必须使用当前主密钥的 key_id**。
因此按主键 keyset 分页扫全表（`id > last`），并在收尾时复查一遍残留行 ——
`created_at` 是秒级精度，用它做 `LIMIT/OFFSET` 分页会在并列时间戳上跳行。
"""

from __future__ import annotations

from sqlalchemy import select

from ..db import db_session, db_write, now_iso, uuid_pk
from ..logging import get_logger, redact
from ..models import Credential, MasterKeyRotation
from . import secretbox

log = get_logger("skillrouter.run")
BATCH = 50


def _rotate_credentials(rotation_id: str | None = None) -> int:
    """把未删除的凭证密文分批重加密到当前主密钥，返回实际重加密的行数。"""
    done = 0
    last = ""
    while True:
        with db_write() as s:
            rows = list(
                s.scalars(
                    select(Credential)
                    .where(Credential.deleted_at.is_(None), Credential.id > last)
                    .order_by(Credential.id)
                    .limit(BATCH)
                )
            )
            if not rows:
                return done
            last = rows[-1].id
            for row in rows:
                if row.secret_ciphertext.split(".")[1] == _key_id():
                    continue
                row.secret_ciphertext = secretbox.rotate(
                    row.secret_ciphertext, record_type="credential", record_id=row.id
                )
                done += 1
            if rotation_id:
                rot = s.get(MasterKeyRotation, rotation_id)
                if rot:
                    rot.rows_done = done


def _count_not_rotated() -> int:
    """复查：仍带旧 key_id 的凭证行数（轮换收尾的自检，必须为 0）。"""
    prefix = f"{secretbox.VERSION}.{_key_id()}."
    with db_session() as s:
        rows = s.scalars(
            select(Credential.secret_ciphertext).where(Credential.deleted_at.is_(None))
        )
        return sum(1 for ciphertext in rows if not ciphertext.startswith(prefix))


def _key_id() -> str:
    from ..settings import settings

    return settings.master_key_id


def rotate_master_key(*, triggered_by: str | None = None) -> dict:
    """全表重加密：credentials / provider 敏感头 / Agent 模型密钥。

    失败可回滚：旧密钥仍在 SKILLROUTER_MASTER_KEY_PREVIOUS 中可用。
    """
    from .. import models  # noqa: F401, PLC0415  确保全部表注册

    rotation_id = uuid_pk("mkr")
    with db_write() as s:
        total = len(list(s.scalars(select(Credential).where(Credential.deleted_at.is_(None)))))
        s.add(
            MasterKeyRotation(
                id=rotation_id,
                from_key_id="previous",
                to_key_id=_key_id(),
                rows_total=total,
                rows_done=0,
                status="running",
                started_at=now_iso(),
            )
        )
    try:
        done = _rotate_credentials(rotation_id)
        # provider 的敏感头与 Agent 会话中的模型密钥（存 settings 表）
        with db_write() as s:
            from ..models import Setting  # noqa: PLC0415

            setting = s.get(Setting, "agent_model")
            if setting and isinstance(setting.value, dict):
                sealed = setting.value.get("api_key_ciphertext")
                if sealed:
                    setting.value = {
                        **setting.value,
                        "api_key_ciphertext": secretbox.rotate(
                            sealed, record_type="setting", record_id="agent_model"
                        ),
                    }
                    setting.updated_by = triggered_by
                    setting.updated_at = now_iso()
        remaining = _count_not_rotated()
        if remaining:
            # 不能带着残留行宣布成功：一旦旧密钥被移除，这些凭证就永久打不开
            raise RuntimeError(
                f"[FATAL] MASTER_KEY_ROTATION_INCOMPLETE 仍有 {remaining} 条凭证未使用当前主密钥"
            )
        with db_write() as s:
            rot = s.get(MasterKeyRotation, rotation_id)
            if rot:
                rot.status = "done"
                rot.rows_done = done
                rot.finished_at = now_iso()
        log.info("master_key_rotated", extra={"object": rotation_id, "detail": redact({"rows": done})})
        return {"rotation_id": rotation_id, "rows": done, "status": "done"}
    except Exception as exc:  # noqa: BLE001
        with db_write() as s:
            rot = s.get(MasterKeyRotation, rotation_id)
            if rot:
                rot.status = "failed"
                rot.error = redact(str(exc))
                rot.finished_at = now_iso()
        log.error("master_key_rotation_failed", extra={"object": rotation_id, "detail": str(exc)})
        raise


def rotation_status(rotation_id: str) -> dict:
    from ..db import db_session  # noqa: PLC0415

    with db_session() as s:
        rot = s.get(MasterKeyRotation, rotation_id)
        if not rot:
            return {"status": "not_found"}
        return {
            "rotation_id": rot.id,
            "status": rot.status,
            "rows_total": rot.rows_total,
            "rows_done": rot.rows_done,
            "started_at": rot.started_at,
            "finished_at": rot.finished_at,
        }


__all__ = ["rotate_master_key", "rotation_status", "_rotate_credentials"]
