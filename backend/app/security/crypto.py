"""Credential cryptography.

Threat model: the SQLite file may be copied, backed up, or read by another
process on the host. The master key lives outside the database — in a keyring
file on the persistent volume with restrictive permissions, or in the
environment. Losing the database alone therefore does not disclose upstream
credentials, and rotating the key re-wraps every stored secret.

Algorithm: AES-256-GCM (authenticated encryption) via `cryptography`, one
random 96-bit nonce per secret, key id bound into the AAD so a ciphertext can
never be silently decrypted under the wrong key.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings

ENVELOPE_VERSION = "v1"
AAD_PREFIX = "skillrouter.credential"


class CryptoError(RuntimeError):
    """Raised when a secret cannot be encrypted or decrypted."""


class KeyringError(CryptoError):
    """Raised when the master keyring is missing or malformed."""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Keyring:
    active: str
    keys: dict[str, bytes]

    def key(self, key_id: str) -> bytes:
        try:
            return self.keys[key_id]
        except KeyError as exc:  # pragma: no cover - defensive
            raise KeyringError(f"unknown key id {key_id!r}; the keyring has no such key") from exc

    def to_json(self) -> dict[str, Any]:
        return {
            "version": 1,
            "active": self.active,
            "keys": {kid: _b64e(raw) for kid, raw in self.keys.items()},
            "updated_at": _now(),
        }


def _generate_key() -> bytes:
    return AESGCM.generate_key(bit_length=256)


def _read_keyring_file(path: Path) -> Keyring:
    if not path.exists():
        raise KeyringError(
            f"master keyring not found at {path}. Run `python -m app.cli init` or set SR_MASTER_KEYS."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        keys = {kid: _b64d(val) for kid, val in raw["keys"].items()}
        active = raw["active"]
    except (OSError, ValueError, KeyError) as exc:
        raise KeyringError(f"master keyring at {path} is unreadable or malformed: {exc}") from exc
    if active not in keys:
        raise KeyringError(f"master keyring active key {active!r} is not present in the key set")
    return Keyring(active=active, keys=keys)


def _keyring_from_env() -> Keyring | None:
    settings = get_settings()
    raw = settings.master_keys
    if not raw:
        return None
    keys: dict[str, bytes] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise KeyringError("SR_MASTER_KEYS must look like 'k1:<base64url-key>,k2:<base64url-key>'")
        kid, value = chunk.split(":", 1)
        try:
            keys[kid.strip()] = _b64d(value.strip())
        except ValueError as exc:
            raise KeyringError(f"SR_MASTER_KEYS entry for {kid!r} is not valid base64") from exc
    if not keys:
        raise KeyringError("SR_MASTER_KEYS is set but contains no keys")
    active = (settings.master_key_active or next(iter(keys))).strip()
    if active not in keys:
        raise KeyringError(f"SR_MASTER_KEY_ACTIVE={active!r} is not one of the supplied keys")
    for kid, key in keys.items():
        if len(key) != 32:
            raise KeyringError(f"key {kid!r} must be 32 raw bytes (AES-256); got {len(key)}")
    return Keyring(active=active, keys=keys)


def load_keyring() -> Keyring:
    """Environment keys win (container deploys); otherwise use the keyring file."""
    env_ring = _keyring_from_env()
    if env_ring is not None:
        return env_ring
    return _read_keyring_file(get_settings().keyring_path)


def create_keyring(path: Path | None = None, *, overwrite: bool = False) -> Keyring:
    settings = get_settings()
    target = Path(path or settings.keyring_path)
    if target.exists() and not overwrite:
        return _read_keyring_file(target)
    ring = Keyring(active="k1", keys={"k1": _generate_key()})
    write_keyring(ring, target)
    return ring


def write_keyring(ring: Keyring, path: Path | None = None) -> Path:
    settings = get_settings()
    target = Path(path or settings.keyring_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(ring.to_json(), indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:  # pragma: no cover - Windows ACL behaviour
        pass
    tmp.replace(target)
    try:
        os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover
        pass
    return target


# ------------------------------------------------------------------ encrypt --
def encrypt_map(values: dict[str, str], *, ring: Keyring | None = None) -> tuple[str, str]:
    """Encrypt a flat string map. Returns (envelope, key_id)."""
    ring = ring or load_keyring()
    key = ring.key(ring.active)
    plaintext = json.dumps(values, ensure_ascii=False).encode("utf-8")
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, f"{AAD_PREFIX}:{ENVELOPE_VERSION}:{ring.active}".encode())
    envelope = ".".join([ENVELOPE_VERSION, ring.active, _b64e(nonce), _b64e(ciphertext)])
    return envelope, ring.active


def decrypt_map(envelope: str, *, ring: Keyring | None = None) -> dict[str, str]:
    ring = ring or load_keyring()
    parts = (envelope or "").split(".")
    if len(parts) != 4 or parts[0] != ENVELOPE_VERSION:
        raise CryptoError("stored secret is not a recognised envelope")
    _, key_id, nonce_b64, data_b64 = parts
    try:
        nonce = _b64d(nonce_b64)
        ciphertext = _b64d(data_b64)
    except ValueError as exc:
        raise CryptoError("stored secret envelope is corrupt") from exc
    key = ring.key(key_id)
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, f"{AAD_PREFIX}:{ENVELOPE_VERSION}:{key_id}".encode())
    except InvalidTag as exc:
        raise CryptoError(
            "credential could not be decrypted: wrong master key or tampered ciphertext"
        ) from exc
    try:
        parsed = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:  # pragma: no cover - defensive
        raise CryptoError("decrypted secret is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise CryptoError("decrypted secret is not a JSON object")
    return {str(k): str(v) for k, v in parsed.items()}


def rewrap(envelope: str, *, ring: Keyring | None = None) -> tuple[str, str, bool]:
    """Re-encrypt under the active key. Returns (envelope, key_id, changed)."""
    ring = ring or load_keyring()
    key_id = (envelope or "").split(".")[1] if envelope else ""
    if key_id == ring.active:
        return envelope, ring.active, False
    values = decrypt_map(envelope, ring=ring)
    new_envelope, new_key = encrypt_map(values, ring=ring)
    return new_envelope, new_key, True


# ------------------------------------------------------------------ masking --
def mask_value(value: str) -> str:
    """A preview that identifies a key without disclosing it."""
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:3]}…{value[-4:]}"


def build_hint(values: dict[str, str]) -> str:
    """Human-readable hint for the credential list (never the secret itself)."""
    for key in ("api_key", "token", "password", "secret", "value"):
        if key in values and values[key]:
            return mask_value(values[key])
    if not values:
        return ""
    first = next(iter(values.values()))
    return mask_value(first)


def generate_master_key_b64() -> str:
    return _b64e(_generate_key())
