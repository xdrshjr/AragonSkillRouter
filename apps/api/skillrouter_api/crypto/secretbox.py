"""AES-256-GCM 信封加密（§7.4）。

密文格式：`v1.<key_id>.<b64url(nonce)>.<b64url(ciphertext)>`，AAD = `f"{record_type}:{record_id}:{key_id}"`。

`record_type` 的全部取值（**新增取值必须同时改这里**，否则轮换/回滚时无法判断
某段密文该用哪个 AAD 打开；§A.3.2-5 / F-32）：

| `record_type`     | `record_id`            | 写入点                                              | 内容 |
|-------------------|------------------------|-----------------------------------------------------|------|
| `credential`      | `credentials.id`       | `services/credentials.py`（create/replace/rotate）、`crypto/keyring.py` | 上游密钥原文 |
| `credential_meta` | `credentials.id`       | `services/credentials.py::_split_meta()`            | 命中 `_SENSITIVE_META` 的 meta 键值（JSON） |
| `provider_secret` | `providers.id`         | 预留：提供商级共享密钥（当前无写入点）              | 提供商密钥原文 |
| `setting`         | 设置键名（如 `agent_model`） | `crypto/keyring.py`                            | 设置值中的敏感字段（轮换时重加密） |
| `file`            | `file_objects.id`      | 预留：需要二次加密的文件内容（当前文件用磁盘权限 + 签名 URL） | 文件字节 |
"""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..errors import ApiError, Code
from . import masterkey

VERSION = "v1"

#: 与上表同步的取值集合（供轮换脚本/自检遍历；新增 `record_type` 时同时改表与集合）
RECORD_TYPES = frozenset({"credential", "credential_meta", "provider_secret", "setting", "file"})


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
