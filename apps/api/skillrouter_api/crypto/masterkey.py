"""主密钥加载/生成/指纹（§7.4）。"""

from __future__ import annotations

import base64
import hashlib
import os
from functools import lru_cache
from pathlib import Path

from ..settings import settings


def _decode(value: str, path_hint: str) -> bytes:
    value = value.strip()
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"[FATAL] MASTER_KEY_INVALID {path_hint} 不是合法 base64：{exc}") from exc
    if len(raw) != 32:
        raise RuntimeError(
            f"[FATAL] MASTER_KEY_INVALID {path_hint} 长度必须为 32 字节（当前 {len(raw)}）"
        )
    return raw


def load_key(value: str | None, filename: str) -> bytes | None:
    if value:
        return _decode(value, "SKILLROUTER_MASTER_KEY")
    path = settings.data_path / filename
    if path.exists():
        return _decode(path.read_text(encoding="utf-8"), str(path))
    return None


@lru_cache(maxsize=1)
def master_key() -> bytes:
    key = load_key(settings.master_key, "credentials-master.key")
    if key is None:
        raise RuntimeError("[FATAL] MASTER_KEY_MISSING 主密钥缺失，无法解密凭证")
    return key


@lru_cache(maxsize=1)
def previous_key() -> bytes | None:
    return load_key(settings.master_key_previous, "credentials-master.previous.key")


@lru_cache(maxsize=1)
def file_signing_key() -> bytes:
    key = load_key(settings.file_signing_secret, "file-signing.key")
    if key is None:
        raise RuntimeError("[FATAL] FILE_SIGNING_SECRET_MISSING 文件签名密钥缺失")
    return key


def key_id() -> str:
    return settings.master_key_id


def fingerprint(key: bytes | None = None) -> str:
    return hashlib.sha256(key or master_key()).hexdigest()[:16]


def write_key_file(path: Path, key: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(base64.urlsafe_b64encode(key).decode() + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows
        pass
