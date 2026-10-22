"""Password hashing (Argon2id) and password-policy checks."""

from __future__ import annotations

import re
import secrets
import string

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from argon2.low_level import Type

# Tuned for a 4 vCPU / 8 GB target: ~64 MiB and 3 passes keeps a single login
# well under the interactive budget while making offline cracking expensive.
_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=2,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)

MIN_PASSWORD_LENGTH = 10


class PasswordPolicyError(ValueError):
    pass


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    if not stored_hash or not password:
        return False
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except (InvalidHashError, ValueError):
        return True


def check_password_policy(password: str, *, username: str | None = None) -> None:
    """Raise PasswordPolicyError with an actionable message."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if password.lower() in {"adminjr", "password", "admin", "12345678", "skillrouter"}:
        raise PasswordPolicyError("Password is a well-known default and cannot be used.")
    if username and username.lower() in password.lower() and len(username) >= 3:
        raise PasswordPolicyError("Password must not contain the username.")
    classes = 0
    classes += bool(re.search(r"[a-z]", password))
    classes += bool(re.search(r"[A-Z]", password))
    classes += bool(re.search(r"\d", password))
    classes += bool(re.search(r"[^A-Za-z0-9]", password))
    if classes < 3:
        raise PasswordPolicyError(
            "Password must mix at least three of: lowercase, uppercase, digits, symbols."
        )


def generate_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        try:
            check_password_policy(candidate)
            return candidate
        except PasswordPolicyError:
            continue
