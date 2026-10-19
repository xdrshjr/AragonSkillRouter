"""密码哈希（argon2id，§7.3）。"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import HashingError, VerificationError, VerifyMismatchError

try:  # argon2-cffi 23.1 用 InvalidHash，21.x 用 InvalidHashError
    from argon2.exceptions import InvalidHashError as _InvalidHash
except ImportError:  # pragma: no cover - 依赖版本差异
    from argon2.exceptions import InvalidHash as _InvalidHash  # type: ignore[assignment]

InvalidHashError = _InvalidHash

_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)

MIN_LENGTH = 12
DEFAULT_PASSWORDS = {"adminjr"}


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError, HashingError):
        return False


def needs_rehash(hashed: str) -> bool:
    try:
        return _hasher.check_needs_rehash(hashed)
    except InvalidHashError:
        return True


def validate_strength(password: str) -> list[str]:
    """密码策略：≥12 字符、含字母+数字、不等于默认值。"""
    issues: list[str] = []
    if len(password) < MIN_LENGTH:
        issues.append(f"密码至少 {MIN_LENGTH} 个字符")
    if not any(c.isalpha() for c in password):
        issues.append("密码必须包含字母")
    if not any(c.isdigit() for c in password):
        issues.append("密码必须包含数字")
    if password in DEFAULT_PASSWORDS:
        issues.append("不能使用默认密码")
    return issues
