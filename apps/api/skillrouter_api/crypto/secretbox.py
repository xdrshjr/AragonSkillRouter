"""AES-256-GCM 信封加密（§7.4）。

密文格式：`v1.<key_id>.<b64url(nonce)>.<b64url(ciphertext)>`，AAD = `f"{record_type}:{record_id}:{key_id}"`。
"""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..errors import ApiError, Code
from . import masterkey

VERSION = "v1"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _aad(record_type: str, record_id: str, key_id: str) -> bytes:
    return f"{record_type}:{record_id}:{key_id}".encode()


def seal(plaintext: str, *, record_type: str, record_id: str, key: bytes | None = None) -> str:
    key = key or masterkey.master_key()
    key_id = masterkey.key_id() if key is masterkey.master_key() else masterkey.key_id()
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode(), _aad(record_type, record_id, key_id))
    return f"{VERSION}.{key_id}.{_b64(nonce)}.{_b64(ct)}"


def open_sealed(ciphertext: str, *, record_type: str, record_id: str) -> str:
    """用当前密钥解封；失败则尝试上一把密钥（轮换期双读）。"""
    parts = ciphertext.split(".")
    if len(parts) != 4 or parts[0] != VERSION:
        raise ApiError(Code.INTERNAL, "凭证密文格式非法")
    _, key_id, nonce_b64, ct_b64 = parts
    nonce, ct = _unb64(nonce_b64), _unb64(ct_b64)
    candidates: list[bytes] = []
    for key in (masterkey.master_key(), masterkey.previous_key()):
        if key:
            candidates.append(key)
    last: Exception | None = None
    for key in candidates:
        try:
            plain = AESGCM(key).decrypt(nonce, ct, _aad(record_type, record_id, key_id))
            return plain.decode()
        except InvalidTag as exc:  # 尝试下一把
            last = exc
            continue
    raise ApiError(Code.INTERNAL, "凭证解密失败（主密钥不匹配）", details={"key_id": key_id}) from last


def rotate(ciphertext: str, *, record_type: str, record_id: str) -> str:
    """把密文重加密到当前密钥。"""
    plain = open_sealed(ciphertext, record_type=record_type, record_id=record_id)
    return seal(plain, record_type=record_type, record_id=record_id)
